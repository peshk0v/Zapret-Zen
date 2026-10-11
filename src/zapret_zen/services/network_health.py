from __future__ import annotations

import re
import socket
import struct
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any, Callable

from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.settings import SettingsManager

# Domains used to probe DNS health.
# CANARY: a domain that is commonly DPI-blocked and therefore commonly poisoned by
#         resolvers answering NXDOMAIN (e.g. youtube.com behind a clean reinstall).
# CONTROL: a neutral domain that must always resolve; used to distinguish
#          "targeted poisoning" from "DNS completely broken / offline".
DEFAULT_DNS_CANARIES: tuple[str, ...] = ("youtube.com", "discord.com")
DEFAULT_DNS_CONTROL: str = "cloudflare.com"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Match an absolute Windows path such as C:\... or \\server\...
_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _system_resolves(hostname: str, timeout: float = 4.0) -> bool:
    """Return True if the system resolver returns any answer for ``hostname``.

    ``socket.getaddrinfo`` uses whatever DNS servers are currently configured on the
    adapter, so this reflects the real state a browser/curl would see. The lookup runs
    in a daemon thread so a slow/hanging resolver cannot block the caller for longer
    than ``timeout``.
    """
    if not hostname:
        return False
    result = {"ok": False}

    def _worker() -> None:
        try:
            socket.getaddrinfo(hostname, None)
            result["ok"] = True
        except Exception:
            result["ok"] = False

    thread = threading.Thread(target=_worker, daemon=True, name="zz-dns-resolve")
    thread.start()
    thread.join(timeout)
    return bool(result["ok"])


def _skip_dns_name(data: bytes, offset: int) -> int:
    length = len(data)
    while offset < length:
        label = data[offset]
        if label == 0:
            return offset + 1
        if label & 0xC0 == 0xC0:  # compression pointer -> 2 bytes total
            return offset + 2
        offset += 1 + label
    return offset


def _query_a_records(server: str, hostname: str, timeout: float = 2.5) -> list[str]:
    """Send a raw DNS A-record query to a specific server over UDP.

    Returns the list of A records, or an empty list on NXDOMAIN, timeout, or any
    parsing/network error. Used to compare "what the configured resolver says" against
    "what the router/gateway says" without pulling in a DNS library.
    """
    if not server or not hostname:
        return []
    try:
        transaction_id = threading.get_ident() & 0xFFFF
        header = struct.pack(">HHHHHH", transaction_id, 0x0100, 1, 0, 0, 0)
        qname = b"".join(
            bytes([len(label)]) + label.encode("idna")
            for label in hostname.strip(".").split(".")
            if label
        ) + b"\x00"
        packet = header + qname + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (server, 53))
            data, _addr = sock.recvfrom(512)
    except Exception:
        return []

    if len(data) < 12:
        return []
    _tid, flags, qdcount, ancount, _nscount, _arcount = struct.unpack(">HHHHHH", data[:12])
    if flags & 0x000F == 3:  # NXDOMAIN
        return []
    offset = 12
    for _ in range(qdcount):
        offset = _skip_dns_name(data, offset) + 4
    answers: list[str] = []
    for _ in range(ancount):
        offset = _skip_dns_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + rdlength]
        offset += rdlength
        if rtype == 1 and rdlength == 4:  # A record
            try:
                answers.append(socket.inet_ntoa(rdata))
            except OSError:
                continue
    return answers


def _default_gateway() -> str:
    """Best-effort lookup of the IPv4 default gateway (usually the router/DNS forwarder)."""
    command = (
        "Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' "
        "| Sort-Object RouteMetric | Select-Object -First 1 -ExpandProperty NextHop"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
            creationflags=_NO_WINDOW,
        )
        for line in reversed((proc.stdout or "").strip().splitlines()):
            candidate = line.strip()
            if candidate and candidate != "0.0.0.0":
                return candidate
    except Exception:
        pass
    # Fallback: parse `route print -4` for the 0.0.0.0 0.0.0.0 route.
    try:
        proc = subprocess.run(
            ["route", "print", "-4"],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
            creationflags=_NO_WINDOW,
        )
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                gateway = parts[2]
                if gateway and gateway != "0.0.0.0":
                    return gateway
    except Exception:
        pass
    return ""


def _flush_dns_cache() -> None:
    try:
        subprocess.run(
            ["ipconfig", "/flushdns"],
            capture_output=True,
            check=False,
            timeout=8,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        pass


class NetworkHealth:
    """Detects and repairs DNS poisoning that silently breaks the DPI bypass.

    The dominant real-world failure is not the zapret strategy at all: after a clean
    Windows install the adapter often ends up on public resolvers (1.1.1.1 / 8.8.8.8)
    that the ISP hijacks, answering ``NXDOMAIN`` for blocked domains such as youtube.com
    while a neutral domain still resolves. Every connectivity test then returns 000 and
    the bypass looks broken even though the strategy is fine.

    This service compares the configured resolver against the router/gateway and, when
    poisoning is detected, resets DNS back to DHCP and flushes the cache - the exact
    repair that restored connectivity during the original incident.
    """

    def __init__(
        self,
        logging: LoggingManager,
        settings: SettingsManager,
        *,
        dns_reset: Callable[[], None] | None = None,
        canaries: tuple[str, ...] = DEFAULT_DNS_CANARIES,
        control: str = DEFAULT_DNS_CONTROL,
    ) -> None:
        self.logging = logging
        self.settings = settings
        self._dns_reset = dns_reset
        self._canaries = tuple(canaries)
        self._control = control
        # Optional callback invoked after a successful repair. Assigned post-construction
        # (e.g. from bootstrap) because NotificationManager is built after this service.
        self._notify_callback: Callable[[dict[str, Any]], None] | None = None

    # -- public API ---------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """Probe DNS health without changing anything."""
        canary = self._first_canary()
        canary_ok = all(_system_resolves(host) for host in self._canaries) if self._canaries else False
        control_ok = _system_resolves(self._control)
        gateway = _default_gateway()
        gateway_canary_ok = bool(gateway) and bool(_query_a_records(gateway, canary))

        if canary_ok:
            status = "ok"
            message = f"DNS resolves {canary} correctly."
        elif control_ok:
            # Neutral domain resolves but the canary does not => targeted poisoning.
            status = "poisoned"
            message = (
                f"DNS poisoning detected: {canary} is blocked while {self._control} resolves. "
                "Repairing by resetting to DHCP DNS."
            )
        else:
            status = "broken"
            message = (
                f"DNS resolution broken for {canary} and {self._control}"
                + (" (router still resolves it)." if gateway_canary_ok else ".")
                + " Repairing by resetting to DHCP DNS."
            )

        return {
            "status": status,
            "message": message,
            "canary": canary,
            "control": self._control,
            "canary_ok": canary_ok,
            "control_ok": control_ok,
            "gateway": gateway,
            "gateway_canary_ok": gateway_canary_ok,
            "repaired": False,
            "repair_error": "",
            "checked_at": datetime.utcnow().isoformat(),
        }

    def repair(self, report: dict[str, Any]) -> dict[str, Any]:
        """Reset DNS to DHCP and flush the cache, then re-check the canary."""
        try:
            if self._dns_reset is not None:
                self._dns_reset()
            else:
                self._reset_dns_via_powershell()
            _flush_dns_cache()
            report["repaired"] = True
            canary = str(report.get("canary", "") or "")
            report["canary_ok"] = _system_resolves(canary)
            if report["canary_ok"]:
                report["status"] = "ok"
                report["message"] = f"DNS repaired: {canary} now resolves after resetting to DHCP DNS."
            try:
                current = self.settings.get()
                self.settings.update(
                    self_heal_repair_count=int(getattr(current, "self_heal_repair_count", 0) or 0) + 1
                )
            except Exception:
                pass
            self.logging.log(
                "info",
                "Network self-heal repaired DNS",
                status=str(report.get("status", "")),
                canary=canary,
                gateway=str(report.get("gateway", "")),
            )
            self._emit_notify(report)
        except Exception as error:
            report["repaired"] = False
            report["repair_error"] = str(error)
            report["message"] = f"DNS self-heal failed: {error}"
            self.logging.log("warning", "Network self-heal DNS repair failed", error=str(error))
        return report

    def _emit_notify(self, report: dict[str, Any]) -> None:
        callback = self._notify_callback
        if not callable(callback):
            return
        try:
            callback(report)
        except Exception:
            pass

    def preflight(self, *, repair: bool | None = None) -> dict[str, Any]:
        """Check DNS health and repair it when poisoning is detected.

        This is the single entry point used before starting the bypass and before
        running a strategy autotest, so that connectivity results are meaningful.
        """
        report = self.check()
        if report.get("status") not in ("poisoned", "broken"):
            return report
        settings = self.settings.get()
        enabled = bool(getattr(settings, "self_heal_enabled", True))
        should_repair = enabled if repair is None else bool(repair)
        if not should_repair:
            self.logging.log("warning", "DNS issue detected but self-heal is disabled", status=report["status"])
            return report
        return self.repair(report)

    # -- helpers ------------------------------------------------------------

    def _first_canary(self) -> str:
        return self._canaries[0] if self._canaries else ""

    @staticmethod
    def _reset_dns_via_powershell() -> None:
        """Fallback reset used when no dns_reset callable was injected.

        Mirrors dns_manager.reset_windows_dns(): reset every active adapter to DHCP and
        clear the per-interface NameServer registry value that can keep a poisoned
        resolver sticky even after -ResetServerAddresses.
        """
        if not sys.platform.startswith("win"):
            return
        script = (
            "$adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' }); "
            "foreach ($a in $adapters) { "
            "  try { Set-DnsClientServerAddress -InterfaceIndex ([int]$a.ifIndex) -ResetServerAddresses -ErrorAction Stop | Out-Null } catch {}; "
            "  $guid = [string]$a.InterfaceGuid; "
            "  foreach ($root in 'Tcpip','Tcpip6') { "
            "    $p = \"HKLM:\\SYSTEM\\CurrentControlSet\\Services\\$root\\Parameters\\Interfaces\\$guid\"; "
            "    if (Test-Path -LiteralPath $p) { try { Set-ItemProperty -LiteralPath $p -Name NameServer -Value '' -ErrorAction Stop } catch {} } "
            "  } "
            "}"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            check=False,
            timeout=20,
            creationflags=_NO_WINDOW,
        )


def service_binary_path(image_path: str) -> str:
    """Extract the executable path from a service BINARY_PATH_NAME value.

    The value may be quoted and may carry arguments, e.g. ``"C:\\x\\WinDivert.dll" w=0``.
    """
    raw = (image_path or "").strip()
    if not raw:
        return ""
    if raw.startswith('"'):
        end = raw.find('"', 1)
        return raw[1:end] if end > 1 else raw.strip('"')
    return raw.split(" ")[0]


def is_dangling_binary_path(binary_path: str) -> bool:
    """True when the path is absolute but the file no longer exists on disk.

    Used to identify orphaned WinDivert driver services that point into a runtime
    folder which has since been deleted - they can never start again and only cause
    "service already exists" errors on the next bypass start.
    """
    from pathlib import Path

    candidate = (binary_path or "").strip().strip('"')
    if not candidate or not _ABSOLUTE_PATH_RE.match(candidate):
        return False
    try:
        return not Path(candidate).exists()
    except Exception:
        return False

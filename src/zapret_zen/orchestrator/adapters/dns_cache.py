"""Domain attribution for the degraded (no-ETW) observer.

The extended TCP table knows addresses only.  An SNI inspector would need to
reassemble TLS handshakes, and the DNS ETW provider is not wired yet.  Windows
keeps the answers of every recent lookup in its client-side cache, and
``ipconfig /displaydns`` exposes that cache to a normal user, without a driver
or elevation.  It is stale by nature, so every mapping gets a short local TTL:
an address that a CDN hands to a different site later will not pin the old name
forever.

This is deliberately a *fallback*.  ETW gives exact answers at query time; the
cache only says "this address was resolved for this name recently", which is
enough to pick a domain to probe and to show a human-readable name in the UI.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
import threading
import time
from typing import Mapping

_HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z0-9-]{2,63}$")
_NAME_LABELS = ("record name", "имя записи", "host name", "имя узла")
_SKIP_LABELS = ("cname", "псевдоним", "alias", "ptr")


def parse_displaydns(text: str) -> dict[str, str]:
    """Map every cached address to the name it was resolved for.

    The parser keys off the *shape* of the values rather than the labels, so it
    survives localized Windows builds: the label text is only used to notice a
    record name and to skip alias records, whose value is another name.  Each
    entry also starts with the queried name on a line of its own, which is a
    locale-independent anchor and is used as a second source for the name.
    """

    result: dict[str, str] = {}
    current = ""
    for raw in str(text or "").splitlines():
        if ":" not in raw:
            header = raw.strip().rstrip(".")
            if header and _HOST_RE.match(header.lower()):
                current = header.lower()
            continue
        label, value = raw.split(":", 1)
        value = value.strip()
        if not value:
            continue
        # Link-local answers may carry a zone index (``fe80::1%12``) that the
        # TCP table never reports, so it is stripped before the lookup.
        address = value.split("%", 1)[0]
        try:
            ipaddress.ip_address(address)
        except ValueError:
            ip = False
        else:
            ip = True
        if ip:
            if current:
                result[address] = current
            continue
        label_lower = label.lower()
        if any(marker in label_lower for marker in _SKIP_LABELS):
            continue
        if not any(marker in label_lower for marker in _NAME_LABELS):
            continue
        candidate = value.lower().rstrip(".")
        if _HOST_RE.match(candidate):
            current = candidate
    return result


class WindowsDnsCache:
    """A slowly refreshed view of the Windows DNS client cache."""

    name = "dns-cache"

    def __init__(
        self,
        *,
        refresh_s: float = 10.0,
        ttl_s: float = 900.0,
        timeout_s: float = 5.0,
    ) -> None:
        self.refresh_s = max(2.0, float(refresh_s))
        self.ttl_s = max(60.0, float(ttl_s))
        self.timeout_s = max(1.0, float(timeout_s))
        self._lock = threading.Lock()
        self._by_ip: dict[str, tuple[str, float]] = {}
        self._last_refresh = 0.0
        self._refreshing = False
        self._detail = "" if sys.platform == "win32" else "Windows only"
        self._available = sys.platform == "win32"

    @property
    def available(self) -> bool:
        return self._available

    @property
    def detail(self) -> str:
        return self._detail

    def host_for(self, address: str) -> str | None:
        address = str(address or "").strip()
        if not address or not self._available:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._by_ip.get(address)
            if entry is not None and now - entry[1] <= self.ttl_s:
                return entry[0]
            due = now - self._last_refresh >= self.refresh_s and not self._refreshing
            if due:
                self._refreshing = True
        if due:
            try:
                self.refresh()
            finally:
                with self._lock:
                    self._refreshing = False
        with self._lock:
            entry = self._by_ip.get(address)
            if entry is None:
                return None
            if time.monotonic() - entry[1] > self.ttl_s:
                return None
            return entry[0]

    def refresh(self) -> Mapping[str, str]:
        self._last_refresh = time.monotonic()
        text = self._read()
        if not text:
            return {}
        mapping = parse_displaydns(text)
        if not mapping:
            return {}
        stamp = time.monotonic()
        with self._lock:
            for address, host in mapping.items():
                self._by_ip[address] = (host, stamp)
            if len(self._by_ip) > 50000:
                cutoff = stamp - self.ttl_s
                self._by_ip = {key: value for key, value in self._by_ip.items() if value[1] >= cutoff}
        return mapping

    def _read(self) -> str:
        if not self._available:
            return ""
        try:
            completed = subprocess.run(
                ["ipconfig", "/displaydns"],
                capture_output=True,
                timeout=self.timeout_s,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as error:
            self._detail = f"ipconfig failed: {error}"
            return ""
        raw = completed.stdout or b""
        if not raw:
            return ""
        text = _decode_output(raw)
        if completed.returncode != 0 and not text.strip():
            return ""
        self._detail = "Windows DNS cache"
        return text


def _decode_output(raw: bytes) -> str:
    """Decode ``ipconfig`` output written in the console (OEM) code page."""

    for encoding in _candidate_encodings():
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _candidate_encodings() -> tuple[str, ...]:
    encodings = ["utf-8"]
    try:
        import ctypes

        oem = int(ctypes.windll.kernel32.GetOEMCP())
    except Exception:
        oem = 0
    if oem > 0:
        encodings.append(f"cp{oem}")
    encodings.append("cp866")
    return tuple(encodings)

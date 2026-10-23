from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any

from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.process_compat import decode_process_output

_VPN_PROCESS_PATTERNS = (
    "nekobox",
    "nekoray",
    "v2rayn",
    "xray",
    "xrayw",
    "sing-box",
    "singbox",
    "clash",
    "mihomo",
    "hiddify",
    "outline",
    "wireguard",
    "openvpn",
    "amnezia",
    "warp",
)

_VPN_ADAPTER_PATTERNS = (
    "wintun",
    "wireguard",
    "openvpn",
    "tap-",
    "tap_windows",
    "vpn",
    "v2ray",
    "xray",
    "nekobox",
    "nekoray",
    "sing-box",
    "clash",
    "mihomo",
    "tun",
)


class VpnDetector:
    """Detects running VPN processes and their network adapters."""

    def __init__(self, logging: LoggingManager) -> None:
        self.logging = logging
        self._creationflags = 0
        self._startupinfo: subprocess.STARTUPINFO | None = None
        if sys.platform.startswith("win"):
            self._creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            self._startupinfo = startup

    def detect_vpn_priority_context(self) -> dict[str, list[str]]:
        script = r"""
$patterns = @('nekobox','nekoray','v2rayn','xray','xrayw','sing-box','singbox','clash','mihomo','hiddify','outline','wireguard','openvpn','amnezia','warp')
$adapterPatterns = @('wintun','wireguard','openvpn','tap-','tap_windows','vpn','v2ray','xray','nekobox','nekoray','sing-box','clash','mihomo','tun')

$procById = @{}
Get-CimInstance Win32_Process | ForEach-Object {
  $name = ([string]$_.Name).ToLowerInvariant()
  $path = ([string]$_.ExecutablePath).ToLowerInvariant()
  $cmd = ([string]$_.CommandLine).ToLowerInvariant()
  foreach ($pattern in $patterns) {
    if ($name.Contains($pattern) -or $path.Contains($pattern) -or $cmd.Contains($pattern)) {
      $procById[[int]$_.ProcessId] = $true
      break
    }
  }
}

$remoteIps = New-Object System.Collections.Generic.HashSet[string]
Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | ForEach-Object {
  $pid = [int]$_.OwningProcess
  if (-not $procById.ContainsKey($pid)) { return }
  $ip = ([string]$_.RemoteAddress).Trim()
  if (-not $ip) { return }
  if ($ip -in @('127.0.0.1','0.0.0.0','::','::1')) { return }
  [void]$remoteIps.Add($ip)
}

$adapterIndexes = New-Object System.Collections.Generic.HashSet[int]
Get-NetAdapter -ErrorAction SilentlyContinue | ForEach-Object {
  $joined = (([string]$_.Name) + ' ' + ([string]$_.InterfaceDescription)).ToLowerInvariant()
  foreach ($pattern in $adapterPatterns) {
    if ($joined.Contains($pattern)) {
      [void]$adapterIndexes.Add([int]$_.ifIndex)
      break
    }
  }
}

[pscustomobject]@{
  adapter_indexes = @($adapterIndexes | Sort-Object)
  remote_ips = @($remoteIps | Sort-Object)
} | ConvertTo-Json -Compress
"""
        proc = self._run_powershell_json(script)
        if not proc:
            return {"adapter_indexes": [], "remote_ips": []}
        try:
            payload = json.loads(proc)
        except json.JSONDecodeError:
            return {"adapter_indexes": [], "remote_ips": []}
        adapter_indexes = payload.get("adapter_indexes", []) if isinstance(payload, dict) else []
        remote_ips = payload.get("remote_ips", []) if isinstance(payload, dict) else []
        if not isinstance(adapter_indexes, list):
            adapter_indexes = [adapter_indexes] if adapter_indexes not in (None, "") else []
        if not isinstance(remote_ips, list):
            remote_ips = [remote_ips] if remote_ips not in (None, "") else []
        return {
            "adapter_indexes": [str(item) for item in adapter_indexes if str(item).strip()],
            "remote_ips": [str(item) for item in remote_ips if self._looks_like_ip_address(str(item))],
        }

    def parse_port_ranges(self, value: str) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for raw in re.split(r"[\s,;]+", str(value or "")):
            token = raw.strip()
            if not token:
                continue
            if "-" in token:
                left, right = token.split("-", 1)
            else:
                left = right = token
            try:
                start = int(left)
                end = int(right)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            if start < 1 or end > 65535:
                continue
            item = (start, end)
            if item in seen:
                continue
            seen.add(item)
            ranges.append(item)
        return ranges

    def format_port_ranges(self, ranges: list[tuple[int, int]]) -> str:
        return ",".join(str(start) if start == end else f"{start}-{end}" for start, end in ranges)

    def subtract_port_ranges(
        self,
        ranges: list[tuple[int, int]],
        excluded_ranges: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        for start, end in ranges:
            segments = [(start, end)]
            for ex_start, ex_end in excluded_ranges:
                next_segments: list[tuple[int, int]] = []
                for seg_start, seg_end in segments:
                    if ex_end < seg_start or ex_start > seg_end:
                        next_segments.append((seg_start, seg_end))
                        continue
                    if seg_start < ex_start:
                        next_segments.append((seg_start, ex_start - 1))
                    if ex_end < seg_end:
                        next_segments.append((ex_end + 1, seg_end))
                segments = next_segments
            result.extend(segment for segment in segments if segment[0] <= segment[1])
        return result

    def _run_powershell_json(self, script: str) -> str:
        startup = self._startupinfo
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            check=False,
            creationflags=self._creationflags,
            startupinfo=startup,
        )
        stderr = decode_process_output(proc.stderr)
        if proc.returncode != 0:
            self.logging.log("warning", "PowerShell helper failed", stderr=stderr.strip()[-1000:])
            return ""
        return decode_process_output(proc.stdout).strip()

    def _looks_like_ip_address(self, value: str) -> bool:
        candidate = value.strip()
        if not candidate:
            return False
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", candidate):
            return True
        return ":" in candidate and re.fullmatch(r"[0-9a-fA-F:]+", candidate) is not None

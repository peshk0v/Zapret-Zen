#!/usr/bin/env python3
"""
Standalone CLI for managing Windows DNS — presets, custom servers, DoH.
Usage:
  xbox_dns_cli.py --preset xbox-dns              # apply xbox-dns.ru servers
  xbox_dns_cli.py --preset comss                 # apply Comss One DNS
  xbox_dns_cli.py --preset cloudflare            # apply Cloudflare 1.1.1.1
  xbox_dns_cli.py --preset google                # apply Google 8.8.8.8
  xbox_dns_cli.py --apply  1.1.1.1 8.8.8.8       # set custom DNS (mixed IPv4/IPv6)
  xbox_dns_cli.py --apply --ipv4 1.1.1.1 --ipv6 2606:4700:4700::1111
  xbox_dns_cli.py --list-presets                 # show available presets
  xbox_dns_cli.py --snapshot                     # snapshot current DNS
  xbox_dns_cli.py --fetch                        # fetch servers from xbox-dns.ru
  xbox_dns_cli.py --restore                      # restore DNS from snapshot
  xbox_dns_cli.py --reset                        # reset DNS to default (DHCP)
  xbox_dns_cli.py --status                       # show current state
  xbox_dns_cli.py --state-file ./my_state.json   # custom state file path
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import parse as urlparse

XBOX_DNS_URL = "https://xbox-dns.ru/"
FALLBACK_IPV4 = ("111.88.96.50", "111.88.96.51")
FALLBACK_IPV6 = ("2a00:ab00:1233:26::50", "2a00:ab00:1233:26::51")

# ─── Presets ─────────────────────────────────────────────────────────────────

PRESETS: dict[str, dict[str, Any]] = {
    "xbox-dns": {
        "name": "XBox DNS",
        "ipv4": ["111.88.96.50", "111.88.96.51"],
        "ipv6": ["2a00:ab00:1233:26::50", "2a00:ab00:1233:26::51"],
        "doh": "https://xbox-dns.ru/dns-query",
        "source": "xbox-dns.ru",
    },
    "comss": {
        "name": "Comss One DNS",
        "ipv4": ["83.220.169.155", "212.109.195.93"],
        "ipv6": [],
        "doh": "https://dns.comss.one/dns-query",
        "source": "comss.one",
    },
    "cloudflare": {
        "name": "Cloudflare",
        "ipv4": ["1.1.1.1", "1.0.0.1"],
        "ipv6": ["2606:4700:4700::1111", "2606:4700:4700::1001"],
        "doh": "https://cloudflare-dns.com/dns-query",
        "source": "cloudflare.com",
    },
    "google": {
        "name": "Google Public DNS",
        "ipv4": ["8.8.8.8", "8.8.4.4"],
        "ipv6": ["2001:4860:4860::8888", "2001:4860:4860::8844"],
        "doh": "https://dns.google/dns-query",
        "source": "google.com",
    },
    "quad9": {
        "name": "Quad9",
        "ipv4": ["9.9.9.9", "149.112.112.112"],
        "ipv6": ["2620:fe::fe", "2620:fe::9"],
        "doh": "https://dns.quad9.net/dns-query",
        "source": "quad9.net",
    },
    "yandex": {
        "name": "Yandex DNS",
        "ipv4": ["77.88.8.8", "77.88.8.1"],
        "ipv6": ["2a02:6b8::feed:0ff", "2a02:6b8:0:1::feed:0ff"],
        "doh": "https://common.dot.dns.yandex.net/dns-query",
        "source": "yandex.com",
    },
}


# ─── Helpers ────────────────────────────────────────────────────────────────

def eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def _run_powershell(script: str) -> subprocess.CompletedProcess:
    preamble = (
        "$OutputEncoding = [System.Text.Encoding]::UTF8;"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", preamble + script],
        capture_output=True, text=False, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    def _decode(raw: bytes | None) -> str:
        if not raw:
            return ""
        for encoding in ("utf-8-sig", "cp866", "cp1251", "latin-1"):
            try:
                return raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    proc.stdout = _decode(proc.stdout or b"")
    proc.stderr = _decode(proc.stderr or b"")
    return proc


# ─── DNS server fetching ────────────────────────────────────────────────────

def parse_xbox_dns_servers(html: str) -> dict[str, list[str]]:
    ipv4: list[str] = []
    ipv6: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[0-9A-Fa-f:.]{3,}", html or ""):
        candidate = token.strip().strip(".,;:()[]{}<>`'\"")
        if not candidate or candidate in seen:
            continue
        try:
            parsed = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if parsed.is_loopback or parsed.is_unspecified:
            continue
        seen.add(candidate)
        if parsed.version == 4:
            ipv4.append(candidate)
        elif parsed.version == 6:
            ipv6.append(candidate)
    return {"ipv4": ipv4[:2], "ipv6": ipv6[:2]}


def resolve_doh_host(doh: str) -> tuple[list[str], list[str]]:
    """Resolve the hostname inside a DNS-over-HTTPS URL to (ipv4, ipv6) addresses."""
    if not doh:
        return [], []
    try:
        host = urlparse.urlsplit(doh).hostname
    except Exception:
        host = ""
    if not host:
        return [], []
    ipv4: list[str] = []
    ipv6: list[str] = []
    seen: set[str] = set()
    try:
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                results = socket.getaddrinfo(host, 443, family, socket.SOCK_STREAM)
            except OSError:
                continue
            for item in results:
                ip = str(item[4][0])
                if not ip or ip in seen or "%" in ip:
                    continue
                seen.add(ip)
                if family == socket.AF_INET:
                    ipv4.append(ip)
                else:
                    ipv6.append(ip)
                if len(ipv4) + len(ipv6) >= 4:
                    break
    except OSError:
        pass
    return ipv4[:2], ipv6[:2]


def fetch_xbox_dns_servers() -> dict[str, Any]:
    try:
        req = urllib.request.Request(
            XBOX_DNS_URL,
            headers={"User-Agent": "Zapret-Hub/2.0 xbox-dns"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        parsed = parse_xbox_dns_servers(html)
        if parsed["ipv4"] and parsed["ipv6"]:
            return {**parsed, "source": "remote"}
        raise ValueError("page did not contain both IPv4 and IPv6 DNS addresses")
    except Exception as exc:
        eprint(f"Fetch from xbox-dns.ru failed: {exc}, using fallback")
        return {
            "ipv4": list(FALLBACK_IPV4),
            "ipv6": list(FALLBACK_IPV6),
            "source": "fallback",
            "last_error": str(exc),
        }


# ─── PowerShell scripts (snapshot / apply / restore) ─────────────────────────

SNAPSHOT_SCRIPT = r"""
$rows = @()
function Test-HubIgnoredAdapter($adapter) {
  $name = (([string]$adapter.Name) + ' ' + ([string]$adapter.InterfaceDescription)).ToLowerInvariant()
  if ($name.Contains('loopback')) { return $true }
  if ($name.Contains('wintun')) { return $true }
  if ($name.Contains('wireguard')) { return $true }
  if ($name.Contains('openvpn')) { return $true }
  if ($name.Contains('tap')) { return $true }
  if ($name.Contains('vpn')) { return $true }
  if ($name.Contains('v2ray')) { return $true }
  if ($name.Contains('xray')) { return $true }
  if ($name.Contains('sing-box')) { return $true }
  if ($name.Contains('clash')) { return $true }
  if ($name.Contains('tun')) { return $true }
  return $false
}
function Get-HubRegistryDns($guid, $family) {
  if (-not $guid) { return "" }
  $root = if ($family -eq "ipv6") { "Tcpip6" } else { "Tcpip" }
  $path = "HKLM:\SYSTEM\CurrentControlSet\Services\$root\Parameters\Interfaces\$guid"
  try {
    $value = (Get-ItemProperty -LiteralPath $path -Name NameServer -ErrorAction Stop).NameServer
    return [string]$value
  } catch { return "" }
}
function Split-HubDnsList($value) {
  @([string]$value -split "[,\s]+" | Where-Object { [string]$_ -ne "" })
}
$adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' -and $_.HardwareInterface -and -not (Test-HubIgnoredAdapter $_) })
if ($adapters.Count -eq 0) {
  $adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' -and -not (Test-HubIgnoredAdapter $_) })
}
$adapters | ForEach-Object {
  $ifIndex = [int]$_.ifIndex
  $alias = [string]$_.Name
  $guid = [string]$_.InterfaceGuid
  $v4 = Get-DnsClientServerAddress -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
  $v6 = Get-DnsClientServerAddress -InterfaceIndex $ifIndex -AddressFamily IPv6 -ErrorAction SilentlyContinue
  $v4Manual = Get-HubRegistryDns $guid "ipv4"
  $v6Manual = Get-HubRegistryDns $guid "ipv6"
  $rows += [pscustomobject]@{
    interface_index = $ifIndex
    interface_alias = $alias
    interface_guid = $guid
    ipv4 = if ($v4Manual) { @(Split-HubDnsList $v4Manual) } else { @($v4.ServerAddresses) }
    ipv6 = if ($v6Manual) { @(Split-HubDnsList $v6Manual) } else { @() }
    ipv4_manual = [bool]$v4Manual
    ipv6_manual = [bool]$v6Manual
  }
}
if ($rows.Count -eq 0) {
  $dnsRows = @(Get-DnsClientServerAddress -ErrorAction SilentlyContinue | Where-Object { $_.ServerAddresses.Count -gt 0 })
  $groups = @{}
  foreach ($dns in $dnsRows) {
    $ifIndex = [int]$dns.InterfaceIndex
    $alias = [string]$dns.InterfaceAlias
    $probe = [pscustomobject]@{ Name = $alias; InterfaceDescription = $alias }
    if ($ifIndex -le 0 -or (Test-HubIgnoredAdapter $probe)) { continue }
    if (-not $groups.ContainsKey($ifIndex)) {
      $adapter = Get-NetAdapter -InterfaceIndex $ifIndex -ErrorAction SilentlyContinue
      $guid = if ($adapter) { [string]$adapter.InterfaceGuid } else { "" }
      $v4Manual = Get-HubRegistryDns $guid "ipv4"
      $v6Manual = Get-HubRegistryDns $guid "ipv6"
      $groups[$ifIndex] = [pscustomobject]@{
        interface_index = $ifIndex
        interface_alias = $alias
        interface_guid = $guid
        ipv4 = @()
        ipv6 = @()
        ipv4_manual = [bool]$v4Manual
        ipv6_manual = [bool]$v6Manual
      }
      if ($v4Manual) { $groups[$ifIndex].ipv4 = @(Split-HubDnsList $v4Manual) }
      if ($v6Manual) { $groups[$ifIndex].ipv6 = @(Split-HubDnsList $v6Manual) }
    }
    if ([string]$dns.AddressFamily -eq 'IPv4' -and -not $groups[$ifIndex].ipv4_manual) {
      $groups[$ifIndex].ipv4 = @($dns.ServerAddresses)
    }
    if ([string]$dns.AddressFamily -eq 'IPv6' -and -not $groups[$ifIndex].ipv6_manual) {
      $groups[$ifIndex].ipv6 = @($dns.ServerAddresses)
    }
  }
  foreach ($entry in $groups.Values) { $rows += $entry }
}
@($rows) | ConvertTo-Json -Compress -Depth 4
"""


def snapshot_windows_dns() -> list[dict[str, Any]]:
    proc = _run_powershell(SNAPSHOT_SCRIPT)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    adapters: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        alias = str(item.get("interface_alias", "") or "").strip()
        if not alias:
            continue
        adapters.append({
            "interface_index": int(item.get("interface_index", 0) or 0),
            "interface_alias": alias,
            "interface_guid": str(item.get("interface_guid", "") or "").strip(),
            "ipv4": [str(v) for v in _ensure_list(item.get("ipv4", [])) if str(v).strip()],
            "ipv6": [str(v) for v in _ensure_list(item.get("ipv6", [])) if str(v).strip()],
            "ipv4_manual": bool(item.get("ipv4_manual", False)),
            "ipv6_manual": bool(item.get("ipv6_manual", False)),
        })
    return adapters


def _ensure_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def apply_windows_dns(adapters: list[dict[str, Any]], ipv4: list[str], ipv6: list[str], doh: str = "") -> None:
    payload = json.dumps(
        {"adapters": adapters, "ipv4": ipv4, "ipv6": ipv6, "doh": doh},
        ensure_ascii=False,
    )
    script = r"""
$payload = @'
__PAYLOAD__
'@ | ConvertFrom-Json
function Clear-HubRegistryDns($guid, $family) {
  if (-not $guid) { return }
  $root = if ($family -eq "ipv6") { "Tcpip6" } else { "Tcpip" }
  $path = "HKLM:\SYSTEM\CurrentControlSet\Services\$root\Parameters\Interfaces\$guid"
  if (Test-Path -LiteralPath $path) {
    try { Set-ItemProperty -LiteralPath $path -Name NameServer -Value "" -ErrorAction Stop } catch {}
  }
}
function Set-HubDoh($ifIndex, $doh, $dohIps) {
  if (-not $doh) { return }
  try {
    if (-not (Get-Command Set-DnsClientDohServerAddress -ErrorAction SilentlyContinue)) { return }
    Get-DnsClientDohServerAddress -InterfaceIndex $ifIndex -ErrorAction SilentlyContinue | ForEach-Object {
      try { Remove-DnsClientDohServerAddress -InterfaceIndex $ifIndex -ServerAddress $_.ServerAddress -DohTemplate $_.DohTemplate -ErrorAction Stop | Out-Null } catch {}
    }
    foreach ($ip in @($dohIps)) {
      if ([string]$ip -eq '') { continue }
      try {
        Set-DnsClientDohServerAddress -InterfaceIndex $ifIndex -ServerAddress ([string]$ip) -DohTemplate ([string]$doh) -AllowFallbackToUdp -ErrorAction Stop | Out-Null
      } catch {}
    }
  } catch {}
}
function Set-HubDnsServers($ifIndex, $guid, $ipv4, $ipv6) {
  $serverList = @(@($ipv4) + @($ipv6) | Where-Object { [string]$_ -ne '' })
  if ($ifIndex -le 0) { return }
  try {
    if ($serverList.Count -gt 0) {
      Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ServerAddresses $serverList -ErrorAction Stop | Out-Null
    } else {
      Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ResetServerAddresses -ErrorAction Stop | Out-Null
    }
  } catch { throw $_ }
}
foreach ($adapter in @($payload.adapters)) {
  $ifIndex = [int]$adapter.interface_index
  if ($ifIndex -le 0) { continue }
  $guid = [string]$adapter.interface_guid
  $dohIps = @(@($payload.ipv4) + @($payload.ipv6) | Where-Object { [string]$_ -ne '' })
  Set-HubDnsServers $ifIndex $guid @($payload.ipv4) @($payload.ipv6)
  Set-HubDoh $ifIndex ([string]$payload.doh) $dohIps
}
""".replace("__PAYLOAD__", payload)
    proc = _run_powershell(script)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Failed to apply DNS.").strip())


def reset_windows_dns() -> None:
    script = r"""
function Test-HubIgnoredAdapter($adapter) {
  $name = (([string]$adapter.Name) + ' ' + ([string]$adapter.InterfaceDescription)).ToLowerInvariant()
  if ($name.Contains('loopback')) { return $true }
  if ($name.Contains('wintun')) { return $true }
  if ($name.Contains('wireguard')) { return $true }
  if ($name.Contains('openvpn')) { return $true }
  if ($name.Contains('tap')) { return $true }
  if ($name.Contains('vpn')) { return $true }
  if ($name.Contains('v2ray')) { return $true }
  if ($name.Contains('xray')) { return $true }
  if ($name.Contains('sing-box')) { return $true }
  if ($name.Contains('clash')) { return $true }
  if ($name.Contains('tun')) { return $true }
  return $false
}
$errors = @()
function Clear-HubDoh($ifIndex) {
  try {
    if (-not (Get-Command Get-DnsClientDohServerAddress -ErrorAction SilentlyContinue)) { return }
    Get-DnsClientDohServerAddress -InterfaceIndex $ifIndex -ErrorAction SilentlyContinue | ForEach-Object {
      try { Remove-DnsClientDohServerAddress -InterfaceIndex $ifIndex -ServerAddress $_.ServerAddress -DohTemplate $_.DohTemplate -ErrorAction Stop | Out-Null } catch {}
    }
  } catch {}
}
$adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' -and $_.HardwareInterface -and -not (Test-HubIgnoredAdapter $_) })
if ($adapters.Count -eq 0) {
  $adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' -and -not (Test-HubIgnoredAdapter $_) })
}
foreach ($adapter in $adapters) {
  $ifIndex = [int]$adapter.ifIndex
  $guid = [string]$adapter.InterfaceGuid
  try {
    Clear-HubDoh $ifIndex
    Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ResetServerAddresses -ErrorAction Stop | Out-Null
    $root4 = "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\$guid"
    $root6 = "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters\Interfaces\$guid"
    if (Test-Path -LiteralPath $root4) { try { Set-ItemProperty -LiteralPath $root4 -Name NameServer -Value "" -ErrorAction Stop } catch {} }
    if (Test-Path -LiteralPath $root6) { try { Set-ItemProperty -LiteralPath $root6 -Name NameServer -Value "" -ErrorAction Stop } catch {} }
  } catch { $errors += "Adapter $($adapter.Name): $_" }
}
if ($errors.Count -gt 0) {
  if ($adapters.Count -eq 0 -or $errors.Count -ge $adapters.Count) {
    throw ($errors -join "`n")
  }
  Write-Warning ("Partial DNS reset: " + ($errors -join " | "))
}
"""
    proc = _run_powershell(script)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Failed to reset DNS.").strip())


def restore_windows_dns(adapters: list[dict[str, Any]]) -> None:
    payload = json.dumps({"adapters": adapters}, ensure_ascii=False)
    script = r"""
$payload = @'
__PAYLOAD__
'@ | ConvertFrom-Json
function Clear-HubRegistryDns($guid, $family) {
  if (-not $guid) { return }
  $root = if ($family -eq "ipv6") { "Tcpip6" } else { "Tcpip" }
  $path = "HKLM:\SYSTEM\CurrentControlSet\Services\$root\Parameters\Interfaces\$guid"
  if (Test-Path -LiteralPath $path) {
    try { Set-ItemProperty -LiteralPath $path -Name NameServer -Value "" -ErrorAction Stop } catch {}
  }
}
function Reset-HubDnsServers($ifIndex, $guid) {
  if ($ifIndex -le 0) { return }
  try {
    Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ResetServerAddresses -ErrorAction Stop | Out-Null
  } catch { throw $_ }
  Clear-HubRegistryDns $guid "ipv4"
  Clear-HubRegistryDns $guid "ipv6"
}
function Set-HubDnsServers($ifIndex, $guid, $ipv4, $ipv6) {
  $serverList = @(@($ipv4) + @($ipv6) | Where-Object { [string]$_ -ne '' })
  if ($ifIndex -le 0) { return }
  if ($serverList.Count -eq 0) {
    Reset-HubDnsServers $ifIndex $guid
    return
  }
  try {
    Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ServerAddresses $serverList -ErrorAction Stop | Out-Null
  } catch { throw $_ }
}
function Clear-HubDoh($ifIndex) {
  try {
    if (-not (Get-Command Get-DnsClientDohServerAddress -ErrorAction SilentlyContinue)) { return }
    Get-DnsClientDohServerAddress -InterfaceIndex $ifIndex -ErrorAction SilentlyContinue | ForEach-Object {
      try { Remove-DnsClientDohServerAddress -InterfaceIndex $ifIndex -ServerAddress $_.ServerAddress -DohTemplate $_.DohTemplate -ErrorAction Stop | Out-Null } catch {}
    }
  } catch {}
}
foreach ($adapter in @($payload.adapters)) {
  $ifIndex = [int]$adapter.interface_index
  if ($ifIndex -le 0) { continue }
  Clear-HubDoh $ifIndex
  $guid = [string]$adapter.interface_guid
  $ipv4Manual = if ($null -ne $adapter.ipv4_manual) { [bool]$adapter.ipv4_manual } else { @($adapter.ipv4).Count -gt 0 }
  $ipv6Manual = if ($null -ne $adapter.ipv6_manual) { [bool]$adapter.ipv6_manual } else { @($adapter.ipv6).Count -gt 0 }
  if (-not $ipv4Manual -and -not $ipv6Manual) {
    Reset-HubDnsServers $ifIndex $guid
    continue
  }
  $restoreV4 = if ($ipv4Manual) { @($adapter.ipv4) } else { @() }
  $restoreV6 = if ($ipv6Manual) { @($adapter.ipv6) } else { @() }
  Set-HubDnsServers $ifIndex $guid $restoreV4 $restoreV6
}
""".replace("__PAYLOAD__", payload)
    proc = _run_powershell(script)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Failed to restore DNS.").strip())


# ─── State file management ──────────────────────────────────────────────────

def read_state(state_path: Path) -> dict[str, Any]:
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def write_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ─── CLI actions ────────────────────────────────────────────────────────────

def cmd_fetch() -> dict[str, Any]:
    servers = fetch_xbox_dns_servers()
    print(json.dumps(servers, ensure_ascii=False, indent=2))
    return servers


def cmd_snapshot(state_path: Path) -> list[dict[str, Any]]:
    adapters = snapshot_windows_dns()
    if not adapters:
        eprint("ERROR: no active network adapters found")
        sys.exit(1)
    state = read_state(state_path)
    state["previous_adapters"] = adapters
    state["snapshot_at"] = datetime.now(timezone.utc).isoformat()
    write_state(state_path, state)
    print(json.dumps(adapters, ensure_ascii=False, indent=2))
    print(f"Snapshot saved to {state_path}", file=sys.stderr)
    return adapters


def cmd_apply(state_path: Path, ipv4: list[str], ipv6: list[str], source: str = "manual", doh: str = "") -> None:
    if not ipv4 and not ipv6 and doh:
        ipv4, ipv6 = resolve_doh_host(doh)
    if not ipv4 and not ipv6:
        eprint("ERROR: at least one --ipv4 or --ipv6 address required")
        sys.exit(1)
    state = read_state(state_path)
    adapters = state.get("previous_adapters")
    if not adapters:
        eprint("No snapshot found, taking one now...")
        adapters = snapshot_windows_dns()
        if not adapters:
            eprint("ERROR: no active network adapters found")
            sys.exit(1)
        state["previous_adapters"] = adapters
    try:
        apply_windows_dns(adapters, ipv4, ipv6, doh=doh)
    except RuntimeError as exc:
        eprint(f"ERROR applying DNS: {exc}")
        if not state.get("active", False):
            try:
                restore_windows_dns(adapters)
            except RuntimeError:
                pass
        state["last_error"] = str(exc)
        write_state(state_path, state)
        sys.exit(1)
    state["active"] = True
    state["servers"] = {"ipv4": ipv4, "ipv6": ipv6, "source": source, "doh": doh}
    state["last_error"] = ""
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_state(state_path, state)
    msg = f"DNS set to IPv4={ipv4} IPv6={ipv6} (source: {source})"
    if doh:
        msg += f"\n  DoH: {doh}"
    print(msg)


def cmd_restore(state_path: Path) -> None:
    state = read_state(state_path)
    if not state.get("active", False):
        eprint("xbox-dns is not active, nothing to restore")
        return
    adapters = state.get("previous_adapters")
    if not adapters:
        eprint("ERROR: no saved DNS snapshot to restore")
        sys.exit(1)
    try:
        restore_windows_dns(adapters)
    except RuntimeError as exc:
        eprint(f"ERROR restoring DNS: {exc}")
        state["last_error"] = str(exc)
        write_state(state_path, state)
        sys.exit(1)
    state["active"] = False
    state["last_error"] = ""
    state["restored_at"] = datetime.now(timezone.utc).isoformat()
    write_state(state_path, state)
    print("DNS settings restored from snapshot")


def cmd_fetch_and_apply(state_path: Path) -> None:
    servers = fetch_xbox_dns_servers()
    ipv4 = servers.get("ipv4", [])
    ipv6 = servers.get("ipv6", [])
    eprint(f"Fetched DNS: IPv4={ipv4} IPv6={ipv6} (source={servers.get('source')})")
    cmd_apply(state_path, ipv4, ipv6)


def cmd_reset(state_path: Path) -> None:
    try:
        reset_windows_dns()
    except RuntimeError as exc:
        eprint(f"ERROR resetting DNS: {exc}")
        sys.exit(1)
    state = read_state(state_path)
    state["active"] = False
    state["last_error"] = ""
    state["reset_at"] = datetime.now(timezone.utc).isoformat()
    write_state(state_path, state)
    print("DNS reset to default (DHCP) on all active adapters")


def cmd_status(state_path: Path) -> None:
    state = read_state(state_path)
    if not state:
        print("No state file found. Run --snapshot first.")
        return
    print(f"Active: {state.get('active', False)}")
    servers = state.get("servers", {})
    if servers:
        doh = servers.get("doh", "") or ""
        print(f"Servers: IPv4={servers.get('ipv4', [])} IPv6={servers.get('ipv6', [])} (source={servers.get('source', '?')})")
        if doh:
            print(f"  DoH: {doh}")
    adapters = state.get("previous_adapters")
    if adapters:
        print(f"Snapshot adapters: {len(adapters)}")
        for a in adapters:
            print(f"  [{a['interface_index']}] {a['interface_alias']}  IPv4={a['ipv4']} IPv6={a['ipv6']}")
    last_error = state.get("last_error", "")
    if last_error:
        print(f"Last error: {last_error}")
    for key in ("snapshot_at", "updated_at", "restored_at", "reset_at"):
        val = state.get(key)
        if val:
            print(f"{key}: {val}")


# ─── Argument parsing ───────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage Windows DNS settings — standalone DNS CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--state-file", default="./xbox_dns_state.json",
                        help="Path to state JSON file (default: ./xbox_dns_state.json)")
    parser.add_argument("--ipv4", nargs="*", default=[], metavar="IP",
                        help="IPv4 DNS servers (use with --apply)")
    parser.add_argument("--ipv6", nargs="*", default=[], metavar="IP",
                        help="IPv6 DNS servers (use with --apply)")
    parser.add_argument("--doh", type=str, default="", metavar="URL",
                        help="DNS-over-HTTPS template (e.g. https://cloudflare-dns.com/dns-query)")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preset", type=str, metavar="NAME",
                       help=f"Apply a preset DNS (choices: {', '.join(PRESETS)})")
    group.add_argument("--list-presets", action="store_true",
                       help="List available DNS presets")
    group.add_argument("--fetch", action="store_true",
                       help="Fetch DNS servers from xbox-dns.ru and print them")
    group.add_argument("--snapshot", action="store_true",
                       help="Snapshot current Windows DNS settings to state file")
    group.add_argument("--apply", nargs="*", metavar="IP",
                       help="Apply DNS servers (positional: IPv4+IPv6 mixed)")
    group.add_argument("--restore", action="store_true",
                       help="Restore DNS from saved snapshot")
    group.add_argument("--reset", action="store_true",
                       help="Reset DNS to default (DHCP) on all adapters")
    group.add_argument("--fetch-and-apply", action="store_true",
                       help="Fetch from xbox-dns.ru and apply immediately")
    group.add_argument("--status", action="store_true",
                       help="Show current state")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    state_path = Path(args.state_file)

    if args.list_presets:
        print(f"{'Preset':<20} {'IPv4':<35} {'IPv6':<45} {'DoH'}")
        print("-" * 120)
        for key, p in PRESETS.items():
            ip4 = ", ".join(p["ipv4"]) if p["ipv4"] else "-"
            ip6 = ", ".join(p["ipv6"]) if p["ipv6"] else "-"
            doh = p.get("doh", "") or "-"
            print(f"{key:<20} {ip4:<35} {ip6:<45} {doh}")
        return

    if args.preset:
        preset = PRESETS.get(args.preset)
        if not preset:
            eprint(f"ERROR: unknown preset '{args.preset}'. Use --list-presets to see available ones.")
            sys.exit(1)
        ipv4 = list(preset["ipv4"])
        ipv6 = list(preset.get("ipv6", []))
        doh = args.doh.strip() or preset.get("doh", "")
        eprint(f"Applying preset '{args.preset}': {preset['name']}")
        if doh:
            eprint(f"  DoH template: {doh}")
        cmd_apply(state_path, ipv4, ipv6, source=args.preset)
        return

    if args.fetch:
        cmd_fetch()
    elif args.snapshot:
        cmd_snapshot(state_path)
    elif args.apply is not None:
        ipv4 = list(args.ipv4) if args.ipv4 else []
        ipv6 = list(args.ipv6) if args.ipv6 else []
        for a in args.apply:
            try:
                parsed = ipaddress.ip_address(a)
                if parsed.version == 4:
                    ipv4.append(a)
                else:
                    ipv6.append(a)
            except ValueError:
                eprint(f"WARNING: '{a}' is not a valid IP address, skipping")
        cmd_apply(state_path, ipv4, ipv6, source="custom", doh=args.doh.strip())
    elif args.restore:
        cmd_restore(state_path)
    elif args.reset:
        cmd_reset(state_path)
    elif args.fetch_and_apply:
        cmd_fetch_and_apply(state_path)
    elif args.status:
        cmd_status(state_path)


if __name__ == "__main__":
    main()

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from html import unescape as html_unescape
from pathlib import Path
from typing import Callable

from zapret_zen import __version__
from zapret_zen.domain import UpdateInfo
from zapret_zen.runtime_env import is_packaged_runtime
from zapret_zen.services.github_network import GitHubNetworkClient, is_github_rate_limit_error
from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.storage import StorageManager
_PS1_TEMPLATE = """\
$ErrorActionPreference = 'SilentlyContinue'
$pidToWait = {{PID}}
$src = '{{SRC}}'
$dst = '{{DST}}'
$launch = '{{LAUNCH}}'
$tempRoot = '{{TEMP_ROOT}}'
$logPath = '{{LOG_PATH}}'
$preserve = @('data', 'mods', 'configs', 'cache', 'logs', 'backups')
$backupRoot = Join-Path '{{SCRIPT_ROOT}}' ('preserve_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Add-Content -LiteralPath $logPath -Value ('[' + (Get-Date -Format s) + '] updater started')

function Remove-PathRobust([string]$targetPath) {
  if (-not (Test-Path $targetPath)) { return $true }
  for ($i = 0; $i -lt 6; $i++) {
    try {
      attrib -r -s -h $targetPath /s /d *> $null
    } catch {}
    try {
      Remove-Item $targetPath -Recurse -Force -ErrorAction Stop
      return $true
    } catch {
      Start-Sleep -Milliseconds 300
    }
  }
  $quarantineRoot = Join-Path $env:TEMP 'zapret_zen_update_quarantine'
  New-Item -ItemType Directory -Path $quarantineRoot -Force | Out-Null
  $moved = Join-Path $quarantineRoot ((Split-Path $targetPath -Leaf) + '_' + [guid]::NewGuid().ToString('N'))
  try {
    Move-Item $targetPath $moved -Force -ErrorAction Stop
    return $true
  } catch {
    return $false
  }
}

function Add-UpdateLog([string]$message) {
  try {
    Add-Content -LiteralPath $logPath -Value ('[' + (Get-Date -Format s) + '] ' + $message)
  } catch {}
}

function Test-StandalonePayload([string]$sourceDir) {
  return (Test-Path (Join-Path $sourceDir 'python311.dll')) -and
         (Test-Path (Join-Path $sourceDir 'python3.dll')) -and
         (Test-Path (Join-Path $sourceDir 'zapret_zen.exe'))
}

function Test-InstalledStandalone([string]$targetDir) {
  return (Test-Path (Join-Path $targetDir 'python311.dll')) -and
         (Test-Path (Join-Path $targetDir 'python3.dll')) -and
         (Test-Path (Join-Path $targetDir 'zapret_zen.exe'))
}

function Overlay-Tree([string]$sourceDir, [string]$targetDir, [string[]]$preserveNames) {
  New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
  $sourceItems = Get-ChildItem -LiteralPath $sourceDir -Force -ErrorAction SilentlyContinue
  $sourceNames = @{}
  foreach ($item in $sourceItems) {
    $sourceNames[$item.Name] = $true
  }
  Get-ChildItem -LiteralPath $targetDir -Force -ErrorAction SilentlyContinue | ForEach-Object {
    if ($preserveNames -contains $_.Name) { return }
    if (-not $sourceNames.ContainsKey($_.Name)) {
      [void](Remove-PathRobust $_.FullName)
    }
  }
  foreach ($item in $sourceItems) {
    if ($preserveNames -contains $item.Name) { continue }
    $dest = Join-Path $targetDir $item.Name
    if ($item.PSIsContainer) {
      Overlay-Tree $item.FullName $dest $preserveNames
    } else {
      if (Test-Path $dest) {
        [void](Remove-PathRobust $dest)
      }
      New-Item -ItemType Directory -Path (Split-Path $dest -Parent) -Force | Out-Null
      try {
        Copy-Item $item.FullName $dest -Force -ErrorAction Stop
      } catch {
        Add-UpdateLog ('copy failed: ' + $item.FullName + ' -> ' + $dest + ' | ' + $_.Exception.Message)
      }
    }
  }
}

for ($i = 0; $i -lt 40; $i++) {
  if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) { break }
  Start-Sleep -Milliseconds 250
}

if (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) {
  Add-Content -LiteralPath $logPath -Value ('[' + (Get-Date -Format s) + '] forcing old process stop')
  Stop-Process -Id $pidToWait -Force -ErrorAction SilentlyContinue
  for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 250
  }
}

try { sc stop zapret *> $null } catch {}
try { sc delete zapret *> $null } catch {}
foreach ($image in @('zapret_zen.exe', 'TgWsProxy_windows.exe', 'winws.exe')) {
  try { taskkill /F /T /IM $image *> $null } catch {}
}

New-Item -ItemType Directory -Path $dst -Force | Out-Null

foreach ($item in $preserve) {
  $dstItem = Join-Path $dst $item
  try {
    if (Test-Path $dstItem) {
      Move-Item $dstItem (Join-Path $backupRoot $item) -Force
    }
  } catch {}
}
Add-Content -LiteralPath $logPath -Value ('[' + (Get-Date -Format s) + '] preserved user dirs')

$sourceIsStandalone = Test-StandalonePayload $src
if ($sourceIsStandalone) {
  Add-UpdateLog 'standalone payload detected'
  $oldInternal = Join-Path $dst '_internal'
  if (Test-Path $oldInternal) {
    [void](Remove-PathRobust $oldInternal)
    Add-UpdateLog 'old _internal removed for standalone update'
  }
}

Overlay-Tree $src $dst $preserve
Add-Content -LiteralPath $logPath -Value ('[' + (Get-Date -Format s) + '] payload copied')

if ($sourceIsStandalone -and -not (Test-InstalledStandalone $dst)) {
  Add-UpdateLog 'standalone validation failed after overlay, retrying top-level runtime files'
  foreach ($fileName in @('zapret_zen.exe', 'python311.dll', 'python3.dll')) {
    $sourceFile = Join-Path $src $fileName
    $targetFile = Join-Path $dst $fileName
    if (Test-Path $sourceFile) {
      [void](Remove-PathRobust $targetFile)
      try {
        Copy-Item $sourceFile $targetFile -Force -ErrorAction Stop
        Add-UpdateLog ('runtime file copied: ' + $fileName)
      } catch {
        Add-UpdateLog ('runtime file copy failed: ' + $fileName + ' | ' + $_.Exception.Message)
      }
    }
  }
}

foreach ($item in $preserve) {
  $backupItem = Join-Path $backupRoot $item
  $target = Join-Path $dst $item
  if (Test-Path $backupItem) {
    try {
      if (Test-Path $target) {
        [void](Remove-PathRobust $target)
      }
    } catch {}
    Move-Item $backupItem $target -Force
  }
}
Add-Content -LiteralPath $logPath -Value ('[' + (Get-Date -Format s) + '] user data restored')

if ($sourceIsStandalone -and -not (Test-InstalledStandalone $dst)) {
  Add-UpdateLog 'standalone validation failed, aborting relaunch to avoid broken install'
  exit 2
}

Start-Sleep -Milliseconds 400
$launch = Join-Path $dst 'zapret_zen.exe'
Start-Process -FilePath $launch -WorkingDirectory $dst
Add-Content -LiteralPath $logPath -Value ('[' + (Get-Date -Format s) + '] relaunched app')
Remove-Item $backupRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500
Remove-Item '{{SELF_DELETE}}' -Force -ErrorAction SilentlyContinue
"""


class UpdatesManager:
    REPO_URL = "https://github.com/peshk0v/Zapret-Zen"
    API_LATEST = "https://api.github.com/repos/peshk0v/Zapret-Zen/releases/latest"
    API_RELEASES = "https://api.github.com/repos/peshk0v/Zapret-Zen/releases"
    ATOM_FEED = "https://github.com/peshk0v/Zapret-Zen/releases.atom"
    SOURCEFORGE_PROJECT_URL = "https://sourceforge.net/projects/zapret-zen"
    _CACHE_FILE = "app_update_check.json"
    _CACHE_TTL_SECONDS = 3600
    _METADATA_TIMEOUT = 8
    _DOWNLOAD_CONNECT_TIMEOUT = 10
    _DOWNLOAD_READ_TIMEOUT = 12
    _DOWNLOAD_BUDGET = 120

    def __init__(self, storage: StorageManager, logging: LoggingManager, *, processes: object | None = None) -> None:
        self.storage = storage
        self.logging = logging
        recovery = getattr(processes, "with_github_connectivity_recovery", None)
        self.github = GitHubNetworkClient(logging, recovery_runner=recovery if callable(recovery) else None)
        self._domain_bypass = getattr(processes, "with_domain_connectivity_recovery", None) if processes is not None else None

    def check_updates(self) -> list[UpdateInfo]:
        app_release = self.fetch_latest_application_release()
        app_status = UpdateInfo(
            target="application",
            current_version=__version__,
            latest_version=str(app_release.get("latest_version", __version__)),
            status=str(app_release.get("status", "error")),
            changelog=str(app_release.get("body", "")),
        )

        cache_file = self.storage.paths.cache_dir / "mods_index.json"
        cache_stamp = datetime.fromtimestamp(cache_file.stat().st_mtime).isoformat() if cache_file.exists() else "missing"
        updates = [
            app_status,
            UpdateInfo(
                target="mods-index",
                current_version=cache_stamp,
                latest_version=cache_stamp,
                status="ready",
                changelog="Local sample index loaded",
            ),
        ]
        self.logging.log("info", "Update check completed", items=len(updates), app_status=app_status.status)
        return updates

    def fetch_latest_application_release(
        self,
        update_branch: str = "release",
        progress: Callable[[float | None, int, int, str, str], None] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, str]:
        cached = self._cached_check_result(update_branch=update_branch) if not force else None
        if cached is not None:
            return cached
        try:
            if progress is not None:
                progress(None, 0, 0, "check-github", "")
            payload = self.github.github_json(self.API_RELEASES, timeout=self._METADATA_TIMEOUT, purpose="app-release-metadata", retry=False)
        except Exception as error:
            if is_github_rate_limit_error(error):
                atom_result = self._fetch_release_via_atom(update_branch)
                if atom_result is not None:
                    return atom_result
                stale = self._cached_check_result(ignore_ttl=True, update_branch=update_branch)
                if stale is not None:
                    return stale
            self.logging.log("warning", "Failed to fetch latest app release", error=str(error))
            if progress is not None:
                progress(None, 0, 0, "check-github-fallback", "")
            sourceforge_result = self._fetch_latest_via_sourceforge(progress, update_branch)
            if sourceforge_result is not None:
                return sourceforge_result
            friendly_error = self._friendly_network_error(error)
            return {
                "status": "error",
                "current_version": __version__,
                "latest_version": __version__,
                "error": friendly_error,
                "html_url": self.REPO_URL + "/releases",
            }

        releases = self._normalize_release_entries(payload, update_branch == "prerelease")
        if not releases:
            return {
                "status": "error",
                "current_version": __version__,
                "latest_version": __version__,
                "error": "No GitHub releases were found.",
                "html_url": self.REPO_URL + "/releases",
                "releases": [],
            }
        return self._compose_release_result(releases, update_branch=update_branch)

    def _fetch_release_via_atom(self, update_branch: str = "release") -> dict[str, str] | None:
        try:
            text = self._download_atom_text()
        except Exception as error:
            self.logging.log("warning", "Failed to fetch app releases via atom feed", error=str(error))
            return None
        releases = self._normalize_atom_entries(text)
        if not releases:
            return None
        return self._compose_release_result(releases, update_branch=update_branch)

    def _fetch_latest_via_sourceforge(
        self,
        progress: Callable[[float | None, int, int, str, str], None] | None = None,
        update_branch: str = "",
    ) -> dict[str, str] | None:
        page_url = self.SOURCEFORGE_PROJECT_URL + "/files/"
        try:
            html = self._download_text(page_url)
        except Exception as error:
            self.logging.log("warning", "SourceForge files page unavailable", error=str(error))
            return None
        if not html:
            return None
        versions = self._extract_sourceforge_versions(html)
        if not versions:
            self.logging.log("warning", "No versions found on SourceForge files page")
            return None
        latest_version = versions[0]
        asset_name = self._detect_sourceforge_asset_name(html) or self._default_asset_name()
        result: dict[str, str] = {
            "status": "available",
            "current_version": __version__,
            "latest_version": latest_version,
            "html_url": self.SOURCEFORGE_PROJECT_URL + "/files/",
            "body": "SourceForge",
            "asset_name": asset_name,
            "asset_url": self._build_sourceforge_asset_url(asset_name),
            "is_hotfix": "",
            "source": "sourceforge",
        }
        if self._version_key(latest_version) <= self._version_key(__version__):
            result["status"] = "up-to-date"
        self._cache_check_result(result, update_branch=update_branch)
        return result

    def _build_sourceforge_asset_url(self, asset_name: str) -> str:
        name = str(asset_name or "").strip()
        if not name:
            return ""
        return f"{self.SOURCEFORGE_PROJECT_URL}/files/{urllib.parse.quote(name)}/download"

    def _default_asset_name(self) -> str:
        machine = platform.machine().lower()
        arch = "arm64" if ("arm" in machine or "aarch64" in machine) else "x64"
        return f"Zapret-Zen-portable-win-{arch}.zip"

    def _download_text(self, url: str) -> str:
        data = self.github.github_bytes(url, timeout=self._METADATA_TIMEOUT, purpose="sourceforge-page", retry=False)
        return data.decode("utf-8", errors="replace")

    def _extract_sourceforge_versions(self, html: str) -> list[str]:
        candidates: set[str] = set()
        for match in re.finditer(r"(?i)zapret[\s_-]*zen[^0-9A-Za-z<]{0,24}(?:v)?(\d+\.\d+\.\d+)", html):
            candidates.add(match.group(1))
        for match in re.finditer(r"(?i)/files/(?:v)?(\d+\.\d+\.\d+)/", html):
            candidates.add(match.group(1))
        valid = [item for item in candidates if self._version_key(item) > self._version_key("0.0.0")]
        return sorted(valid, key=self._version_key, reverse=True)

    def _detect_sourceforge_asset_name(self, html: str) -> str:
        arch = "arm64" if ("arm" in platform.machine().lower() or "aarch64" in platform.machine().lower()) else "x64"
        pattern = re.compile(r"[^\"'<>\s]*zapret[^\"'<>\s]*win_(?:" + arch + r")\.zip", re.IGNORECASE)
        for match in pattern.finditer(html):
            name = re.sub(r"^.*?/(?=[^/]*$)", "", match.group(0))
            name = re.sub(r"[?&].*$", "", name)
            if name.lower().startswith(("zapret-", "zapret_")) and name.lower().endswith(".zip"):
                return name
        return ""

    @staticmethod
    def _friendly_network_error(error: BaseException) -> str:
        text = str(error)
        reason = getattr(error, "reason", None)
        winerror = getattr(reason, "winerror", None)
        if winerror == 10060:
            return "Connection timed out (WinError 10060). Please check your network connection and try again."
        if isinstance(error, TimeoutError) or "timed out" in text.lower() or "timeout" in text.lower():
            return "Connection timed out. Please check your network connection and try again."
        if "CERTIFICATE_VERIFY_FAILED" in text.upper():
            return "Unable to verify the server certificate on this system. Please try again later."
        if "HTTP Error 404" in text:
            return "The requested update was not found on the update server."
        return "Unable to reach the update server. Please check your network connection and try again."

    @staticmethod
    def _friendly_download_error(errors: list[str]) -> str:
        combined = "\n".join(errors)
        lower = combined.lower()
        if "10060" in combined or "winerror 10060" in lower:
            return "Connection timed out (WinError 10060) while downloading the update. Please check your network connection and try again."
        if "timed out" in lower or "timeout" in lower:
            return "Connection timed out while downloading the update. Please check your network connection and try again."
        if "certificate_verify_failed" in lower:
            return "Unable to verify the update server certificate. Please try again later."
        if "404" in combined:
            return "The requested update was not found on any mirror. Please try again later."
        return "The update could not be downloaded from any mirror. Please try again later."

    def _compose_release_result(self, releases: list[dict[str, object]], *, update_branch: str = "") -> dict[str, str]:
        latest = releases[0]
        release_payload = latest["payload"]
        latest_version = str(latest["version"]).strip() or __version__
        html_url = str(latest["html_url"]).strip() or (self.REPO_URL + "/releases")
        body = str(latest["body"]).strip()
        asset = self._pick_release_asset(release_payload.get("assets") or [])
        latest_release_stamp = self._release_timestamp(latest, asset)
        installed_stamp = self._installed_build_timestamp()
        is_newer_version = self._version_key(latest_version) > self._version_key(__version__)
        is_same_version_hotfix = (
            self._version_key(latest_version) == self._version_key(__version__)
            and latest_release_stamp is not None
            and installed_stamp is not None
            and latest_release_stamp.timestamp() > installed_stamp.timestamp() + 300
        )
        status = "available" if is_newer_version or is_same_version_hotfix else "up-to-date"
        newer_releases = [
            {
                "version": str(item["version"]),
                "body": str(item["body"]),
                "html_url": str(item["html_url"]),
                "is_latest": bool(idx == 0),
                "is_hotfix": bool(idx == 0 and is_same_version_hotfix),
            }
            for idx, item in enumerate(releases)
            if self._version_key(str(item["version"])) > self._version_key(__version__) or (idx == 0 and is_same_version_hotfix)
        ]
        result = {
            "status": status,
            "current_version": __version__,
            "latest_version": latest_version,
            "html_url": html_url,
            "body": body,
            "asset_name": str(asset.get("name", "")) if asset else "",
            "asset_url": str(asset.get("browser_download_url", "")) if asset else "",
            "is_hotfix": bool(is_same_version_hotfix),
            "release_updated_at": latest_release_stamp.isoformat() if latest_release_stamp else "",
            "installed_build_at": installed_stamp.isoformat() if installed_stamp else "",
            "releases": newer_releases,
        }
        self._cache_check_result(result, update_branch=update_branch)
        return result

    def _request_json(self, url: str, *, timeout: int) -> object:
        return self.github.github_json(url, timeout=timeout, purpose="app-release-metadata")

    def _download_bytes(self, url: str, *, timeout: int) -> bytes:
        return self.github.github_bytes(url, timeout=timeout, purpose="app-update-download")

    def _download_atom_text(self) -> str:
        data = self.github.github_bytes(self.ATOM_FEED, timeout=10, purpose="app-release-atom", retry=False)
        return data.decode("utf-8", errors="replace")

    def _cache_path(self) -> Path:
        return self.storage.paths.cache_dir / self._CACHE_FILE

    def _cached_check_result(self, *, ignore_ttl: bool = False, update_branch: str = "") -> dict[str, str] | None:
        try:
            raw = self.storage.read_json(self._cache_path(), default={}) or {}
        except Exception:
            return None
        result = raw.get("result")
        if not isinstance(result, dict):
            return None
        status = str(result.get("status", ""))
        if status not in {"available", "up-to-date"}:
            return None
        cached_branch = str(raw.get("branch", "") or "")
        if update_branch and cached_branch != update_branch:
            return None
        if ignore_ttl:
            return dict(result)
        checked_at = raw.get("checked_at")
        try:
            stamp = datetime.fromisoformat(str(checked_at))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if age <= self._CACHE_TTL_SECONDS:
            return dict(result)
        return None

    def _cache_check_result(self, result: dict[str, str], *, update_branch: str = "") -> None:
        if str(result.get("status", "")) not in {"available", "up-to-date"}:
            return
        try:
            self.storage.write_json(
                self._cache_path(),
                {"checked_at": datetime.now(timezone.utc).isoformat(), "branch": update_branch, "result": result},
            )
        except Exception:
            pass

    @staticmethod
    def _atom_text(entry: ET.Element, tag: str, ns: dict[str, str]) -> str:
        node = entry.find(f"a:{tag}", ns)
        return str(node.text or "") if node is not None else ""

    def _normalize_atom_entries(self, text: str) -> list[dict[str, object]]:
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return []
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries: list[dict[str, object]] = []
        for entry in root.findall("a:entry", ns):
            title = html_unescape(self._atom_text(entry, "title", ns))
            link = entry.find("a:link", ns)
            href = str(link.get("href", "")) if link is not None else ""
            updated_raw = self._atom_text(entry, "updated", ns)
            content = html_unescape(self._atom_text(entry, "content", ns))
            body = re.sub(r"<[^>]+>", "", content).strip()
            tag_match = re.search(r"/releases/tag/([^/?#]+)", href)
            version = ""
            if tag_match:
                version = urllib.parse.unquote(tag_match.group(1)).lstrip("v")
            if not version:
                title_match = re.match(r"\s*release\s+v?(.+?)\s*$", title, re.IGNORECASE)
                if title_match:
                    version = title_match.group(1).strip().lstrip("v")
            if not version:
                continue
            entries.append(
                {
                    "version": version,
                    "body": body[:4000],
                    "html_url": href or self.REPO_URL + "/releases",
                    "published_at": updated_raw,
                    "updated_at": updated_raw,
                    "payload": {"assets": []},
                }
            )
        entries.sort(key=lambda item: self._version_key(str(item["version"])), reverse=True)
        return entries

    def _is_certificate_error(self, error: Exception) -> bool:
        return "CERTIFICATE_VERIFY_FAILED" in str(error).upper()

    def _normalize_release_entries(self, payload: object, include_prerelease: bool = False) -> list[dict[str, object]]:
        if not isinstance(payload, list):
            return []
        entries: list[dict[str, object]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if bool(item.get("draft")):
                continue
            if not include_prerelease and bool(item.get("prerelease")):
                continue
            version = str(item.get("tag_name") or item.get("name") or "").strip().lstrip("v")
            if not version:
                continue
            entries.append(
                {
                    "version": version,
                    "body": str(item.get("body") or ""),
                    "html_url": str(item.get("html_url") or self.REPO_URL + "/releases"),
                    "published_at": str(item.get("published_at") or ""),
                    "updated_at": str(item.get("updated_at") or ""),
                    "payload": item,
                }
            )
        entries.sort(key=lambda item: self._version_key(str(item["version"])), reverse=True)
        return entries

    def _release_timestamp(self, release: dict[str, object], asset: dict[str, object] | None) -> datetime | None:
        candidates = [
            self._parse_github_datetime(str(release.get("published_at") or "")),
            self._parse_github_datetime(str(release.get("updated_at") or "")),
        ]
        if asset:
            candidates.extend(
                [
                    self._parse_github_datetime(str(asset.get("created_at") or "")),
                    self._parse_github_datetime(str(asset.get("updated_at") or "")),
                ]
            )
        valid = [item for item in candidates if item is not None]
        return max(valid) if valid else None

    def _installed_build_timestamp(self) -> datetime | None:
        if not is_packaged_runtime():
            return None
        candidates: list[Path] = []
        try:
            candidates.append(Path(sys.executable))
        except Exception:
            pass
        try:
            candidates.append(Path(__file__))
        except Exception:
            pass
        stamps: list[datetime] = []
        for path in candidates:
            try:
                if path.exists():
                    stamps.append(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
            except OSError:
                continue
        return max(stamps) if stamps else None

    def _parse_github_datetime(self, value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def prepare_update(
        self,
        release_info: dict[str, str],
        progress: Callable[[float | None, int, int, str, str], None] | None = None,
    ) -> dict[str, str]:
        asset_url = str(release_info.get("asset_url") or "").strip()
        asset_name = str(release_info.get("asset_name") or "").strip() or self._default_asset_name()
        if not asset_url and not self._build_sourceforge_asset_url(asset_name):
            raise ValueError("No downloadable asset was found for this platform.")

        temp_root = Path(tempfile.mkdtemp(prefix="zapret_zen_update_"))
        try:
            zip_path = temp_root / asset_name
            self._download_update_asset(release_info, zip_path, progress)
            return self._extract_update_package(zip_path, temp_root, release_info, progress=progress)
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise

    def _download_update_asset(
        self,
        release_info: dict[str, str],
        destination: Path,
        progress: Callable[[float | None, int, int, str, str], None] | None,
    ) -> None:
        asset_url = str(release_info.get("asset_url") or "").strip()
        asset_name = str(release_info.get("asset_name") or "").strip() or self._default_asset_name()
        sf_url = self._build_sourceforge_asset_url(asset_name)
        github_domains = ("github.com", "api.github.com", "codeload.github.com")
        errors: list[str] = []
        prefer_sourceforge = str(release_info.get("source", "")) == "sourceforge"

        if asset_url and not prefer_sourceforge:
            try:
                if progress is not None:
                    progress(None, 0, 0, "download-github", "")
                self._download_with_progress(asset_url, destination, progress, "download-github")
                return
            except Exception as error:
                errors.append(f"github: {error}")
                self.logging.log("warning", "App update GitHub download failed", error=str(error))
            if self._domain_bypass is not None:
                try:
                    if progress is not None:
                        progress(None, 0, 0, "download-zapret", ",".join(github_domains))
                    self._domain_bypass(
                        github_domains,
                        lambda: self._download_with_progress(asset_url, destination, progress, "download-github"),
                        "app-update-download",
                    )
                    return
                except Exception as error:
                    errors.append(f"github-zapret: {error}")
                    self.logging.log("warning", "App update download via Zapret+GitHub failed", error=str(error))

        if sf_url:
            try:
                if progress is not None:
                    progress(None, 0, 0, "download-sourceforge", "")
                self._download_with_progress(sf_url, destination, progress, "download-sourceforge")
                return
            except Exception as error:
                errors.append(f"sourceforge: {error}")
                self.logging.log("warning", "App update SourceForge download failed", error=str(error))
            if self._domain_bypass is not None:
                try:
                    if progress is not None:
                        progress(None, 0, 0, "download-zapret", "sourceforge.net")
                    self._domain_bypass(
                        ("sourceforge.net",),
                        lambda: self._download_with_progress(sf_url, destination, progress, "download-sourceforge"),
                        "app-update-download",
                    )
                    return
                except Exception as error:
                    errors.append(f"sourceforge-zapret: {error}")
                    self.logging.log("warning", "App update download via Zapret+SourceForge failed", error=str(error))

        raise RuntimeError(self._friendly_download_error(errors))

    def _download_with_progress(
        self,
        url: str,
        destination: Path,
        progress: Callable[[float | None, int, int, str, str], None] | None,
        status_token: str,
    ) -> None:
        def on_bytes(received: int, total: int, fraction: float | None) -> None:
            if progress is not None:
                progress(fraction, received, total, status_token, "")

        self.github.download_stream(
            url,
            destination,
            connect_timeout=self._DOWNLOAD_CONNECT_TIMEOUT,
            read_timeout=self._DOWNLOAD_READ_TIMEOUT,
            total_timeout=self._DOWNLOAD_BUDGET,
            purpose="app-update-download",
            min_bytes=1,
            progress_cb=on_bytes if progress is not None else None,
        )

    def prepare_local_update(self, zip_path: str) -> dict[str, str]:
        src = Path(zip_path).resolve()
        if not src.exists():
            raise FileNotFoundError(f"Update file not found: {zip_path}")

        temp_root = Path(tempfile.mkdtemp(prefix="zapret_zen_local_update_"))
        try:
            return self._extract_update_package(src, temp_root, {"latest_version": ""})
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise

    def _extract_update_package(
        self,
        zip_path: Path,
        temp_root: Path,
        release_info: dict[str, str],
        progress: Callable[[float | None, int, int, str, str], None] | None = None,
    ) -> dict[str, str]:
        if progress is not None:
            progress(0.0, 0, 0, "extract", "")
        extract_root = temp_root / "payload"
        extract_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as archive:
            archive.extractall(extract_root)

        payload_root = self._resolve_payload_root(extract_root)
        launch_exe = payload_root / "zapret_zen.exe"
        if not launch_exe.exists():
            raise FileNotFoundError("The update package does not contain zapret_zen.exe.")

        if progress is not None:
            progress(1.0, 0, 0, "extract", "")
        return {
            "temp_root": str(temp_root),
            "extract_root": str(payload_root),
            "launch_exe": str(launch_exe),
            "version": str(release_info.get("latest_version", "")),
        }

    def _resolve_payload_root(self, extract_root: Path) -> Path:
        direct_exe = extract_root / "zapret_zen.exe"
        if direct_exe.exists():
            return extract_root
        named_root = extract_root / "zapret_zen"
        if (named_root / "zapret_zen.exe").exists():
            return named_root
        for candidate in extract_root.iterdir():
            if candidate.is_dir() and (candidate / "zapret_zen.exe").exists():
                return candidate
        return extract_root

    def launch_update(self, prepared_update: dict[str, str]) -> None:
        extract_root = Path(prepared_update["extract_root"])
        install_root = self.storage.paths.install_root
        current_executable = Path(sys.executable).resolve()
        current_pid = os.getpid()
        script_root = Path(tempfile.gettempdir()) / "zapret_zen_updates"
        script_root.mkdir(parents=True, exist_ok=True)
        script_path = script_root / f"apply_update_{int(datetime.now(timezone.utc).timestamp() * 1000)}.ps1"
        launcher_path = script_root / f"apply_update_{int(datetime.now(timezone.utc).timestamp() * 1000)}.cmd"
        log_path = script_root / f"apply_update_{int(datetime.now(timezone.utc).timestamp() * 1000)}.log"

        template = _PS1_TEMPLATE
        def _esc(val: str) -> str:
            return str(val).replace("'", "''")
        script = (
            template
            .replace("{{PID}}", str(current_pid))
            .replace("{{SRC}}", _esc(extract_root))
            .replace("{{DST}}", _esc(install_root))
            .replace("{{LAUNCH}}", _esc(current_executable))
            .replace("{{TEMP_ROOT}}", _esc(Path(prepared_update["temp_root"])))
            .replace("{{LOG_PATH}}", _esc(log_path))
            .replace("{{SCRIPT_ROOT}}", _esc(script_root))
            .replace("{{SELF_DELETE}}", _esc(script_path))
        )
        script_path.write_text(script, encoding="utf-8")
        launcher = textwrap.dedent(
            f"""
            @echo off
            start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script_path}"
            exit /b 0
            """
        ).strip() + "\n"
        launcher_path.write_text(launcher, encoding="utf-8")

        startupinfo = None
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0

        subprocess.Popen(
            [
                "cmd.exe",
                "/c",
                str(launcher_path),
            ],
            creationflags=creationflags,
            startupinfo=startupinfo,
            cwd=str(install_root),
        )
        self.logging.log("info", "App update launched", target_version=prepared_update.get("version", ""), source=str(extract_root))

    def _pick_release_asset(self, assets: list[dict[str, object]]) -> dict[str, object] | None:
        machine = platform.machine().lower()
        want_arm = "arm" in machine or "aarch64" in machine
        pattern = re.compile(r"portable.*win_arm64\.zip$", re.IGNORECASE) if want_arm else re.compile(r"portable.*win_x64\.zip$", re.IGNORECASE)
        for asset in assets:
            name = str(asset.get("name") or "")
            if pattern.search(name):
                return asset
        return None

    @staticmethod
    def _version_key(version: str) -> tuple[int, ...]:
        m = re.match(r"(\d+(?:\.\d+)*)", version)
        if not m:
            return (0,)
        base_parts = [int(p) for p in m.group(1).split(".")]
        while len(base_parts) > 1 and base_parts[-1] == 0:
            base_parts.pop()
        base = tuple(base_parts)
        remaining = version[m.end():]
        suffix_weights = {"": 3, "p": 2, "b": 1, "d": 0}
        suffix_letter = ""
        suffix_num = 0
        if remaining:
            suffix_match = re.match(r"([a-zA-Z])(\d*)", remaining)
            if suffix_match:
                suffix_letter = suffix_match.group(1)
                suffix_num = int(suffix_match.group(2)) if suffix_match.group(2) else 0
        weight = suffix_weights.get(suffix_letter, -1)
        return base + (weight, suffix_num)

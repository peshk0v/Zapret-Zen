from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable

from zapret_zen.services.github_network import GitHubNetworkClient
from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.storage import StorageManager


class RuntimeUpdateManager:
    """Download and install runtime updates (zapret, tg-ws-proxy)."""

    def __init__(
        self,
        storage: StorageManager,
        logging: LoggingManager,
        github: GitHubNetworkClient,
        stop_component: Callable[[str], Any],
        start_component: Callable[[str], Any],
        is_image_running: Callable[[str], bool],
        rebuild_snapshot: Callable[[], None],
        tg_running: Callable[[], bool] | None = None,
        probe_tgws_worker: Callable[[Path], tuple[bool, str]] | None = None,
    ) -> None:
        self.storage = storage
        self.logging = logging
        self.github = github
        self._stop_component = stop_component
        self._start_component = start_component
        self._is_image_running = is_image_running
        self._rebuild_snapshot = rebuild_snapshot
        self._tg_running = tg_running
        self._probe_tgws_worker = probe_tgws_worker

    def fetch_latest_zapret_release(self) -> dict[str, str]:
        api_url = "https://api.github.com/repos/Flowseal/zapret-discord-youtube/releases/latest"
        try:
            payload = self.github.github_json(api_url, timeout=20, purpose="zapret-release-metadata")
            if not isinstance(payload, dict):
                raise ValueError("Invalid zapret release metadata")
        except Exception as error:
            self.logging.log("warning", "Zapret release metadata fallback", error=str(error))
            return {
                "latest_version": "",
                "asset_url": "",
                "asset_name": "",
                "zipball_url": "https://codeload.github.com/Flowseal/zapret-discord-youtube/zip/refs/heads/main",
            }
        latest_version = str(payload.get("tag_name") or payload.get("name") or "").strip().lstrip("v")
        asset = next(
            (
                item
                for item in list(payload.get("assets") or [])
                if isinstance(item, dict) and str(item.get("name", "")).lower().endswith(".zip")
            ),
            None,
        )
        return {
            "latest_version": latest_version,
            "asset_url": str((asset or {}).get("browser_download_url", "")),
            "asset_name": str((asset or {}).get("name", "")),
            "zipball_url": str(payload.get("zipball_url") or ""),
        }

    def fetch_latest_tg_ws_proxy_release(self) -> dict[str, str]:
        api_url = "https://api.github.com/repos/Flowseal/tg-ws-proxy/releases/latest"
        fallback_url = "https://codeload.github.com/Flowseal/tg-ws-proxy/zip/refs/heads/main"
        try:
            payload = self.github.github_json(api_url, timeout=20, purpose="tg-ws-proxy-release-metadata")
            if not isinstance(payload, dict):
                raise ValueError("Invalid tg-ws-proxy release metadata")
        except Exception as error:
            self.logging.log("warning", "TG WS Proxy release metadata fallback", error=str(error))
            return {
                "latest_version": "",
                "source_url": fallback_url,
            }
        latest_version = str(payload.get("tag_name") or payload.get("name") or "").strip().lstrip("v")
        return {
            "latest_version": latest_version,
            "source_url": str(payload.get("zipball_url") or "").strip() or fallback_url,
        }

    def update_zapret_runtime(self) -> dict[str, str]:
        release = self.fetch_latest_zapret_release()
        latest_version = str(release.get("latest_version", "")).strip()
        current_version = self.storage._detect_zapret_version()
        if latest_version and current_version == latest_version:
            return {"status": "up-to-date", "version": current_version}
        candidates = [
            (
                str(release.get("asset_url", "")).strip(),
                str(release.get("asset_name", "") or "zapret-release.zip"),
            ),
            (
                str(release.get("zipball_url", "")).strip(),
                "zapret-source.zip",
            ),
        ]
        candidates = [(url, name) for url, name in candidates if url]
        if not candidates:
            return {"status": "error", "error": "No zapret archive URL found"}
        return self._install_zapret_archive(version=latest_version or current_version, candidates=candidates)

    def _install_zapret_archive(self, *, version: str, candidates: list[tuple[str, str]]) -> dict[str, str]:
        current_version = self.storage._detect_zapret_version()
        if version and current_version == version:
            return {"status": "up-to-date", "version": current_version}
        runtime_root = self.storage.paths.runtime_dir / "zapret-discord-youtube"
        was_running = self._is_image_running("winws.exe")
        temp_root = Path(tempfile.mkdtemp(prefix="zapret_zen_zapret_update_"))
        try:
            last_error = ""
            source_root: Path | None = None
            for index, (archive_url, archive_name) in enumerate(candidates):
                try:
                    zip_path = temp_root / f"{index}_{Path(archive_name).name or 'zapret.zip'}"
                    self._download_to_file(archive_url, zip_path, timeout=75)
                    extract_root = temp_root / f"extract_{index}"
                    extract_root.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(zip_path, "r") as archive:
                        archive.extractall(extract_root)
                    source_root = self._find_extracted_zapret_root(extract_root)
                    if source_root is not None:
                        break
                    last_error = f"Invalid zapret archive structure: {archive_name}"
                except Exception as error:
                    last_error = str(error)
                    self.logging.log("warning", "Zapret archive download failed", url=archive_url, error=last_error)
            if source_root is None:
                return {"status": "error", "error": last_error or "Invalid zapret archive"}
            if was_running:
                self._stop_component("zapret")
            backup = self.storage.create_backup(runtime_root, "pre-update-zapret")
            if runtime_root.exists():
                shutil.rmtree(runtime_root, ignore_errors=True)
            shutil.copytree(source_root, runtime_root, dirs_exist_ok=True)
            if version:
                self._patch_zapret_local_version(runtime_root, version)
            self.storage.ensure_layout()
            self._rebuild_snapshot()
            if was_running:
                self._start_component("zapret")
            self.logging.log("info", "Zapret updated", version=version, backup=str(backup or ""))
            return {"status": "updated", "version": version or current_version}
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def update_tg_ws_proxy_runtime(self) -> dict[str, str]:
        release = self.fetch_latest_tg_ws_proxy_release()
        latest_version = str(release.get("latest_version", "")).strip()
        current_version = self.storage._detect_tgws_version()
        if latest_version and current_version == latest_version:
            return {"status": "up-to-date", "version": current_version}
        candidates = [
            (str(release.get("source_url", "")).strip(), "tg-ws-proxy-source.zip"),
        ]
        candidates = [(url, name) for url, name in candidates if url]
        if not candidates:
            return {"status": "error", "error": "No tg-ws-proxy source archive found"}
        runtime_root = self.storage.paths.runtime_dir / "tg-ws-proxy"
        was_running = bool(self._tg_running()) if self._tg_running is not None else False
        temp_root = Path(tempfile.mkdtemp(prefix="zapret_zen_tgws_update_"))
        try:
            last_error = ""
            source_root: Path | None = None
            for index, (archive_url, archive_name) in enumerate(candidates):
                try:
                    zip_path = temp_root / f"{index}_{archive_name}"
                    self._download_to_file(archive_url, zip_path)
                    extract_root = temp_root / f"extract_{index}"
                    extract_root.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(zip_path, "r") as archive:
                        archive.extractall(extract_root)
                    source_root = self._find_extracted_tgws_root(extract_root)
                    if source_root is not None:
                        break
                    last_error = f"Invalid tg-ws-proxy archive structure: {archive_name}"
                except Exception as error:
                    last_error = str(error)
                    self.logging.log("warning", "TG WS Proxy archive download failed", url=archive_url, error=last_error)
            if source_root is None:
                return {"status": "error", "error": last_error or "Invalid tg-ws-proxy archive"}
            if was_running:
                try:
                    self._stop_component("tg-ws-proxy")
                except Exception as error:
                    self.logging.log("warning", "TG WS Proxy stop before update failed", error=str(error))
            backup = None
            if runtime_root.exists():
                backup = self.storage.create_backup(runtime_root, "pre-update-tgws")
                shutil.rmtree(runtime_root, ignore_errors=True)
            shutil.copytree(source_root, runtime_root)
            self.storage.ensure_layout()
            probe_ok, probe_error = self._verify_tgws_runtime(runtime_root)
            if not probe_ok and backup is not None:
                self._restore_tgws_backup(backup, runtime_root)
                if was_running:
                    try:
                        self._start_component("tg-ws-proxy")
                    except Exception as error:
                        self.logging.log("warning", "TG WS Proxy restart after rollback failed", error=str(error))
                self.logging.log(
                    "error",
                    "TG WS Proxy update rolled back",
                    version=latest_version or current_version,
                    error=probe_error,
                    backup=str(backup),
                )
                return {"status": "rolled-back", "version": current_version, "error": probe_error}
            if not probe_ok:
                self.logging.log(
                    "error",
                    "TG WS Proxy update failed the startup probe",
                    version=latest_version or current_version,
                    error=probe_error,
                )
                return {"status": "error", "version": latest_version or current_version, "error": probe_error}
            self._rebuild_snapshot()
            if was_running:
                try:
                    self._start_component("tg-ws-proxy")
                except Exception as error:
                    self.logging.log("warning", "TG WS Proxy restart after update failed", error=str(error))
            self.logging.log(
                "info",
                "TG WS Proxy updated from source",
                version=latest_version or current_version,
                backup=str(backup or ""),
            )
            return {"status": "updated", "version": latest_version or current_version}
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def _verify_tgws_runtime(self, runtime_root: Path) -> tuple[bool, str]:
        if self._probe_tgws_worker is None:
            return True, ""
        try:
            return self._probe_tgws_worker(runtime_root)
        except Exception as error:
            return False, f"Worker probe raised an error: {error}"

    def _restore_tgws_backup(self, backup: Path, runtime_root: Path) -> None:
        source = backup / runtime_root.name
        shutil.rmtree(runtime_root, ignore_errors=True)
        shutil.copytree(source, runtime_root, dirs_exist_ok=True)
        self.storage.ensure_layout()
        self._rebuild_snapshot()

    def _find_extracted_tgws_root(self, extract_root: Path) -> Path | None:
        candidates = [extract_root]
        candidates.extend(path for path in extract_root.iterdir() if path.is_dir())
        for candidate in candidates:
            if (candidate / "proxy" / "tg_ws_proxy.py").exists():
                return candidate
        for candidate in extract_root.rglob("*"):
            if candidate.is_dir() and (candidate / "proxy" / "tg_ws_proxy.py").exists():
                return candidate
        return None

    def _download_to_file(self, url: str, destination: Path, timeout: int = 60) -> None:
        self.github.github_download(url, destination, timeout=timeout, purpose=f"download:{Path(destination).name}", min_bytes=1024)

    def _find_extracted_zapret_root(self, extract_root: Path) -> Path | None:
        candidates = [extract_root]
        candidates.extend(path for path in extract_root.iterdir() if path.is_dir())
        for candidate in candidates:
            if (candidate / "bin").exists() and (candidate / "lists").exists():
                return candidate
        for candidate in extract_root.rglob("*"):
            if candidate.is_dir() and (candidate / "bin").exists() and (candidate / "lists").exists():
                return candidate
        return None

    def _patch_zapret_local_version(self, runtime_root: Path, version: str) -> None:
        service_bat = runtime_root / "service.bat"
        if not service_bat.exists():
            return
        try:
            content = service_bat.read_text(encoding="utf-8", errors="ignore")
            updated = re.sub(
                r'(?im)^(\s*set\s+"?LOCAL_VERSION\s*=\s*)[^"\r\n]+("?\s*)$',
                rf"\g<1>{version}\2",
                content,
                count=1,
            )
            if updated != content:
                service_bat.write_text(updated, encoding="utf-8")
        except Exception:
            pass

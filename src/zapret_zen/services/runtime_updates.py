from __future__ import annotations

import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable

from zapret_zen.platform_utils import IS_LINUX, IS_WINDOWS
from zapret_zen.services.github_network import GitHubNetworkClient
from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.storage import StorageManager

_NFQWS_BOL_VAN_REPO = "bol-van/zapret"
_STRATEGIES_FLOWSEAL_REPO = "Flowseal/zapret-discord-youtube"
_STRATEGIES_ARCHIVE_URL = "https://github.com/Flowseal/zapret-discord-youtube/archive/refs/heads/main.zip"

_NFQWS_ARCHIVE_DIR = "linux-x86_64"


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
    ) -> None:
        self.storage = storage
        self.logging = logging
        self.github = github
        self._stop_component = stop_component
        self._start_component = start_component
        self._is_image_running = is_image_running
        self._rebuild_snapshot = rebuild_snapshot
        self._tg_running = tg_running

    def fetch_latest_zapret_release(self) -> dict[str, str]:
        if IS_WINDOWS:
            api_url = "https://api.github.com/repos/Flowseal/zapret-discord-youtube/releases/latest"
            fallback_zipball = "https://codeload.github.com/Flowseal/zapret-discord-youtube/zip/refs/heads/main"
        else:
            api_url = "https://api.github.com/repos/Sergeydigl3/zapret-discord-youtube-rust/releases/latest"
            fallback_zipball = "https://codeload.github.com/Sergeydigl3/zapret-discord-youtube-rust/zip/refs/heads/main"
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
                "zipball_url": fallback_zipball,
            }
        latest_version = str(payload.get("tag_name") or payload.get("name") or "").strip().lstrip("v")
        asset = None
        if IS_WINDOWS:
            asset = next(
                (
                    item
                    for item in list(payload.get("assets") or [])
                    if isinstance(item, dict) and str(item.get("name", "")).lower().endswith(".zip")
                ),
                None,
            )
        else:
            asset = next(
                (
                    item
                    for item in list(payload.get("assets") or [])
                    if isinstance(item, dict) and "linux" in str(item.get("name", "")).lower()
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

    def ensure_zapret_rust_dependencies(self) -> dict[str, str]:
        """Ensure the zapret-rust runtime has its nfqws binary and strategy files.

        The zapret-rust binary does not bundle nfqws or the strategy (.bat) files;
        it downloads them itself on first run. Zapret-Zen runs it non-interactively,
        so we provision these dependencies up front. No-op on Windows.
        """
        if not IS_LINUX:
            return {"status": "skipped", "reason": "not-linux"}
        runtime_root = self.storage.paths.runtime_dir / "zapret-discord-youtube-rust"
        bin_dir = runtime_root / "bin"
        nfqws_path = bin_dir / "nfqws"
        strategies_dir = runtime_root / "zapret-discord-youtube-linux"
        strategies_ok = (strategies_dir / "general.bat").exists()

        if nfqws_path.exists() and nfqws_path.is_file() and strategies_ok:
            return {"status": "ok", "material": "present"}

        temp_root = Path(tempfile.mkdtemp(prefix="zapret_zen_zapret_deps_"))
        downloaded = []
        try:
            if not (nfqws_path.exists() and nfqws_path.is_file()):
                nfqws_result = self._ensure_zapret_nfqws(runtime_root, bin_dir, nfqws_path, temp_root)
                if not nfqws_result.get("ok"):
                    return {
                        "status": "error",
                        "error": nfqws_result.get("error", "Failed to download nfqws"),
                    }
                downloaded.append("nfqws")

            if not strategies_ok:
                strategies_result = self._ensure_zapret_strategies(runtime_root, strategies_dir, temp_root)
                if not strategies_result.get("ok"):
                    return {
                        "status": "error",
                        "error": strategies_result.get("error", "Failed to download strategies"),
                    }
                downloaded.append("strategies")

            self.storage.ensure_layout()
            self.logging.log("info", "Zapret-rust dependencies ensured", required=", ".join(downloaded) or "none")
            return {"status": "ok", "material": ", ".join(downloaded) or "present"}
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def _ensure_zapret_nfqws(self, runtime_root: Path, bin_dir: Path, nfqws_path: Path, temp_root: Path) -> dict[str, str]:
        arch_dir = _NFQWS_ARCHIVE_DIR
        tag = ""
        archive_url = ""
        release_asset = ""
        try:
            payload = self.github.github_json(
                f"https://api.github.com/repos/{_NFQWS_BOL_VAN_REPO}/releases/latest",
                timeout=25,
                purpose="zapret-nfqws-release-metadata",
            )
            if not isinstance(payload, dict):
                raise ValueError("Invalid nfqws release metadata")
            tag = str(payload.get("tag_name") or "").strip()
            if isinstance(payload.get("assets"), list):
                for asset in payload["assets"]:
                    if isinstance(asset, dict):
                        name = str(asset.get("name") or "")
                        if name.endswith(".tar.gz"):
                            archive_url = str(asset.get("browser_download_url") or "")
                            release_asset = name
                            break
            if not archive_url:
                raise ValueError("No nfqws tar.gz asset found")
        except Exception as error:
            self.logging.log("warning", "Failed to resolve nfqws release metadata", error=str(error))
            return {"ok": False, "error": f"Could not resolve zapret nfqws release: {error}"}

        archive_path = temp_root / (release_asset or "zapret.tar.gz")
        try:
            self._download_to_file(archive_url, archive_path, timeout=150)
            extract_root = temp_root / "zapret"
            extract_root.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive_path, "r:gz") as archive:
                archive.extractall(extract_root)
            candidates = [extract_root]
            candidates.extend(path for path in extract_root.iterdir() if path.is_dir())
            source_nfqws: Path | None = None
            for candidate in candidates:
                probe = candidate / "binaries" / arch_dir / "nfqws"
                if probe.exists() and probe.is_file():
                    source_nfqws = probe
                    break
            if source_nfqws is None:
                for candidate in extract_root.rglob(f"binaries/{arch_dir}/nfqws"):
                    if candidate.is_file():
                        source_nfqws = candidate
                        break
            if source_nfqws is None:
                raise ValueError(f"nfqws binary for {arch_dir} not found in {release_asset or 'archive'}")
            bin_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_nfqws, nfqws_path)
            nfqws_path.chmod(0o755)
            if tag:
                (runtime_root / ".nfqws_version").write_text(tag.lstrip("v"), encoding="utf-8")
            self.logging.log("info", "nfqws installed for zapret-rust", version=tag, arch=arch_dir)
            return {"ok": True, "version": tag}
        except Exception as error:
            self.logging.log("warning", "Failed to install nfqws", error=str(error))
            return {"ok": False, "error": f"Failed to install nfqws: {error}"}

    def _ensure_zapret_strategies(self, runtime_root: Path, strategies_dir: Path, temp_root: Path) -> dict[str, str]:
        zip_path = temp_root / "strategies.zip"
        try:
            self._download_to_file(_STRATEGIES_ARCHIVE_URL, zip_path, timeout=150)
            extract_root = temp_root / "strategies_src"
            extract_root.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as archive:
                archive.extractall(extract_root)
            source: Path | None = None
            for candidate in extract_root.iterdir():
                if candidate.is_dir() and (candidate / "general.bat").exists():
                    source = candidate
                    break
            if source is None:
                for candidate in extract_root.rglob("*"):
                    if candidate.is_dir() and (candidate / "general.bat").exists():
                        source = candidate
                        break
            if source is None:
                raise ValueError("Strategy archive does not contain general.bat")
            if strategies_dir.exists():
                shutil.rmtree(strategies_dir, ignore_errors=True)
            shutil.copytree(source, strategies_dir)
            self.logging.log("info", "Zapret strategies installed", source=str(source.name))
            return {"ok": True}
        except Exception as error:
            self.logging.log("warning", "Failed to install zapret strategies", error=str(error))
            return {"ok": False, "error": f"Failed to install strategies: {error}"}

    def _apply_zapret_dependency_metadata(self, runtime_root: Path) -> None:
        return

    def update_zapret_runtime(self) -> dict[str, str]:
        release = self.fetch_latest_zapret_release()
        latest_version = str(release.get("latest_version", "")).strip()
        current_version = self.storage._detect_zapret_version()
        if latest_version and current_version == latest_version:
            return {"status": "up-to-date", "version": current_version}
        if not IS_WINDOWS:
            asset_url = str(release.get("asset_url", "")).strip()
            asset_name = str(release.get("asset_name", "")).strip()
            if not asset_url:
                return {"status": "error", "error": "No zapret-rust binary URL found"}
            return self._install_zapret_binary(version=latest_version or current_version, asset_url=asset_url, asset_name=asset_name)
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

    def _install_zapret_binary(self, *, version: str, asset_url: str, asset_name: str) -> dict[str, str]:
        current_version = self.storage._detect_zapret_version()
        if version and current_version == version:
            return {"status": "up-to-date", "version": current_version}
        runtime_root = self.storage.paths.runtime_dir / "zapret-discord-youtube-rust"
        binary_path = runtime_root / "zapret-rust"
        was_running = self._is_image_running("nfqws") or self._is_image_running("zapret-rust")
        temp_root = Path(tempfile.mkdtemp(prefix="zapret_zen_zapret_rust_update_"))
        try:
            if was_running:
                self._stop_component("zapret")
            backup = self.storage.create_backup(runtime_root, "pre-update-zapret-rust")
            download_path = temp_root / (asset_name or "zapret-rust")
            self._download_to_file(asset_url, download_path, timeout=150)
            runtime_root.mkdir(parents=True, exist_ok=True)
            if binary_path.exists():
                binary_path.unlink()
            shutil.copy2(download_path, binary_path)
            binary_path.chmod(0o755)
            if version:
                (runtime_root / ".version").write_text(version.lstrip("v"), encoding="utf-8")
            self.storage.ensure_layout()
            self._rebuild_snapshot()
            if was_running:
                self._start_component("zapret")
            self.logging.log("info", "Zapret-rust updated", version=version, backup=str(backup or ""))
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

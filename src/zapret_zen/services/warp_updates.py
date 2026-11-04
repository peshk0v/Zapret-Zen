from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

import certifi
import ssl

from zapret_zen import __version__
from zapret_zen.services.github_network import GitHubNetworkClient
from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.storage import StorageManager
from zapret_zen.services.warp_manager import (
    WARP_CLI_NAME,
    WARP_DIR_NAME,
    WARP_SVC_NAME,
    WARP_VERSION_FILE,
    WarpManager,
)

# ── Источник бинарников Cloudflare WARP ─────────────────────────────────────
# Официальный стабильный (GA) дистрибутив Cloudflare WARP для Windows.
# URL отдаёт MSI-инсталлятор (редирект на версионный файл вида
# Cloudflare-WARP-2026.8.2115.0.msi, ~60 МБ). Файлы извлекаются
# административной установкой `msiexec /a` БЕЗ реальной установки:
# на компьютере пользователя ничего не регистрируется.
WARP_DOWNLOAD_URL = "https://downloads.cloudflareclient.com/v1/download/windows/ga"
WARP_VERSION_URL_TEMPLATE = "https://downloads.cloudflareclient.com/v1/download/windows/version/{version}"

_VERSION_RE = re.compile(r"(\d{4}\.\d+(?:\.\d+){1,3})")

MSI_MIN_BYTES = 5 * 1024 * 1024
DOWNLOAD_TIMEOUT = 600
EXTRACT_TIMEOUT = 300


class WarpUpdateManager:
    """Автообновление бинарников WARP по сценарию RuntimeUpdateManager.

    fetch_latest_warp_release() -> скачать MSI -> msiexec /a (извлечение без
    установки) -> проверка warp-cli/warp-svc -> бэкап -> атомарная замена
    runtime/warp -> version.json -> рестарт компонента.
    """

    def __init__(
        self,
        storage: StorageManager,
        logging: LoggingManager,
        github: GitHubNetworkClient,
        warp: WarpManager,
        stop_component: Callable[[str], Any],
        start_component: Callable[[str], Any],
        list_states: Callable[[], list[Any]],
    ) -> None:
        self.storage = storage
        self.logging = logging
        self.github = github
        self.warp = warp
        self.stop_component = stop_component
        self.start_component = start_component
        self.list_states = list_states
        self._creationflags = 0
        self._startupinfo: subprocess.STARTUPINFO | None = None
        if sys.platform.startswith("win"):
            self._creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            self._startupinfo = startup

    # ── Version / release metadata ───────────────────────────────────────────
    def current_version(self) -> str:
        return self.warp.current_version()

    def fetch_latest_warp_release(self) -> dict[str, str]:
        """Версия и URL последнего GA-релиза Cloudflare WARP для Windows.

        Версия определяется по финальному URL редиректа (имя MSI содержит
        версию). При недоступности метаданных возвращается пустая версия —
        проверка обновлений в этом случае просто не предлагается.
        """
        fallback = {"latest_version": "", "asset_url": WARP_DOWNLOAD_URL, "asset_name": ""}
        try:
            request = Request(
                WARP_DOWNLOAD_URL,
                method="HEAD",
                headers={"User-Agent": f"ZapretZen/{__version__}"},
            )
            final_url = ""
            disposition = ""
            for _label, context in (
                ("system", ssl.create_default_context()),
                ("certifi", ssl.create_default_context(cafile=certifi.where())),
            ):
                try:
                    with urlopen(request, timeout=20, context=context) as response:
                        final_url = str(response.geturl() or "")
                        disposition = str(response.headers.get("Content-Disposition") or "")
                    break
                except Exception:
                    continue
            candidate = final_url or disposition
            match = _VERSION_RE.search(candidate)
            if not match:
                # Нет версии в URL — пробуем версионный endpoint напрямую
                # (га-URL остаётся источником скачивания).
                return fallback
            return {
                "latest_version": match.group(1),
                "asset_url": WARP_DOWNLOAD_URL,
                "asset_name": f"Cloudflare-WARP-{match.group(1)}.msi",
            }
        except Exception:
            return fallback

    def _file_version(self, path: Path) -> str:
        """ProductVersion файла через PowerShell (надёжный источник версии)."""
        if not path.is_file() or not sys.platform.startswith("win"):
            return ""
        script = f"(Get-Item -LiteralPath '{path}').VersionInfo.ProductVersion"
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                timeout=20,
                creationflags=self._creationflags,
                startupinfo=self._startupinfo,
                check=False,
            )
            value = (proc.stdout or b"").decode("utf-8", errors="ignore").strip()
            return value.splitlines()[-1].strip() if value else ""
        except Exception:
            return ""

    # ── Download / extract ───────────────────────────────────────────────────
    def _download_msi(self, url: str, destination: Path, progress_cb: Callable[..., None] | None) -> None:
        def _progress(received: int, total: int, fraction: float | None) -> None:
            if progress_cb is not None:
                progress_cb(received, total, fraction)

        self.github.download_stream(
            url,
            destination,
            connect_timeout=20,
            read_timeout=30,
            total_timeout=DOWNLOAD_TIMEOUT,
            purpose="download:warp-msi",
            min_bytes=MSI_MIN_BYTES,
            progress_cb=_progress,
        )

    def _extract_msi(self, msi_path: Path, target_dir: Path) -> tuple[bool, str]:
        """Административная установка MSI во временный каталог (без установки)."""
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                [
                    "msiexec",
                    "/a",
                    str(msi_path),
                    "/qn",
                    f"TARGETDIR={target_dir}",
                    "/norestart",
                ],
                capture_output=True,
                timeout=EXTRACT_TIMEOUT,
                creationflags=self._creationflags,
                startupinfo=self._startupinfo,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "msiexec extraction timed out"
        except OSError as error:
            return False, str(error)
        code = int(proc.returncode or 0)
        # 0 = ok, 3010 = reboot required (файлы скопированы).
        if code not in (0, 3010):
            stderr = (proc.stderr or b"").decode("utf-8", errors="ignore").strip()
            return False, f"msiexec exited with code {code}. {stderr[:300]}"
        return True, ""

    def _collect_warp_files(self, extract_root: Path, staging: Path) -> tuple[bool, str]:
        """Копирует warp-cli/warp-svc и сопутствующие файлы в staging-каталог."""
        cli = next(extract_root.rglob(WARP_CLI_NAME), None)
        svc = next(extract_root.rglob(WARP_SVC_NAME), None)
        if cli is None or svc is None:
            missing = [name for name, found in ((WARP_CLI_NAME, cli), (WARP_SVC_NAME, svc)) if found is None]
            return False, f"MSI payload is missing: {', '.join(missing)}"
        source_dirs: list[Path] = []
        for exe in (cli, svc):
            parent = exe.parent.resolve()
            if not any(parent == existing or parent in existing.parents for existing in source_dirs):
                source_dirs.append(parent)
        staging.mkdir(parents=True, exist_ok=True)
        for source_dir in source_dirs:
            try:
                for entry in source_dir.iterdir():
                    target = staging / entry.name
                    if entry.is_dir():
                        shutil.copytree(entry, target, dirs_exist_ok=True)
                    else:
                        shutil.copy2(entry, target)
            except OSError as error:
                return False, f"copy failed from {source_dir}: {error}"
        if not (staging / WARP_CLI_NAME).is_file() or not (staging / WARP_SVC_NAME).is_file():
            return False, "staging validation failed: warp-cli/warp-svc missing"
        return True, ""

    def _write_version_manifest(self, warp_dir: Path, version: str) -> None:
        payload = {
            "version": version or "unknown",
            "source": WARP_DOWNLOAD_URL,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            (warp_dir / WARP_VERSION_FILE).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ── Main update flow ─────────────────────────────────────────────────────
    def update_warp_runtime(
        self, progress_cb: Callable[..., None] | None = None
    ) -> dict[str, str]:
        """Скачивает и устанавливает/обновляет бинарники WARP.

        Работает и как install (компонент ещё не установлен), и как update.
        Старая версия остаётся до успешной распаковки новой (атомарная замена).
        """
        if not sys.platform.startswith("win"):
            return {"status": "error", "error": "WARP is supported on Windows only."}
        release = self.fetch_latest_warp_release()
        latest_version = str(release.get("latest_version", "") or "").strip()
        current_version = self.current_version()
        installed = self.warp.is_installed()
        if installed and latest_version and current_version == latest_version:
            return {"status": "up-to-date", "version": current_version}
        asset_url = str(release.get("asset_url") or WARP_DOWNLOAD_URL)

        runtime_root = self.storage.paths.runtime_dir / WARP_DIR_NAME
        was_running = False
        try:
            states = {item.component_id: item for item in self.list_states()}
            was_running = bool(states.get("warp") and states["warp"].status == "running")
        except Exception:
            was_running = False
        if was_running:
            try:
                self.stop_component("warp")
            except Exception as error:
                self.logging.log("warning", "WARP update: stop failed", error=str(error))

        temp_root = Path(tempfile.mkdtemp(prefix="zapret_zen_warp_update_"))
        try:
            msi_path = temp_root / "Cloudflare-WARP.msi"
            try:
                self._download_msi(asset_url, msi_path, progress_cb)
            except Exception as error:
                return {"status": "error", "error": f"WARP download failed: {error}"}

            extract_root = temp_root / "extract"
            ok, err = self._extract_msi(msi_path, extract_root)
            if not ok:
                return {"status": "error", "error": f"WARP extraction failed: {err}"}

            staging = temp_root / "staging"
            ok, err = self._collect_warp_files(extract_root, staging)
            if not ok:
                return {"status": "error", "error": f"WARP payload invalid: {err}"}

            # Версия из ProductVersion нового warp-cli.exe (авторитетный источник).
            new_version = self._file_version(staging / WARP_CLI_NAME)
            if not new_version:
                new_version = latest_version or "unknown"
            if installed and current_version and new_version == current_version:
                return {"status": "up-to-date", "version": current_version}

            # Бэкап старой версии (runtime/warp), затем атомарная замена.
            backup_reason = None
            if runtime_root.exists():
                try:
                    self.storage.create_backup(runtime_root, "pre-update-warp")
                    backup_reason = "pre-update-warp"
                except Exception as error:
                    self.logging.log("warning", "WARP update: backup failed", error=str(error))

            old_dir = runtime_root.with_name(f"{WARP_DIR_NAME}.old_{int(time.time())}")
            try:
                if runtime_root.exists():
                    runtime_root.rename(old_dir)
                # staging живёт в %TEMP% (возможно, другой диск) — move, не rename.
                shutil.move(str(staging), str(runtime_root))
            except OSError as error:
                # Откат: возвращаем старую версию на место.
                try:
                    if old_dir.exists() and not runtime_root.exists():
                        old_dir.rename(runtime_root)
                except OSError:
                    pass
                return {"status": "error", "error": f"WARP swap failed: {error}"}

            if not (runtime_root / WARP_CLI_NAME).is_file():
                try:
                    if old_dir.exists():
                        shutil.rmtree(runtime_root, ignore_errors=True)
                        old_dir.rename(runtime_root)
                except OSError:
                    pass
                return {"status": "error", "error": "WARP post-swap validation failed"}

            self._write_version_manifest(runtime_root, new_version)
            if old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)

            try:
                self.storage.ensure_layout()  # обновляет версию в components.json
            except Exception as error:
                self.logging.log("warning", "WARP update: ensure_layout failed", error=str(error))

            self.logging.log(
                "info",
                "WARP binaries updated",
                version=new_version,
                backup=backup_reason or "none",
            )
            if was_running:
                try:
                    self.start_component("warp")
                except Exception as error:
                    self.logging.log("warning", "WARP update: restart failed", error=str(error))
            return {"status": "updated", "version": new_version}
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

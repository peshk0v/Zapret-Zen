from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

from zapret_zen.domain import ComponentState
from zapret_zen.runtime_env import is_packaged_runtime
from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.settings import SettingsManager
from zapret_zen.services.storage import StorageManager


class TelegramProxyManager:
    """Manages tg-ws-proxy lifecycle: start, stop, link generation."""

    def __init__(
        self,
        storage: StorageManager,
        logging: LoggingManager,
        settings: SettingsManager,
    ) -> None:
        self.storage = storage
        self.logging = logging
        self.settings = settings
        self._creationflags = 0
        self._startupinfo: subprocess.STARTUPINFO | None = None
        if sys.platform.startswith("win"):
            self._creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            self._startupinfo = startup

    def parse_dc_ip_settings(self, value: str) -> list[str]:
        import re
        result: list[str] = []
        for raw in re.split(r"[\n,;]+", str(value or "")):
            item = raw.strip()
            if item:
                result.append(item)
        if not result:
            return ["__empty__"]
        return result

    def build_worker_command(self, worker: str, install_root: Path, **kwargs: Any) -> list[str]:
        if is_packaged_runtime():
            cmd = [sys.executable, "--worker", worker]
        else:
            cmd = [self._worker_python_executable(), "-m", "zapret_zen.worker_entry", "--worker", worker]
        for key, value in kwargs.items():
            option = "--" + key.replace("_", "-")
            if isinstance(value, (list, tuple)):
                for item in value:
                    cmd.extend([option, str(item)])
                continue
            cmd.extend([option, str(value)])
        return cmd

    def build_worker_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if not is_packaged_runtime():
            src_root = str(self.storage.paths.install_root / "src")
            current = str(env.get("PYTHONPATH", "") or "")
            parts = [item for item in current.split(os.pathsep) if item]
            if src_root not in parts:
                parts.insert(0, src_root)
            env["PYTHONPATH"] = os.pathsep.join(parts)
        return env

    def _worker_python_executable(self) -> str:
        if is_packaged_runtime():
            return sys.executable
        install_root = self.storage.paths.install_root
        candidates = [
            install_root / ".venv" / "Scripts" / "pythonw.exe",
            install_root / ".venv" / "Scripts" / "python.exe",
            install_root / ".venv" / "bin" / "python",
            Path(sys.executable),
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return sys.executable

    def ensure_secret(self) -> str:
        settings = self.settings.get()
        secret = (settings.tg_proxy_secret or "").strip().lower()
        if secret.startswith("dd") and len(secret) > 2:
            secret = secret[2:]
        if not secret:
            secret = secrets.token_hex(16)
        if secret != settings.tg_proxy_secret:
            self.settings.update(tg_proxy_secret=secret)
        return secret

    def prompt_proxy_link(self) -> str:
        settings = self.settings.get()
        secret = self.ensure_secret()
        return self.build_proxy_link(
            host=settings.tg_proxy_host,
            port=int(settings.tg_proxy_port),
            secret=secret,
        )

    def build_proxy_link(self, *, host: str, port: int, secret: str) -> str:
        """Return the tg:// proxy deep link without launching a browser or Telegram.

        Callers are expected to copy this to the clipboard.  Never uses ``webbrowser``/
        ``os.startfile`` (which previously froze the UI and opened an empty browser).
        """
        return (
            f"tg://proxy?server={host}"
            f"&port={int(port)}&secret=dd{secret}"
        )

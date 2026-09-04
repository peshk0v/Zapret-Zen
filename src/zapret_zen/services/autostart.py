from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess

from zapret_zen.platform_utils import IS_WINDOWS
from zapret_zen.services.logging_service import LoggingManager

if IS_WINDOWS:
    import winreg


class AutostartManager:
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    APP_NAME = "ZapretZen"
    TASK_NAME = "ZapretZen"
    LEGACY_APP_NAMES = ("ZapretHub", "Zapret-Zen")

    def __init__(self, logging: LoggingManager) -> None:
        self.logging = logging

    def is_enabled(self) -> bool:
        if not IS_WINDOWS:
            return self._xdg_autostart_exists()
        return self._task_exists() or self._run_entry_exists()

    def set_enabled(self, enabled: bool) -> bool:
        if not IS_WINDOWS:
            return self._set_xdg_autostart(enabled)
        command = self._build_command()
        self._remove_legacy_run_entries()
        self._delete_task()
        result = False
        if enabled:
            result = self._create_task(command)
            if not result:
                result = self._set_run_entry(command)
        else:
            result = self.is_enabled()
        self.logging.log("info", "Windows autostart changed", enabled=enabled, actual=result, command=command if enabled else "")
        return result

    def ensure_runs_elevated(self) -> None:
        if not IS_WINDOWS:
            return
        if not self._task_exists():
            if not self._run_entry_exists():
                return
            command = self._build_command()
            if self._create_task(command):
                self._remove_legacy_run_entries()
                self.logging.log("info", "Windows autostart migrated from Run entry to scheduled task")
            return
        self._create_task(self._build_command())

    # ── Linux XDG autostart ────────────────────────────────────────────
    #
    # User-level autostart on Linux/BSD: a freedesktop .desktop launcher in
    # $XDG_CONFIG_HOME/autostart (default ~/.config/autostart).  This is pure
    # user-space and never requires root/sudo; the directory is created if
    # missing and the file is written with the current user's permissions.

    @staticmethod
    def _xdg_autostart_dir() -> Path:
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(xdg) / "autostart"

    def _xdg_autostart_path(self) -> Path:
        return self._xdg_autostart_dir() / "zapret-zen.desktop"

    def _xdg_autostart_exists(self) -> bool:
        return self._xdg_autostart_path().exists()

    def _xdg_exec_line(self) -> str:
        """Build the Exec= line for the current runtime (frozen or source).

        Uses a quoted, absolute path so launching works regardless of the
        caller's PATH and survives a shell that reads the .desktop later.
        """
        executable = Path(sys.executable)
        is_source = executable.suffix.lower() == ".exe" or executable.name in (
            "python",
            "python3",
            "zapret-zen",
        ) or executable.is_dir()
        if executable.suffix.lower() == ".exe" and executable.name.lower() not in (
            "python.exe",
            "python3.exe",
        ):
            # Frozen Windows-style binary: exec the binary directly.
            return f'"{executable}" --autostart-launch'
        if is_source and executable.is_dir():
            # A frozen AppDir/self-extracting dir: launch the entrypoint inside.
            main_module = Path(__file__).resolve().parents[1] / "main.py"
            if main_module.exists():
                return f'"{executable / "zapret-zen"}" --autostart-launch'
        if is_source:
            main_module = Path(__file__).resolve().parents[1] / "main.py"
            return f'"{sys.executable}" "{main_module}" --autostart-launch'
        return f'"{sys.executable}" --autostart-launch'

    def _set_xdg_autostart(self, enabled: bool) -> bool:
        target = self._xdg_autostart_path()
        if not enabled:
            try:
                target.unlink(missing_ok=True)
            except OSError as error:
                self.logging.log("warning", "Failed to remove autostart entry", path=str(target), error=str(error))
                return target.exists()
            return not target.exists()
        try:
            # User-level dir; exists_ok makes repeated toggles a no-op.
            os.makedirs(target.parent, mode=0o755, exist_ok=True)
            icon_name = "zapret-zen"
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Zapret-Zen\n"
                "Name[ru]=Запрет-Зен\n"
                "Comment=Zapret-Zen autostart\n"
                "Comment[ru]=Автозапуск Zapret-Zen\n"
                f"Exec={self._xdg_exec_line()}\n"
                f"Icon={icon_name}\n"
                "Terminal=false\n"
                "Hidden=false\n"
                "NoDisplay=false\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
            target.write_text(content, encoding="utf-8")
            os.chmod(target, 0o644)
        except OSError as error:
            self.logging.log(
                "warning",
                "Failed to write autostart entry",
                path=str(target),
                error=str(error),
            )
            return False
        ok = target.exists()
        if ok:
            self.logging.log("info", "Linux autostart entry written", path=str(target))
        return ok

    # ── Windows schtasks / registry ────────────────────────────────────

    def _build_command(self) -> str:
        executable = Path(sys.executable)
        if executable.suffix.lower() == ".exe" and executable.name.lower() != "python.exe":
            return f'"{executable}" --autostart-launch'
        main_module = Path(__file__).resolve().parents[1] / "main.py"
        return f'"{executable}" "{main_module}" --autostart-launch'

    def _task_exists(self) -> bool:
        proc = self._run_schtasks(["/Query", "/TN", self.TASK_NAME])
        return proc.returncode == 0

    def _run_entry_exists(self) -> bool:
        if not IS_WINDOWS:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY, 0, winreg.KEY_READ) as key:
                for name in (self.APP_NAME, *self.LEGACY_APP_NAMES):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except FileNotFoundError:
                        continue
                    if str(value or "").strip():
                        return True
        except FileNotFoundError:
            return False
        return False

    def _create_task(self, command: str) -> bool:
        proc = self._run_schtasks(
            [
                "/Create",
                "/F",
                "/SC",
                "ONLOGON",
                "/TN",
                self.TASK_NAME,
                "/TR",
                command,
                "/RL",
                "HIGHEST",
                "/IT",
            ]
        )
        if proc.returncode != 0:
            self.logging.log("warning", "Failed to create autostart task", error=(proc.stderr or proc.stdout or "").strip())
            return False
        return True

    def _delete_task(self) -> None:
        proc = self._run_schtasks(["/Delete", "/F", "/TN", self.TASK_NAME])
        if proc.returncode != 0:
            self.logging.log("warning", "Failed to delete autostart task (may require admin)", error=(proc.stderr or proc.stdout or "").strip())

    def _run_schtasks(self, args: list[str]) -> CompletedProcess[str]:
        proc = subprocess.run(
            ["schtasks", *args],
            capture_output=True,
            text=False,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return CompletedProcess(
            proc.args,
            proc.returncode,
            self._decode_process_output(proc.stdout),
            self._decode_process_output(proc.stderr),
        )

    @staticmethod
    def _decode_process_output(output: bytes | None) -> str:
        if not output:
            return ""
        for encoding in ("utf-8-sig", "cp866", "cp1251", "mbcs"):
            try:
                return output.decode(encoding)
            except UnicodeDecodeError:
                continue
            except LookupError:
                continue
        return output.decode("utf-8", errors="replace")

    def _set_run_entry(self, command: str) -> bool:
        if not IS_WINDOWS:
            return False
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, self.APP_NAME, 0, winreg.REG_SZ, command)
            return self._run_entry_exists()
        except OSError as error:
            self.logging.log("warning", "Failed to create autostart Run entry", error=str(error))
            return False

    def _remove_legacy_run_entries(self) -> None:
        if not IS_WINDOWS:
            return
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                for name in (self.APP_NAME, *self.LEGACY_APP_NAMES):
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            return

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from typing import Mapping

from zapret_zen.platform_utils import IS_LINUX
from zapret_zen.services.logging_service import LoggingManager


class SystemdServiceManager:
    """Manage zapret-zen's background components as systemd *user* services.

    On Linux each autostart-able component (Zapret, TG WS Proxy) is owned by a
    ``systemctl --user`` unit under ``~/.config/systemd/user/``.  The desktop app
    delegates component start/stop/autostart to these units instead of holding
    a bare ``subprocess.Popen``, so components survive as independent daemons on
    login.  Every operation is best-effort: callers keep their existing
    subprocess fallback when systemd is unavailable or a command fails.
    """

    UNIT_PREFIX = "zapret-zen"

    def __init__(self, logging: LoggingManager) -> None:
        self.logging = logging
        self._available: bool | None = None

    # ── availability ──────────────────────────────────────────────────

    def available(self) -> bool:
        """Return True when the per-user systemd manager can be driven."""
        if not IS_LINUX:
            return False
        if self._available is not None:
            return self._available
        runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
        ok = bool(runtime and Path(runtime).is_dir())
        if ok and not Path(runtime, "systemd", "private").exists():
            probe = self._systemctl(["--no-pager", "--plain", "list-units", "--type=service", "-q", "--all"])
            ok = probe.returncode == 0
        self._available = ok
        return ok

    # ── unit names / paths ────────────────────────────────────────────

    @staticmethod
    def unit_name(component_id: str) -> str:
        safe = component_id.replace("/", "-").replace(".", "-")
        return f"{SystemdServiceManager.UNIT_PREFIX}-{safe}.service"

    @classmethod
    def unit_dir(cls) -> Path:
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(xdg) / "systemd" / "user"

    @classmethod
    def unit_path(cls, component_id: str) -> Path:
        return cls.unit_dir() / cls.unit_name(component_id)

    def unit_exists(self, component_id: str) -> bool:
        return self.unit_path(component_id).exists()

    # ── writing units ─────────────────────────────────────────────────

    def write_unit(
        self,
        component_id: str,
        *,
        description: str,
        command: list[str],
        working_directory: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
        stdout_path: str | Path | None = None,
    ) -> bool:
        target = self.unit_path(component_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        exec_line = self._format_exec(command)
        lines = [
            "[Unit]",
            f"Description={description}",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={exec_line}",
        ]
        if working_directory:
            lines.append(f"WorkingDirectory={shlex.quote(str(working_directory))}")
        if environment:
            for key in sorted(environment):
                value = str(environment[key])
                lines.append(f'Environment="{key}={value}"')
        # Write raw (unquoted) append target; systemd path accepts a plain path.
        if stdout_path:
            lines.append(f"StandardOutput=append:{stdout_path}")
            lines.append(f"StandardError=append:{stdout_path}")
        lines.append("Restart=no")
        lines.append("RemainAfterExit=no")
        lines.append("")
        lines.append("[Install]")
        lines.append("WantedBy=default.target")
        lines.append("")
        try:
            target.write_text("\n".join(lines), encoding="utf-8")
        except OSError as error:
            self.logging.log("warning", "Failed to write systemd unit", unit=target.name, error=str(error))
            return False
        self._daemon_reload()
        return target.exists()

    def remove_unit(self, component_id: str) -> bool:
        target = self.unit_path(component_id)
        if not target.exists():
            return True
        try:
            target.unlink(missing_ok=True)
        except OSError as error:
            self.logging.log("warning", "Failed to remove systemd unit", unit=target.name, error=str(error))
            return False
        self._daemon_reload()
        return not target.exists()

    @staticmethod
    def _format_exec(command: list[str]) -> str:
        if not command:
            return "/bin/true"
        return " ".join(shlex.quote(part) for part in command)

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self, component_id: str) -> bool:
        return self._systemctl(["start", self.unit_name(component_id)]).returncode == 0

    def stop(self, component_id: str) -> bool:
        unit = self.unit_name(component_id)
        proc = self._systemctl(["stop", unit])
        if proc.returncode != 0 and not self.is_active(component_id):
            return True
        return proc.returncode == 0 or not self.is_active(component_id)

    def is_active(self, component_id: str) -> bool:
        proc = self._systemctl(["is-active", "--quiet", self.unit_name(component_id)])
        return proc.returncode == 0

    def main_pid(self, component_id: str) -> int:
        proc = self._systemctl(["show", "--value", "--property=MainPID", self.unit_name(component_id)])
        pid = proc.stdout.strip()
        return int(pid) if pid.isdigit() else 0

    # ── autostart (enable / disable at login) ─────────────────────────

    def enable(self, component_id: str) -> bool:
        return self._systemctl(["enable", "--now", self.unit_name(component_id)]).returncode == 0

    def disable(self, component_id: str) -> bool:
        return self._systemctl(["disable", self.unit_name(component_id)]).returncode == 0

    def is_enabled(self, component_id: str) -> bool:
        proc = self._systemctl(["is-enabled", self.unit_name(component_id)])
        return proc.returncode == 0

    # ── low level ─────────────────────────────────────────────────────

    def _daemon_reload(self) -> bool:
        return self._systemctl(["daemon-reload"]).returncode == 0

    def _systemctl(self, args: list[str]) -> CompletedProcess[str]:
        try:
            proc = subprocess.run(
                ["systemctl", "--user", *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
                start_new_session=True,
            )
        except OSError as error:
            self.logging.log("warning", "systemctl unavailable", error=str(error))
            return CompletedProcess(["systemctl", "--user", *args], 1, "", str(error))
        except subprocess.TimeoutExpired:
            self.logging.log("warning", "systemctl timed out", args=" ".join(args))
            return CompletedProcess(["systemctl", "--user", *args], 1, "", "timeout")
        return CompletedProcess(proc.args, proc.returncode, proc.stdout or "", proc.stderr or "")

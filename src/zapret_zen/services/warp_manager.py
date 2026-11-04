from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from zapret_zen.domain import ComponentState
from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.process_compat import decode_process_output
from zapret_zen.services.settings import SettingsManager
from zapret_zen.services.storage import StorageManager

# ── Конфигурация компонента ─────────────────────────────────────────────────
# Бинарники живут в runtime/warp/ (относительно runtime-каталога приложения).
# Источник: официальный дистрибутив Cloudflare WARP для Windows.
# См. warp_updates.WARP_DOWNLOAD_URL для URL обновления.
WARP_DIR_NAME = "warp"
WARP_CLI_NAME = "warp-cli.exe"
WARP_SVC_NAME = "warp-svc.exe"
WARP_LOG_NAME = "warp.log"
WARP_VERSION_FILE = "version.json"

# Приватная служба Windows, устанавливаемая ИЗ папки приложения
# (binPath указывает на runtime/warp/warp-svc.exe). Не используется
# системный "CloudflareWARP" из чужой установки Cloudflare One Client.
WARP_SERVICE_NAME = "ZapretZenWarp"
# Стандартная служба официального клиента — только для обнаружения,
# никогда не останавливается и не удаляется нами.
WARP_SYSTEM_SERVICE_NAME = "CloudflareWARP"

# Таймауты (секунды)
CLI_TIMEOUT_DEFAULT = 10.0
CLI_TIMEOUT_REGISTRATION = 30.0
CONNECT_TIMEOUT = 12.0
SVC_PROBE_TIMEOUT = 6.0

# Канонические статусы warp-cli
WARP_STATUS_CONNECTED = "connected"
WARP_STATUS_CONNECTING = "connecting"
WARP_STATUS_RECONNECTING = "reconnecting"
WARP_STATUS_DISCONNECTED = "disconnected"
WARP_STATUS_REGISTRATION_MISSING = "registration_missing"
WARP_STATUS_ERROR = "error"
WARP_STATUS_UNKNOWN = "unknown"


def parse_warp_status(text: str) -> str:
    """Устойчивый парсинг вывода ``warp-cli status``.

    Порядок проверок важен: "disconnected" содержит "connected",
    "reconnecting" — "connecting".
    """
    lowered = str(text or "").lower()
    if not lowered.strip():
        return WARP_STATUS_UNKNOWN
    if "registration missing" in lowered or "no registration" in lowered:
        return WARP_STATUS_REGISTRATION_MISSING
    if "reconnecting" in lowered:
        return WARP_STATUS_RECONNECTING
    if "disconnected" in lowered:
        return WARP_STATUS_DISCONNECTED
    if "connecting" in lowered:
        return WARP_STATUS_CONNECTING
    if "connected" in lowered:
        return WARP_STATUS_CONNECTED
    if any(
        marker in lowered
        for marker in (
            "unable to connect",
            "failed to connect",
            "cannot connect",
            "daemon",
            "error",
            "failed",
            "not found",
        )
    ):
        return WARP_STATUS_ERROR
    return WARP_STATUS_UNKNOWN


def parse_warp_settings(text: str) -> dict[str, Any]:
    """Парсинг ``warp-cli settings``: protocol и masque h2-only."""
    lowered = str(text or "").lower()
    protocol = ""
    for line in lowered.splitlines():
        if "protocol" not in line:
            continue
        if "masque" in line:
            protocol = "masque"
            break
        if "wireguard" in line:
            protocol = "wireguard"
            break
    h2_only: bool | None = None
    for line in lowered.splitlines():
        if "h2-only" not in line and "h2 only" not in line:
            continue
        # Строка вида "h2-only: true" / "h2-only = false" / "h2-only: enabled".
        tail = re.split(r"[:=]", line, maxsplit=1)
        value = tail[1].strip() if len(tail) > 1 else ""
        if value in ("false", "no", "off", "0", "disabled"):
            h2_only = False
        elif value in ("true", "yes", "on", "1", "enabled"):
            h2_only = True
        else:
            # Маркер присутствует без явного значения — считаем включённым.
            h2_only = True
        break
    if h2_only is None and protocol == "masque":
        # Явного маркера отключения нет — считаем выключенным, только если
        # секция masque-options вообще упоминается.
        if "masque" in lowered and "option" in lowered:
            h2_only = False
    return {"protocol": protocol, "h2_only": h2_only}


class WarpManager:
    """Единый wrapper вокруг warp-cli/warp-svc из папки компонента.

    Все вызовы warp-cli идут только через ``_run_cli``: таймауты, коды
    возврата, парсинг stdout/stderr, запись в logs/warp.log и общий лог.
    """

    def __init__(
        self,
        storage: StorageManager,
        logging: LoggingManager,
        settings: SettingsManager,
    ) -> None:
        self.storage = storage
        self.logging = logging
        self.settings = settings
        self._svc_process: subprocess.Popen[bytes] | None = None
        self._svc_mode = ""  # "process" | "service" | ""
        self._status_cache: tuple[float, str] = (0.0, "")
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

    # ── Paths ────────────────────────────────────────────────────────────────
    @property
    def warp_dir(self) -> Path:
        return self.storage.paths.runtime_dir / WARP_DIR_NAME

    @property
    def cli_path(self) -> Path:
        return self.warp_dir / WARP_CLI_NAME

    @property
    def svc_path(self) -> Path:
        return self.warp_dir / WARP_SVC_NAME

    @property
    def log_path(self) -> Path:
        return self.storage.paths.logs_dir / WARP_LOG_NAME

    # ── Logging ──────────────────────────────────────────────────────────────
    def _log(self, message: str, *, level: str = "info", app_log: bool = False) -> None:
        stamp = datetime.utcnow().isoformat()
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8", errors="ignore") as handle:
                handle.write(f"[{stamp}] {level.upper()}: {message}\n")
        except Exception:
            pass
        if app_log:
            try:
                self.logging.log(level, f"WARP: {message}", component_id="warp")
            except Exception:
                pass

    def logs(self, limit: int = 250) -> list[str]:
        """Последние строки logs/warp.log (метод ``logs()`` компонента)."""
        try:
            if not self.log_path.exists():
                return []
            lines = [
                line
                for line in self.log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip()
            ]
            return lines[-limit:]
        except Exception:
            return []

    # ── Helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def is_admin() -> bool:
        if not sys.platform.startswith("win"):
            return False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def is_installed(self) -> bool:
        try:
            return self.cli_path.is_file() and self.svc_path.is_file()
        except Exception:
            return False

    def current_version(self) -> str:
        """Версия бинарников из манифеста runtime/warp/version.json."""
        path = self.warp_dir / WARP_VERSION_FILE
        try:
            if not path.is_file():
                return "unknown"
            import json

            payload = json.loads(path.read_text(encoding="utf-8"))
            return str(payload.get("version") or "unknown")
        except Exception:
            return "unknown"

    def _run_quiet(self, cmd: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=self._creationflags,
            startupinfo=self._startupinfo,
            check=False,
        )

    def _run_cli(self, args: list[str], *, timeout: float = CLI_TIMEOUT_DEFAULT) -> tuple[int, str, str]:
        """Запуск warp-cli с таймаутом; возвращает (code, stdout, stderr)."""
        if not self.cli_path.is_file():
            self._log(f"warp-cli not found: {self.cli_path}", level="error")
            return 127, "", "warp-cli.exe not found"
        cmd = [str(self.cli_path), "--accept-tos", *args]
        self._log("$ warp-cli --accept-tos " + " ".join(args))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                cwd=str(self.warp_dir),
                timeout=timeout,
                creationflags=self._creationflags,
                startupinfo=self._startupinfo,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._log(f"warp-cli timed out after {timeout}s: {' '.join(args)}", level="warning")
            return 124, "", f"timeout after {timeout}s"
        except OSError as error:
            self._log(f"warp-cli failed to spawn: {error}", level="error")
            return 126, "", str(error)
        stdout = decode_process_output(proc.stdout)
        stderr = decode_process_output(proc.stderr)
        combined = (stdout + ("\n" if stdout and stderr else "") + stderr).strip()
        if combined:
            for line in combined.splitlines():
                if line.strip():
                    self._log(f"  {line.strip()}")
        if proc.returncode not in (0, None):
            self._log(f"  exit code {proc.returncode}", level="warning")
        return int(proc.returncode or 0), stdout, stderr

    # ── Status ───────────────────────────────────────────────────────────────
    def status(self, *, fresh: bool = False) -> str:
        """Один из канонических статусов warp-cli (устойчивый парсинг)."""
        now = time.monotonic()
        if not fresh and self._status_cache[1] and (now - self._status_cache[0]) < 0.5:
            return self._status_cache[1]
        if not self.is_installed():
            result = WARP_STATUS_UNKNOWN
        else:
            _code, stdout, stderr = self._run_cli(["status"], timeout=CLI_TIMEOUT_DEFAULT)
            result = parse_warp_status(stdout or stderr)
        self._status_cache = (now, result)
        return result

    def status_text(self) -> str:
        """Человекочитаемый статус для UI (Connected / Registration missing...)."""
        mapping = {
            WARP_STATUS_CONNECTED: "Connected",
            WARP_STATUS_CONNECTING: "Connecting",
            WARP_STATUS_RECONNECTING: "Reconnecting",
            WARP_STATUS_DISCONNECTED: "Disconnected",
            WARP_STATUS_REGISTRATION_MISSING: "Registration missing",
            WARP_STATUS_ERROR: "Error",
            WARP_STATUS_UNKNOWN: "Unknown",
        }
        if not self.is_installed():
            return "Not installed"
        return mapping.get(self.status(), "Unknown")

    def current_state(self) -> ComponentState:
        """Состояние компонента для ComponentState (status + detail)."""
        if not self.is_installed():
            return ComponentState(
                component_id="warp",
                status="error",
                detail="Not installed",
                last_error="WARP binaries are not installed. Press Install.",
            )
        status = self.status()
        if status == WARP_STATUS_CONNECTED:
            return ComponentState(component_id="warp", status="running", detail="Connected")
        if status in (WARP_STATUS_CONNECTING, WARP_STATUS_RECONNECTING):
            return ComponentState(
                component_id="warp",
                status="running",
                detail="Connecting" if status == WARP_STATUS_CONNECTING else "Reconnecting",
            )
        if status == WARP_STATUS_DISCONNECTED:
            return ComponentState(component_id="warp", status="stopped", detail="Disconnected")
        if status == WARP_STATUS_REGISTRATION_MISSING:
            return ComponentState(
                component_id="warp",
                status="error",
                detail="Registration missing",
                last_error="WARP registration is missing. Press 'Reinstall registration'.",
            )
        if status == WARP_STATUS_ERROR:
            return ComponentState(component_id="warp", status="error", detail="Error")
        return ComponentState(component_id="warp", status="stopped", detail="Unknown")

    def wait_for_status(self, target: str, timeout: float = CONNECT_TIMEOUT) -> bool:
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if self.status(fresh=True) == target:
                return True
            time.sleep(0.7)
        return self.status(fresh=True) == target

    # ── Service (warp-svc) management ────────────────────────────────────────
    def _sc(self, *args: str, timeout: float = 10.0) -> tuple[int, str]:
        try:
            proc = self._run_quiet(["sc.exe", *args], timeout=timeout)
        except Exception:
            return 1, ""
        return int(proc.returncode or 0), decode_process_output(proc.stdout)

    def _service_state(self, name: str) -> str:
        code, output = self._sc("query", name)
        if code != 0:
            return ""
        match = re.search(r"STATE\s*:\s*\d+\s+(\w+)", output)
        return match.group(1).upper() if match else ""

    def _daemon_reachable(self) -> bool:
        return self.status(fresh=True) not in (WARP_STATUS_ERROR, WARP_STATUS_UNKNOWN)

    def ensure_svc_running(self) -> bool:
        """Поднимает демона warp-svc из папки компонента.

        Сначала пробуем чистый фоновый процесс (без служб), затем — приватную
        службу Windows из папки приложения (fallback). Порядок и результат
        каждого шага пишутся в лог.
        """
        if not self.is_installed():
            self._log("ensure_svc: binaries missing", level="error", app_log=True)
            return False
        # Демон уже отвечает (например, системная служба CloudflareWARP) — не трогаем.
        if self._daemon_reachable():
            self._log("ensure_svc: daemon already reachable")
            self._svc_mode = self._svc_mode or "external"
            return True

        # 1) Фоновый процесс из папки компонента.
        if self._svc_process is None or self._svc_process.poll() is not None:
            self._log(f"ensure_svc: spawning background process {self.svc_path}", app_log=True)
            try:
                self._svc_process = subprocess.Popen(
                    [str(self.svc_path)],
                    cwd=str(self.warp_dir),
                    creationflags=self._creationflags,
                    startupinfo=self._startupinfo,
                )
            except OSError as error:
                self._log(f"ensure_svc: process spawn failed: {error}", level="warning", app_log=True)
                self._svc_process = None
        if self._svc_process is not None and self._svc_process.poll() is None:
            deadline = time.monotonic() + SVC_PROBE_TIMEOUT
            while time.monotonic() < deadline:
                if self._daemon_reachable():
                    self._svc_mode = "process"
                    self._log("ensure_svc: background process is serving warp-cli", app_log=True)
                    return True
                time.sleep(0.6)
            self._log("ensure_svc: background process did not answer, falling back to private service", level="warning", app_log=True)
            try:
                self._svc_process.terminate()
                self._svc_process.wait(timeout=3)
            except Exception:
                try:
                    self._svc_process.kill()
                except Exception:
                    pass
            self._svc_process = None

        # 2) Fallback: приватная служба Windows из папки приложения.
        state = self._service_state(WARP_SERVICE_NAME)
        if state in ("", "STOPPED"):
            bin_path = f'"{self.svc_path}"'
            self._log(f"ensure_svc: creating private service {WARP_SERVICE_NAME} -> {self.svc_path}", app_log=True)
            code, output = self._sc("create", WARP_SERVICE_NAME, f"binPath={bin_path}", "start=demand")
            if code != 0 and "1072" not in output and "1073" not in output:
                self._log(f"ensure_svc: sc create failed: {output.strip()}", level="error", app_log=True)
                return False
        code, output = self._sc("start", WARP_SERVICE_NAME)
        if code != 0 and "1056" not in output:
            self._log(f"ensure_svc: sc start failed: {output.strip()}", level="warning", app_log=True)
        deadline = time.monotonic() + SVC_PROBE_TIMEOUT
        while time.monotonic() < deadline:
            if self._daemon_reachable():
                self._svc_mode = "service"
                self._log("ensure_svc: private service is serving warp-cli", app_log=True)
                return True
            time.sleep(0.6)
        self._log("ensure_svc: daemon is not reachable after all attempts", level="error", app_log=True)
        return False

    def stop_svc(self) -> None:
        """Останавливает демона (процесс или приватную службу)."""
        if self._svc_process is not None and self._svc_process.poll() is None:
            self._log("stop_svc: terminating background process", app_log=True)
            try:
                self._svc_process.terminate()
                self._svc_process.wait(timeout=4)
            except Exception:
                try:
                    self._svc_process.kill()
                    self._svc_process.wait(timeout=3)
                except Exception:
                    pass
        self._svc_process = None
        if self._service_state(WARP_SERVICE_NAME) in ("RUNNING", "START_PENDING"):
            self._log(f"stop_svc: stopping private service {WARP_SERVICE_NAME}", app_log=True)
            self._sc("stop", WARP_SERVICE_NAME)
        self._svc_mode = ""
        self._status_cache = (0.0, "")

    # ── warp-cli operations ──────────────────────────────────────────────────
    def connect(self, timeout: float = CLI_TIMEOUT_DEFAULT) -> bool:
        code, _out, _err = self._run_cli(["connect"], timeout=timeout)
        return code == 0

    def disconnect(self, timeout: float = CLI_TIMEOUT_DEFAULT) -> bool:
        code, _out, _err = self._run_cli(["disconnect"], timeout=timeout)
        return code == 0

    def registration_show(self) -> tuple[bool, str]:
        code, stdout, stderr = self._run_cli(["registration", "show"], timeout=CLI_TIMEOUT_DEFAULT)
        return code == 0, (stdout or stderr)

    def registration_new(self) -> bool:
        code, _out, _err = self._run_cli(
            ["registration", "new"], timeout=CLI_TIMEOUT_REGISTRATION
        )
        return code == 0

    def registration_delete(self) -> bool:
        code, _out, _err = self._run_cli(
            ["registration", "delete"], timeout=CLI_TIMEOUT_REGISTRATION
        )
        return code == 0

    def set_protocol(self, protocol: str) -> bool:
        """tunnel protocol set MASQUE|WireGuard (сохраняет выбор в настройки)."""
        value = "MASQUE" if str(protocol or "").lower() == "masque" else "WireGuard"
        code, _out, _err = self._run_cli(["tunnel", "protocol", "set", value])
        if code == 0:
            self.settings.update(warp_protocol=value.lower())
            self._log(f"set_protocol: {value}", app_log=True)
            self._status_cache = (0.0, "")
        return code == 0

    def set_masque_h2_only(self, enabled: bool) -> bool:
        args = ["tunnel", "masque-options", "set" if enabled else "unset", "h2-only"]
        code, _out, _err = self._run_cli(args)
        if code == 0:
            self.settings.update(warp_masque_h2_only=bool(enabled))
            self._log(f"set_masque_h2_only: {bool(enabled)}", app_log=True)
        return code == 0

    def read_settings(self) -> dict[str, Any]:
        _code, stdout, _err = self._run_cli(["settings"], timeout=CLI_TIMEOUT_DEFAULT)
        return parse_warp_settings(stdout)

    def apply_desired_settings(self) -> None:
        """Применяет protocol/h2-only из настроек приложения к демону."""
        current = self.settings.get()
        desired_protocol = str(current.warp_protocol or "masque").lower()
        parsed = self.read_settings()
        live_protocol = str(parsed.get("protocol") or "").lower()
        if live_protocol and live_protocol != desired_protocol:
            self.set_protocol(desired_protocol)
        if desired_protocol == "masque":
            desired_h2 = bool(current.warp_masque_h2_only)
            live_h2 = parsed.get("h2_only")
            if live_h2 is None or bool(live_h2) != desired_h2:
                self.set_masque_h2_only(desired_h2)

    # ── Auto-fix (один раз при первой неудаче) ───────────────────────────────
    def auto_fix_sequence(self, *, manual: bool = False) -> dict[str, Any]:
        """disconnect → registration delete → new → MASQUE → h2-only → connect.

        Каждый шаг логируется; после фикса ждём Connected. При успешном
        автофиксе в настройках выставляются MASQUE + h2-only и флаг
        auto_fix_applied (повторный автофикс не выполняется).
        """
        label = "manual reinstall registration" if manual else "auto-fix"
        self._log(f"{label}: started", app_log=True)
        steps: list[dict[str, Any]] = []

        def step(name: str, fn: Callable[[], Any]) -> Any:
            self._log(f"{label} step: {name}")
            try:
                result = fn()
            except Exception as error:
                self._log(f"{label} step '{name}' failed: {error}", level="error")
                steps.append({"step": name, "ok": False, "error": str(error)})
                return None
            ok = bool(result)
            steps.append({"step": name, "ok": ok})
            self._log(f"{label} step '{name}': {'ok' if ok else 'failed'}")
            return result

        step("disconnect", self.disconnect)
        step("registration delete", self.registration_delete)
        step("registration new", self.registration_new)
        step("tunnel protocol set MASQUE", lambda: self.set_protocol("masque"))
        step("masque-options set h2-only", lambda: self.set_masque_h2_only(True))
        step("connect", self.connect)

        connected = self.wait_for_status(WARP_STATUS_CONNECTED, CONNECT_TIMEOUT)
        self._log(
            f"{label}: {'connected' if connected else 'still not connected'}",
            level="info" if connected else "error",
            app_log=True,
        )
        try:
            self.settings.update(
                warp_auto_fix_applied=True,
                warp_protocol="masque",
                warp_masque_h2_only=True,
            )
        except Exception:
            pass
        return {
            "status": "connected" if connected else "failed",
            "manual": manual,
            "steps": steps,
        }

    # ── Lifecycle: install / start / stop / uninstall ────────────────────────
    def start(self) -> ComponentState:
        """Старт компонента: служба → настройки → connect (+ автофикс)."""
        if not sys.platform.startswith("win"):
            return ComponentState(
                component_id="warp",
                status="error",
                last_error="WARP is supported on Windows only.",
            )
        if not self.is_installed():
            return ComponentState(
                component_id="warp",
                status="error",
                detail="Not installed",
                last_error="WARP binaries are not installed. Press Install.",
            )
        if not self.is_admin():
            return ComponentState(
                component_id="warp",
                status="error",
                last_error="Administrator rights are required for WARP.",
            )
        self._log("start: beginning WARP startup sequence", app_log=True)
        if not self.ensure_svc_running():
            return ComponentState(
                component_id="warp",
                status="error",
                detail="Error",
                last_error="WARP service (warp-svc) failed to start. Check logs/warp.log.",
            )
        try:
            self.apply_desired_settings()
        except Exception as error:
            self._log(f"apply desired settings failed: {error}", level="warning")

        current = self.status(fresh=True)
        fix_needed = False
        if current == WARP_STATUS_CONNECTED:
            self._log("start: already connected", app_log=True)
            return ComponentState(component_id="warp", status="running", detail="Connected")
        if current == WARP_STATUS_REGISTRATION_MISSING:
            # Регистрации нет — connect заведомо не поднимет туннель.
            fix_needed = True
            self._log("start: registration missing, auto-fix required", level="warning", app_log=True)
        else:
            self.connect()
            if self.wait_for_status(WARP_STATUS_CONNECTED, CONNECT_TIMEOUT):
                self._log("start: connected on first attempt", app_log=True)
                return ComponentState(component_id="warp", status="running", detail="Connected")
            self._log("start: first connect attempt failed", level="warning", app_log=True)
            fix_needed = True

        if fix_needed:
            settings = self.settings.get()
            already_applied = bool(getattr(settings, "warp_auto_fix_applied", False))
            if already_applied:
                # Автофикс уже выполнялся — ждём ручного действия пользователя.
                state = self.current_state()
                if state.status != "error":
                    state.last_error = (
                        "WARP did not connect. Press 'Reinstall registration'."
                    )
                self._log("start: auto-fix already applied, manual action required", level="warning", app_log=True)
                return state
            result = self.auto_fix_sequence()
            if result.get("status") == "connected":
                return ComponentState(component_id="warp", status="running", detail="Connected")
            state = self.current_state()
            state.last_error = (
                "WARP did not connect even after auto-fix. "
                "Press 'Reinstall registration' and check logs/warp.log."
            )
            return state

        state = self.current_state()
        if state.status == "stopped":
            state.last_error = "WARP is not connected. Check logs/warp.log."
        return state

    def stop(self) -> ComponentState:
        """Остановка: disconnect → остановка демона (процесс/служба)."""
        if self.is_installed():
            try:
                self.disconnect()
            except Exception as error:
                self._log(f"stop: disconnect failed: {error}", level="warning")
        try:
            self.stop_svc()
        except Exception as error:
            self._log(f"stop: stop_svc failed: {error}", level="warning", app_log=True)
        self._log("stop: WARP stopped", app_log=True)
        return ComponentState(component_id="warp", status="stopped", detail="Disconnected")

    def uninstall(self) -> bool:
        """Полное удаление компонента: стоп, удаление приватной службы, файлов."""
        try:
            self.stop()
        except Exception:
            pass
        removed_service = False
        if self._service_state(WARP_SERVICE_NAME):
            self._sc("stop", WARP_SERVICE_NAME)
            code, output = self._sc("delete", WARP_SERVICE_NAME)
            removed_service = code == 0
            self._log(f"uninstall: service delete -> {output.strip()}", app_log=True)
        removed_files = False
        for attempt in range(3):
            try:
                if self.warp_dir.exists():
                    import shutil

                    shutil.rmtree(self.warp_dir, ignore_errors=False)
                removed_files = not self.warp_dir.exists()
                break
            except OSError as error:
                self._log(f"uninstall: rmtree attempt {attempt + 1} failed: {error}", level="warning")
                time.sleep(0.4)
        self._log(
            f"uninstall: service_removed={removed_service} files_removed={removed_files}",
            app_log=True,
        )
        return removed_files or not self.warp_dir.exists()

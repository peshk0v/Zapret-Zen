from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError

from zapret_hub.domain import ComponentDefinition, ComponentState
from zapret_hub.runtime_env import is_packaged_runtime
from zapret_hub.services.github_network import GitHubNetworkClient, is_recoverable_github_error
from zapret_hub.services.logging_service import LoggingManager
from zapret_hub.services.service_catalog import prioritize_generals_for_services
from zapret_hub.services.service_rules import SERVICE_RULES
from zapret_hub.services.settings import SettingsManager
from zapret_hub.services.storage import StorageManager

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

_ZAPRET_DRIVER_SERVICE_NAMES = ("zapret", "WinDivert", "WinDivert14")
_TORRENT_PROCESS_NAMES = (
    "qbittorrent.exe",
    "qbittorrent",
    "transmission-qt.exe",
    "transmission.exe",
    "utorrent.exe",
    "bittorrent.exe",
    "deluge.exe",
    "aria2c.exe",
    "biglybt.exe",
    "vuze.exe",
    "tixati.exe",
    "webtorrent.exe",
)


class _WindowsJob:
    def __init__(self) -> None:
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.job = self.kernel32.CreateJobObjectW(None, None)
        if not self.job:
            return

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JobObjectExtendedLimitInformation = 9

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self.kernel32.SetInformationJobObject(
            self.job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )

    def assign_pid(self, pid: int) -> None:
        if not self.job:
            return
        PROCESS_ALL_ACCESS = 0x1F0FFF
        handle = self.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if handle:
            self.kernel32.AssignProcessToJobObject(self.job, handle)
            self.kernel32.CloseHandle(handle)


class ProcessManager:
    def __init__(
        self,
        storage: StorageManager,
        logging: LoggingManager,
        settings: SettingsManager,
    ) -> None:
        self.storage = storage
        self.logging = logging
        self.settings = settings
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._process_lock = threading.RLock()
        self._states: dict[str, ComponentState] = {}
        self._current_zapret_runtime: Path | None = None
        self._state_cache: list[ComponentState] = []
        self._state_cache_at = 0.0
        self._hub_runtime_token = secrets.token_urlsafe(24)
        self._log_streams: dict[str, Any] = {}
        self._telegram_proxy_launch_info: dict[str, Any] | None = None
        self._diagnostic_runtime_override = False
        self._job = _WindowsJob() if sys.platform.startswith("win") else None
        self.github = GitHubNetworkClient(logging, recovery_runner=self.with_github_connectivity_recovery)
        self._creationflags = 0
        self._startupinfo: subprocess.STARTUPINFO | None = None
        if sys.platform.startswith("win"):
            self._creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            self._startupinfo = startup

    def list_components(self) -> list[ComponentDefinition]:
        raw_items = self.storage.read_json(self.storage.paths.data_dir / "components.json", default=[])
        settings = self.settings.get()
        components = [ComponentDefinition(**item) for item in raw_items]
        for component in components:
            component.enabled = component.id in settings.enabled_component_ids
            component.autostart = component.id in settings.autostart_component_ids
        return components

    def list_zapret_generals(self) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        bundles = self._get_zapret_bundles(enabled_only=True, include_hidden_generals=True)
        for bundle in bundles:
            bundle_id = bundle["id"]
            bundle_title = bundle["title"]
            root = bundle["path"]
            for script in sorted(root.glob("*.bat")):
                name = script.name.lower()
                if name.startswith("service"):
                    continue
                option_id = f"{bundle_id}|{script.name}"
                options.append(
                    {
                        "id": option_id,
                        "name": script.name,
                        "bundle": bundle_title,
                        "bundle_id": bundle_id,
                        "path": str(script),
                    }
                )
        return sorted(options, key=self._general_option_sort_key)

    def prompt_telegram_proxy_link(self) -> None:
        settings = self.settings.get()
        secret = (settings.tg_proxy_secret or "").strip().lower()
        if secret.startswith("dd") and len(secret) > 2:
            secret = secret[2:]
        if not secret:
            secret = secrets.token_hex(16)
            settings = self.settings.update(tg_proxy_secret=secret)
        self._ensure_telegram_and_open_proxy_link(
            host=settings.tg_proxy_host,
            port=int(settings.tg_proxy_port),
            secret=secret,
        )

    def consume_telegram_proxy_launch_info(self) -> dict[str, Any] | None:
        info = self._telegram_proxy_launch_info
        self._telegram_proxy_launch_info = None
        return dict(info) if isinstance(info, dict) else None

    def list_states(self) -> list[ComponentState]:
        if self._state_cache and (time.time() - self._state_cache_at) < 0.7:
            return [
                ComponentState(
                    component_id=state.component_id,
                    status=state.status,
                    pid=state.pid,
                    last_error=state.last_error,
                )
                for state in self._state_cache
            ]
        states = self._compute_states()
        self._state_cache = [
            ComponentState(
                component_id=state.component_id,
                status=state.status,
                pid=state.pid,
                last_error=state.last_error,
            )
            for state in states
        ]
        self._state_cache_at = time.time()
        return states

    def _compute_states(self) -> list[ComponentState]:
        states: list[ComponentState] = []
        settings = self.settings.get()
        for component in self.list_components():
            state = self._states.get(component.id, ComponentState(component_id=component.id))
            if component.id == "zapret":
                state.status = "running" if self._is_image_running("winws.exe") else "stopped"
                state.pid = None
            elif component.id == "tg-ws-proxy":
                worker = self._processes.get(component.id)
                listening = self._is_port_listening(settings.tg_proxy_host, int(settings.tg_proxy_port))
                if (worker and worker.poll() is None) or listening:
                    state.status = "running"
                    state.pid = worker.pid if worker and worker.poll() is None else None
                else:
                    state.status = "stopped"
                    state.pid = None
            elif component.id == "goshkow-vpn":
                process = self._processes.get(component.id)
                if process and process.poll() is None:
                    state.status = "running"
                    state.pid = process.pid
                else:
                    state.status = "stopped"
                    state.pid = None
            else:
                process = self._processes.get(component.id)
                if process and process.poll() is None:
                    state.status = "running"
                    state.pid = process.pid
                else:
                    state.status = "stopped"
                    state.pid = None
            states.append(state)
        return states

    def _invalidate_state_cache(self) -> None:
        self._state_cache = []
        self._state_cache_at = 0.0

    def start_component(self, component_id: str) -> ComponentState:
        with self._process_lock:
            return self._start_component_unlocked(component_id)

    def _start_component_unlocked(self, component_id: str) -> ComponentState:
        component = next(item for item in self.list_components() if item.id == component_id)
        if component.id == "zapret":
            self.stop_component("goshkow-vpn")
            state = self._start_zapret(component_id)
            self._invalidate_state_cache()
            return state
        if component.id == "tg-ws-proxy":
            state = self._start_tg_ws_proxy(component_id)
            self._invalidate_state_cache()
            return state
        if component.id == "goshkow-vpn":
            state = self._start_goshkow_vpn(component_id)
            self._invalidate_state_cache()
            return state
        current = self._processes.get(component_id)
        if current and current.poll() is None:
            return self._states.get(component_id, ComponentState(component_id=component_id, status="running", pid=current.pid))

        process = subprocess.Popen(
            component.command,
            text=True,
            creationflags=self._creationflags,
            startupinfo=self._startupinfo,
        )
        if self._job:
            self._job.assign_pid(process.pid)
        state = ComponentState(component_id=component_id, status="running", pid=process.pid)
        self._processes[component_id] = process
        self._states[component_id] = state
        self.logging.log("info", "Component started", component_id=component_id, pid=process.pid)
        self._invalidate_state_cache()
        return state

    def stop_component(self, component_id: str) -> ComponentState:
        with self._process_lock:
            return self._stop_component_unlocked(component_id)

    def _stop_component_unlocked(self, component_id: str) -> ComponentState:
        state = self._states.get(component_id, ComponentState(component_id=component_id))

        if component_id == "zapret":
            active_runtime = self._current_zapret_runtime
            self._force_stop_zapret_runtime()
            self._close_source_log_stream("zapret")
            self._processes.pop(component_id, None)
            if active_runtime is not None:
                self._reset_active_runtime_dir(active_runtime)
            state.status = "stopped" if not self._is_image_running("winws.exe") else "running"
            state.pid = None
            if state.status != "stopped":
                state.last_error = "Failed to stop winws.exe"
            self._states[component_id] = state
            self.logging.log("info", "Zapret stopped")
            self._invalidate_state_cache()
            return state

        if component_id == "tg-ws-proxy":
            process = self._processes.get(component_id)
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            if process and process.pid:
                self._run_quiet(["taskkill", "/PID", str(process.pid), "/F"])
            self._processes.pop(component_id, None)
            self._kill_image("TgWsProxy_windows.exe")
            self._close_source_log_stream("tg-ws-proxy")
            state.status = "stopped"
            state.pid = None
            self._states[component_id] = state
            self.logging.log("info", "TG WS Proxy stopped")
            self._invalidate_state_cache()
            return state
        if component_id == "goshkow-vpn":
            process = self._processes.get(component_id)
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            if process and process.pid:
                self._run_quiet(["taskkill", "/PID", str(process.pid), "/F", "/T"])
            self._processes.pop(component_id, None)
            self._close_source_log_stream(component_id)
            state.status = "stopped"
            state.pid = None
            state.last_error = ""
            self._states[component_id] = state
            self.logging.log("info", "goshkow vpn stopped")
            self._invalidate_state_cache()
            return state
        process = self._processes.get(component_id)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self._processes.pop(component_id, None)
        state.status = "stopped"
        state.pid = None
        self._states[component_id] = state
        self.logging.log("info", "Component stopped", component_id=component_id)
        self._close_source_log_stream(component_id)
        self._invalidate_state_cache()
        return state

    def start_enabled_components(self) -> list[ComponentState]:
        started = []
        for component in self.list_components():
            if component.enabled:
                try:
                    started.append(self.start_component(component.id))
                except Exception as error:
                    state = ComponentState(
                        component_id=component.id,
                        status="error",
                        last_error=str(error),
                    )
                    self._states[component.id] = state
                    self.logging.log("error", "Enabled component failed to start", component_id=component.id, error=str(error))
                    started.append(state)
        return started

    def start_autostart_components(self) -> list[ComponentState]:
        started = []
        for component in self.list_components():
            if not (component.enabled and component.autostart):
                continue
            try:
                started.append(self.start_component(component.id))
            except Exception as error:
                state = ComponentState(
                    component_id=component.id,
                    status="error",
                    last_error=str(error),
                )
                self._states[component.id] = state
                self.logging.log("error", "Autostart component failed to start", component_id=component.id, error=str(error))
                started.append(state)
        return started

    def stop_all(self) -> list[ComponentState]:
        stopped = [self.stop_component(component.id) for component in self.list_components()]
        self._cleanup_merged_runtime()
        return stopped

    def toggle_component_enabled(self, component_id: str) -> ComponentDefinition:
        components = self.list_components()
        target = next(component for component in components if component.id == component_id)
        target.enabled = not target.enabled
        enabled_ids = sorted(component.id for component in components if component.enabled)
        self.settings.update(enabled_component_ids=enabled_ids)
        if not target.enabled:
            self.stop_component(component_id)
        self.logging.log("info", "Component enabled state changed", component_id=component_id, enabled=target.enabled)
        self._invalidate_state_cache()
        return target

    def toggle_component_autostart(self, component_id: str) -> ComponentDefinition:
        components = self.list_components()
        target = next(component for component in components if component.id == component_id)
        target.autostart = not target.autostart
        autostart_ids = sorted(component.id for component in components if component.autostart)
        self.settings.update(autostart_component_ids=autostart_ids)
        self.logging.log("info", "Component autostart state changed", component_id=component_id, autostart=target.autostart)
        return target

    def _start_zapret(self, component_id: str) -> ComponentState:
        # всегда перезапускаем, чтобы не было конфликтов со сторонними процессами
        self.stop_component(component_id)
        selected_option = self._resolve_selected_general_option()
        if selected_option is None:
            state = ComponentState(component_id=component_id, status="error", last_error="No general script found.")
            self._states[component_id] = state
            return state

        selected_script = Path(selected_option["path"])
        selected_bundle_root = Path(selected_script).parent
        active_root: Path | None = None
        process: subprocess.Popen[Any] | None = None
        try:
            active_root = self._prepare_active_zapret_runtime(
                selected_bundle_root=selected_bundle_root,
                selected_bundle_id=selected_option["bundle_id"],
                selected_script_name=selected_script.name,
            )
            self._current_zapret_runtime = active_root
            self._apply_zapret_runtime_switches(active_root)
            active_script = active_root / selected_script.name
            self._ensure_zapret_user_lists(active_root / "lists")
            self._materialize_visible_merged_runtime(active_root)
            bin_dir = active_root / "bin"
            lists_dir = active_root / "lists"
            if not active_script.exists():
                raise FileNotFoundError(f"Selected general was not materialized: {active_script}")
            if not (bin_dir / "winws.exe").exists():
                raise FileNotFoundError(f"winws.exe was not materialized: {bin_dir / 'winws.exe'}")
            winws_command = self._extract_winws_command(active_script, bin_dir=bin_dir, lists_dir=lists_dir)
            winws_command = self._apply_selected_service_command_extensions(winws_command, lists_dir=lists_dir)
            winws_command = self._apply_vpn_priority_to_command(winws_command, lists_dir=lists_dir)
            if not winws_command:
                state = ComponentState(
                    component_id=component_id,
                    status="error",
                    last_error="Failed to parse winws command from selected general file.",
                )
                self._states[component_id] = state
                self.logging.log("error", "Zapret command parse failed", script=str(active_script))
                return state
            process = subprocess.Popen(
                winws_command,
                cwd=str(bin_dir),
                creationflags=self._creationflags,
                startupinfo=self._startupinfo,
                stdout=self._open_source_log_stream("zapret"),
                stderr=subprocess.STDOUT,
            )
            if self._job:
                self._job.assign_pid(process.pid)
            self._processes[component_id] = process
            running = False
            for _ in range(24):
                if self._is_image_running("winws.exe"):
                    running = True
                    break
                time.sleep(0.25)
            if running:
                try:
                    (active_root / ".driver_path_in_use").write_text(datetime.utcnow().isoformat(), encoding="utf-8")
                except Exception:
                    pass
                state = ComponentState(component_id=component_id, status="running", pid=process.pid)
                self.logging.log("info", "Zapret started", script=str(active_script), command=winws_command[0])
            else:
                self._close_source_log_stream("zapret")
                log_hint = self._recent_source_log_error("zapret")
                error_message = log_hint or "winws did not start. Run app as Administrator and check antivirus exclusions for WinDivert."
                state = ComponentState(
                    component_id=component_id,
                    status="error",
                    last_error=error_message,
                )
                self.logging.log("error", "Zapret failed to start", script=str(active_script), error=error_message)
        except OSError as error:
            if getattr(error, "winerror", 0) == 740:
                state = ComponentState(
                    component_id=component_id,
                    status="error",
                    last_error="Administrator rights are required for winws/WinDivert.",
                )
                self.logging.log("error", "Zapret start failed: admin required")
            else:
                state = ComponentState(component_id=component_id, status="error", last_error=str(error))
                self.logging.log("error", "Zapret start failed", error=str(error))
        except shutil.Error as error:
            state = ComponentState(component_id=component_id, status="error", last_error=str(error))
            self.logging.log("error", "Zapret runtime build failed", error=str(error))
        except Exception as error:
            state = ComponentState(component_id=component_id, status="error", last_error=str(error))
            self.logging.log("error", "Zapret start crashed", error=str(error))
        if state.status != "running":
            if process and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
            self._force_stop_zapret_runtime()
            if active_root is not None:
                self._reset_active_runtime_dir(active_root)
            self._current_zapret_runtime = None
        self._states[component_id] = state
        return state

    def _start_goshkow_vpn_legacy_gui_disabled(self, component_id: str) -> ComponentState:
        state = ComponentState(component_id=component_id, status="error")
        state.last_error = "Legacy goshkow vpn GUI startup path is disabled."
        return state
        current = self._processes.get(component_id)
        if current and current.poll() is None:
            existing = self._states.get(component_id, ComponentState(component_id=component_id, status="running", pid=current.pid))
            existing.status = "running"
            existing.pid = current.pid
            return existing
        state = ComponentState(component_id=component_id)
        vpn_state = self.storage.read_json(self.storage.paths.data_dir / "goshkow_vpn.json", default={}) or {}
        if not isinstance(vpn_state, dict) or not vpn_state.get("subscription_url") or not vpn_state.get("servers"):
            state.status = "error"
            state.last_error = "Сначала добавьте подписку goshkow vpn."
            self._states[component_id] = state
            return state
        zapret_running = bool(self._is_image_running("winws.exe"))
        if sys.platform.startswith("win") and not self._is_admin_windows():
            self.settings.update(
                selected_runtime_mode="goshkow-vpn",
                zapret_was_running_before_goshkow_vpn=zapret_running,
            )
            self.settings.update(goshkow_vpn_pending_start=True)
            state.status = "error"
            state.last_error = "Для запуска TUN-режима нужны права администратора. Перезапустите приложение от имени администратора."
            self._states[component_id] = state
            return state
        try:
            self.settings.update(
                selected_runtime_mode="goshkow-vpn",
                zapret_was_running_before_goshkow_vpn=zapret_running,
                goshkow_vpn_pending_start=False,
            )
            if zapret_running:
                self.stop_component("zapret")
            exe = self._ensure_v2rayn_runtime()
            self._write_goshkow_vpn_subscription_hint(vpn_state)
            process = subprocess.Popen(
                [str(exe)],
                cwd=str(exe.parent),
                creationflags=self._creationflags,
                startupinfo=self._startupinfo,
                stdout=self._open_source_log_stream(component_id),
                stderr=subprocess.STDOUT,
            )
            if self._job:
                self._job.assign_pid(process.pid)
            state.status = "running"
            state.pid = process.pid
            state.last_error = ""
            self._processes[component_id] = process
            self._states[component_id] = state
            self.logging.log("info", "goshkow vpn started", pid=process.pid)
            return state
        except Exception as error:
            state.status = "error"
            state.last_error = str(error)
            self._states[component_id] = state
            self.logging.log("error", "goshkow vpn failed to start", error=str(error))
            return state

    def _is_admin_windows(self) -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _write_goshkow_vpn_subscription_hint(self, vpn_state: dict[str, Any]) -> None:
        target = self.storage.paths.runtime_dir / "v2rayN" / "goshkow-vpn-subscription.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        selected_id = str(vpn_state.get("selected_server_id", "") or "")
        selected = None
        for item in vpn_state.get("servers", []) or []:
            if isinstance(item, dict) and str(item.get("id", "")) == selected_id:
                selected = item
                break
        lines = [
            "goshkow vpn",
            f"subscription={vpn_state.get('subscription_url', '')}",
            f"selected={selected.get('name', '') if isinstance(selected, dict) else ''}",
            "Этот файл создан Zapret Hub для интеграции подписки vpn.goshkow.ru с v2rayN.",
        ]
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _ensure_v2rayn_runtime(self) -> Path:
        runtime_root = self.storage.paths.runtime_dir / "v2rayN"
        for candidate in runtime_root.rglob("sing-box.exe"):
            if candidate.name.lower() == "sing-box.exe":
                return candidate
        runtime_root.mkdir(parents=True, exist_ok=True)
        archive = self._download_latest_v2rayn_archive()
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(runtime_root)
        for candidate in runtime_root.rglob("sing-box.exe"):
            if candidate.name.lower() == "sing-box.exe":
                return candidate
        raise FileNotFoundError("v2rayN был загружен, но исполняемый файл не найден.")

    def _download_latest_v2rayn_archive(self) -> Path:
        api_url = "https://api.github.com/repos/2dust/v2rayN/releases/latest"
        release = self.github.github_json(api_url, timeout=20, purpose="v2rayn-release-metadata")
        assets = release.get("assets", []) if isinstance(release, dict) else []
        selected_url = ""
        selected_name = ""
        for marker in ("windows-64-desktop", "windows-64"):
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                name = str(asset.get("name", "") or "")
                url = str(asset.get("browser_download_url", "") or "")
                if url and name.lower().endswith(".zip") and marker in name.lower():
                    selected_url = url
                    selected_name = name
                    break
            if selected_url:
                break
        if not selected_url:
            raise FileNotFoundError("В последнем релизе v2rayN не найден Windows x64 архив.")
        target = self.storage.paths.cache_dir / selected_name
        target.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(selected_url, target)
        return target

    def _start_goshkow_vpn(self, component_id: str) -> ComponentState:
        current = self._processes.get(component_id)
        if current and current.poll() is None:
            existing = self._states.get(component_id, ComponentState(component_id=component_id, status="running", pid=current.pid))
            existing.status = "running"
            existing.pid = current.pid
            return existing
        state = ComponentState(component_id=component_id)
        vpn_state = self.storage.read_json(self.storage.paths.data_dir / "goshkow_vpn.json", default={}) or {}
        if not isinstance(vpn_state, dict) or str(vpn_state.get("subscription_state", "") or "") != "valid":
            state.status = "error"
            state.last_error = "Сначала добавьте валидную подписку goshkow vpn."
            self._states[component_id] = state
            return state
        selected_server = self._selected_goshkow_vpn_server(vpn_state)
        if selected_server is None:
            state.status = "error"
            state.last_error = "Не выбрана локация goshkow vpn."
            self._states[component_id] = state
            return state
        saved_flag = bool(getattr(self.settings.get(), "zapret_was_running_before_goshkow_vpn", False))
        zapret_running = bool(self._is_image_running("winws.exe") or saved_flag)
        try:
            config_path = self._write_goshkow_vpn_runtime_config(vpn_state, selected_server)
            core = self._ensure_goshkow_vpn_core()
            self.logging.log(
                "info",
                "goshkow vpn launching",
                component_id=component_id,
                selected_server=str(selected_server.get("name", "") or selected_server.get("id", "") or ""),
                tun_enabled=bool(vpn_state.get("tun_enabled", True)),
                routing_mode=str(vpn_state.get("routing_mode", "global") or "global"),
                system_proxy_mode=str(vpn_state.get("system_proxy_mode", "pac") or "pac"),
                processes=[
                    item.strip()
                    for item in str(vpn_state.get("processes", "") or "").split(",")
                    if item.strip()
                ],
                config=str(config_path),
            )
        except Exception as error:
            state.status = "error"
            state.last_error = str(error)
            self._states[component_id] = state
            self.logging.log("error", "goshkow vpn failed to prepare", error=str(error))
            return state
        if sys.platform.startswith("win") and not self._is_admin_windows():
            self.settings.update(
                selected_runtime_mode="goshkow-vpn",
                zapret_was_running_before_goshkow_vpn=zapret_running,
            )
            self.settings.update(goshkow_vpn_pending_start=True)
            state.status = "error"
            state.last_error = "Для запуска TUN-режима нужны права администратора. Перезапустите приложение от имени администратора."
            self._states[component_id] = state
            return state
        try:
            self.settings.update(
                selected_runtime_mode="goshkow-vpn",
                zapret_was_running_before_goshkow_vpn=zapret_running,
                goshkow_vpn_pending_start=False,
            )
            if zapret_running:
                self.stop_component("zapret")
            process = subprocess.Popen(
                [str(core), "run", "-c", str(config_path)],
                cwd=str(core.parent),
                creationflags=self._creationflags,
                startupinfo=self._startupinfo,
                stdout=self._open_source_log_stream(component_id),
                stderr=subprocess.STDOUT,
            )
            if self._job:
                self._job.assign_pid(process.pid)
            for _ in range(8):
                if process.poll() is not None:
                    break
                time.sleep(0.25)
            if process.poll() is not None:
                log_error = self._recent_source_log_error(component_id)
                state.status = "error"
                state.pid = None
                state.last_error = log_error or "goshkow vpn завершился сразу после запуска. Проверьте конфигурацию и права администратора."
                self._states[component_id] = state
                self._close_source_log_stream(component_id)
                self.logging.log("error", "goshkow vpn exited early", error=state.last_error, config=str(config_path))
                return state
            self._processes[component_id] = process
            state.status = "running"
            state.pid = process.pid
            state.last_error = ""
            self._states[component_id] = state
            self.logging.log("info", "goshkow vpn started", pid=process.pid, config=str(config_path))
            return state
        except Exception as error:
            state.status = "error"
            state.last_error = str(error)
            self._states[component_id] = state
            self.logging.log("error", "goshkow vpn failed to start", error=str(error))
            return state

    def _selected_goshkow_vpn_server(self, vpn_state: dict[str, Any]) -> dict[str, Any] | None:
        selected_id = str(vpn_state.get("selected_server_id", "") or "")
        for item in vpn_state.get("servers", []) or []:
            if isinstance(item, dict) and str(item.get("id", "")) == selected_id:
                return dict(item)
        return None

    def _goshkow_vpn_runtime_root(self) -> Path:
        return self.storage.paths.runtime_dir / "v2rayN"

    def _ensure_goshkow_vpn_core(self) -> Path:
        runtime_root = self._goshkow_vpn_runtime_root()
        for candidate in runtime_root.rglob("sing-box.exe"):
            if candidate.name.lower() == "sing-box.exe":
                return candidate
        runtime_root.mkdir(parents=True, exist_ok=True)
        archive = self._download_latest_v2rayn_archive()
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(runtime_root)
        for candidate in runtime_root.rglob("sing-box.exe"):
            if candidate.name.lower() == "sing-box.exe":
                return candidate
        raise FileNotFoundError("В runtime v2rayN не найден sing-box.exe.")

    def _write_goshkow_vpn_runtime_config(self, vpn_state: dict[str, Any], selected_server: dict[str, Any]) -> Path:
        runtime_root = self._goshkow_vpn_runtime_root()
        config_dir = runtime_root / "goshkow-vpn"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"
        config = self._build_goshkow_vpn_config(vpn_state, selected_server)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return config_path

    def _build_goshkow_vpn_config(self, vpn_state: dict[str, Any], selected_server: dict[str, Any]) -> dict[str, Any]:
        endpoint = self._parse_goshkow_vpn_endpoint(selected_server)
        if endpoint is None:
            raise ValueError("Формат выбранной локации пока не поддерживается во встроенном TUN-режиме.")
        processes = [item.strip() for item in str(vpn_state.get("processes", "") or "").split(",") if item.strip()]
        rules_mode = str(vpn_state.get("rules_mode", "blacklist") or "blacklist")
        routing_mode = str(vpn_state.get("routing_mode", "global") or "global")
        tun_enabled = bool(vpn_state.get("tun_enabled", True))
        route_final = "proxy"
        route_rules: list[dict[str, Any]] = [
            {"action": "sniff"},
            {"action": "route", "protocol": "bittorrent", "outbound": "direct"},
            {"action": "route", "process_name": list(_TORRENT_PROCESS_NAMES), "outbound": "direct"},
            {"action": "hijack-dns", "port": 53, "network": ["tcp", "udp"]},
            {"protocol": "dns", "action": "hijack-dns"},
            {"action": "route", "ip_is_private": True, "outbound": "direct"},
        ]
        if processes:
            route_rules.append({"action": "route", "process_name": processes, "outbound": "proxy"})
            route_final = "direct"
        inbound: dict[str, Any]
        if tun_enabled:
            inbound = {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": "zapret_hub_tun",
                "address": ["172.18.0.1/30"],
                "mtu": 9000,
                "auto_route": True,
                "strict_route": True,
                "stack": "system",
            }
        else:
            inbound = {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": 10808,
            }

        config: dict[str, Any] = {
            "log": {"level": "info"},
            "dns": {
                "strategy": "ipv4_only",
                "independent_cache": True,
                "servers": [
                    {"tag": "local", "type": "local", "prefer_go": True},
                    {
                        "tag": "remote",
                        "type": "https",
                        "server": "1.1.1.1",
                        "server_port": 443,
                        "path": "/dns-query",
                        "connect_timeout": "4s",
                        "detour": "proxy",
                        "tls": {"enabled": True, "server_name": "cloudflare-dns.com"},
                    },
                ],
                "final": "remote",
            },
            "inbounds": [inbound],
            "outbounds": [
                endpoint,
                {"type": "direct", "tag": "direct"},
                {"type": "block", "tag": "block"},
            ],
            "route": {
                "auto_detect_interface": True,
                "default_domain_resolver": {"server": "local", "strategy": "ipv4_only"},
                "final": route_final,
                "rules": route_rules,
            },
        }
        if str(vpn_state.get("system_proxy_mode", "clear") or "clear") == "set":
            config["experimental"] = {"clash_api": {"external_controller": "127.0.0.1:9090"}}
        return config

    def _parse_goshkow_vpn_endpoint(self, selected_server: dict[str, Any]) -> dict[str, Any] | None:
        raw = str(selected_server.get("raw", "") or "").strip()
        if not raw:
            return None
        parsed = urllib.parse.urlparse(raw)
        scheme = parsed.scheme.lower()
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        host = parsed.hostname or str(selected_server.get("host", "") or "")
        port = int(parsed.port or 443)
        if scheme == "vless":
            uuid = urllib.parse.unquote(parsed.username or "")
            if not uuid or not host:
                return None
            transport = str(query.get("type", ["tcp"])[0] or "tcp").lower()
            security = str(query.get("security", [""])[0] or "").lower()
            service_name = str(query.get("serviceName", query.get("service_name", [""]))[0] or "").strip()
            server_name = str(query.get("sni", query.get("servername", [host]))[0] or host).strip()
            flow = str(query.get("flow", [""])[0] or "").strip()
            alpn = [item for item in str(query.get("alpn", [""])[0] or "").split(",") if item]
            tls: dict[str, Any] = {}
            if security:
                tls["enabled"] = True
                tls["server_name"] = server_name or host
                if alpn:
                    tls["alpn"] = alpn
                tls["insecure"] = str(query.get("allowInsecure", ["0"])[0]).lower() in {"1", "true", "yes"}
                fingerprint = str(query.get("fp", [""])[0] or "").strip()
                if fingerprint:
                    tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
                if security == "reality":
                    public_key = str(query.get("pbk", [""])[0] or "").strip()
                    short_id = str(query.get("sid", [""])[0] or "").strip()
                    if public_key:
                        tls["reality"] = {"enabled": True, "public_key": public_key, "short_id": short_id}
            outbound: dict[str, Any] = {
                "type": "vless",
                "tag": "proxy",
                "server": host,
                "server_port": port,
                "uuid": uuid,
            }
            if not self._looks_like_ip_address(host):
                outbound["domain_resolver"] = "local"
            if flow:
                outbound["flow"] = flow
            if tls:
                outbound["tls"] = tls
            if transport == "grpc":
                outbound["transport"] = {"type": "grpc", "service_name": service_name or "goshkow-vpn"}
            return outbound
        if scheme == "trojan":
            password = urllib.parse.unquote(parsed.username or "")
            if not password or not host:
                return None
            outbound = {
                "type": "trojan",
                "tag": "proxy",
                "server": host,
                "server_port": port,
                "password": password,
                "tls": {
                    "enabled": True,
                    "server_name": str(query.get("sni", [host])[0] or host),
                    "insecure": str(query.get("allowInsecure", ["0"])[0]).lower() in {"1", "true", "yes"},
                },
            }
            if not self._looks_like_ip_address(host):
                outbound["domain_resolver"] = "local"
            if str(query.get("security", ["tls"])[0] or "tls").lower() == "reality":
                public_key = str(query.get("pbk", [""])[0] or "").strip()
                short_id = str(query.get("sid", [""])[0] or "").strip()
                if public_key:
                    outbound["tls"]["reality"] = {"enabled": True, "public_key": public_key, "short_id": short_id}
            return outbound
        if scheme in {"ss", "shadowsocks"}:
            method = str(query.get("method", [""])[0] or "").strip()
            password = urllib.parse.unquote(parsed.password or "")
            if not method or not password or not host:
                return None
            return {
                "type": "shadowsocks",
                "tag": "proxy",
                "server": host,
                "server_port": port,
                "method": method,
                "password": password,
                **({"domain_resolver": "local"} if not self._looks_like_ip_address(host) else {}),
            }
        if scheme in {"hysteria2", "hy2"}:
            password = urllib.parse.unquote(parsed.username or "")
            if not password or not host:
                return None
            return {
                "type": "hysteria2",
                "tag": "proxy",
                "server": host,
                "server_port": port,
                "password": password,
                **({"domain_resolver": "local"} if not self._looks_like_ip_address(host) else {}),
                "tls": {
                    "enabled": True,
                    "server_name": str(query.get("sni", [host])[0] or host),
                    "insecure": str(query.get("insecure", ["0"])[0]).lower() in {"1", "true", "yes"},
                },
            }
        if scheme == "vmess":
            vmess = self._parse_vmess(raw)
            if vmess is None:
                return None
            host = vmess.get("host", host)
            uuid = vmess.get("uuid", "")
            if not host or not uuid:
                return None
            outbound = {
                "type": "vmess",
                "tag": "proxy",
                "server": host,
                "server_port": int(vmess.get("port", port) or port),
                "uuid": uuid,
            }
            if not self._looks_like_ip_address(host):
                outbound["domain_resolver"] = "local"
            return outbound
        return None

    def _parse_vmess(self, raw: str) -> dict[str, str] | None:
        try:
            payload = raw.split("://", 1)[1].strip()
            padded = payload + "=" * (-len(payload) % 4)
            data = json.loads(base64.b64decode(padded).decode("utf-8", errors="ignore"))
            if not isinstance(data, dict):
                return None
            return {
                "host": str(data.get("add", "") or ""),
                "uuid": str(data.get("id", "") or ""),
                "port": str(data.get("port", "") or ""),
            }
        except Exception:
            return None

    def _extract_winws_command(self, script_path: Path, bin_dir: Path, lists_dir: Path) -> list[str]:
        game_filter, game_filter_tcp, game_filter_udp = self._get_game_filter_values(script_path.parent)
        lines = self._read_batch_logical_lines(script_path)
        for line in lines:
            if "winws.exe" not in line.lower():
                continue
            try:
                parts = shlex.split(line, posix=False)
            except ValueError:
                continue
            if not parts:
                continue
            winws_idx = next((i for i, item in enumerate(parts) if "winws.exe" in item.lower()), -1)
            if winws_idx < 0:
                continue

            executable = self._expand_batch_value(
                parts[winws_idx],
                script_dir=script_path.parent,
                bin_dir=bin_dir,
                lists_dir=lists_dir,
                game_filter=game_filter,
                game_filter_tcp=game_filter_tcp,
                game_filter_udp=game_filter_udp,
            ).strip().strip('"')
            if not executable:
                continue
            exe_path = Path(executable)
            if not exe_path.is_absolute():
                script_relative = script_path.parent / executable
                if script_relative.exists():
                    exe_path = script_relative
                elif exe_path.name.lower() == "winws.exe":
                    exe_path = bin_dir / "winws.exe"
                else:
                    exe_path = bin_dir / exe_path.name
            args: list[str] = []
            for raw_arg in parts[winws_idx + 1 :]:
                arg = self._expand_batch_value(
                    raw_arg,
                    script_dir=script_path.parent,
                    bin_dir=bin_dir,
                    lists_dir=lists_dir,
                    game_filter=game_filter,
                    game_filter_tcp=game_filter_tcp,
                    game_filter_udp=game_filter_udp,
                ).strip()
                if not arg or arg == "^":
                    continue
                # убираем лишние кавычки из bat-синтаксиса
                if arg.startswith('"') and arg.endswith('"') and len(arg) >= 2:
                    arg = arg[1:-1]
                if '="' in arg and arg.endswith('"'):
                    key, value = arg.split('="', 1)
                    arg = f"{key}={value[:-1]}"
                args.append(arg)
            return [str(exe_path), *args]
        return []

    def _apply_vpn_priority_to_command(self, command: list[str], *, lists_dir: Path) -> list[str]:
        if not command or not sys.platform.startswith("win"):
            return command
        try:
            vpn_data = self._detect_vpn_priority_context()
        except Exception as error:
            self.logging.log("warning", "Failed to detect VPN priority context", error=str(error))
            return command

        adapter_indexes = [int(item) for item in vpn_data.get("adapter_indexes", []) if str(item).isdigit()]
        remote_ips = [str(item).strip() for item in vpn_data.get("remote_ips", []) if str(item).strip()]
        excluded_udp_ports = self._parse_port_ranges(self.settings.get().zapret_udp_exclude_ports)
        if excluded_udp_ports:
            command = self._exclude_udp_ports_from_command(command, excluded_udp_ports)
        if not adapter_indexes and not remote_ips:
            return command

        updated = list(command)
        raw_parts: list[str] = []
        if adapter_indexes:
            raw_filter = " and ".join(f"(ifIdx != {index} and subIfIdx != {index})" for index in sorted(set(adapter_indexes)))
            raw_parts.append(raw_filter)

        if remote_ips:
            vpn_exclude_path = lists_dir / "ipset-vpn-exclude.txt"
            vpn_exclude_path.write_text("\n".join(sorted(set(remote_ips))) + "\n", encoding="utf-8")
            updated.append(f"--ipset-exclude={vpn_exclude_path}")

        if raw_parts:
            combined_filter = " and ".join(f"({part})" for part in raw_parts)
            updated.append(f"--wf-raw-part={combined_filter}")

        self.logging.log(
            "info",
            "Applied VPN priority safeguards to zapret",
            adapter_indexes=sorted(set(adapter_indexes)),
            remote_ips=sorted(set(remote_ips)),
            excluded_udp_ports=self.settings.get().zapret_udp_exclude_ports,
        )
        return updated

    def _exclude_udp_ports_from_command(self, command: list[str], excluded_ranges: list[tuple[int, int]]) -> list[str]:
        updated: list[str] = []
        for arg in command:
            if arg.startswith("--wf-udp=") or arg.startswith("--filter-udp="):
                key, value = arg.split("=", 1)
                ranges = self._parse_port_ranges(value)
                if ranges:
                    filtered = self._subtract_port_ranges(ranges, excluded_ranges)
                    value = self._format_port_ranges(filtered) or "12"
                    arg = f"{key}={value}"
            updated.append(arg)
        return updated

    def _subtract_port_ranges(
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

    def _format_port_ranges(self, ranges: list[tuple[int, int]]) -> str:
        return ",".join(str(start) if start == end else f"{start}-{end}" for start, end in ranges)

    def _parse_port_ranges(self, value: str) -> list[tuple[int, int]]:
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

    def _detect_vpn_priority_context(self) -> dict[str, list[str]]:
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
            text=True,
            check=False,
            creationflags=self._creationflags,
            startupinfo=startup,
        )
        if proc.returncode != 0:
            self.logging.log("warning", "PowerShell helper failed", stderr=(proc.stderr or "").strip()[-1000:])
            return ""
        return (proc.stdout or "").strip()

    def _looks_like_ip_address(self, value: str) -> bool:
        candidate = value.strip()
        if not candidate:
            return False
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", candidate):
            return True
        return ":" in candidate and re.fullmatch(r"[0-9a-fA-F:]+", candidate) is not None

    def _read_batch_logical_lines(self, script_path: Path) -> list[str]:
        raw_lines = script_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        logical_lines: list[str] = []
        current = ""
        for raw in raw_lines:
            line = raw.strip()
            if not line or line.startswith("::") or line.lower().startswith("rem "):
                continue
            if current:
                current = f"{current} {line}"
            else:
                current = line
            if current.endswith("^"):
                current = current[:-1].rstrip()
                continue
            logical_lines.append(current)
            current = ""
        if current:
            logical_lines.append(current)
        return logical_lines

    def _expand_batch_value(
        self,
        value: str,
        *,
        script_dir: Path,
        bin_dir: Path,
        lists_dir: Path,
        game_filter: str,
        game_filter_tcp: str,
        game_filter_udp: str,
    ) -> str:
        result = value
        script_prefix = str(script_dir) + os.sep
        replacements = {
            "%~dp0": script_prefix,
            "%CD%": str(script_dir),
            "%BIN%": str(bin_dir) + os.sep,
            "%LISTS%": str(lists_dir) + os.sep,
            "%GameFilter%": game_filter,
            "%GameFilterTCP%": game_filter_tcp,
            "%GameFilterUDP%": game_filter_udp,
        }
        for key, replacement in replacements.items():
            result = result.replace(key, replacement).replace(key.lower(), replacement).replace(key.upper(), replacement)
        return result

    def _fortnite_service_selected(self) -> bool:
        return "fortnite" in {str(item) for item in list(self.settings.get().selected_service_ids or [])}

    def _should_force_fortnite_runtime_modes(self) -> bool:
        return self._fortnite_service_selected() and not self._diagnostic_runtime_override

    def _get_game_filter_values(self, runtime_root: Path) -> tuple[str, str, str]:
        mode_from_settings = (self.settings.get().zapret_game_filter_mode or "").strip().lower()
        if self._should_force_fortnite_runtime_modes():
            mode_from_settings = "tcpudp"
        if mode_from_settings == "auto":
            mode_from_settings = ""
        if mode_from_settings in {"all", "tcpudp"}:
            return ("1024-65535", "1024-65535", "1024-65535")
        if mode_from_settings == "tcp":
            return ("1024-65535", "1024-65535", "12")
        if mode_from_settings == "udp":
            return ("1024-65535", "12", "1024-65535")
        if mode_from_settings == "disabled":
            return ("12", "12", "12")
        mode_file = runtime_root / "utils" / "game_filter.enabled"
        if not mode_file.exists():
            return ("12", "12", "12")
        mode = mode_file.read_text(encoding="utf-8", errors="ignore").strip().lower()
        if mode in {"all", "tcpudp"}:
            return ("1024-65535", "1024-65535", "1024-65535")
        if mode == "tcp":
            return ("1024-65535", "1024-65535", "12")
        if mode == "udp":
            return ("1024-65535", "12", "1024-65535")
        return ("12", "12", "12")

    def _apply_zapret_runtime_switches(self, runtime_root: Path) -> None:
        settings = self.settings.get()
        lists_dir = runtime_root / "lists"
        utils_dir = runtime_root / "utils"
        lists_dir.mkdir(parents=True, exist_ok=True)
        utils_dir.mkdir(parents=True, exist_ok=True)

        ipset_mode = (settings.zapret_ipset_mode or "loaded").strip().lower()
        if self._should_force_fortnite_runtime_modes():
            ipset_mode = "any"
        ipset_all = lists_dir / "ipset-all.txt"
        if ipset_mode == "none":
            ipset_all.write_text("203.0.113.113/32\n", encoding="utf-8")
        elif ipset_mode == "any":
            ipset_all.write_text("", encoding="utf-8")
        elif not ipset_all.exists():
            ipset_all.write_text("", encoding="utf-8")

        game_mode = (settings.zapret_game_filter_mode or "disabled").strip().lower()
        if self._should_force_fortnite_runtime_modes():
            game_mode = "tcpudp"
        game_flag = utils_dir / "game_filter.enabled"
        if game_mode in ("all", "tcp", "udp", "tcpudp"):
            game_flag.write_text(game_mode, encoding="utf-8")
        elif game_flag.exists():
            game_flag.unlink(missing_ok=True)

    def _start_tg_ws_proxy(self, component_id: str) -> ComponentState:
        # всегда перезапускаем, чтобы не было конфликтов со сторонними процессами
        self.stop_component(component_id)

        settings = self.settings.get()
        secret = (settings.tg_proxy_secret or "").strip().lower()
        if secret.startswith("dd") and len(secret) > 2:
            secret = secret[2:]
        if not secret:
            secret = secrets.token_hex(16)
        if secret != settings.tg_proxy_secret:
            settings = self.settings.update(tg_proxy_secret=secret)
        # подчищаем старый процесс, если он остался в трее
        self._kill_image("TgWsProxy_windows.exe")
        try:
            (self.storage.paths.logs_dir / "tg_worker_error.log").unlink(missing_ok=True)
        except Exception:
            pass
        command = self._build_worker_command(
            "tg-ws-proxy",
            tg_host=settings.tg_proxy_host,
            tg_port=int(settings.tg_proxy_port),
            tg_secret=secret,
            tg_dc_ip=self._parse_tg_dc_ip_settings(settings.tg_proxy_dc_ip),
            tg_cfproxy_enabled=bool(settings.tg_proxy_cfproxy_enabled),
            tg_cfproxy_priority=bool(settings.tg_proxy_cfproxy_priority),
            tg_cfproxy_domain=settings.tg_proxy_cfproxy_domain,
            tg_fake_tls_domain=settings.tg_proxy_fake_tls_domain,
            tg_buf_kb=int(settings.tg_proxy_buf_kb or 256),
            tg_pool_size=int(settings.tg_proxy_pool_size or 4),
        )
        process = subprocess.Popen(
            command,
            cwd=str(self.storage.paths.install_root),
            creationflags=self._creationflags,
            startupinfo=self._startupinfo,
            env=self._build_worker_env(),
            stdout=self._open_source_log_stream("tg-ws-proxy"),
            stderr=subprocess.STDOUT,
        )
        listen_host = settings.tg_proxy_host
        listen_port = int(settings.tg_proxy_port)
        ready = False
        for _ in range(16):
            if process.poll() is not None:
                break
            if self._is_port_listening(listen_host, listen_port):
                ready = True
                break
            time.sleep(0.35)
        if not ready:
            error_hint = "TG WS Proxy worker did not open listening port."
            worker_error_log = self.storage.paths.logs_dir / "tg_worker_error.log"
            if worker_error_log.exists():
                error_hint = worker_error_log.read_text(encoding="utf-8")[-1000:]
            state = ComponentState(
                component_id=component_id,
                status="error",
                last_error=error_hint,
            )
            self._states[component_id] = state
            self.logging.log("error", "TG WS Proxy worker failed to start", error=error_hint)
            return state
        if self._job:
            self._job.assign_pid(process.pid)
        state = ComponentState(component_id=component_id, status="running", pid=process.pid)
        self._processes[component_id] = process
        self._states[component_id] = state
        self.logging.log("info", "TG WS Proxy worker started", pid=process.pid)
        signature = (
            f"{settings.tg_proxy_host}:{int(settings.tg_proxy_port)}:{secret}:"
            f"{settings.tg_proxy_dc_ip}:{settings.tg_proxy_cfproxy_enabled}:"
            f"{settings.tg_proxy_cfproxy_priority}:{settings.tg_proxy_cfproxy_domain}:"
            f"{settings.tg_proxy_fake_tls_domain}:{settings.tg_proxy_buf_kb}:{settings.tg_proxy_pool_size}"
        )
        if settings.tg_proxy_link_prompt_signature != signature:
            self._ensure_telegram_and_open_proxy_link(
                host=settings.tg_proxy_host,
                port=int(settings.tg_proxy_port),
                secret=secret,
            )
            self.settings.update(tg_proxy_link_prompt_signature=signature)
        return state

    def _build_worker_command(self, worker: str, **kwargs: Any) -> list[str]:
        cmd: list[str]
        if is_packaged_runtime():
            cmd = [sys.executable, "--worker", worker]
        else:
            cmd = [self._worker_python_executable(), "-m", "zapret_hub.worker_entry", "--worker", worker]

        for key, value in kwargs.items():
            option = "--" + key.replace("_", "-")
            if isinstance(value, (list, tuple)):
                for item in value:
                    cmd.extend([option, str(item)])
                continue
            cmd.extend([option, str(value)])
        return cmd

    def _parse_tg_dc_ip_settings(self, value: str) -> list[str]:
        result: list[str] = []
        for raw in re.split(r"[\n,;]+", str(value or "")):
            item = raw.strip()
            if item:
                result.append(item)
        if not result:
            # Upstream applies hard-coded defaults when --dc-ip is omitted.
            # A worker-local sentinel asks it to keep the map truly empty.
            return ["__empty__"]
        return result

    def _build_worker_env(self) -> dict[str, str]:
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
            install_root / ".venv" / "Scripts" / "python.exe",
            install_root / ".venv" / "bin" / "python",
            Path(sys.executable),
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return sys.executable

    def _get_zapret_bundles(self, enabled_only: bool, *, include_hidden_generals: bool = False) -> list[dict[str, Any]]:
        bundles: list[dict[str, Any]] = []
        base = self.storage.paths.runtime_dir / "zapret-discord-youtube"
        unified_root = self.storage.paths.mods_dir / "unified-by-goshkow"
        index_map = {
            str(item.get("id", "")): str(item.get("name", "")).strip()
            for item in (self.storage.read_json(self.storage.paths.cache_dir / "mods_index.json", default=[]) or [])
            if isinstance(item, dict)
        }
        installed_raw = self.storage.read_json(self.storage.paths.data_dir / "installed_mods.json", default=[]) or []
        for raw in installed_raw:
            if raw.get("source_type") != "zapret_bundle":
                continue
            if enabled_only and not raw.get("enabled"):
                continue
            path = Path(raw.get("path", ""))
            if not path.exists():
                continue
            mod_id = str(raw.get("id", "bundle"))
            if mod_id == "unified-by-goshkow":
                continue
            title = str(raw.get("name") or "").strip() or index_map.get(mod_id) or mod_id
            bundles.append({"id": mod_id, "title": title, "path": path})
        if include_hidden_generals and unified_root.exists():
            bundles.insert(0, {"id": "unified-general", "title": "Hub", "path": unified_root})
        if base.exists():
            bundles.append({"id": "base", "title": "", "path": base})
        return bundles

    def _general_option_sort_key(self, item: dict[str, str]) -> tuple[int, int, str]:
        bundle_id = str(item.get("bundle_id", ""))
        name = str(item.get("name", ""))
        lowered = name.lower()
        number = -1
        match = re.search(r"alt\s*(\d+)", lowered)
        if match:
            number = int(match.group(1))
        elif lowered == "general.bat":
            number = 0
        modified_rank = 0 if bundle_id == "unified-general" else 1 if bundle_id != "base" else 2
        return (modified_rank, -number, lowered)

    def _resolve_selected_general_option(self) -> dict[str, str] | None:
        options = self.list_zapret_generals()
        if not options:
            return None
        settings = self.settings.get()
        selected = settings.selected_zapret_general
        picked = next((item for item in options if item["id"] == selected), None)
        if picked is None:
            preferred = options[0]
            selected = preferred["id"]
            self.settings.update(selected_zapret_general=selected)
            picked = preferred
        return picked

    def _prepare_active_zapret_runtime(self, selected_bundle_root: Path, selected_bundle_id: str, selected_script_name: str) -> Path:
        self._cleanup_inactive_zapret_runtimes()
        active_root = self._next_active_runtime_dir()
        base_root = self.storage.paths.runtime_dir / "zapret-discord-youtube"
        if base_root.exists():
            shutil.copytree(base_root, active_root, dirs_exist_ok=True, ignore=self._runtime_copy_ignore)
        else:
            shutil.copytree(selected_bundle_root, active_root, dirs_exist_ok=True, ignore=self._runtime_copy_ignore)

        lists_target = active_root / "lists"
        bin_target = active_root / "bin"
        utils_target = active_root / "utils"
        lists_target.mkdir(parents=True, exist_ok=True)
        bin_target.mkdir(parents=True, exist_ok=True)
        utils_target.mkdir(parents=True, exist_ok=True)

        layered_bundles = self._get_zapret_bundles(enabled_only=True)
        for bundle in layered_bundles:
            bundle_id = bundle["id"]
            bundle_root = Path(bundle["path"])
            if bundle_id != "base":
                self._overlay_zapret_bundle_runtime(active_root, bundle_root)
            lists_source = bundle_root / "lists"
            if not lists_source.exists():
                continue
            self._merge_lists_into_target(lists_target, lists_source)

        self._apply_selected_service_rules(active_root)

        selected_script = selected_bundle_root / selected_script_name
        if selected_script.exists():
            shutil.copy2(selected_script, active_root / selected_script.name)
        if selected_bundle_id == "unified-general":
            self._overlay_zapret_bundle_runtime(active_root, selected_bundle_root)
            if selected_script.exists():
                shutil.copy2(selected_script, active_root / selected_script.name)

        self._apply_user_collection_overrides(lists_target)
        self._materialize_visible_merged_runtime(active_root)
        return active_root

    def _overlay_zapret_bundle_runtime(self, active_root: Path, bundle_root: Path) -> None:
        for script in bundle_root.glob("*.bat"):
            if script.name.lower().startswith("service"):
                continue
            shutil.copy2(script, active_root / script.name)

        for folder_name in ("bin", "utils"):
            source_dir = bundle_root / folder_name
            target_dir = active_root / folder_name
            if not source_dir.exists():
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            for source in source_dir.glob("*"):
                if source.is_file():
                    shutil.copy2(source, target_dir / source.name)

    def _materialize_visible_merged_runtime(self, active_root: Path) -> None:
        target_root = self.storage.paths.merged_runtime_dir / "zapret"
        if target_root.exists():
            shutil.rmtree(target_root, ignore_errors=True)
        shutil.copytree(active_root, target_root, dirs_exist_ok=True, ignore=self._runtime_copy_ignore)

    def _runtime_copy_ignore(self, directory: str, names: list[str]) -> set[str]:
        ignored_names = {".git", ".github", "__pycache__", ".mypy_cache", ".pytest_cache"}
        ignored_suffixes = {".pyc", ".pyo"}
        return {name for name in names if name in ignored_names or Path(name).suffix.lower() in ignored_suffixes}

    def _merge_lists_into_target(self, target_lists: Path, source_lists: Path) -> None:
        for source in source_lists.glob("*.txt"):
            target = target_lists / source.name
            existing = self._read_list_lines(target)
            incoming = self._read_list_lines(source)
            merged = self._merge_with_conflict_resolution(target_lists, target.name.lower(), existing, incoming)
            target.write_text("\n".join(merged) + ("\n" if merged else ""), encoding="utf-8")

    def _merge_with_conflict_resolution(
        self,
        target_lists: Path,
        filename: str,
        existing: list[str],
        incoming: list[str],
    ) -> list[str]:
        conflict_map = {
            "list-general.txt": "list-exclude.txt",
            "list-exclude.txt": "list-general.txt",
            "ipset-all.txt": "ipset-exclude.txt",
            "ipset-exclude.txt": "ipset-all.txt",
            "list-general-user.txt": "list-exclude-user.txt",
            "list-exclude-user.txt": "list-general-user.txt",
            "ipset-all-user.txt": "ipset-exclude-user.txt",
            "ipset-exclude-user.txt": "ipset-all-user.txt",
        }
        merged: list[str] = []
        seen: set[str] = set()
        for line in [*existing, *incoming]:
            if not line or line in seen:
                continue
            seen.add(line)
            merged.append(line)
        opposite = conflict_map.get(filename)
        if not opposite:
            return merged
        opposite_path = target_lists / opposite
        if not opposite_path.exists():
            return merged
        opposite_values = set(self._read_list_lines(opposite_path))
        return [line for line in merged if line not in opposite_values]

    def _apply_user_collection_overrides(self, lists_dir: Path) -> None:
        overrides_path = self.storage.paths.data_dir / "file_overrides.json"
        raw = self.storage.read_json(overrides_path, default={}) or {}
        mapping = {
            "domains": "list-general.txt",
            "exclude_domains": "list-exclude.txt",
            "all_ips": "ipset-all.txt",
            "ips": "ipset-exclude.txt",
        }
        for kind, filename in mapping.items():
            target = lists_dir / filename
            values = self._read_list_lines(target)
            override = raw.get(kind, {}) if isinstance(raw, dict) else {}
            removed = {str(item).strip() for item in list((override or {}).get("removed", []) or []) if str(item).strip()}
            added = [str(item).strip() for item in list((override or {}).get("added", []) or []) if str(item).strip()]
            result = [item for item in values if item not in removed]
            seen = set(result)
            for item in added:
                if item in seen:
                    continue
                seen.add(item)
                result.append(item)
            target.write_text("\n".join(result) + ("\n" if result else ""), encoding="utf-8")

    def _apply_selected_service_rules(self, active_root: Path) -> None:
        selected_ids = list(self.settings.get().selected_service_ids or [])
        if not selected_ids:
            return
        lists_dir = active_root / "lists"
        lists_dir.mkdir(parents=True, exist_ok=True)
        mapping = {
            "list-general.txt": "list_general",
            "list-exclude.txt": "list_exclude",
            "list-google.txt": "list_google",
            "ipset-all.txt": "ipset_all",
            "ipset-exclude.txt": "ipset_exclude",
        }
        for filename, attr in mapping.items():
            incoming: list[str] = []
            for service_id in selected_ids:
                rule = SERVICE_RULES.get(str(service_id))
                if rule is None:
                    continue
                incoming.extend(getattr(rule, attr))
            if not incoming:
                continue
            target = lists_dir / filename
            existing = self._read_list_lines(target)
            merged = self._merge_with_conflict_resolution(lists_dir, filename, existing, incoming)
            target.write_text("\n".join(merged) + ("\n" if merged else ""), encoding="utf-8")
        for service_id in selected_ids:
            rule = SERVICE_RULES.get(str(service_id))
            if rule is None:
                continue
            for filename, lines in rule.extra_lists:
                safe_name = Path(filename).name
                if not safe_name.endswith(".txt"):
                    continue
                target = lists_dir / safe_name
                existing = self._read_list_lines(target)
                merged = self._merge_with_conflict_resolution(lists_dir, safe_name.lower(), existing, list(lines))
                target.write_text("\n".join(merged) + ("\n" if merged else ""), encoding="utf-8")
            for filename, relative_source in getattr(rule, "extra_list_files", ()):
                safe_name = Path(filename).name
                if not safe_name.endswith(".txt"):
                    continue
                source = (self.storage.paths.install_root / str(relative_source)).resolve()
                if not source.exists() or not source.is_file():
                    continue
                incoming = self._read_list_lines(source)
                if not incoming:
                    continue
                target = lists_dir / safe_name
                existing = self._read_list_lines(target)
                merged = self._merge_with_conflict_resolution(lists_dir, safe_name.lower(), existing, incoming)
                target.write_text("\n".join(merged) + ("\n" if merged else ""), encoding="utf-8")
        self._merge_selected_service_hosts(active_root)

    def _merge_selected_service_hosts(self, active_root: Path) -> None:
        selected_ids = list(self.settings.get().selected_service_ids or [])
        incoming: list[str] = []
        for service_id in selected_ids:
            rule = SERVICE_RULES.get(str(service_id))
            if rule is None:
                continue
            incoming.extend(rule.hosts)
        if not incoming:
            return
        service_dir = active_root / ".service"
        service_dir.mkdir(parents=True, exist_ok=True)
        target = service_dir / "hosts"
        existing = self._read_hosts_lines(target)
        merged: list[str] = []
        seen: set[str] = set()
        for line in [*existing, *incoming]:
            if not line.strip() or line.lstrip().startswith("#"):
                merged.append(line)
                continue
            key = " ".join(line.split()).lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(line)
        target.write_text("\n".join(merged) + ("\n" if merged else ""), encoding="utf-8")

    def _read_hosts_lines(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        return [raw.rstrip() for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines()]

    def _apply_selected_service_command_extensions(self, command: list[str], *, lists_dir: Path) -> list[str]:
        if not command:
            return command
        selected_ids = list(self.settings.get().selected_service_ids or [])
        extra_args: list[str] = []
        seen_segments: set[tuple[str, ...]] = set()
        for service_id in selected_ids:
            rule = SERVICE_RULES.get(str(service_id))
            if rule is None or not rule.winws_args:
                continue
            segment = tuple(rule.winws_args)
            if segment in seen_segments:
                continue
            seen_segments.add(segment)
            for arg in segment:
                extra_args.append(str(arg).replace("{lists}", str(lists_dir)))
        if not extra_args:
            return command
        return [*command, *extra_args]

    def _read_list_lines(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        lines: list[str] = []
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line:
                continue
            lines.append(line)
        return lines

    def auto_select_working_general(self) -> dict[str, object] | None:
        options = self.list_zapret_generals()
        if not options:
            return None
        original = self.settings.get().selected_zapret_general
        best_result: dict[str, object] | None = None
        for option in options:
            outcome = self._run_general_connectivity_check(option["id"])
            if best_result is None or int(outcome.get("passed_targets", 0)) > int(best_result.get("passed_targets", 0)):
                best_result = {
                    "id": option["id"],
                    "status": outcome["status"],
                    "passed_targets": outcome.get("passed_targets", 0),
                    "total_targets": outcome.get("total_targets", 0),
                }
            if outcome["status"] == "ok":
                self.stop_component("zapret")
                self.logging.log("info", "Auto-selected zapret general", general=option["id"])
                return {
                    "id": option["id"],
                    "status": "ok",
                    "passed_targets": outcome.get("passed_targets", 0),
                    "total_targets": outcome.get("total_targets", 0),
                }
            self.stop_component("zapret")
        if best_result is not None and best_result.get("id"):
            self.settings.update(selected_zapret_general=str(best_result["id"]))
            return best_result
        self.settings.update(selected_zapret_general=original)
        return None

    def _capture_diagnostic_settings(self) -> dict[str, object]:
        settings = self.settings.get()
        return {
            "selected_zapret_general": settings.selected_zapret_general,
            "zapret_ipset_mode": settings.zapret_ipset_mode,
            "zapret_game_filter_mode": settings.zapret_game_filter_mode,
            "zapret_udp_exclude_ports": settings.zapret_udp_exclude_ports,
        }

    def _restore_diagnostic_settings(self, snapshot: dict[str, object]) -> None:
        self.settings.update(
            selected_zapret_general=str(snapshot.get("selected_zapret_general", "") or ""),
            zapret_ipset_mode=str(snapshot.get("zapret_ipset_mode", "loaded") or "loaded"),
            zapret_game_filter_mode=str(snapshot.get("zapret_game_filter_mode", "disabled") or "disabled"),
            zapret_udp_exclude_ports=str(snapshot.get("zapret_udp_exclude_ports", "51820") or "51820"),
        )

    def _prepare_diagnostic_runtime(self, *, general_id: str, ipset_mode: str, game_mode: str) -> bool:
        original_running = self._is_image_running("winws.exe")
        if original_running:
            self.stop_component("zapret")
        self.settings.update(
            selected_zapret_general=general_id,
            zapret_ipset_mode=ipset_mode,
            zapret_game_filter_mode=game_mode,
        )
        return original_running

    def run_single_general_diagnostic(
        self,
        general_id: str,
        *,
        ipset_mode: str = "loaded",
        game_mode: str = "tcpudp",
        progress_callback: callable | None = None,
        stop_callback: callable | None = None,
    ) -> dict[str, object]:
        options = {item["id"]: item for item in self.list_zapret_generals()}
        option = options.get(general_id)
        if option is None:
            return {"status": "error", "error": "general not found", "passed_targets": 0, "total_targets": 0}
        settings_snapshot = self._capture_diagnostic_settings()
        self._diagnostic_runtime_override = True
        original_running = self._prepare_diagnostic_runtime(
            general_id=general_id,
            ipset_mode=ipset_mode,
            game_mode=game_mode,
        )
        try:
            outcome = self._run_general_connectivity_check(
                general_id,
                stop_callback=stop_callback,
                targets=self._load_standard_test_targets(),
                progress_callback=progress_callback,
            )
            return {
                "id": option["id"],
                "name": option["name"],
                "bundle": option["bundle"],
                "status": str(outcome["status"]),
                "error": str(outcome.get("error", "")),
                "passed_targets": int(outcome.get("passed_targets", 0)),
                "total_targets": int(outcome.get("total_targets", 0)),
                "failed_targets": list(outcome.get("failed_targets", []) or []),
                "ipset_mode": ipset_mode,
                "game_mode": game_mode,
            }
        finally:
            self.stop_component("zapret")
            self._restore_diagnostic_settings(settings_snapshot)
            self._diagnostic_runtime_override = False
            if original_running and str(settings_snapshot.get("selected_zapret_general", "")):
                self.start_component("zapret")

    def run_general_diagnostics(
        self,
        progress_callback: callable | None = None,
        stop_callback: callable | None = None,
    ) -> list[dict[str, str]]:
        options = self.list_zapret_generals()
        options = prioritize_generals_for_services(options, self.settings.get().selected_service_ids)
        if not options:
            return []

        settings_snapshot = self._capture_diagnostic_settings()
        original_running = self._is_image_running("winws.exe")
        results: list[dict[str, str]] = []
        targets = self._load_standard_test_targets()
        per_general_steps = max(2, len(targets) + 1)
        total_steps = len(options) * per_general_steps

        try:
            self._diagnostic_runtime_override = True
            if original_running:
                self.stop_component("zapret")
            for index, option in enumerate(options, start=1):
                if stop_callback is not None and stop_callback():
                    break
                self.settings.update(
                    selected_zapret_general=option["id"],
                    zapret_ipset_mode=str(option.get("ipset_mode", "loaded") or "loaded"),
                    zapret_game_filter_mode=str(option.get("game_mode", "tcpudp") or "tcpudp"),
                )
                base_step = (index - 1) * per_general_steps
                if progress_callback is not None:
                    progress_callback(base_step + 1, total_steps, option["name"])
                outcome = self._run_general_connectivity_check(
                    option["id"],
                    stop_callback=stop_callback,
                    targets=targets,
                    progress_callback=(
                        lambda completed, total, target_name, *, _base=base_step, _steps=per_general_steps, _option=option: (
                            progress_callback(
                                min(_base + 1 + completed, _base + _steps),
                                total_steps,
                                f"{_option['name']} - {target_name} ({completed}/{total})",
                            )
                            if progress_callback is not None
                            else None
                        )
                    ),
                )
                if progress_callback is not None:
                    progress_callback(base_step + per_general_steps, total_steps, option["name"])
                results.append(
                    {
                        "id": option["id"],
                        "name": option["name"],
                        "bundle": option["bundle"],
                        "status": str(outcome["status"]),
                        "error": str(outcome.get("error", "")),
                        "passed_targets": str(outcome.get("passed_targets", 0)),
                        "total_targets": str(outcome.get("total_targets", 0)),
                        "failed_targets": list(outcome.get("failed_targets", []) or []),
                        "ipset_mode": str(option.get("ipset_mode", "loaded") or "loaded"),
                        "game_mode": str(option.get("game_mode", "tcpudp") or "tcpudp"),
                    }
                )
                self.stop_component("zapret")
        finally:
            self._diagnostic_runtime_override = False
            self._restore_diagnostic_settings(settings_snapshot)
            if original_running and str(settings_snapshot.get("selected_zapret_general", "")):
                self.start_component("zapret")

        return results

    def run_settings_diagnostics(
        self,
        progress_callback: callable | None = None,
        stop_callback: callable | None = None,
    ) -> dict[str, object]:
        original = self.settings.get()
        general_id = str(original.selected_zapret_general or "").strip()
        if not general_id:
            return {"results": [], "status": "error", "error": "No selected general"}
        ipset_modes = ["loaded", "none", "any"]
        game_modes = ["disabled", "tcpudp", "tcp", "udp"]
        combinations = [(ipset, game) for ipset in ipset_modes for game in game_modes]
        targets = self._load_standard_test_targets()
        results: list[dict[str, object]] = []
        total = max(1, len(combinations))
        original_running = self._is_image_running("winws.exe")
        try:
            if original_running:
                self.stop_component("zapret")
            for index, (ipset_mode, game_mode) in enumerate(combinations, start=1):
                if stop_callback is not None and stop_callback():
                    break
                self.settings.update(
                    selected_zapret_general=general_id,
                    zapret_ipset_mode=ipset_mode,
                    zapret_game_filter_mode=game_mode,
                )
                started_at = time.time()
                outcome = self._run_general_connectivity_check(general_id, stop_callback=stop_callback, targets=targets)
                elapsed = round(time.time() - started_at, 2)
                passed = int(outcome.get("passed_targets", 0))
                total_targets = int(outcome.get("total_targets", 0))
                results.append(
                    {
                        "ipset_mode": ipset_mode,
                        "game_mode": game_mode,
                        "status": str(outcome.get("status", "error")),
                        "passed_targets": passed,
                        "total_targets": total_targets,
                        "elapsed": elapsed,
                    }
                )
                if progress_callback is not None:
                    progress_callback(index, total, f"{ipset_mode} / {game_mode}")
                self.stop_component("zapret")
        finally:
            self.settings.update(
                selected_zapret_general=original.selected_zapret_general,
                zapret_ipset_mode=original.zapret_ipset_mode,
                zapret_game_filter_mode=original.zapret_game_filter_mode,
            )
            if original_running and general_id:
                self.start_component("zapret")

        ranked = sorted(
            results,
            key=lambda item: (-int(item.get("passed_targets", 0)), float(item.get("elapsed", 999999.0))),
        )
        best = ranked[0] if ranked else None
        return {"results": ranked, "best": best, "status": "ok" if ranked else "error"}

    def with_github_connectivity_recovery(self, operation: Callable[[], Any], purpose: str) -> Any:
        snapshot = self._capture_github_recovery_snapshot()
        errors: list[str] = []
        try:
            result = self._try_github_operation(operation, errors, f"{purpose}: current")
            if result[0]:
                return result[1]

            if bool(snapshot["was_running"]):
                self.stop_component("zapret")
                time.sleep(0.8)
                result = self._try_github_operation(operation, errors, f"{purpose}: stopped")
                if result[0]:
                    return result[1]

                self._restore_github_recovery_snapshot(snapshot, restart=True)
                time.sleep(1.2)
                result = self._try_github_operation(operation, errors, f"{purpose}: original-restarted")
                if result[0]:
                    return result[1]

            for candidate in self._github_recovery_candidates(snapshot):
                self.stop_component("zapret")
                self._apply_github_recovery_settings(
                    selected_zapret_general=str(candidate["general_id"]),
                    zapret_ipset_mode=str(candidate["ipset_mode"]),
                    zapret_game_filter_mode=str(candidate["game_mode"]),
                    zapret_udp_exclude_ports=str(snapshot["zapret_udp_exclude_ports"]),
                )
                state = self.start_component("zapret")
                if state.status != "running":
                    errors.append(f"{purpose}: failed to start temporary Zapret profile {candidate}")
                    continue
                time.sleep(1.0)
                result = self._try_github_operation(operation, errors, f"{purpose}: {candidate['label']}")
                if result[0]:
                    return result[1]
        finally:
            self._restore_github_recovery_snapshot(snapshot, restart=bool(snapshot["was_running"]))
        raise RuntimeError("; ".join(errors) or "GitHub request failed after Zapret recovery")

    def _try_github_operation(self, operation: Callable[[], Any], errors: list[str], label: str) -> tuple[bool, Any]:
        try:
            return True, operation()
        except Exception as error:
            errors.append(f"{label}: {error}")
            self.logging.log("warning", "GitHub recovery attempt failed", attempt=label, error=str(error))
            if not is_recoverable_github_error(error):
                raise
            time.sleep(0.8)
            return False, None

    def _capture_github_recovery_snapshot(self) -> dict[str, object]:
        settings = self.settings.get()
        return {
            "selected_zapret_general": settings.selected_zapret_general,
            "zapret_ipset_mode": settings.zapret_ipset_mode,
            "zapret_game_filter_mode": settings.zapret_game_filter_mode,
            "zapret_udp_exclude_ports": settings.zapret_udp_exclude_ports,
            "was_running": self._is_image_running("winws.exe"),
        }

    def _restore_github_recovery_snapshot(self, snapshot: dict[str, object], *, restart: bool) -> None:
        self.settings.update(
            selected_zapret_general=str(snapshot.get("selected_zapret_general", "") or ""),
            zapret_ipset_mode=str(snapshot.get("zapret_ipset_mode", "loaded") or "loaded"),
            zapret_game_filter_mode=str(snapshot.get("zapret_game_filter_mode", "disabled") or "disabled"),
            zapret_udp_exclude_ports=str(snapshot.get("zapret_udp_exclude_ports", "51820") or "51820"),
        )
        if restart:
            try:
                self.stop_component("zapret")
                self.start_component("zapret")
            except Exception as error:
                self.logging.log("warning", "Failed to restore Zapret after GitHub recovery", error=str(error))
        else:
            try:
                self.stop_component("zapret")
            except Exception:
                pass

    def _apply_github_recovery_settings(self, **changes: str) -> None:
        current = self.settings.get()
        for key, value in changes.items():
            setattr(current, key, value)

    def _github_recovery_candidates(self, snapshot: dict[str, object]) -> list[dict[str, str]]:
        current_general = str(snapshot.get("selected_zapret_general", "") or "").strip()
        base_general = ""
        for option in self.list_zapret_generals():
            if str(option.get("name", "")).lower() == "general.bat" and str(option.get("bundle_id", "")) == "base":
                base_general = str(option.get("id", "") or "")
                break
        favorite_general = next((item for item in self.settings.get().favorite_zapret_generals if str(item).strip()), "")
        general_fallback = favorite_general or base_general
        raw: list[tuple[str, str, str, str]] = [
            ("current loaded/disabled", current_general, "loaded", "disabled"),
            ("current any/disabled", current_general, "any", "disabled"),
            ("current loaded/tcp+udp", current_general, "loaded", "tcpudp"),
        ]
        if general_fallback and general_fallback != current_general:
            raw.append(("fallback loaded/disabled", general_fallback, "loaded", "disabled"))
        candidates: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for label, general_id, ipset_mode, game_mode in raw:
            if not general_id:
                continue
            key = (general_id, ipset_mode, game_mode)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "label": label,
                    "general_id": general_id,
                    "ipset_mode": ipset_mode,
                    "game_mode": game_mode,
                }
            )
        return candidates

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
        try:
            payload = self.github.github_json(api_url, timeout=20, purpose="tg-ws-proxy-release-metadata")
            if not isinstance(payload, dict):
                raise ValueError("Invalid tg-ws-proxy release metadata")
        except Exception as error:
            self.logging.log("warning", "TG WS Proxy release metadata fallback", error=str(error))
            return {
                "latest_version": "",
                "source_url": "https://codeload.github.com/Flowseal/tg-ws-proxy/zip/refs/heads/main",
                "exe_url": "https://github.com/Flowseal/tg-ws-proxy/releases/latest/download/TgWsProxy_windows.exe",
                "exe_name": "TgWsProxy_windows.exe",
            }
        latest_version = str(payload.get("tag_name") or payload.get("name") or "").strip().lstrip("v")
        assets = [item for item in list(payload.get("assets") or []) if isinstance(item, dict)]
        windows_asset = next(
            (
                item
                for item in assets
                if str(item.get("name", "")).strip().lower() == "tgwsproxy_windows.exe"
            ),
            None,
        )
        return {
            "latest_version": latest_version,
            "source_url": str(payload.get("zipball_url") or "").strip(),
            "exe_url": str((windows_asset or {}).get("browser_download_url", "")).strip(),
            "exe_name": str((windows_asset or {}).get("name", "")).strip() or "TgWsProxy_windows.exe",
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
        temp_root = Path(tempfile.mkdtemp(prefix="zapret_hub_zapret_update_"))
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
                except (HTTPError, URLError, TimeoutError, zipfile.BadZipFile, OSError) as error:
                    last_error = str(error)
                    self.logging.log("warning", "Zapret archive download failed", url=archive_url, error=last_error)
            if source_root is None:
                return {"status": "error", "error": last_error or "Invalid zapret archive"}
            if was_running:
                self.stop_component("zapret")
            backup = self.storage.create_backup(runtime_root, "pre-update-zapret")
            if runtime_root.exists():
                shutil.rmtree(runtime_root, ignore_errors=True)
            shutil.copytree(source_root, runtime_root, dirs_exist_ok=True)
            if version:
                self._patch_zapret_local_version(runtime_root, version)
            self.storage._ensure_default_bundled_mod("unified-by-goshkow", {
                "name": "Hub",
                "author": "goshkow",
                "description": "Bundled unified pack",
                "version": "1.9.9a-unified3",
                "source_url": "bundled://unified-by-goshkow",
            }, force_refresh=True)
            self.storage.ensure_layout()
            self._rebuild_visible_zapret_runtime_snapshot()
            if was_running:
                self.start_component("zapret")
            self.logging.log("info", "Zapret updated", version=version, backup=str(backup or ""))
            return {"status": "updated", "version": version or current_version}
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

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

    def _rebuild_visible_zapret_runtime_snapshot(self) -> None:
        selected = self._resolve_selected_general_option()
        if selected is not None:
            active_root = self._prepare_active_zapret_runtime(
                selected_bundle_root=Path(selected["path"]).parent,
                selected_bundle_id=str(selected.get("bundle_id", "")),
                selected_script_name=Path(selected["path"]).name,
            )
            self._apply_zapret_runtime_switches(active_root)
            self._ensure_zapret_user_lists(active_root / "lists")
            self._materialize_visible_merged_runtime(active_root)
            self._reset_active_runtime_dir(active_root)
            return
        base_root = self.storage.paths.runtime_dir / "zapret-discord-youtube"
        if base_root.exists():
            target_root = self.storage.paths.merged_runtime_dir / "zapret"
            if target_root.exists():
                shutil.rmtree(target_root, ignore_errors=True)
            shutil.copytree(base_root, target_root, dirs_exist_ok=True, ignore=self._runtime_copy_ignore)

    def rebuild_zapret_runtime_snapshot(self) -> None:
        self._rebuild_visible_zapret_runtime_snapshot()

    def update_tg_ws_proxy_runtime(self) -> dict[str, str]:
        release = self.fetch_latest_tg_ws_proxy_release()
        latest_version = str(release.get("latest_version", "")).strip()
        current_version = self.storage._detect_tgws_version()
        if latest_version and current_version == latest_version:
            return {"status": "up-to-date", "version": current_version}
        source_url = str(release.get("source_url", "")).strip()
        exe_url = str(release.get("exe_url", "")).strip()
        if not source_url or not exe_url:
            return {"status": "error", "error": "No tg-ws-proxy source or Windows asset found"}

        runtime_root = self.storage.paths.runtime_dir / "tg-ws-proxy"
        was_running = False
        try:
            tg_state = next((item for item in self.list_states() if item.component_id == "tg-ws-proxy"), None)
            was_running = bool(tg_state and tg_state.status == "running")
        except Exception:
            was_running = False
        temp_root = Path(tempfile.mkdtemp(prefix="zapret_hub_tgws_update_"))
        try:
            source_zip = temp_root / "tg-ws-proxy.zip"
            self._download_to_file(source_url, source_zip, timeout=75)
            extract_root = temp_root / "extract"
            extract_root.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(source_zip, "r") as archive:
                archive.extractall(extract_root)
            source_root = next((p for p in extract_root.iterdir() if p.is_dir() and (p / "proxy").exists()), None)
            if source_root is None:
                return {"status": "error", "error": "Invalid tg-ws-proxy source archive"}

            windows_exe_path = temp_root / str(release.get("exe_name", "TgWsProxy_windows.exe"))
            self._download_to_file(exe_url, windows_exe_path, timeout=75)

            if was_running:
                self.stop_component("tg-ws-proxy")

            backup = self.storage.create_backup(runtime_root, "pre-update-tg-ws-proxy")
            staging_root = temp_root / "runtime_new"
            shutil.copytree(source_root, staging_root, dirs_exist_ok=True)
            (staging_root / "bin").mkdir(parents=True, exist_ok=True)
            shutil.copy2(windows_exe_path, staging_root / "bin" / "TgWsProxy_windows.exe")

            if runtime_root.exists():
                shutil.rmtree(runtime_root, ignore_errors=True)
            shutil.copytree(staging_root, runtime_root, dirs_exist_ok=True)
            init_py = runtime_root / "proxy" / "__init__.py"
            if latest_version and init_py.exists():
                try:
                    content = init_py.read_text(encoding="utf-8", errors="ignore")
                    content = re.sub(r'__version__\s*=\s*["\'].*?["\']', f'__version__ = "{latest_version}"', content, count=1)
                    init_py.write_text(content, encoding="utf-8")
                except Exception:
                    pass
            self.storage.ensure_layout()
            if was_running:
                self.start_component("tg-ws-proxy")
            self.logging.log(
                "info",
                "TG WS Proxy updated",
                version=latest_version,
                backup=str(backup or ""),
            )
            return {"status": "updated", "version": latest_version or current_version}
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def _cleanup_merged_runtime(self) -> None:
        self._cleanup_inactive_zapret_runtimes()
        current_root = self._current_zapret_runtime
        if current_root and current_root.exists():
            self._reset_active_runtime_dir(current_root)
        self._current_zapret_runtime = None

    def _run_general_connectivity_check(
        self,
        general_id: str,
        stop_callback: callable | None = None,
        targets: list[dict[str, str]] | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        self.settings.update(selected_zapret_general=general_id)
        state = self._start_zapret("zapret")
        if state.status != "running":
            return {
                "status": "error",
                "error": state.last_error or "failed to start",
                "passed_targets": 0,
                "total_targets": 0,
                "failed_targets": [],
            }

        targets = list(targets or self._load_standard_test_targets())
        if not targets:
            return {
                "status": "ok",
                "error": "",
                "passed_targets": 0,
                "total_targets": 0,
                "failed_targets": [],
            }

        if stop_callback is not None and stop_callback():
            return {
                "status": "cancelled",
                "error": "cancelled",
                "passed_targets": 0,
                "total_targets": len(targets),
                "failed_targets": [str(target.get("name", "")) for target in targets],
            }

        passed_targets = 0
        failed_names: list[str] = []
        completed_targets = 0
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(targets)))) as executor:
            future_map = {executor.submit(self._target_is_reachable, target): target for target in targets}
            for future in as_completed(future_map):
                if stop_callback is not None and stop_callback():
                    executor.shutdown(wait=False, cancel_futures=True)
                    return {
                        "status": "cancelled",
                        "error": "cancelled",
                        "passed_targets": passed_targets,
                        "total_targets": len(targets),
                        "failed_targets": failed_names,
                    }
                target = future_map[future]
                try:
                    ok = future.result()
                except Exception:
                    ok = False
                if ok:
                    passed_targets += 1
                else:
                    failed_names.append(str(target["name"]))
                completed_targets += 1
                if progress_callback is not None:
                    progress_callback(completed_targets, len(targets), str(target.get("name", "")))

        if failed_names:
            return {
                "status": "error",
                "error": f"failed targets: {', '.join(failed_names[:6])}",
                "passed_targets": passed_targets,
                "total_targets": len(targets),
                "failed_targets": failed_names,
            }
        return {
            "status": "ok",
            "error": "",
            "passed_targets": passed_targets,
            "total_targets": len(targets),
            "failed_targets": [],
        }

    def _load_standard_test_targets(self) -> list[dict[str, str]]:
        targets_file = self.storage.paths.runtime_dir / "zapret-discord-youtube" / "utils" / "targets.txt"
        targets: list[dict[str, str]] = []
        if targets_file.exists():
            pattern = re.compile(r'^\s*(.+?)\s*=\s*"(.+)"\s*$')
            for raw in targets_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                match = pattern.match(raw.strip())
                if not match:
                    continue
                name = match.group(1).strip()
                value = match.group(2).strip()
                targets.append(self._convert_test_target(name, value))
        if targets:
            return self._append_selected_service_test_targets(targets)

        defaults = [
            ("Google Main", "https://www.google.com"),
            ("Google DNS 8.8.8.8", "PING:8.8.8.8"),
        ]
        return self._append_selected_service_test_targets([self._convert_test_target(name, value) for name, value in defaults])

    def _append_selected_service_test_targets(self, targets: list[dict[str, str]]) -> list[dict[str, str]]:
        result = self._filter_unselected_service_test_targets(targets)
        seen: set[tuple[str, str]] = set()
        for target in result:
            marker = (
                str(target.get("type", "")),
                str(target.get("url") or target.get("host") or target.get("name") or ""),
            )
            seen.add(marker)
        for service_id in self.settings.get().selected_service_ids or []:
            rule = SERVICE_RULES.get(str(service_id))
            if rule is None:
                continue
            for name, value in rule.test_targets:
                converted = self._convert_test_target(name, value)
                marker = (
                    str(converted.get("type", "")),
                    str(converted.get("url") or converted.get("host") or converted.get("name") or ""),
                )
                if marker in seen:
                    continue
                seen.add(marker)
                result.append(converted)
        return result

    def _filter_unselected_service_test_targets(self, targets: list[dict[str, str]]) -> list[dict[str, str]]:
        selected = {str(item) for item in self.settings.get().selected_service_ids or []}
        service_domains: dict[str, set[str]] = {}
        for service_id, rule in SERVICE_RULES.items():
            domains = {item.lower().lstrip(".") for item in rule.list_general if "/" not in item and "*" not in item}
            for _name, value in rule.test_targets:
                converted = self._convert_test_target(_name, value)
                host = str(converted.get("host", "")).lower().lstrip(".")
                if host:
                    domains.add(host)
            service_domains[service_id] = domains

        filtered: list[dict[str, str]] = []
        for target in targets:
            host = str(target.get("host", "")).lower().lstrip(".")
            name = str(target.get("name", "")).lower()
            matched_service = ""
            if host:
                for service_id, domains in service_domains.items():
                    if any(host == domain or host.endswith(f".{domain}") for domain in domains):
                        matched_service = service_id
                        break
            if not matched_service:
                for service_id, domains in service_domains.items():
                    title_token = service_id.replace("-desktop", "").replace("-", " ")
                    if title_token and title_token in name:
                        matched_service = service_id
                        break
                    if any(domain.split(".", 1)[0] in name for domain in domains):
                        matched_service = service_id
                        break
            if matched_service and matched_service not in selected:
                continue
            filtered.append(target)
        return filtered

    def _convert_test_target(self, name: str, value: str) -> dict[str, str]:
        if value.upper().startswith("PING:"):
            host = value.split(":", 1)[1].strip()
            return {"name": name, "type": "ping", "host": host}
        host = value.replace("https://", "").replace("http://", "").split("/", 1)[0].strip()
        return {"name": name, "type": "url", "url": value, "host": host}

    def _target_is_reachable(self, target: dict[str, str]) -> bool:
        target_type = target.get("type", "url")
        if target_type == "ping":
            return self._ping_target(target.get("host", ""))

        url = target.get("url", "").strip()
        if not url:
            return False
        tests = [
            ["--http1.1"],
            ["--tlsv1.2", "--tls-max", "1.2"],
            ["--tlsv1.3", "--tls-max", "1.3"],
        ]
        for extra in tests:
            if self._curl_target(url, extra):
                return True
        return False

    def _curl_target(self, url: str, extra_args: list[str]) -> bool:
        curl_path = shutil.which("curl.exe") or shutil.which("curl")
        if not curl_path:
            return False
        proc = self._run_quiet(
            [
                curl_path,
                "-I",
                "-s",
                "--connect-timeout",
                "2",
                "-m",
                "3",
                "-o",
                "NUL",
                "-w",
                "%{http_code}",
                "--show-error",
                *extra_args,
                url,
            ]
        )
        code = (proc.stdout or "").strip()
        return proc.returncode == 0 and bool(code)

    def _ping_target(self, host: str) -> bool:
        if not host:
            return False
        proc = self._run_quiet(["ping", "-n", "1", "-w", "1200", host])
        return proc.returncode == 0

    def _build_zapret_args(self, bin_dir: Path, lists_dir: Path) -> list[str]:
        tls_google = str(bin_dir / "tls_clienthello_www_google_com.bin")
        tls_4pda = str(bin_dir / "tls_clienthello_4pda_to.bin")
        quic_google = str(bin_dir / "quic_initial_www_google_com.bin")
        list_general = str(lists_dir / "list-general.txt")
        list_general_user = str(lists_dir / "list-general-user.txt")
        list_exclude = str(lists_dir / "list-exclude.txt")
        list_exclude_user = str(lists_dir / "list-exclude-user.txt")
        ipset_all = str(lists_dir / "ipset-all.txt")
        ipset_all_user = str(lists_dir / "ipset-all-user.txt")
        ipset_exclude = str(lists_dir / "ipset-exclude.txt")
        ipset_exclude_user = str(lists_dir / "ipset-exclude-user.txt")

        return [
            "--wf-tcp=80,443,2053,2083,2087,2096,8443",
            "--wf-udp=443,19294-19344,50000-50100",
            "--filter-udp=443",
            f"--hostlist={list_general}",
            f"--hostlist={list_general_user}",
            f"--hostlist-exclude={list_exclude}",
            f"--hostlist-exclude={list_exclude_user}",
            f"--ipset-exclude={ipset_exclude}",
            f"--ipset-exclude={ipset_exclude_user}",
            "--dpi-desync=fake",
            "--dpi-desync-repeats=6",
            f"--dpi-desync-fake-quic={quic_google}",
            "--new",
            "--filter-udp=19294-19344,50000-50100",
            "--filter-l7=discord,stun",
            "--dpi-desync=fake",
            "--dpi-desync-repeats=6",
            "--new",
            "--filter-tcp=2053,2083,2087,2096,8443",
            "--hostlist-domains=discord.media",
            "--dpi-desync=multisplit",
            "--dpi-desync-split-seqovl=681",
            "--dpi-desync-split-pos=1",
            f"--dpi-desync-split-seqovl-pattern={tls_google}",
            "--new",
            "--filter-tcp=443",
            f"--hostlist={str(lists_dir / 'list-google.txt')}",
            "--ip-id=zero",
            "--dpi-desync=multisplit",
            "--dpi-desync-split-seqovl=681",
            "--dpi-desync-split-pos=1",
            f"--dpi-desync-split-seqovl-pattern={tls_google}",
            "--new",
            "--filter-tcp=80,443",
            f"--hostlist={list_general}",
            f"--hostlist={list_general_user}",
            f"--hostlist-exclude={list_exclude}",
            f"--hostlist-exclude={list_exclude_user}",
            f"--ipset-exclude={ipset_exclude}",
            f"--ipset-exclude={ipset_exclude_user}",
            "--dpi-desync=multisplit",
            "--dpi-desync-split-seqovl=568",
            "--dpi-desync-split-pos=1",
            f"--dpi-desync-split-seqovl-pattern={tls_4pda}",
            "--new",
            "--filter-udp=443",
            f"--ipset={ipset_all}",
            f"--ipset={ipset_all_user}",
            f"--hostlist-exclude={list_exclude}",
            f"--hostlist-exclude={list_exclude_user}",
            f"--ipset-exclude={ipset_exclude}",
            f"--ipset-exclude={ipset_exclude_user}",
            "--dpi-desync=fake",
            "--dpi-desync-repeats=6",
            f"--dpi-desync-fake-quic={quic_google}",
            "--new",
            "--filter-tcp=80,443,8443",
            f"--ipset={ipset_all}",
            f"--ipset={ipset_all_user}",
            f"--hostlist-exclude={list_exclude}",
            f"--hostlist-exclude={list_exclude_user}",
            f"--ipset-exclude={ipset_exclude}",
            f"--ipset-exclude={ipset_exclude_user}",
            "--dpi-desync=multisplit",
            "--dpi-desync-split-seqovl=568",
            "--dpi-desync-split-pos=1",
            f"--dpi-desync-split-seqovl-pattern={tls_4pda}",
            "--new",
            "--filter-tcp=1024-65535",
            f"--ipset={ipset_all}",
            f"--ipset={ipset_all_user}",
            f"--ipset-exclude={ipset_exclude}",
            f"--ipset-exclude={ipset_exclude_user}",
            "--dpi-desync=multisplit",
            "--dpi-desync-any-protocol=1",
            "--dpi-desync-cutoff=n3",
            "--dpi-desync-split-seqovl=568",
            "--dpi-desync-split-pos=1",
            f"--dpi-desync-split-seqovl-pattern={tls_4pda}",
            "--new",
            "--filter-udp=1024-65535",
            f"--ipset={ipset_all}",
            f"--ipset={ipset_all_user}",
            f"--ipset-exclude={ipset_exclude}",
            f"--ipset-exclude={ipset_exclude_user}",
            "--dpi-desync=fake",
            "--dpi-desync-repeats=12",
            "--dpi-desync-any-protocol=1",
            f"--dpi-desync-fake-unknown-udp={quic_google}",
            "--dpi-desync-cutoff=n2",
        ]

    def _ensure_zapret_user_lists(self, lists_dir: Path) -> None:
        defaults = {
            "ipset-all-user.txt": "",
            "ipset-exclude-user.txt": "",
            "list-general-user.txt": "",
            "list-exclude-user.txt": "",
        }
        for filename, content in defaults.items():
            source = self.storage.paths.configs_dir / filename
            target = lists_dir / filename
            if source.exists():
                try:
                    target.write_text(source.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
                    continue
                except Exception:
                    pass
            if not target.exists():
                target.write_text(content, encoding="utf-8")

    def _is_image_running(self, image_name: str) -> bool:
        proc = self._run_quiet(["tasklist", "/FI", f"IMAGENAME eq {image_name}"])
        output = (proc.stdout or "").lower()
        return image_name.lower() in output

    def _kill_image(self, image_name: str) -> None:
        self._run_quiet(["taskkill", "/IM", image_name, "/F", "/T"])

    def _force_stop_zapret_runtime(self) -> None:
        process = self._processes.get("zapret")
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        if process and process.pid:
            self._run_quiet(["taskkill", "/PID", str(process.pid), "/F", "/T"])
        self._cleanup_zapret_driver_services(self._current_zapret_runtime)
        for _ in range(8):
            self._kill_image("winws.exe")
            if not self._is_image_running("winws.exe"):
                break
            time.sleep(0.35)
        self._processes.pop("zapret", None)
        self._current_zapret_runtime = None

    def _reset_active_runtime_dir(self, active_root: Path) -> None:
        driver_marker = active_root / ".driver_path_in_use"
        if driver_marker.exists() and (active_root / "bin" / "WinDivert64.sys").exists():
            self._cleanup_zapret_driver_services(active_root)
            if self._driver_service_references_runtime(active_root):
                self.logging.log(
                    "info",
                    "Keeping Zapret active runtime path because a driver service still references it",
                    path=str(active_root),
                )
                return
        for _ in range(6):
            try:
                shutil.rmtree(active_root, ignore_errors=False)
                return
            except PermissionError:
                self._force_stop_zapret_runtime()
                self._cleanup_zapret_driver_services(active_root)
                time.sleep(0.35)
            except Exception:
                shutil.rmtree(active_root, ignore_errors=True)
                if not active_root.exists():
                    return
        quarantine_root = Path(tempfile.gettempdir()) / "zapret_hub_runtime_cleanup"
        quarantine_root.mkdir(parents=True, exist_ok=True)
        quarantine_target = quarantine_root / f"active_zapret_{int(time.time() * 1000)}"
        try:
            shutil.move(str(active_root), str(quarantine_target))
            shutil.rmtree(quarantine_target, ignore_errors=True)
        except Exception:
            shutil.rmtree(active_root, ignore_errors=True)

    def _next_active_runtime_dir(self) -> Path:
        self.storage.paths.merged_runtime_dir.mkdir(parents=True, exist_ok=True)
        return self.storage.paths.merged_runtime_dir / f"active_zapret_{int(time.time() * 1000)}"

    def _cleanup_inactive_zapret_runtimes(self) -> None:
        merged_root = self.storage.paths.merged_runtime_dir
        if not merged_root.exists():
            return
        current_root = self._current_zapret_runtime.resolve() if self._current_zapret_runtime and self._current_zapret_runtime.exists() else None
        for candidate in merged_root.glob("active_zapret*"):
            try:
                if current_root and candidate.resolve() == current_root:
                    continue
            except Exception:
                pass
            self._reset_active_runtime_dir(candidate)

    def _zapret_service_exists(self) -> bool:
        proc = self._run_quiet(["sc", "query", "zapret"])
        return proc.returncode == 0

    def _cleanup_zapret_driver_services(self, runtime_root: Path | None = None) -> None:
        for service_name in _ZAPRET_DRIVER_SERVICE_NAMES:
            if not self._service_exists(service_name):
                continue
            if service_name.lower() != "zapret":
                image_path = self._service_image_path(service_name)
                if not image_path:
                    continue
                if image_path and runtime_root is not None and not self._path_mentions_runtime(image_path, runtime_root):
                    continue
                if image_path and runtime_root is None and not self._path_mentions_runtime(
                    image_path,
                    self.storage.paths.install_root,
                ):
                    continue
            self._run_quiet(["sc", "stop", service_name])
            self._run_quiet(["sc", "delete", service_name])

    def _driver_service_references_runtime(self, runtime_root: Path) -> bool:
        for service_name in _ZAPRET_DRIVER_SERVICE_NAMES:
            if not self._service_exists(service_name):
                continue
            image_path = self._service_image_path(service_name)
            if image_path and self._path_mentions_runtime(image_path, runtime_root):
                return True
        return False

    def _service_exists(self, service_name: str) -> bool:
        proc = self._run_quiet(["sc", "query", service_name])
        return proc.returncode == 0

    def _service_image_path(self, service_name: str) -> str:
        proc = self._run_quiet(["sc", "qc", service_name])
        if proc.returncode != 0:
            return ""
        for line in (proc.stdout or "").splitlines():
            if "BINARY_PATH_NAME" not in line:
                continue
            return line.split(":", 1)[-1].strip().strip('"')
        return ""

    def _path_mentions_runtime(self, image_path: str, runtime_root: Path) -> bool:
        raw = image_path.strip().strip('"').lower()
        if not raw:
            return False
        try:
            runtime_text = str(runtime_root.resolve()).lower()
        except Exception:
            runtime_text = str(runtime_root).lower()
        return runtime_text in raw

    def _run_quiet(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            creationflags=self._creationflags,
            startupinfo=self._startupinfo,
        )

    def _is_port_listening(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.8):
                return True
        except OSError:
            return False

    def _open_source_log_stream(self, source: str):
        self._close_source_log_stream(source)
        path = Path(self.logging.source_log_path(source))
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a", encoding="utf-8", errors="ignore")
        handle.write(f"\n[{datetime.utcnow().isoformat()}] session-start\n")
        handle.flush()
        self._log_streams[source] = handle
        return handle

    def _recent_source_log_error(self, source: str) -> str:
        path = Path(self.logging.source_log_path(source))
        if not path.exists():
            return ""
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return ""
        for line in reversed(lines[-80:]):
            text = line.strip()
            if not text or text.startswith("["):
                continue
            lowered = text.lower()
            if "error" in lowered or "failed" in lowered or "windivert" in lowered:
                return text
        return ""

    def _close_source_log_stream(self, source: str) -> None:
        handle = self._log_streams.pop(source, None)
        if handle is None:
            return
        try:
            handle.flush()
            handle.close()
        except Exception:
            pass

    def _is_telegram_running(self) -> bool:
        for image_name in ("Telegram.exe", "Telegram Desktop.exe"):
            if self._is_image_running(image_name):
                return True
        return False

    def _telegram_desktop_candidates(self) -> list[Path]:
        candidates = [
            Path(os.environ.get("APPDATA", "")) / "Telegram Desktop" / "Telegram.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Telegram Desktop" / "Telegram.exe",
            Path(os.environ.get("ProgramFiles", "")) / "Telegram Desktop" / "Telegram.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Telegram Desktop" / "Telegram.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "Telegram.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "Telegram Desktop.exe",
        ]
        for image_name in ("Telegram.exe", "telegram.exe", "Telegram Desktop.exe"):
            resolved = shutil.which(image_name)
            if resolved:
                candidates.append(Path(resolved))
        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique

    def _start_telegram_desktop(self) -> tuple[bool, bool]:
        candidates = self._telegram_desktop_candidates()
        candidate_found = False
        for candidate in candidates:
            if candidate.exists():
                candidate_found = True
                try:
                    subprocess.Popen(
                        [str(candidate)],
                        creationflags=self._creationflags,
                        startupinfo=self._startupinfo,
                    )
                    self.logging.log("info", "Telegram launch requested", path=str(candidate))
                    return candidate_found, True
                except Exception as error:
                    self.logging.log("warning", "Failed to start Telegram", path=str(candidate), error=str(error))
        return candidate_found, False

    def _ensure_telegram_and_open_proxy_link(self, host: str, port: int, secret: str) -> dict[str, Any]:
        self.logging.log("info", "TG WS Proxy auto-connect requested", component_id="tg-ws-proxy", host=host, port=port)
        running_before = self._is_telegram_running()
        candidate_found = False
        launch_requested = False
        if not running_before:
            self.logging.log("info", "Telegram Desktop is not running, attempting to launch it", component_id="tg-ws-proxy")
            candidate_found, launch_requested = self._start_telegram_desktop()
            for _ in range(40):
                if self._is_telegram_running():
                    self.logging.log("info", "Telegram Desktop detected after launch", component_id="tg-ws-proxy")
                    break
                time.sleep(0.25)
        running_after = self._is_telegram_running()
        self.logging.log("info", "Sending proxy link to Telegram", component_id="tg-ws-proxy")
        link_opened = self._open_telegram_proxy_link(host=host, port=port, secret=secret)
        info = {
            "running_before": running_before,
            "running_after": running_after,
            "desktop_candidate_found": candidate_found,
            "launch_requested": launch_requested,
            "link_opened": link_opened,
            "missing": not running_after and not candidate_found and not link_opened,
        }
        self._telegram_proxy_launch_info = info
        if not running_after and not link_opened:
            self.logging.log("warning", "Telegram was not detected after proxy start", component_id="tg-ws-proxy")
        return info

    def _open_telegram_proxy_link(self, host: str, port: int, secret: str) -> bool:
        link = f"tg://proxy?server={host}&port={port}&secret=dd{secret}"
        try:
            if sys.platform.startswith("win"):
                os.startfile(link)  # type: ignore[attr-defined]
            else:
                webbrowser.open(link)
            self.logging.log("info", "Telegram proxy link opened", link=link)
            return True
        except Exception as error:
            self.logging.log("warning", "Failed to open Telegram proxy link", link=link, error=str(error))
            return False

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

from zapret_zen.domain import ComponentDefinition, ComponentState
from zapret_zen.platform import (
    IS_WINDOWS,
    IS_LINUX,
    dev_null,
    get_creation_flags,
    get_startup_info,
    has_zapret_rust,
    is_image_running as _platform_is_image_running,
    is_admin,
    kill_image_by_name as _platform_kill_image,
    kill_process_by_pid as _platform_kill_pid,
    zapret_binary_name,
    zapret_rust_binary_name,
)
from zapret_zen.runtime_env import is_packaged_runtime
from zapret_zen.services.github_network import GitHubNetworkClient, is_recoverable_github_error
from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.service_catalog import ALWAYS_APPLY_SERVICE_IDS
from zapret_zen.services.service_rules import SERVICE_RULES
from zapret_zen.services.settings import SettingsManager
from zapret_zen.services.storage import StorageManager
from zapret_zen.services.vpn_detector import VpnDetector
from zapret_zen.services.github_recovery import GitHubRecovery
from zapret_zen.services.tg_proxy_manager import TelegramProxyManager
from zapret_zen.services.runtime_diagnostics import RuntimeDiagnostics
from zapret_zen.services.runtime_updates import RuntimeUpdateManager
from zapret_zen.services.zapret_runtime import ZapretRuntimeBuilder

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
        if not IS_WINDOWS:
            self.kernel32 = None
            self.job = None
            return
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
        if not self.job or not IS_WINDOWS:
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
        self._job = _WindowsJob() if IS_WINDOWS else None
        self.github = GitHubNetworkClient(logging, recovery_runner=self.with_github_connectivity_recovery)
        self._creationflags = get_creation_flags()
        self._startupinfo = get_startup_info()
        self._app_started_services: set[str] = set()
        self.vpn_detector = VpnDetector(logging)
        self.runtime_builder = ZapretRuntimeBuilder(storage, logging, settings)
        self._batch_current_bundle_id: str | None = None
        self.tg_proxy_manager = TelegramProxyManager(storage, logging, settings)
        self.github_recovery = GitHubRecovery(
            logging, settings,
            start_component=self.start_component,
            stop_component=self.stop_component,
            is_image_running=self._is_image_running,
            list_zapret_generals=self.list_zapret_generals,
        )
        self.diagnostics = RuntimeDiagnostics(
            logging, settings,
            stop_component=self.stop_component,
            start_component=self.start_component,
            is_image_running=self._is_image_running,
            list_zapret_generals=self.list_zapret_generals,
            build_zapret_args=self._build_zapret_args,
            load_standard_test_targets=self._load_standard_test_targets,
            run_connectivity_check=self._run_general_connectivity_check,
            run_batch_connectivity_check=self._run_batch_connectivity_check,
            reset_batch_state=self._reset_batch_state,
            set_diagnostic_override=self._set_diagnostic_runtime_override,
        )
        self.updates = RuntimeUpdateManager(
            storage, logging, self.github,
            stop_component=self.stop_component,
            start_component=self.start_component,
            is_image_running=self._is_image_running,
            rebuild_snapshot=self.rebuild_zapret_runtime_snapshot,
            tg_running=self._tg_worker_alive,
        )

    def _tg_worker_alive(self) -> bool:
        worker = self._processes.get("tg-ws-proxy")
        return worker is not None and worker.poll() is None

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
                if IS_LINUX:
                    was_running = _platform_is_image_running("nfqws")
                else:
                    was_running = _platform_is_image_running("winws.exe")
                state.status = "running" if was_running else "stopped"
                state.pid = None
            elif component.id == "tg-ws-proxy":
                worker = self._processes.get(component.id)
                listening = self._is_port_listening(settings.tg_proxy_host, int(settings.tg_proxy_port))
                if (worker and worker.poll() is None) or listening:
                    state.status = "running"
                    state.pid = worker.pid if worker and worker.poll() is None else None
                elif state.status != "error":
                    state.status = "stopped"
                    state.pid = None
            elif component.id == "dns-manager":
                if IS_LINUX:
                    state.status = "unavailable"
                    state.last_error = "DNS Manager is not supported on Linux."
                else:
                    active = self._dns_manager_is_active()
                    state.status = "running" if active else "stopped"
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
            state = self._start_zapret(component_id)
            self._invalidate_state_cache()
            return state
        if component.id == "tg-ws-proxy":
            state = self._start_tg_ws_proxy(component_id)
            self._invalidate_state_cache()
            return state
        if component.id == "dns-manager":
            if IS_LINUX:
                state = ComponentState(
                    component_id=component_id,
                    status="error",
                    last_error="DNS Manager is not supported on Linux.",
                )
                self._states[component_id] = state
                self._invalidate_state_cache()
                return state
            state = self._start_dns_manager(component_id)
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
            if IS_LINUX:
                return self._stop_zapret_linux(component_id)
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
            if IS_WINDOWS and process and process.pid:
                self._run_quiet(["taskkill", "/PID", str(process.pid), "/F"])
            self._processes.pop(component_id, None)
            if IS_WINDOWS:
                self._kill_image("TgWsProxy_windows.exe")
            else:
                _platform_kill_image("tg-ws-proxy")
            self._close_source_log_stream("tg-ws-proxy")
            state.status = "stopped"
            state.pid = None
            self._states[component_id] = state
            self.logging.log("info", "TG WS Proxy stopped")
            self._invalidate_state_cache()
            return state

        if component_id == "dns-manager":
            if IS_LINUX:
                state = ComponentState(component_id=component_id, status="stopped")
                state.last_error = "DNS Manager is not supported on Linux."
                self._states[component_id] = state
                self._invalidate_state_cache()
                return state
            state = self._stop_dns_manager(component_id)
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

    def _stop_zapret_linux(self, component_id: str) -> ComponentState:
        state = self._states.get(component_id, ComponentState(component_id=component_id))
        process = self._processes.get(component_id)
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        _platform_kill_image("nfqws")
        self._close_source_log_stream("zapret")
        self._processes.pop(component_id, None)
        self._current_zapret_runtime = None
        still_running = _platform_is_image_running("nfqws")
        state.status = "stopped" if not still_running else "running"
        state.pid = None
        if state.status != "stopped":
            state.last_error = "Failed to stop nfqws"
        self._states[component_id] = state
        self.logging.log("info", "Zapret (Linux) stopped")
        self._invalidate_state_cache()
        return state

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
        elif component_id == "dns-manager":
            self.start_component(component_id)
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
        if IS_LINUX:
            return self._start_zapret_rust(component_id)
        return self._start_zapret_winws(component_id)

    def _start_zapret_rust(self, component_id: str) -> ComponentState:
        self.stop_component(component_id)

        runtime_dir = self.storage.paths.runtime_dir / "zapret-discord-youtube-rust"
        binary = runtime_dir / zapret_rust_binary_name()
        if not binary.exists():
            binary = self.storage.paths.runtime_dir / "bin" / zapret_rust_binary_name()
        if not binary.exists():
            state = ComponentState(
                component_id=component_id,
                status="error",
                last_error=f"{zapret_rust_binary_name()} not found. Install zapret-discord-youtube-rust into runtime/zapret-discord-youtube-rust/.",
            )
            self._states[component_id] = state
            self._invalidate_state_cache()
            return state

        selected_option = self._resolve_selected_general_option()
        strategy_name = ""
        if selected_option:
            strategy_name = Path(selected_option["path"]).stem

        cmd = [str(binary)]
        settings = self.settings.get()
        interface = (getattr(settings, "zapret_interface", "any") or "any").strip()
        if interface:
            cmd.extend(["--interface", interface])
        if strategy_name:
            cmd.extend(["--strategy", strategy_name])

        game_mode = (settings.zapret_game_filter_mode or "disabled").strip().lower()
        if game_mode in ("all", "tcp", "tcpudp"):
            cmd.append("--gamefiltertcp")
            cmd.append("--gamefilterudp")
        elif game_mode == "tcp":
            cmd.append("--gamefiltertcp")
        elif game_mode == "udp":
            cmd.append("--gamefilterudp")

        self.logging.log("info", "Zapret-rust starting", command=" ".join(cmd))
        process = subprocess.Popen(
            cmd,
            cwd=str(runtime_dir),
            stdout=self._open_source_log_stream("zapret"),
            stderr=subprocess.STDOUT,
        )
        if self._job:
            self._job.assign_pid(process.pid)
        self._processes[component_id] = process

        running = False
        for _ in range(24):
            if _platform_is_image_running("nfqws"):
                running = True
                break
            time.sleep(0.25)

        if running:
            state = ComponentState(component_id=component_id, status="running", pid=process.pid)
            self.logging.log("info", "Zapret-rust started", binary=str(binary))
        else:
            self._close_source_log_stream("zapret")
            log_hint = self._recent_source_log_error("zapret")
            error_message = log_hint or (
                "zapret-rust did not start. Ensure nfqws has CAP_NET_ADMIN "
                "(sudo setcap cap_net_admin+ep $(which nfqws)) and nftables or iptables is installed."
            )
            state = ComponentState(
                component_id=component_id,
                status="error",
                last_error=error_message,
            )
            self.logging.log("error", "Zapret-rust failed to start", error=error_message)
        self._states[component_id] = state
        self._invalidate_state_cache()
        return state

    def _start_zapret_winws(self, component_id: str) -> ComponentState:
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
                self._track_active_driver_services()
                state = ComponentState(component_id=component_id, status="running", pid=process.pid)
                self.logging.log("info", "Zapret started", script=str(active_script), command=winws_command[0])
            else:
                self._close_source_log_stream("zapret")
                log_hint = self._recent_source_log_error("zapret")
                error_message = log_hint or "winws did not start. Run app as Administrator and check antivirus exclusions for WinDivert."
                if not log_hint:
                    started_then_exited = self._check_log_hint("zapret", ("windivert", "capture is started"))
                    if started_then_exited:
                        error_message = "winws started but exited immediately. This script may not target the tested sites."
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

    def _start_zapret_for_batch(
        self,
        component_id: str,
        *,
        general_id: str,
        ipset_mode: str,
        game_mode: str,
    ) -> ComponentState:
        self.stop_component(component_id)
        selected_option = self._resolve_selected_general_option()
        if selected_option is None:
            state = ComponentState(component_id=component_id, status="error", last_error="No general script found.")
            self._states[component_id] = state
            return state
        selected_script = Path(selected_option["path"])
        selected_bundle_root = Path(selected_script).parent
        selected_bundle_id = str(selected_option.get("bundle_id", "") or "")
        same_bundle = (
            self._batch_current_bundle_id is not None
            and self._batch_current_bundle_id == selected_bundle_id
            and self._current_zapret_runtime is not None
            and self._current_zapret_runtime.exists()
        )
        active_root: Path | None = None
        process: subprocess.Popen[Any] | None = None
        try:
            if same_bundle:
                active_root = self._current_zapret_runtime
                self._apply_zapret_runtime_switches(active_root)
            else:
                active_root = self._prepare_active_zapret_runtime(
                    selected_bundle_root=selected_bundle_root,
                    selected_bundle_id=selected_bundle_id,
                    selected_script_name=selected_script.name,
                )
                self._current_zapret_runtime = active_root
                self._apply_zapret_runtime_switches(active_root)
                self._ensure_zapret_user_lists(active_root / "lists")
                self._materialize_visible_merged_runtime(active_root)
            self._batch_current_bundle_id = selected_bundle_id
            active_script = active_root / selected_script.name
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
            for _ in range(10):
                if self._is_image_running("winws.exe"):
                    running = True
                    break
                time.sleep(0.2)
            if running:
                try:
                    (active_root / ".driver_path_in_use").write_text(datetime.utcnow().isoformat(), encoding="utf-8")
                except Exception:
                    pass
                self._track_active_driver_services()
                state = ComponentState(component_id=component_id, status="running", pid=process.pid)
            else:
                self._close_source_log_stream("zapret")
                log_hint = self._recent_source_log_error("zapret")
                error_message = log_hint or "winws did not start."
                state = ComponentState(component_id=component_id, status="error", last_error=error_message)
        except OSError as error:
            if getattr(error, "winerror", 0) == 740:
                state = ComponentState(
                    component_id=component_id,
                    status="error",
                    last_error="Administrator rights are required for winws/WinDivert.",
                )
            else:
                state = ComponentState(component_id=component_id, status="error", last_error=str(error))
        except Exception as error:
            state = ComponentState(component_id=component_id, status="error", last_error=str(error))
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
            if not same_bundle:
                self._force_stop_zapret_runtime()
                if active_root is not None:
                    self._reset_active_runtime_dir(active_root)
                self._current_zapret_runtime = None
        self._states[component_id] = state
        return state

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
        if not command or not IS_WINDOWS:
            return command
        try:
            vpn_data = self.vpn_detector.detect_vpn_priority_context()
        except Exception as error:
            self.logging.log("warning", "Failed to detect VPN priority context", error=str(error))
            return command

        adapter_indexes = [int(item) for item in vpn_data.get("adapter_indexes", []) if str(item).isdigit()]
        remote_ips = [str(item).strip() for item in vpn_data.get("remote_ips", []) if str(item).strip()]
        excluded_udp_ports = self.vpn_detector.parse_port_ranges(self.settings.get().zapret_udp_exclude_ports)
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
                ranges = self.vpn_detector.parse_port_ranges(value)
                if ranges:
                    filtered = self.vpn_detector.subtract_port_ranges(ranges, excluded_ranges)
                    value = self.vpn_detector.format_port_ranges(filtered) or "12"
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
        if not IS_WINDOWS:
            return {"adapter_indexes": [], "remote_ips": []}
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
        if not IS_WINDOWS:
            return ""
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

    def _set_diagnostic_runtime_override(self, enabled: bool) -> None:
        self._diagnostic_runtime_override = bool(enabled)

    QUIC_BLOCK_RULE_NAME = "Zapret-Zen Block QUIC"

    def _quic_block_rule_exists(self) -> bool:
        if not IS_WINDOWS:
            return False
        try:
            proc = self._run_quiet([
                "netsh", "advfirewall", "firewall", "show", "rule",
                f"name={self.QUIC_BLOCK_RULE_NAME}",
            ])
        except Exception as error:
            self.logging.log("warning", "Failed to query QUIC firewall rule", error=str(error))
            return False
        return proc.returncode == 0

    def set_quic_blocked(self, blocked: bool) -> bool:
        if not IS_WINDOWS:
            return True
        exists = self._quic_block_rule_exists()
        if blocked == exists:
            return True
        command = [
            "netsh", "advfirewall", "firewall",
            "add" if blocked else "delete", "rule",
            f"name={self.QUIC_BLOCK_RULE_NAME}",
        ]
        if blocked:
            command += ["dir=out", "action=block", "protocol=udp", "remoteport=443"]
        try:
            proc = self._run_quiet(command)
        except Exception as error:
            self.logging.log("warning", "Failed to change QUIC firewall rule", enable=blocked, error=str(error))
            return False
        ok = proc.returncode == 0
        if ok:
            self.logging.log("info", "QUIC firewall rule updated", enabled=blocked)
        else:
            self.logging.log(
                "warning",
                "QUIC firewall rule change failed",
                enabled=blocked,
                output=(proc.stderr or proc.stdout or "").strip(),
            )
        return ok

    def ensure_quic_firewall_state(self) -> None:
        try:
            desired = bool(self.settings.get().zapret_block_quic)
        except Exception:
            return
        self.set_quic_blocked(desired)

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
        if IS_WINDOWS:
            self._kill_image("TgWsProxy_windows.exe")
        else:
            _platform_kill_image("tg-ws-proxy")
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
        self.logging.log("info", "TG WS Proxy starting", command=" ".join(command))
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
        exit_code = None
        for _ in range(16):
            exit_code = process.poll()
            if exit_code is not None:
                break
            if self._is_port_listening(listen_host, listen_port):
                ready = True
                break
            time.sleep(0.35)
        if not ready:
            if exit_code is None:
                exit_code = process.poll()
            error_hint = "TG WS Proxy worker did not open listening port."
            worker_error_log = self.storage.paths.logs_dir / "tg_worker_error.log"
            if worker_error_log.exists():
                try:
                    error_hint = worker_error_log.read_text(encoding="utf-8")[-1000:]
                except Exception:
                    pass
            if exit_code is not None:
                error_hint += f" (exit code: {exit_code})"
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.wait(timeout=2)
            except Exception:
                pass
            self._close_source_log_stream("tg-ws-proxy")
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

    def _import_dns_manager(self):
        script_path = self.storage.paths.runtime_dir / "dns_manager.py"
        if not script_path.exists():
            raise FileNotFoundError(f"dns_manager.py not found at {script_path}")
        import importlib.util
        spec = importlib.util.spec_from_file_location("dns_manager", str(script_path))
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load dns_manager.py from {script_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _dns_manager_state_file(self) -> Path:
        return self.storage.paths.data_dir / "dns_manager_state.json"

    def _dns_manager_is_active(self) -> bool:
        state_file = self._dns_manager_state_file()
        if not state_file.exists():
            return False
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return bool(data.get("active", False))
        except (json.JSONDecodeError, OSError):
            return False

    def _start_dns_manager(self, component_id: str) -> ComponentState:
        settings = self.settings.get()
        preset = (settings.selected_dns_preset or "").strip()
        if not preset:
            try:
                presets = self.list_dns_presets()
                if presets:
                    preset = str(presets[0].get("id", "")).strip()
            except Exception:
                preset = ""
        if not preset:
            state = ComponentState(
                component_id=component_id,
                status="error",
                last_error="No DNS preset selected.",
            )
            self._states[component_id] = state
            return state
        try:
            dns = self._import_dns_manager()
            state_file = self._dns_manager_state_file()

            if self._dns_manager_is_active():
                try:
                    state = dns.read_state(state_file)
                    adapters = state.get("previous_adapters")
                    if adapters:
                        try:
                            dns.restore_windows_dns(adapters)
                        except RuntimeError:
                            pass
                except Exception:
                    pass
                try:
                    dns.reset_windows_dns()
                except RuntimeError:
                    pass
                try:
                    dns.write_state(state_file, {"active": False})
                except Exception:
                    pass

            p = dns.PRESETS.get(preset)
            if p is None:
                raise ValueError(f"Unknown DNS preset: {preset}")

            adapters = dns.snapshot_windows_dns()
            if not adapters:
                raise RuntimeError("No active network adapters found for DNS snapshot")

            dns.apply_windows_dns(adapters, list(p["ipv4"]), list(p.get("ipv6", [])))
            dns.write_state(state_file, {
                "active": True,
                "servers": {"ipv4": list(p["ipv4"]), "ipv6": list(p.get("ipv6", [])), "source": preset},
                "previous_adapters": adapters,
                "snapshot_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
                "last_error": "",
            })

            state = ComponentState(component_id=component_id, status="running")
            self._states[component_id] = state
            self.logging.log("info", "DNS Manager applied preset", preset=preset)
            return state
        except Exception as error:
            state = ComponentState(
                component_id=component_id,
                status="error",
                last_error=str(error),
            )
            self._states[component_id] = state
            self.logging.log("error", "DNS Manager failed to apply preset", preset=preset, error=str(error))
            return state

    def _stop_dns_manager(self, component_id: str) -> ComponentState:
        state = ComponentState(component_id=component_id, status="stopped")
        try:
            dns = self._import_dns_manager()
            state_file = self._dns_manager_state_file()

            s = dns.read_state(state_file)
            adapters = s.get("previous_adapters")
            if adapters:
                try:
                    dns.restore_windows_dns(adapters)
                except RuntimeError as error:
                    self.logging.log("warning", "DNS Manager restore had issues", error=str(error))
            try:
                dns.reset_windows_dns()
            except RuntimeError as error:
                state.last_error = str(error)
                self.logging.log("warning", "DNS Manager reset had issues", error=str(error))
            dns.write_state(state_file, {
                "active": False,
                "reset_at": datetime.utcnow().isoformat(),
                "last_error": state.last_error,
            })
            self.logging.log("info", "DNS Manager stopped (DNS reset)")
        except Exception as error:
            state.last_error = str(error)
            try:
                dns = self._import_dns_manager()
                dns.write_state(self._dns_manager_state_file(), {
                    "active": False,
                    "reset_at": datetime.utcnow().isoformat(),
                    "last_error": str(error),
                })
            except Exception:
                pass
            self.logging.log("warning", "DNS Manager reset failed", error=str(error))
        self._states[component_id] = state
        return state

    def list_dns_presets(self) -> list[dict[str, str]]:
        try:
            dns = self._import_dns_manager()
            presets = []
            existing_ids: set[str] = set()
            for key, p in dns.PRESETS.items():
                presets.append({
                    "id": key,
                    "name": p.get("name", key),
                    "ipv4": ", ".join(p.get("ipv4", [])),
                    "ipv6": ", ".join(p.get("ipv6", [])),
                    "doh": p.get("doh", ""),
                })
                existing_ids.add(key)
            presets.extend(self._load_mod_dns_presets(existing_ids))
            return presets
        except Exception as error:
            self.logging.log("warning", "Failed to list DNS presets", error=str(error))
            return []

    def _load_mod_dns_presets(self, existing_ids: set[str]) -> list[dict[str, str]]:
        installed_path = self.storage.paths.data_dir / "installed_mods.json"
        if not installed_path.exists():
            return []
        try:
            installed = json.loads(installed_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(installed, list):
            return []
        presets: list[dict[str, str]] = []
        for mod_entry in installed:
            if not isinstance(mod_entry, dict):
                continue
            if not mod_entry.get("enabled", False):
                continue
            mod_path = Path(str(mod_entry.get("path", "")))
            dns_file = mod_path / "utils" / "dns_manager.json"
            if not dns_file.is_file():
                continue
            try:
                raw = json.loads(dns_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.logging.log("warning", "Failed to read dns_manager.json", mod_id=mod_entry.get("id", ""))
                continue
            if not isinstance(raw, list):
                continue
            mod_id = str(mod_entry.get("id", "mod"))
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                preset_id = str(entry.get("id", "")).strip()
                if not preset_id:
                    continue
                if preset_id in existing_ids:
                    continue
                existing_ids.add(preset_id)
                ipv4_list = entry.get("ipv4", [])
                ipv6_list = entry.get("ipv6", [])
                if not isinstance(ipv4_list, list):
                    ipv4_list = []
                if not isinstance(ipv6_list, list):
                    ipv6_list = []
                presets.append({
                    "id": preset_id,
                    "name": str(entry.get("name", preset_id)),
                    "ipv4": ", ".join(str(ip) for ip in ipv4_list),
                    "ipv6": ", ".join(str(ip) for ip in ipv6_list),
                    "doh": entry.get("doh", ""),
                })
        return presets

    def _build_worker_command(self, worker: str, **kwargs: Any) -> list[str]:
        cmd: list[str]
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
            install_root / ".venv" / "Scripts" / "pythonw.exe",
            install_root / ".venv" / "Scripts" / "python.exe",
            install_root / ".venv" / "bin" / "python",
            Path(sys.executable),
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return sys.executable

    def _get_zapret_bundles(self, enabled_only: bool, *, include_hidden_generals: bool = False) -> list[dict[str, Any]]:
        return self.runtime_builder.get_zapret_bundles(enabled_only, include_hidden_generals=include_hidden_generals)

    def _general_option_sort_key(self, item: dict[str, str]) -> tuple[int, int, str]:
        return self.runtime_builder.general_option_sort_key(item)

    def _resolve_selected_general_option(self) -> dict[str, str] | None:
        options = self.list_zapret_generals()
        if not options:
            return None
        settings = self.settings.get()
        selected = settings.selected_zapret_general
        picked = next((item for item in options if item["id"] == selected), None)
        if picked is None:
            picked = options[0]
        return picked

    def _prepare_active_zapret_runtime(self, selected_bundle_root: Path, selected_bundle_id: str, selected_script_name: str) -> Path:
        return self.runtime_builder.prepare_active_zapret_runtime(selected_bundle_root, selected_bundle_id, selected_script_name)

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
        selected_ids = list(dict.fromkeys([*ALWAYS_APPLY_SERVICE_IDS, *selected_ids]))
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
        selected_ids = list(dict.fromkeys([*ALWAYS_APPLY_SERVICE_IDS, *selected_ids]))
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
        return self.diagnostics.auto_select_working_general()

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
        if IS_LINUX:
            original_running = _platform_is_image_running("nfqws")
        else:
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
        return self.diagnostics.run_single_general_diagnostic(
            general_id, ipset_mode=ipset_mode, game_mode=game_mode,
            progress_callback=progress_callback, stop_callback=stop_callback,
        )

    def run_general_diagnostics(
        self,
        progress_callback: callable | None = None,
        stop_callback: callable | None = None,
    ) -> list[dict[str, str]]:
        return self.diagnostics.run_general_diagnostics(progress_callback=progress_callback, stop_callback=stop_callback)

    def run_general_diagnostic_batch(
        self,
        batch: list[dict[str, str]],
        *,
        targets: list[dict[str, str]] | None = None,
        progress_callback: callable | None = None,
        result_callback: callable | None = None,
        stop_callback: callable | None = None,
    ) -> list[dict[str, object]]:
        return self.diagnostics.run_general_diagnostic_batch(
            batch, targets=targets, progress_callback=progress_callback,
            result_callback=result_callback, stop_callback=stop_callback,
        )

    def run_settings_diagnostics(
        self,
        progress_callback: callable | None = None,
        stop_callback: callable | None = None,
    ) -> dict[str, object]:
        return self.diagnostics.run_settings_diagnostics(progress_callback=progress_callback, stop_callback=stop_callback)

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
        blocked_names: list[str] = []
        completed_targets = 0
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(targets)))) as executor:
            future_map = {executor.submit(self._check_target, target): target for target in targets}
            for future in as_completed(future_map):
                if stop_callback is not None and stop_callback():
                    executor.shutdown(wait=False, cancel_futures=True)
                    return {
                        "status": "cancelled",
                        "error": "cancelled",
                        "passed_targets": 0,
                        "total_targets": len(targets),
                        "failed_targets": [str(target.get("name", "")) for target in targets],
                        "blocked_targets": [],
                    }
                target = future_map[future]
                try:
                    result = future.result()
                except Exception:
                    result = "failed"
                if result == "ok":
                    passed_targets += 1
                elif result == "blocked":
                    blocked_names.append(str(target["name"]))
                else:
                    failed_names.append(str(target["name"]))
                completed_targets += 1
                if progress_callback is not None:
                    progress_callback(completed_targets, len(targets), str(target.get("name", "")))

        if failed_names or blocked_names:
            error_parts: list[str] = []
            if failed_names:
                error_parts.append(f"failed targets: {', '.join(failed_names[:6])}")
            if blocked_names:
                error_parts.append(f"explicitly blocked (HTTP 451): {', '.join(blocked_names[:6])}")
            return {
                "status": "error",
                "error": "; ".join(error_parts),
                "passed_targets": passed_targets,
                "total_targets": len(targets),
                "failed_targets": failed_names,
                "blocked_targets": blocked_names,
            }
        return {
            "status": "ok",
            "error": "",
            "passed_targets": passed_targets,
            "total_targets": len(targets),
            "failed_targets": [],
            "blocked_targets": [],
        }

    def _run_batch_connectivity_check(
        self,
        general_id: str,
        *,
        ipset_mode: str = "loaded",
        game_mode: str = "tcpudp",
        stop_callback: callable | None = None,
        targets: list[dict[str, str]] | None = None,
    ) -> dict[str, object]:
        self.settings.update(
            selected_zapret_general=general_id,
            zapret_ipset_mode=ipset_mode,
            zapret_game_filter_mode=game_mode,
        )
        state = self._start_zapret_for_batch(
            "zapret",
            general_id=general_id,
            ipset_mode=ipset_mode,
            game_mode=game_mode,
        )
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
                "failed_targets": [str(t.get("name", "")) for t in targets],
            }
        passed_targets = 0
        failed_names: list[str] = []
        blocked_names: list[str] = []
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(targets)))) as executor:
            future_map = {executor.submit(self._check_target, target): target for target in targets}
            for future in as_completed(future_map):
                if stop_callback is not None and stop_callback():
                    executor.shutdown(wait=False, cancel_futures=True)
                    return {
                        "status": "cancelled",
                        "error": "cancelled",
                        "passed_targets": 0,
                        "total_targets": len(targets),
                        "failed_targets": [str(t.get("name", "")) for t in targets],
                    }
                target = future_map[future]
                try:
                    result = future.result()
                except Exception:
                    result = "failed"
                if result == "ok":
                    passed_targets += 1
                elif result == "blocked":
                    blocked_names.append(str(target["name"]))
                else:
                    failed_names.append(str(target["name"]))
        if failed_names or blocked_names:
            error_parts: list[str] = []
            if failed_names:
                error_parts.append(f"failed targets: {', '.join(failed_names[:6])}")
            if blocked_names:
                error_parts.append(f"explicitly blocked (HTTP 451): {', '.join(blocked_names[:6])}")
            return {
                "status": "error",
                "error": "; ".join(error_parts),
                "passed_targets": passed_targets,
                "total_targets": len(targets),
                "failed_targets": failed_names,
                "blocked_targets": blocked_names,
            }
        return {
            "status": "ok",
            "error": "",
            "passed_targets": passed_targets,
            "total_targets": len(targets),
            "failed_targets": [],
            "blocked_targets": [],
        }

    def _reset_batch_state(self) -> None:
        self._batch_current_bundle_id = None
        self.stop_component("zapret")
        self._force_stop_zapret_runtime()
        self._current_zapret_runtime = None

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

    def with_domain_connectivity_recovery(
        self,
        domains: list[str] | tuple[str, ...] | str,
        operation: Callable[[], Any],
        purpose: str,
    ) -> Any:
        raw_domains: list[str]
        if isinstance(domains, str):
            raw_domains = [domains]
        else:
            raw_domains = list(domains)
        domain_list: list[str] = []
        for item in raw_domains:
            value = str(item or "").strip().lower()
            if value and value not in domain_list:
                domain_list.append(value)
        if not domain_list:
            return self.with_github_connectivity_recovery(operation, purpose)
        snapshot = self._capture_github_recovery_snapshot()
        errors: list[str] = []
        try:
            result = self._try_github_operation(operation, errors, f"{purpose}: direct")
            if result[0]:
                return result[1]
            state = self.start_component("zapret")
            if getattr(state, "status", None) != "running":
                raise RuntimeError(f"{purpose}: Zapret could not be enabled for domain bypass")
            time.sleep(1.0)
            self._inject_runtime_domains(domain_list)
            try:
                self.stop_component("zapret")
                state = self.start_component("zapret")
                if getattr(state, "status", None) != "running":
                    raise RuntimeError(f"{purpose}: Zapret restart with domains failed")
                time.sleep(1.0)
                result = self._try_github_operation(operation, errors, f"{purpose}: zapret+{','.join(domain_list)}")
                if result[0]:
                    return result[1]
            finally:
                self._remove_runtime_domains(domain_list)
        finally:
            self._restore_github_recovery_snapshot(snapshot, restart=bool(snapshot["was_running"]))
        raise RuntimeError("; ".join(errors) or "Download failed after domain bypass")

    def _inject_runtime_domains(self, domains: list[str]) -> None:
        overrides_path = self.storage.paths.data_dir / "file_overrides.json"
        raw = self.storage.read_json(overrides_path, default={}) or {}
        entry = raw.get("domains")
        entry = entry if isinstance(entry, dict) else {}
        added = {str(item).strip() for item in list(entry.get("added", []) or []) if str(item).strip()}
        for domain in domains:
            added.add(domain)
        entry["added"] = sorted(added)
        raw["domains"] = entry
        self.storage.write_json(overrides_path, raw)
        self.logging.log("info", "Temporary domains injected for download bypass", domains=list(domains))

    def _remove_runtime_domains(self, domains: list[str]) -> None:
        try:
            overrides_path = self.storage.paths.data_dir / "file_overrides.json"
            raw = self.storage.read_json(overrides_path, default={}) or {}
            entry = raw.get("domains")
            entry = entry if isinstance(entry, dict) else {}
            added = [str(item).strip() for item in list(entry.get("added", []) or []) if str(item).strip()]
            keep = [item for item in added if item not in set(domains)]
            entry["added"] = keep
            raw["domains"] = entry
            self.storage.write_json(overrides_path, raw)
        except Exception as error:
            self.logging.log("warning", "Failed to roll back temporary bypass domains", error=str(error))

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
        if IS_LINUX:
            was_running = _platform_is_image_running("nfqws")
        else:
            was_running = self._is_image_running("winws.exe")
        return {
            "selected_zapret_general": settings.selected_zapret_general,
            "zapret_ipset_mode": settings.zapret_ipset_mode,
            "zapret_game_filter_mode": settings.zapret_game_filter_mode,
            "zapret_udp_exclude_ports": settings.zapret_udp_exclude_ports,
            "was_running": was_running,
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
        for service_id in list(dict.fromkeys([*ALWAYS_APPLY_SERVICE_IDS, *list(self.settings.get().selected_service_ids or [])])):
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
            if matched_service and matched_service not in selected and str(matched_service) not in ALWAYS_APPLY_SERVICE_IDS:
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
        return self._check_target(target) == "ok"

    def _check_target(self, target: dict[str, str]) -> str:
        target_type = target.get("type", "url")
        if target_type == "ping":
            return "ok" if self._ping_target(target.get("host", "")) else "failed"
        url = target.get("url", "").strip()
        if not url:
            return "failed"
        tests = [
            ["--http1.1"],
            ["--tlsv1.2", "--tls-max", "1.2"],
            ["--tlsv1.3", "--tls-max", "1.3"],
        ]
        saw_451 = False
        for extra in tests:
            status = self._curl_status(url, extra)
            if status == 451:
                saw_451 = True
            elif status > 0:
                return "ok"
        return "blocked" if saw_451 else "failed"

    def _target_explicitly_blocked(self, target: dict[str, str]) -> bool:
        return self._check_target(target) == "blocked"

    def _curl_status(self, url: str, extra_args: list[str]) -> int:
        curl_path = shutil.which("curl.exe") or shutil.which("curl")
        if not curl_path:
            return 0
        null_device = dev_null()
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
                null_device,
                "-w",
                "%{http_code}",
                "--show-error",
                *extra_args,
                url,
            ]
        )
        code = (proc.stdout or "").strip()
        if not code or not code.isdigit():
            return 0
        return int(code)

    def _curl_target(self, url: str, extra_args: list[str]) -> bool:
        return self._curl_status(url, extra_args) > 0

    def _ping_target(self, host: str) -> bool:
        if not host:
            return False
        if IS_WINDOWS:
            proc = self._run_quiet(["ping", "-n", "1", "-w", "1200", host])
        else:
            proc = self._run_quiet(["ping", "-c", "1", "-W", "1", host])
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

    def fetch_latest_zapret_release(self) -> dict[str, str]:
        return self.updates.fetch_latest_zapret_release()

    def fetch_latest_tg_ws_proxy_release(self) -> dict[str, str]:
        return self.updates.fetch_latest_tg_ws_proxy_release()

    def update_zapret_runtime(self) -> dict[str, str]:
        return self.updates.update_zapret_runtime()

    def update_tg_ws_proxy_runtime(self) -> dict[str, str]:
        return self.updates.update_tg_ws_proxy_runtime()

    def check_component_updates(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for component_id, fetch_fn, detect_fn in [
            ("zapret", self.updates.fetch_latest_zapret_release, self.storage._detect_zapret_version),
            ("tg_ws_proxy", self.updates.fetch_latest_tg_ws_proxy_release, self.storage._detect_tgws_version),
        ]:
            try:
                release = fetch_fn()
                latest = str(release.get("latest_version", "")).strip()
                current = detect_fn()
                if latest and current not in ("", "unknown") and latest != current:
                    result[component_id] = {
                        "latest_version": latest,
                        "current_version": current,
                        "component_name": "Zapret" if component_id == "zapret" else "TG WS Proxy",
                    }
            except Exception:
                continue
        return result

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

    def _cleanup_merged_runtime(self) -> None:
        merged_root = self.storage.paths.merged_runtime_dir
        if not merged_root.exists():
            return
        for entry in merged_root.iterdir():
            if entry.is_dir():
                try:
                    shutil.rmtree(entry, ignore_errors=True)
                except Exception:
                    pass

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
        return _platform_is_image_running(image_name)

    def _kill_image(self, image_name: str) -> None:
        _platform_kill_image(image_name)

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
            _platform_kill_pid(process.pid)

        if IS_WINDOWS:
            self._cleanup_zapret_driver_services(self._current_zapret_runtime)
            for _ in range(8):
                self._kill_image("winws.exe")
                if not self._is_image_running("winws.exe"):
                    break
                time.sleep(0.35)
        else:
            for _ in range(4):
                _platform_kill_image("nfqws")
                if not _platform_is_image_running("nfqws"):
                    break
                time.sleep(0.35)

        self._processes.pop("zapret", None)
        self._current_zapret_runtime = None

    def _reset_active_runtime_dir(self, active_root: Path) -> None:
        driver_marker = active_root / ".driver_path_in_use"
        if IS_WINDOWS and driver_marker.exists() and (active_root / "bin" / "WinDivert64.sys").exists():
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
        quarantine_root = Path(tempfile.gettempdir()) / "zapret_zen_runtime_cleanup"
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
        if not IS_WINDOWS:
            return False
        proc = self._run_quiet(["sc", "query", "zapret"])
        return proc.returncode == 0

    def _track_active_driver_services(self) -> None:
        if not IS_WINDOWS:
            return
        for service_name in _ZAPRET_DRIVER_SERVICE_NAMES:
            if self._service_exists(service_name):
                self._app_started_services.add(service_name)

    def _cleanup_zapret_driver_services(self, runtime_root: Path | None = None) -> None:
        if not IS_WINDOWS:
            return
        for service_name in _ZAPRET_DRIVER_SERVICE_NAMES:
            if not self._service_exists(service_name):
                self._app_started_services.discard(service_name)
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
            owner = "app" if service_name in self._app_started_services else "external/unknown"
            self.logging.log("info", "Stopping driver service", service_name=service_name, owner=owner)
            stop_result = self._run_quiet(["sc", "stop", service_name])
            if stop_result.returncode != 0:
                self.logging.log(
                    "warning",
                    "sc stop failed for driver service",
                    service_name=service_name,
                    error=(stop_result.stderr or "").strip(),
                )
            for _ in range(10):
                state = self._service_state(service_name)
                if state in ("", "STOPPED"):
                    break
                time.sleep(0.5)
            else:
                final_state = self._service_state(service_name)
                self.logging.log(
                    "warning",
                    "Driver service did not reach STOPPED in time",
                    service_name=service_name,
                    final_state=final_state,
                )
            del_result = self._run_quiet(["sc", "delete", service_name])
            if del_result.returncode != 0:
                self.logging.log(
                    "warning",
                    "sc delete failed for driver service",
                    service_name=service_name,
                    error=(del_result.stderr or "").strip(),
                    hint="A program may still be using WinDivert. Close other DPI bypass tools, then restart the app.",
                )
            self._app_started_services.discard(service_name)

    def _driver_service_references_runtime(self, runtime_root: Path) -> bool:
        for service_name in _ZAPRET_DRIVER_SERVICE_NAMES:
            if not self._service_exists(service_name):
                continue
            image_path = self._service_image_path(service_name)
            if image_path and self._path_mentions_runtime(image_path, runtime_root):
                return True
        return False

    def _service_exists(self, service_name: str) -> bool:
        if not IS_WINDOWS:
            return False
        proc = self._run_quiet(["sc", "query", service_name])
        return proc.returncode == 0

    def _service_state(self, service_name: str) -> str:
        if not IS_WINDOWS:
            return ""
        proc = self._run_quiet(["sc", "query", service_name])
        if proc.returncode != 0:
            return ""
        for line in (proc.stdout or "").splitlines():
            if "STATE" in line:
                parts = line.split()
                if len(parts) >= 4:
                    return parts[3]
        return ""

    def _service_image_path(self, service_name: str) -> str:
        if not IS_WINDOWS:
            return ""
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
            if "windivert initialized" in lowered or "capture is started" in lowered:
                continue
            if "error" in lowered or "failed" in lowered or "windivert" in lowered:
                return text
        return ""

    def _check_log_hint(self, source: str, phrases: tuple[str, ...]) -> bool:
        path = Path(self.logging.source_log_path(source))
        if not path.exists():
            return False
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:
            return False
        return any(phrase in text for phrase in phrases)

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
        if IS_WINDOWS:
            for image_name in ("Telegram.exe", "Telegram Desktop.exe"):
                if self._is_image_running(image_name):
                    return True
            return False
        return _platform_is_image_running("telegram-desktop")

    def _telegram_desktop_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        if IS_WINDOWS:
            candidates.extend([
                Path(os.environ.get("APPDATA", "")) / "Telegram Desktop" / "Telegram.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Telegram Desktop" / "Telegram.exe",
                Path(os.environ.get("ProgramFiles", "")) / "Telegram Desktop" / "Telegram.exe",
                Path(os.environ.get("ProgramFiles(x86)", "")) / "Telegram Desktop" / "Telegram.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "Telegram.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "Telegram Desktop.exe",
            ])
            for image_name in ("Telegram.exe", "telegram.exe", "Telegram Desktop.exe"):
                resolved = shutil.which(image_name)
                if resolved:
                    candidates.append(Path(resolved))
        else:
            for name in ("telegram-desktop", "Telegram"):
                resolved = shutil.which(name)
                if resolved:
                    candidates.append(Path(resolved))
            xdg_dirs = [
                Path.home() / ".local" / "share" / "applications",
                Path("/usr/share/applications"),
                Path("/usr/local/share/applications"),
            ]
            for d in xdg_dirs:
                desktop_file = d / "telegram.desktop"
                if desktop_file.exists():
                    for line in desktop_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if line.lower().startswith("exec="):
                            exec_val = line.split("=", 1)[1].strip().split()[0]
                            p = Path(exec_val)
                            if p.exists():
                                candidates.append(p)
                            break
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

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from zapret_zen.platform_utils import IS_WINDOWS
from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.settings import SettingsManager


class RuntimeDiagnostics:
    """Connectivity checks, test targets, diagnostic runtime management."""

    def __init__(
        self,
        logging: LoggingManager,
        settings: SettingsManager,
        stop_component: Callable[[str], Any],
        start_component: Callable[[str], Any],
        is_image_running: Callable[[str], bool],
        list_zapret_generals: Callable[[], list[dict[str, str]]],
        build_zapret_args: Callable[[Path, Path], list[str]],
        load_standard_test_targets: Callable[[], list[dict[str, str]]],
        run_connectivity_check: Callable[..., dict[str, object]] | None = None,
        run_batch_connectivity_check: Callable[..., dict[str, object]] | None = None,
        reset_batch_state: Callable[[], None] | None = None,
        set_diagnostic_override: Callable[[bool], None] | None = None,
    ) -> None:
        self.logging = logging
        self.settings = settings
        self._stop_component = stop_component
        self._start_component = start_component
        self._is_image_running = is_image_running
        self._list_zapret_generals = list_zapret_generals
        self._build_zapret_args = build_zapret_args
        self._load_standard_test_targets = load_standard_test_targets
        self._run_connectivity_check = run_connectivity_check
        self._run_batch_connectivity_check = run_batch_connectivity_check
        self._reset_batch_state = reset_batch_state
        self._set_diagnostic_override = set_diagnostic_override
        self._diagnostic_runtime_override = False

    def _set_runtime_override(self, value: bool) -> None:
        self._diagnostic_runtime_override = value
        if self._set_diagnostic_override is not None:
            self._set_diagnostic_override(value)

    def capture_diagnostic_settings(self) -> dict[str, object]:
        settings = self.settings.get()
        return {
            "selected_zapret_general": settings.selected_zapret_general,
            "zapret_ipset_mode": settings.zapret_ipset_mode,
            "zapret_game_filter_mode": settings.zapret_game_filter_mode,
            "zapret_udp_exclude_ports": settings.zapret_udp_exclude_ports,
        }

    def restore_diagnostic_settings(self, snapshot: dict[str, object]) -> None:
        self.settings.update(
            selected_zapret_general=str(snapshot.get("selected_zapret_general", "") or ""),
            zapret_ipset_mode=str(snapshot.get("zapret_ipset_mode", "loaded") or "loaded"),
            zapret_game_filter_mode=str(snapshot.get("zapret_game_filter_mode", "disabled") or "disabled"),
            zapret_udp_exclude_ports=str(snapshot.get("zapret_udp_exclude_ports", "51820") or "51820"),
        )

    def prepare_diagnostic_runtime(self, *, general_id: str, ipset_mode: str, game_mode: str) -> bool:
        if IS_WINDOWS:
            _image = "winws.exe"
            original_running = self._is_image_running(_image)
        else:
            original_running = self._is_image_running("nfqws") or self._is_image_running("zapret-rust")
        if original_running:
            self._stop_component("zapret")
        self.settings.update(
            selected_zapret_general=general_id,
            zapret_ipset_mode=ipset_mode,
            zapret_game_filter_mode=game_mode,
        )
        return original_running

    def auto_select_working_general(self) -> dict[str, object] | None:
        options = self._list_zapret_generals()
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
                self._stop_component("zapret")
                self.logging.log("info", "Auto-selected zapret general", general=option["id"])
                return {
                    "id": option["id"],
                    "status": "ok",
                    "passed_targets": outcome.get("passed_targets", 0),
                    "total_targets": outcome.get("total_targets", 0),
                }
            self._stop_component("zapret")
        if best_result is not None and best_result.get("id"):
            self.settings.update(selected_zapret_general=str(best_result["id"]))
            return best_result
        self.settings.update(selected_zapret_general=original)
        return None

    def run_single_general_diagnostic(
        self,
        general_id: str,
        *,
        ipset_mode: str = "loaded",
        game_mode: str = "tcpudp",
        progress_callback: Callable | None = None,
        stop_callback: Callable | None = None,
    ) -> dict[str, object]:
        options = {item["id"]: item for item in self._list_zapret_generals()}
        option = options.get(general_id)
        if option is None:
            return {"status": "error", "error": "general not found", "passed_targets": 0, "total_targets": 0}
        settings_snapshot = self.capture_diagnostic_settings()
        self._set_runtime_override(True)
        original_running = self.prepare_diagnostic_runtime(
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
            self._stop_component("zapret")
            self.restore_diagnostic_settings(settings_snapshot)
            self._set_runtime_override(False)
            if original_running and str(settings_snapshot.get("selected_zapret_general", "")):
                self._start_component("zapret")

    def run_general_diagnostic_batch(
        self,
        batch: list[dict[str, str]],
        *,
        targets: list[dict[str, str]] | None = None,
        progress_callback: Callable | None = None,
        result_callback: Callable | None = None,
        stop_callback: Callable | None = None,
    ) -> list[dict[str, object]]:
        options_map = {item["id"]: item for item in self._list_zapret_generals()}
        settings_snapshot = self.capture_diagnostic_settings()
        if IS_WINDOWS:
            _image = "winws.exe"
            original_running = self._is_image_running(_image)
        else:
            original_running = self._is_image_running("nfqws") or self._is_image_running("zapret-rust")
        results: list[dict[str, object]] = []
        targets = targets if targets else self._load_standard_test_targets()
        total = max(1, len(batch))
        check_fn = self._run_batch_connectivity_check or self._run_connectivity_check
        try:
            self._set_runtime_override(True)
            if original_running:
                self._stop_component("zapret")
            for index, entry in enumerate(batch, start=1):
                if stop_callback is not None and stop_callback():
                    break
                general_id = str(entry.get("general_id", "") or "").strip()
                ipset_mode = str(entry.get("ipset_mode", "loaded") or "loaded")
                game_mode = str(entry.get("game_mode", "tcpudp") or "tcpudp")
                option = options_map.get(general_id)
                if option is None:
                    result: dict[str, object] = {
                        "id": general_id,
                        "name": general_id,
                        "bundle": "",
                        "status": "error",
                        "error": "general not found",
                        "passed_targets": 0,
                        "total_targets": 0,
                        "failed_targets": [],
                        "ipset_mode": ipset_mode,
                        "game_mode": game_mode,
                    }
                    results.append(result)
                    if progress_callback is not None:
                        progress_callback(index, total, general_id)
                    if result_callback is not None:
                        result_callback(result)
                    continue
                if progress_callback is not None:
                    progress_callback(index, total, option.get("name", general_id))
                outcome = check_fn(
                    general_id,
                    ipset_mode=ipset_mode,
                    game_mode=game_mode,
                    stop_callback=stop_callback,
                    targets=targets,
                )
                result = {
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
                results.append(result)
                if result_callback is not None:
                    result_callback(result)
                if stop_callback is not None and stop_callback():
                    break
        finally:
            self._stop_component("zapret")
            self.restore_diagnostic_settings(settings_snapshot)
            self._set_runtime_override(False)
            if original_running and str(settings_snapshot.get("selected_zapret_general", "")):
                self._start_component("zapret")
            if self._reset_batch_state is not None:
                self._reset_batch_state()
        return results

    def run_general_diagnostics(
        self,
        progress_callback: Callable | None = None,
        stop_callback: Callable | None = None,
    ) -> list[dict[str, str]]:
        options = self._list_zapret_generals()
        if not options:
            return []
        settings_snapshot = self.capture_diagnostic_settings()
        if IS_WINDOWS:
            _image = "winws.exe"
            original_running = self._is_image_running(_image)
        else:
            original_running = self._is_image_running("nfqws") or self._is_image_running("zapret-rust")
        results: list[dict[str, str]] = []
        targets = self._load_standard_test_targets()
        per_general_steps = max(2, len(targets) + 1)
        total_steps = len(options) * per_general_steps
        try:
            self._set_runtime_override(True)
            if original_running:
                self._stop_component("zapret")
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
                self._stop_component("zapret")
        finally:
            self._set_runtime_override(False)
            self.restore_diagnostic_settings(settings_snapshot)
            if original_running and str(settings_snapshot.get("selected_zapret_general", "")):
                self._start_component("zapret")
        return results

    def run_settings_diagnostics(
        self,
        progress_callback: Callable | None = None,
        stop_callback: Callable | None = None,
    ) -> dict[str, object]:
        original = self.settings.get()
        general_id = str(original.selected_zapret_general or "").strip()
        if not general_id:
            return {"results": [], "status": "error", "error": "No selected general"}
        ipset_modes = ["loaded", "none", "any"]
        game_modes = ["disabled", "tcpudp", "tcp", "udp"]
        combinations = [(ipset, game) for ipset in ipset_modes for game in game_modes]
        results: list[dict[str, object]] = []
        total = max(1, len(combinations))
        original_running = self._is_image_running("winws.exe")
        settings_snapshot = self.capture_diagnostic_settings()
        targets = self._load_standard_test_targets()
        check_fn = self._run_batch_connectivity_check or self._run_connectivity_check
        try:
            if original_running:
                self._stop_component("zapret")
            for index, (ipset_mode, game_mode) in enumerate(combinations, start=1):
                if stop_callback is not None and stop_callback():
                    break
                self.settings.update(
                    selected_zapret_general=general_id,
                    zapret_ipset_mode=ipset_mode,
                    zapret_game_filter_mode=game_mode,
                )
                if progress_callback is not None:
                    progress_callback(index, total, f"{ipset_mode} / {game_mode}")
                outcome = check_fn(
                    general_id,
                    ipset_mode=ipset_mode,
                    game_mode=game_mode,
                    stop_callback=stop_callback,
                    targets=targets,
                )
                results.append({
                    "ipset_mode": ipset_mode,
                    "game_mode": game_mode,
                    "status": str(outcome.get("status", "error")),
                    "passed_targets": str(outcome.get("passed_targets", 0)),
                    "total_targets": str(outcome.get("total_targets", 0)),
                    "failed_targets": list(outcome.get("failed_targets", []) or []),
                })
                self._stop_component("zapret")
        finally:
            self._stop_component("zapret")
            self.restore_diagnostic_settings(settings_snapshot)
            if original_running and str(settings_snapshot.get("selected_zapret_general", "")):
                self._start_component("zapret")
            if self._reset_batch_state is not None:
                self._reset_batch_state()
        best = max(results, key=lambda r: int(r.get("passed_targets", 0))) if results else None
        return {"results": results, "best": best, "status": "ok" if results else "error"}

    def _run_general_connectivity_check(
        self,
        general_id: str,
        *,
        stop_callback: Callable | None = None,
        targets: list[dict[str, str]] | None = None,
        progress_callback: Callable | None = None,
    ) -> dict[str, object]:
        if targets is None:
            targets = self._load_standard_test_targets()
        if self._run_connectivity_check is None:
            return {"status": "error", "passed_targets": 0, "total_targets": len(targets), "failed_targets": [], "error": "connectivity check is unavailable"}
        return dict(self._run_connectivity_check(general_id, stop_callback=stop_callback, targets=targets, progress_callback=progress_callback))

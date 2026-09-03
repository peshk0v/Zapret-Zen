from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import tempfile
import traceback
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from zapret_zen.domain import FileRecord
from zapret_zen.services.service_catalog import (
    SERVICE_PRESETS,
    SERVICE_PRESET_IDS,
)
from zapret_zen.services.tg_proxy_tuner import (
    collect_bridge_domains,
    decide_tuning,
    parse_dc_ids,
    parse_dc_ips,
    probe_bridges,
    probe_direct,
)

def _snapshot(context) -> dict[str, Any]:
    settings = context.settings.get()
    return {
        "components": [asdict(item) for item in context.processes.list_components()],
        "states": [asdict(item) for item in context.processes.list_states()],
        "settings": {
            "selected_zapret_general": settings.selected_zapret_general,
            "favorite_zapret_generals": list(settings.favorite_zapret_generals or []),
            "enabled_mod_ids": list(settings.enabled_mod_ids or []),
            "selected_service_ids": list(settings.selected_service_ids or []),
            "zapret_ipset_mode": settings.zapret_ipset_mode,
            "zapret_game_filter_mode": settings.zapret_game_filter_mode,
            "zapret_udp_exclude_ports": settings.zapret_udp_exclude_ports,
            "selected_runtime_mode": getattr(settings, "selected_runtime_mode", "zapret"),
            "autostart_windows": bool(settings.autostart_windows),
            "apply_update_on_next_launch": bool(getattr(settings, "apply_update_on_next_launch", False)),
            "selected_dns_preset": settings.selected_dns_preset,
        },
        "dns_presets": context.processes.list_dns_presets(),
    }

def _mods_payload(context) -> dict[str, Any]:
    return {
        "index": context.mods.fetch_index(),
        "installed": list(context.mods.list_installed()),
        "pending_mod_welcome": context.settings.get().pending_mod_welcome,
    }


def _check_mod_welcome(context, mod_id: str) -> None:
    installed = {item.id: item for item in context.mods.list_installed()}
    entry = installed.get(mod_id)
    if entry is None:
        return
    mod_path = Path(str(entry.path or ""))
    meta_path = mod_path / "mod.json"
    if not meta_path.is_file():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    welcome = meta.get("welcome")
    if not isinstance(welcome, str) or not welcome.strip():
        return
    mod_name = str(meta.get("name", entry.name or mod_id))
    context.settings.update(pending_mod_welcome={"mod_name": mod_name, "text": welcome.strip()})

def _general_file_records(context) -> list[FileRecord]:
    records: list[FileRecord] = []
    seen: set[str] = set()
    for option in context.processes.list_zapret_generals():
        path = Path(str(option.get("path", "") or ""))
        if not path.exists() or not path.is_file():
            continue
        resolved = str(path.resolve()).lower()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            relative = str(path.relative_to(context.paths.install_root))
        except ValueError:
            relative = str(path)
        bundle = str(option.get("bundle", "") or "").strip()
        label = f"{bundle}/{path.name}" if bundle else relative
        records.append(FileRecord(path=str(path), relative_path=label, size=path.stat().st_size))
    return sorted(records, key=lambda item: item.relative_path.lower())

def _host_file_records(context) -> list[FileRecord]:
    path = context.files.ensure_local_hosts_file()
    try:
        relative = str(path.relative_to(context.paths.install_root))
    except ValueError:
        relative = str(path)
    return [FileRecord(path=str(path), relative_path=relative, size=path.stat().st_size)]

def _stop_zapret_for_reconfiguration(context) -> bool:
    states = {item.component_id: item for item in context.processes.list_states()}
    was_running = bool(states.get("zapret") and states["zapret"].status == "running")
    if was_running:
        context.processes.stop_component("zapret")
    return was_running

def _finish_zapret_reconfiguration(context, *, restart: bool) -> bool:
    context.merge.rebuild()
    context.files._invalidate_collection_cache()
    context.files.rebuild_materialized_collections()
    context.processes.rebuild_zapret_runtime_snapshot()
    if restart:
        state = context.processes.start_component("zapret")
        return bool(getattr(state, "status", "") == "running")
    return False

def _restart_zapret_if_running(context) -> bool:
    was_running = _stop_zapret_for_reconfiguration(context)
    return _finish_zapret_reconfiguration(context, restart=was_running)

def _sync_telegram_component_from_services(context) -> None:
    settings = context.settings.get()
    selected = {str(item) for item in list(settings.selected_service_ids or [])}
    enabled = {str(item) for item in list(settings.enabled_component_ids or [])}
    autostart = {str(item) for item in list(settings.autostart_component_ids or [])}

    if "telegram-desktop" in selected:
        enabled.add("tg-ws-proxy")
        autostart.add("tg-ws-proxy")
    else:
        enabled.discard("tg-ws-proxy")
        autostart.discard("tg-ws-proxy")
    if "zapret" in enabled:
        autostart.add("zapret")
    else:
        autostart.discard("zapret")
    if enabled != set(settings.enabled_component_ids or []) or autostart != set(settings.autostart_component_ids or []):
        context.settings.update(
            enabled_component_ids=sorted(enabled),
            autostart_component_ids=sorted(autostart),
        )

def _runtime_running_states(context) -> tuple[dict[str, Any], bool, bool]:
    states = {item.component_id: item for item in context.processes.list_states()}
    any_running = any(item.status == "running" for item in states.values())
    zapret_running = bool(states.get("zapret") and states["zapret"].status == "running")

    return states, any_running, zapret_running

def _prepare_general_autotest_runtime(context) -> dict[str, Any]:
    _states, _any_running, zapret_running = _runtime_running_states(context)
    settings = context.settings.get()
    restore = {
        "was_running": bool(zapret_running),
        "selected_runtime_mode": str(getattr(settings, "selected_runtime_mode", "zapret") or "zapret"),
        "zapret_running": bool(zapret_running),
        "enabled_component_ids": list(settings.enabled_component_ids or []),
    }
    if zapret_running:
        context.processes.stop_component("zapret")
    return restore

def _restore_general_autotest_runtime(context, restore: dict[str, Any]) -> bool:
    if not bool(restore.get("was_running", False)):
        return False
    mode = str(restore.get("selected_runtime_mode", "") or "").strip()
    if not mode:
        return False
    if bool(restore.get("zapret_running", False)):
        _finish_zapret_reconfiguration(context, restart=True)
        return True
    if mode == "zapret":
        _finish_zapret_reconfiguration(context, restart=True)
        return True
    return False

def _set_enabled_components(context, enabled: set[str]) -> None:
    context.settings.update(enabled_component_ids=sorted(enabled))

def _start_enabled_aux_components(context, *, exclude: set[str] | None = None) -> None:
    excluded = {str(item) for item in (exclude or set())}
    for component in context.processes.list_components():
        if not component.enabled or component.id in excluded:
            continue
        context.processes.start_component(component.id)

def _set_zapret_enabled_from_components(context, enabled_target: bool) -> dict[str, Any]:
    settings = context.settings.get()
    enabled = {str(item) for item in list(settings.enabled_component_ids or [])}
    _states, any_running, zapret_running = _runtime_running_states(context)

    if enabled_target:
        enabled.add("zapret")
        _set_enabled_components(context, enabled)
        context.settings.update(
            selected_runtime_mode="zapret",
        )
        if any_running:
            context.processes.start_component("zapret")
    else:
        enabled.discard("zapret")
        _set_enabled_components(context, enabled)
        if zapret_running:
            context.processes.stop_component("zapret")
    return _snapshot(context)


def _attach_telegram_proxy_info(context, result: dict[str, Any]) -> None:
    try:
        info = context.processes.consume_telegram_proxy_launch_info()
    except Exception:
        info = None
    if isinstance(info, dict) and info:
        result["telegram_proxy"] = info

def _worker_main(task_queue, result_queue) -> None:
    from zapret_zen.bootstrap import bootstrap_application

    def _emit_progress(task_id: str, action: str, payload: dict[str, Any]) -> None:
        result_queue.put({"id": task_id, "action": action, "ok": True, "kind": "progress", "payload": payload})

    context = bootstrap_application()
    try:
        context.processes.ensure_quic_firewall_state()
    except Exception:
        pass
    while True:
        task = task_queue.get()
        if not isinstance(task, dict):
            continue
        action = str(task.get("action", ""))
        if action == "shutdown":
            try:
                context.processes.stop_all()
            except Exception:
                pass
            result_queue.put({"id": task.get("id", ""), "action": action, "ok": True, "payload": {}})
            break
        task_id = str(task.get("id", ""))
        payload = task.get("payload", {}) or {}
        try:
            result = _run_action(context, action, payload, lambda progress: _emit_progress(task_id, action, progress))
            result_queue.put({"id": task_id, "action": action, "ok": True, "payload": result or {}})
        except Exception as error:
            try:
                context.logging.log(
                    "error",
                    "Backend task failed",
                    action=action,
                    error=str(error),
                    traceback=traceback.format_exc(),
                )
            except Exception:
                pass
            result_queue.put(
                {
                    "id": task_id,
                    "action": action,
                    "ok": False,
                    "error": str(error),
                    "source": _action_error_source(action),
                }
            )

def _action_error_source(action: str) -> str:
    normalized = (action or "").strip().lower()
    if "tg_ws_proxy" in normalized or "tg-ws-proxy" in normalized or "tg_proxy" in normalized or "telegram" in normalized:
        return "tg-ws-proxy"
    if "zapret" in normalized or "general" in normalized or "merge" in normalized:
        return "zapret"
    if "mod" in normalized:
        return "mods"
    if "settings" in normalized:
        return "settings"
    if "file" in normalized:
        return "files"
    return "backend"


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------

ActionHandler = Callable[..., dict[str, Any]]
_ACTION_HANDLERS: dict[str, ActionHandler] = {}


def _register_action(name: str):
    def decorator(fn: ActionHandler) -> ActionHandler:
        _ACTION_HANDLERS[name] = fn
        return fn
    return decorator


@contextmanager
def _reconfigure_zapret(context):
    """Context manager: stop zapret → yield → rebuild & optionally restart."""
    was_running = _stop_zapret_for_reconfiguration(context)
    yield
    _finish_zapret_reconfiguration(context, restart=was_running)


_TG_PROXY_SETTINGS_FIELDS = (
    "tg_proxy_host",
    "tg_proxy_port",
    "tg_proxy_secret",
    "tg_proxy_dc_ip",
    "tg_proxy_media_mode",
    "tg_proxy_cfproxy_enabled",
    "tg_proxy_cfproxy_priority",
    "tg_proxy_cfproxy_domain",
    "tg_proxy_fake_tls_domain",
    "tg_proxy_buf_kb",
    "tg_proxy_pool_size",
)


def _tg_settings_signature(context):
    settings = context.settings.get()
    return tuple(getattr(settings, field, None) for field in _TG_PROXY_SETTINGS_FIELDS)


def _is_component_running(context, component_id: str) -> bool:
    for item in context.processes.list_states():
        if item.component_id == component_id and item.status == "running":
            return True
    return False


def _restart_tg_ws_proxy_if_settings_changed(context, signature_before, was_running: bool) -> None:
    if not was_running or _tg_settings_signature(context) == signature_before:
        return
    try:
        context.processes.stop_component("tg-ws-proxy")
        context.processes.start_component("tg-ws-proxy")
        context.logging.log("info", "tg-ws-proxy restarted after mod settings change")
    except Exception as error:
        context.logging.log("warning", "Failed to restart tg-ws-proxy after mod settings change", error=str(error))


def _run_action(context, action: str, payload: dict[str, Any], emit_progress: callable | None = None) -> dict[str, Any]:
    payload = {key: value for key, value in payload.items() if not str(key).startswith("_")}
    context.settings.reload()
    handler = _ACTION_HANDLERS.get(action)
    if handler is None:
        return {}
    return handler(context, payload, emit_progress)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

@_register_action("toggle_master_runtime")
def _handle_toggle_master_runtime(context, payload, emit_progress):
    _sync_telegram_component_from_services(context)
    components = context.processes.list_components()
    comp_by_id = {c.id: c for c in components}
    states = {item.component_id: item for item in context.processes.list_states()}
    active_ids = [c.id for c in components if c.enabled and c.id != "dns-manager"]
    running_ids = {
        component_id
        for component_id, state in states.items()
        if state.status == "running" and component_id != "dns-manager"
    }
    if running_ids:
        for cid in list(running_ids):
            if emit_progress:
                emit_progress({"action": "toggle_master_runtime", "status": f"stop_{cid}"})
            context.processes.stop_component(cid)
        mode = "disconnect"
    else:
        for cid in active_ids:
            if emit_progress:
                emit_progress({"action": "toggle_master_runtime", "status": f"start_{cid}"})
            context.processes.start_component(cid)
        mode = "connect"
    if emit_progress:
        emit_progress({"action": "toggle_master_runtime", "mode": mode})
    result = {"mode": mode}
    result.update(_snapshot(context))
    _attach_telegram_proxy_info(context, result)
    return result


@_register_action("load_startup_snapshot")
def _handle_load_startup_snapshot(context, payload, emit_progress):
    _sync_telegram_component_from_services(context)
    current = context.settings.get()
    if not str(current.selected_zapret_general or "").strip():
        options = context.processes.list_zapret_generals()
        if options:
            context.settings.update(selected_zapret_general=str(options[0]["id"]))
    result = _snapshot(context)
    result.update(_mods_payload(context))
    result["general_options"] = list(context.processes.list_zapret_generals())
    return result


@_register_action("load_components_payload")
def _handle_load_components_payload(context, payload, emit_progress):
    _sync_telegram_component_from_services(context)
    current = context.settings.get()
    options = context.processes.list_zapret_generals()
    if not str(current.selected_zapret_general or "").strip() and options:
        context.settings.update(selected_zapret_general=str(options[0]["id"]))
    result = _snapshot(context)
    result["general_options"] = options
    return result


@_register_action("ensure_zapret_dependencies")
def _handle_ensure_zapret_dependencies(context, payload, emit_progress):
    result = context.processes.ensure_zapret_dependencies()
    _sync_telegram_component_from_services(context)
    current = context.settings.get()
    options = context.processes.list_zapret_generals()
    if not str(current.selected_zapret_general or "").strip() and options:
        context.settings.update(selected_zapret_general=str(options[0]["id"]))
    snapshot = _snapshot(context)
    snapshot["general_options"] = options
    snapshot["setup_status"] = result
    return snapshot


@_register_action("zapret_sudoers_status")
def _handle_zapret_sudoers_status(context, payload, emit_progress):
    return {"configured": context.processes.zapret_sudoers_configured()}


@_register_action("configure_zapret_sudoers")
def _handle_configure_zapret_sudoers(context, payload, emit_progress):
    result = context.processes.configure_zapret_sudoers()
    result["general_options"] = context.processes.list_zapret_generals()
    return result


@_register_action("start_enabled_components")
def _handle_start_enabled_components(context, payload, emit_progress):
    _sync_telegram_component_from_services(context)
    autostart_only = bool(payload.get("autostart_only", False)) if isinstance(payload, dict) else False
    components = context.processes.list_components()
    states = {item.component_id: item for item in context.processes.list_states()}
    started: list[str] = []
    skipped: list[str] = []
    for component in components:
        if component.id == "dns-manager":
            continue
        if not component.enabled:
            continue
        if autostart_only and not component.autostart:
            continue
        state = states.get(component.id)
        if state is not None and state.status == "running":
            skipped.append(component.id)
            continue
        context.processes.start_component(component.id)
        started.append(component.id)
    if started or skipped:
        context.logging.log("info", "Start enabled components completed", started=started, skipped_running=skipped, autostart_only=autostart_only)
    # Force fresh state computation after all components started
    import time
    context.processes._invalidate_state_cache()
    time.sleep(0.5)  # Give processes time to fully initialize
    context.processes._invalidate_state_cache()  # Invalidate again for fresh data
    result = _snapshot(context)
    _attach_telegram_proxy_info(context, result)
    return result


@_register_action("start_component")
def _handle_start_component(context, payload, emit_progress):
    component_id = str(payload.get("component_id", "")).strip()
    if component_id:
        if component_id == "zapret":
            result = _set_zapret_enabled_from_components(context, True)
            _attach_telegram_proxy_info(context, result)
            return result
        context.processes.start_component(component_id)
    result = _snapshot(context)
    _attach_telegram_proxy_info(context, result)
    return result


@_register_action("stop_component")
def _handle_stop_component(context, payload, emit_progress):
    component_id = str(payload.get("component_id", "")).strip()
    if component_id:
        if component_id == "zapret":
            return _set_zapret_enabled_from_components(context, False)
        context.processes.stop_component(component_id)
    return _snapshot(context)


@_register_action("prepare_general_autotest_runtime")
def _handle_prepare_general_autotest_runtime(context, payload, emit_progress):
    restore = _prepare_general_autotest_runtime(context)
    result = {"restore_runtime": restore}
    result.update(_snapshot(context))
    return result


@_register_action("restore_general_autotest_runtime")
def _handle_restore_general_autotest_runtime(context, payload, emit_progress):
    restore = payload.get("restore_runtime", {})
    restored = _restore_general_autotest_runtime(context, restore if isinstance(restore, dict) else {})
    result = {"runtime_restored": restored}
    result.update(_snapshot(context))
    _attach_telegram_proxy_info(context, result)
    return result


@_register_action("apply_settings")
def _handle_apply_settings(context, payload, emit_progress):
    before = context.settings.get()
    try:
        client_revision = int(payload.get("client_revision", 0) or 0)
    except (TypeError, ValueError):
        client_revision = 0
    effective_payload = {key: value for key, value in payload.items() if key != "client_revision"}
    tg_before = (
        before.tg_proxy_host,
        int(before.tg_proxy_port),
        before.tg_proxy_secret,
        before.tg_proxy_dc_ip,
        bool(before.tg_proxy_cfproxy_enabled),
        bool(before.tg_proxy_cfproxy_priority),
        before.tg_proxy_cfproxy_domain,
        before.tg_proxy_fake_tls_domain,
        int(before.tg_proxy_buf_kb),
        int(before.tg_proxy_pool_size),
    )
    zapret_before = (
        before.zapret_ipset_mode,
        before.zapret_game_filter_mode,
        before.zapret_udp_exclude_ports,
        before.selected_zapret_general,
    )
    theme_before = before.theme
    language_before = before.language
    autostart_before = bool(before.autostart_windows)
    quic_before = bool(before.zapret_block_quic)
    context.settings.update(**effective_payload)
    if "fortnite" in {str(item) for item in list(before.selected_service_ids or [])}:
        effective_payload["zapret_ipset_mode"] = "any"
        effective_payload["zapret_game_filter_mode"] = "tcpudp"
    requested_zapret = (
        str(effective_payload.get("zapret_ipset_mode", before.zapret_ipset_mode)),
        str(effective_payload.get("zapret_game_filter_mode", before.zapret_game_filter_mode)),
        str(effective_payload.get("zapret_udp_exclude_ports", before.zapret_udp_exclude_ports)),
        str(effective_payload.get("selected_zapret_general", before.selected_zapret_general)),
    )
    zapret_changed = zapret_before != requested_zapret
    zapret_was_running = _stop_zapret_for_reconfiguration(context) if zapret_changed else False
    tg_after = (
        str(effective_payload.get("tg_proxy_host", context.settings.get().tg_proxy_host)),
        int(effective_payload.get("tg_proxy_port", context.settings.get().tg_proxy_port)),
        str(effective_payload.get("tg_proxy_secret", context.settings.get().tg_proxy_secret)),
        str(effective_payload.get("tg_proxy_dc_ip", context.settings.get().tg_proxy_dc_ip)),
        bool(effective_payload.get("tg_proxy_cfproxy_enabled", context.settings.get().tg_proxy_cfproxy_enabled)),
        bool(effective_payload.get("tg_proxy_cfproxy_priority", context.settings.get().tg_proxy_cfproxy_priority)),
        str(effective_payload.get("tg_proxy_cfproxy_domain", context.settings.get().tg_proxy_cfproxy_domain)),
        str(effective_payload.get("tg_proxy_fake_tls_domain", context.settings.get().tg_proxy_fake_tls_domain)),
        int(effective_payload.get("tg_proxy_buf_kb", context.settings.get().tg_proxy_buf_kb)),
        int(effective_payload.get("tg_proxy_pool_size", context.settings.get().tg_proxy_pool_size)),
    )
    current = context.settings.get()
    zapret_after = (
        current.zapret_ipset_mode,
        current.zapret_game_filter_mode,
        current.zapret_udp_exclude_ports,
        current.selected_zapret_general,
    )
    states = {item.component_id: item for item in context.processes.list_states()}
    if tg_before != tg_after and states.get("tg-ws-proxy") and states["tg-ws-proxy"].status == "running":
        context.processes.stop_component("tg-ws-proxy")
        context.processes.start_component("tg-ws-proxy")
    zapret_restarted = False
    if zapret_before != zapret_after:
        zapret_restarted = _finish_zapret_reconfiguration(context, restart=zapret_was_running)
    quic_after = bool(effective_payload.get("zapret_block_quic", context.settings.get().zapret_block_quic))
    if quic_before != quic_after:
        context.processes.set_quic_blocked(quic_after)
    result = {
        "theme_changed": theme_before != context.settings.get().theme,
        "language_changed": language_before != context.settings.get().language,
        "autostart_changed": autostart_before != bool(context.settings.get().autostart_windows),
        "native_window_changed": bool(before.native_window_on_linux) != bool(context.settings.get().native_window_on_linux),
        "client_revision": client_revision,
        "zapret_restarted": zapret_restarted,
    }
    result.update(_snapshot(context))
    return result


@_register_action("select_general")
def _handle_select_general(context, payload, emit_progress):
    selected = str(payload.get("selected", "")).strip()
    if not selected:
        return {}
    zapret_was_running = _stop_zapret_for_reconfiguration(context)
    settings = context.settings.get()
    settings.selected_zapret_general = selected
    context.settings.save()
    zapret_restarted = _finish_zapret_reconfiguration(context, restart=zapret_was_running)
    result = {"selected": selected, "zapret_restarted": zapret_restarted}
    result.update(_snapshot(context))
    return result


@_register_action("set_selected_services")
def _handle_set_selected_services(context, payload, emit_progress):
    raw_ids = payload.get("service_ids", []) or []
    try:
        client_revision = int(payload.get("client_revision", 0) or 0)
    except (TypeError, ValueError):
        client_revision = 0
    requested = {str(item).strip() for item in raw_ids if str(item).strip() in SERVICE_PRESET_IDS}
    ordered = [preset.id for preset in SERVICE_PRESETS if preset.id in requested]
    settings = context.settings.get()
    before_services = set(settings.selected_service_ids or [])
    enabled_components = set(settings.enabled_component_ids or [])
    autostart_components = set(settings.autostart_component_ids or [])
    has_zapret_services = bool(requested - {"telegram-desktop", "ai"})
    if has_zapret_services:
        enabled_components.add("zapret")
        autostart_components.add("zapret")
    else:
        enabled_components.discard("zapret")
        autostart_components.discard("zapret")
    if "telegram-desktop" in requested:
        enabled_components.add("tg-ws-proxy")
        autostart_components.add("tg-ws-proxy")
    else:
        enabled_components.discard("tg-ws-proxy")
        autostart_components.discard("tg-ws-proxy")
    if "telegram-desktop" in before_services and "telegram-desktop" not in requested:
        states = {item.component_id: item for item in context.processes.list_states()}
        if states.get("tg-ws-proxy") and states["tg-ws-proxy"].status == "running":
            context.processes.stop_component("tg-ws-proxy")
    elif "telegram-desktop" in requested and "telegram-desktop" not in before_services:
        states = {item.component_id: item for item in context.processes.list_states()}
        if any(item.status == "running" for item in states.values()):
            try:
                context.processes.start_component("tg-ws-proxy")
            except Exception:
                pass
    if not has_zapret_services:
        states = {item.component_id: item for item in context.processes.list_states()}
        if states.get("zapret") and states["zapret"].status == "running":
            context.processes.stop_component("zapret")
    zapret_was_running = _stop_zapret_for_reconfiguration(context)
    settings_changes = {
        "selected_service_ids": ordered,
        "enabled_component_ids": sorted(enabled_components),
        "autostart_component_ids": sorted(autostart_components),
    }
    context.settings.update(**settings_changes)
    zapret_restarted = _finish_zapret_reconfiguration(context, restart=zapret_was_running)
    result = {"selected_service_ids": ordered, "client_revision": client_revision, "zapret_restarted": zapret_restarted}
    result.update(_snapshot(context))
    result.update(_mods_payload(context))
    result["general_options"] = list(context.processes.list_zapret_generals())
    _attach_telegram_proxy_info(context, result)
    return result


@_register_action("toggle_component_enabled")
def _handle_toggle_component_enabled(context, payload, emit_progress):
    component_id = str(payload.get("component_id", "")).strip()
    if component_id:
        if component_id == "zapret":
            result = _set_zapret_enabled_from_components(
                context,
                "zapret" not in {str(item) for item in list(context.settings.get().enabled_component_ids or [])},
            )
            result["component"] = next(
                (asdict(item) for item in context.processes.list_components() if item.id == "zapret"),
                {},
            )
            return result
        component = context.processes.toggle_component_enabled(component_id)
        if component_id == "tg-ws-proxy":
            settings = context.settings.get()
            selected = {str(item) for item in list(settings.selected_service_ids or [])}
            autostart = {str(item) for item in list(settings.autostart_component_ids or [])}
            if component.enabled:
                selected.add("telegram-desktop")
                autostart.add("tg-ws-proxy")
            else:
                selected.discard("telegram-desktop")
                autostart.discard("tg-ws-proxy")
                states = {item.component_id: item for item in context.processes.list_states()}
                if states.get("tg-ws-proxy") and states["tg-ws-proxy"].status == "running":
                    context.processes.stop_component("tg-ws-proxy")
            ordered = [preset.id for preset in SERVICE_PRESETS if preset.id in selected]
            context.settings.update(
                selected_service_ids=ordered,
                autostart_component_ids=sorted(autostart),
            )
        if component_id == "zapret":
            settings = context.settings.get()
            autostart = {str(item) for item in list(settings.autostart_component_ids or [])}
            if component.enabled:
                autostart.add("zapret")
            else:
                autostart.discard("zapret")
            context.settings.update(autostart_component_ids=sorted(autostart))
        result = {"component": asdict(component)}
        result.update(_snapshot(context))
        return result
    return {}


@_register_action("toggle_component_autostart")
def _handle_toggle_component_autostart(context, payload, emit_progress):
    component_id = str(payload.get("component_id", "")).strip()
    if component_id:
        component = context.processes.toggle_component_autostart(component_id)
        result = {"component": asdict(component)}
        result.update(_snapshot(context))
        return result
    return {}


@_register_action("toggle_mod")
def _handle_toggle_mod(context, payload, emit_progress):
    mod_id = str(payload.get("mod_id", "")).strip()
    if not mod_id:
        return {}
    tg_sig_before = _tg_settings_signature(context)
    tg_was_running = _is_component_running(context, "tg-ws-proxy")
    try:
        with _reconfigure_zapret(context):
            installed = {item.id: item for item in context.mods.list_installed()}
            was_enabled = bool(installed.get(mod_id) and installed[mod_id].enabled)
            if mod_id not in installed:
                context.mods.install(mod_id)
                installed = {item.id: item for item in context.mods.list_installed()}
            if mod_id in installed:
                context.mods.set_enabled(mod_id, not was_enabled)
            if not was_enabled:
                _check_mod_welcome(context, mod_id)
    except Exception as error:
        context.logging.log("warning", "Mod toggle failed", mod_id=mod_id, error=str(error))
    _restart_tg_ws_proxy_if_settings_changed(context, tg_sig_before, tg_was_running)
    result = {"mod_id": mod_id}
    result.update(_snapshot(context))
    result.update(_mods_payload(context))
    return result


@_register_action("install_mod")
def _handle_install_mod(context, payload, emit_progress):
    mod_id = str(payload.get("mod_id", "")).strip()
    if not mod_id:
        result = {"mod_id": mod_id}
        result.update(_snapshot(context))
        result.update(_mods_payload(context))
        return result
    installed_before = {item.id: item for item in context.mods.list_installed()}
    was_enabled = bool(installed_before.get(mod_id) and bool(installed_before[mod_id].enabled))
    tg_sig_before = _tg_settings_signature(context)
    tg_was_running = _is_component_running(context, "tg-ws-proxy")
    try:
        with _reconfigure_zapret(context):
            context.mods.install(mod_id)
            context.mods.set_enabled(mod_id, True)
        if not was_enabled:
            _check_mod_welcome(context, mod_id)
    except Exception as error:
        context.logging.log("warning", "Mod install failed", mod_id=mod_id, error=str(error))
        raise
    _restart_tg_ws_proxy_if_settings_changed(context, tg_sig_before, tg_was_running)
    result = {"mod_id": mod_id}
    result.update(_snapshot(context))
    result.update(_mods_payload(context))
    return result


@_register_action("remove_mod")
def _handle_remove_mod(context, payload, emit_progress):
    mod_id = str(payload.get("mod_id", "")).strip()
    with _reconfigure_zapret(context):
        if mod_id:
            context.mods.remove(mod_id)
    result = {"mod_id": mod_id}
    result.update(_snapshot(context))
    result.update(_mods_payload(context))
    return result


@_register_action("create_mod")
def _handle_create_mod(context, payload, emit_progress):
    name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "") or "")
    author = str(payload.get("author", "") or "")
    entry = context.mods.create_empty(name=name or "Custom mod", description=description, author=author)
    result = {"mod_id": entry.id}
    result.update(_snapshot(context))
    result.update(_mods_payload(context))
    return result


@_register_action("update_mod_metadata")
def _handle_update_mod_metadata(context, payload, emit_progress):
    mod_id = str(payload.get("mod_id", "")).strip()
    context.mods.update_metadata(
        mod_id,
        name=str(payload.get("name", "") or ""),
        description=str(payload.get("description", "") or ""),
        author=str(payload.get("author", "") or ""),
        version=str(payload.get("version", "") or ""),
    )
    result = {"mod_id": mod_id, "files": context.mods.list_files(mod_id)}
    result.update(_snapshot(context))
    result.update(_mods_payload(context))
    return result


@_register_action("load_mod_editor")
def _handle_load_mod_editor(context, payload, emit_progress):
    mod_id = str(payload.get("mod_id", "")).strip()
    installed = {item.id: item for item in context.mods.list_installed()}
    entry = installed[mod_id]
    return {"mod": asdict(entry), "files": context.mods.list_files(mod_id)}


@_register_action("read_mod_file")
def _handle_read_mod_file(context, payload, emit_progress):
    mod_id = str(payload.get("mod_id", "")).strip()
    path = str(payload.get("path", "") or "")
    return {"mod_id": mod_id, "path": path, "content": context.mods.read_file(mod_id, path)}


@_register_action("write_mod_file")
def _handle_write_mod_file(context, payload, emit_progress):
    mod_id = str(payload.get("mod_id", "")).strip()
    path = str(payload.get("path", "") or "")
    content = str(payload.get("content", "") or "")
    with _reconfigure_zapret(context):
        context.mods.write_file(mod_id, path, content)
    result = {"mod_id": mod_id, "path": path, "files": context.mods.list_files(mod_id)}
    result.update(_snapshot(context))
    result.update(_mods_payload(context))
    result["general_options"] = list(context.processes.list_zapret_generals())
    return result


@_register_action("delete_mod_file")
def _handle_delete_mod_file(context, payload, emit_progress):
    mod_id = str(payload.get("mod_id", "")).strip()
    path = str(payload.get("path", "") or "")
    with _reconfigure_zapret(context):
        context.mods.delete_file(mod_id, path)
    result = {"mod_id": mod_id, "files": context.mods.list_files(mod_id)}
    result.update(_snapshot(context))
    result.update(_mods_payload(context))
    result["general_options"] = list(context.processes.list_zapret_generals())
    return result


@_register_action("import_mod_from_github")
def _handle_import_mod_from_github(context, payload, emit_progress):
    repo_url = str(payload.get("repo_url", "")).strip()
    previous_selected_general = str(payload.get("previous_selected_general", "")).strip()
    imported_id = ""
    tg_sig_before = _tg_settings_signature(context)
    tg_was_running = _is_component_running(context, "tg-ws-proxy")
    with _reconfigure_zapret(context):
        if repo_url:
            entry = context.mods.import_from_github(repo_url)
            imported_id = str(entry.id)
            if previous_selected_general:
                context.settings.update(selected_zapret_general=previous_selected_general)
    _restart_tg_ws_proxy_if_settings_changed(context, tg_sig_before, tg_was_running)
    if imported_id:
        _check_mod_welcome(context, imported_id)
    result = {"repo_url": repo_url, "mod_id": imported_id}
    result.update(_snapshot(context))
    result.update(_mods_payload(context))
    result["general_options"] = list(context.processes.list_zapret_generals())
    return result


@_register_action("import_mod_from_paths")
def _handle_import_mod_from_paths(context, payload, emit_progress):
    raw_paths = payload.get("paths", []) or []
    paths = [str(item).strip() for item in raw_paths if str(item).strip()]
    previous_selected_general = str(payload.get("previous_selected_general", "")).strip()
    imported_id = ""
    tg_sig_before = _tg_settings_signature(context)
    tg_was_running = _is_component_running(context, "tg-ws-proxy")
    with _reconfigure_zapret(context):
        if paths:
            entry = context.mods.import_from_paths(paths)
            imported_id = str(entry.id)
            if previous_selected_general:
                context.settings.update(selected_zapret_general=previous_selected_general)
    _restart_tg_ws_proxy_if_settings_changed(context, tg_sig_before, tg_was_running)
    if imported_id:
        _check_mod_welcome(context, imported_id)
    result = {"paths": paths, "mod_id": imported_id}
    result.update(_snapshot(context))
    result.update(_mods_payload(context))
    result["general_options"] = list(context.processes.list_zapret_generals())
    return result


@_register_action("import_mod_from_path")
def _handle_import_mod_from_path(context, payload, emit_progress):
    path = str(payload.get("path", "")).strip()
    previous_selected_general = str(payload.get("previous_selected_general", "")).strip()
    imported_id = ""
    tg_sig_before = _tg_settings_signature(context)
    tg_was_running = _is_component_running(context, "tg-ws-proxy")
    with _reconfigure_zapret(context):
        if path:
            entry = context.mods.import_from_path(path)
            imported_id = str(entry.id)
            if previous_selected_general:
                context.settings.update(selected_zapret_general=previous_selected_general)
    _restart_tg_ws_proxy_if_settings_changed(context, tg_sig_before, tg_was_running)
    if imported_id:
        _check_mod_welcome(context, imported_id)
    result = {"path": path, "mod_id": imported_id}
    result.update(_snapshot(context))
    result.update(_mods_payload(context))
    result["general_options"] = list(context.processes.list_zapret_generals())
    return result


@_register_action("move_mod")
def _handle_move_mod(context, payload, emit_progress):
    mod_id = str(payload.get("mod_id", "")).strip()
    direction = int(payload.get("direction", 0) or 0)
    with _reconfigure_zapret(context):
        if mod_id and direction:
            context.mods.move(mod_id, direction)
    result = _snapshot(context)
    result.update(_mods_payload(context))
    return result


@_register_action("set_mod_emoji")
def _handle_set_mod_emoji(context, payload, emit_progress):
    mod_id = str(payload.get("mod_id", "")).strip()
    emoji = str(payload.get("emoji", "")).strip()
    if mod_id and emoji:
        context.mods.set_emoji(mod_id, emoji)
    result = _snapshot(context)
    result.update(_mods_payload(context))
    return result


@_register_action("restart_zapret_if_running")
def _handle_restart_zapret_if_running(context, payload, emit_progress):
    zapret_restarted = _restart_zapret_if_running(context)
    result = _snapshot(context)
    result["zapret_restarted"] = zapret_restarted
    return result


@_register_action("add_collection_values")
def _handle_add_collection_values(context, payload, emit_progress):
    collection_id = str(payload.get("collection_id", "")).strip()
    raw = str(payload.get("raw", "") or "")
    with _reconfigure_zapret(context):
        values = context.files.add_collection_values(collection_id, raw)
    result = _snapshot(context)
    result["files_payload"] = {
        "mode_index": 1,
        "collection_id": collection_id,
        "collection_values": list(values),
    }
    return result


@_register_action("remove_collection_value")
def _handle_remove_collection_value(context, payload, emit_progress):
    collection_id = str(payload.get("collection_id", "")).strip()
    value = str(payload.get("value", "") or "")
    with _reconfigure_zapret(context):
        values = context.files.remove_collection_value(collection_id, value)
    result = _snapshot(context)
    result["files_payload"] = {
        "mode_index": 1,
        "collection_id": collection_id,
        "collection_values": list(values),
    }
    return result


@_register_action("reset_user_overrides")
def _handle_reset_user_overrides(context, payload, emit_progress):
    collection_id = str(payload.get("collection_id", "")).strip()
    with _reconfigure_zapret(context):
        context.files.reset_user_overrides()
    values = context.files.read_collection(collection_id) if collection_id else []
    result = _snapshot(context)
    result["files_payload"] = {
        "mode_index": 1,
        "collection_id": collection_id,
        "collection_values": list(values),
    }
    return result


@_register_action("load_files_payload")
def _handle_load_files_payload(context, payload, emit_progress):
    mode_index = int(payload.get("mode_index", 0) or 0)
    collection_id = str(payload.get("collection_id", "")).strip()
    file_filter = str(payload.get("file_filter", "all") or "all")
    records = None
    if mode_index == 2:
        if file_filter == "generals":
            records = _general_file_records(context)
        elif file_filter == "hosts":
            records = _host_file_records(context)
        else:
            records = context.files.list_files()
    return {
        "files_payload": {
            "mode_index": mode_index,
            "collection_id": collection_id,
            "file_filter": file_filter,
            "records": records,
            "collection_values": context.files.read_collection(collection_id) if mode_index == 1 else None,
        }
    }


@_register_action("write_file_text")
def _handle_write_file_text(context, payload, emit_progress):
    full_path = str(payload.get("path", "")).strip()
    content = str(payload.get("content", "") or "")
    with _reconfigure_zapret(context):
        if full_path:
            context.files.write_text(full_path, content)
    result = _snapshot(context)
    result["path"] = full_path
    return result


@_register_action("rebuild_merge_runtime")
def _handle_rebuild_merge_runtime(context, payload, emit_progress):
    with _reconfigure_zapret(context):
        pass
    result = _snapshot(context)
    result.update(_mods_payload(context))
    return result


@_register_action("apply_system_hosts")
def _handle_apply_system_hosts(context, payload, emit_progress):
    action = str(payload.get("action", "apply"))
    if action == "revert":
        ok, msg = context.files.revert_system_hosts()
    else:
        entries = context.files.collect_mod_hosts_entries()
        ok, msg = context.files.apply_system_hosts(entries)
    return {"ok": ok, "message": msg}


@_register_action("load_system_hosts")
def _handle_load_system_hosts(context, payload, emit_progress):
    content = context.files.read_system_hosts()
    entries = context.files.collect_mod_hosts_entries()
    return {"content": content, "mod_entries": entries}


@_register_action("set_favorite_generals")
def _handle_set_favorite_generals(context, payload, emit_progress):
    favorites = [str(item).strip() for item in (payload.get("favorites", []) or []) if str(item).strip()]
    current = context.settings.get()
    current.favorite_zapret_generals = favorites
    context.settings.save()
    return _snapshot(context)


@_register_action("set_general_autotest_done")
def _handle_set_general_autotest_done(context, payload, emit_progress):
    done = bool(payload.get("done", True))
    current = context.settings.get()
    current.general_autotest_done = done
    context.settings.save()
    return _snapshot(context)


@_register_action("run_general_diagnostics")
def _handle_run_general_diagnostics(context, payload, emit_progress):
    cancel_path = str(payload.get("cancel_path", "") or "")
    results = context.processes.run_general_diagnostics(
        progress_callback=(
            lambda current, total, name: emit_progress(
                {"current": current, "total": total, "name": name}
            )
            if emit_progress is not None
            else None
        ),
        stop_callback=(lambda: bool(cancel_path) and os.path.exists(cancel_path)),
    )
    return {"results": results}


@_register_action("run_general_diagnostic_single")
def _handle_run_general_diagnostic_single(context, payload, emit_progress):
    general_id = str(payload.get("general_id", "")).strip()
    cancel_path = str(payload.get("cancel_path", "") or "")
    ipset_mode = str(payload.get("ipset_mode", "loaded") or "loaded").strip()
    game_mode = str(payload.get("game_mode", "tcpudp") or "tcpudp").strip()
    return context.processes.run_single_general_diagnostic(
        general_id,
        ipset_mode=ipset_mode,
        game_mode=game_mode,
        progress_callback=(
            lambda current, total, name: emit_progress(
                {"current": current, "total": total, "name": name}
            )
            if emit_progress is not None
            else None
        ),
        stop_callback=(lambda: bool(cancel_path) and os.path.exists(cancel_path)),
    )


@_register_action("run_general_diagnostic_batch")
def _handle_run_general_diagnostic_batch(context, payload, emit_progress):
    batch = payload.get("batch", [])
    targets = payload.get("targets", None)
    cancel_path = str(payload.get("cancel_path", "") or "")
    results = context.processes.run_general_diagnostic_batch(
        batch,
        targets=targets if isinstance(targets, list) else None,
        progress_callback=(
            lambda current, total, name: emit_progress(
                {"kind": "progress", "current": current, "total": total, "name": name}
            )
            if emit_progress is not None
            else None
        ),
        result_callback=(
            lambda result: emit_progress(
                {"kind": "general_result", "result": result}
            )
            if emit_progress is not None
            else None
        ),
        stop_callback=(lambda: bool(cancel_path) and os.path.exists(cancel_path)),
    )
    return {"results": results}


@_register_action("run_settings_diagnostics")
def _handle_run_settings_diagnostics(context, payload, emit_progress):
    cancel_path = str(payload.get("cancel_path", "") or "")
    return context.processes.run_settings_diagnostics(
        progress_callback=(
            lambda current, total, name: emit_progress(
                {"current": current, "total": total, "name": name}
            )
            if emit_progress is not None
            else None
        ),
        stop_callback=(lambda: bool(cancel_path) and os.path.exists(cancel_path)),
    )


@_register_action("update_zapret_runtime")
def _handle_update_zapret_runtime(context, payload, emit_progress):
    result = context.processes.update_zapret_runtime()
    result.update(_snapshot(context))
    return result


@_register_action("update_tg_ws_proxy_runtime")
def _handle_update_tg_ws_proxy_runtime(context, payload, emit_progress):
    result = context.processes.update_tg_ws_proxy_runtime()
    result.update(_snapshot(context))
    return result


@_register_action("run_tg_proxy_tuning")
def _handle_run_tg_proxy_tuning(context, payload, emit_progress):
    mode = str((payload or {}).get("mode", "full") or "full").strip().lower()
    current = context.settings.get()
    bridge_domains = collect_bridge_domains(
        context.storage.paths.runtime_dir,
        current.tg_proxy_cfproxy_domain,
    )
    total = len(bridge_domains) + (2 if mode != "fast" else 1)
    steps = 0

    def emit(stage: str, name: str) -> None:
        nonlocal steps
        steps += 1
        if emit_progress is not None:
            emit_progress(
                {
                    "stage": stage,
                    "current": steps,
                    "total": total,
                    "name": name,
                }
            )

    emit("bridges", "Проверка мостов")
    dc_ids = parse_dc_ids(current.tg_proxy_dc_ip)
    bridge_results = probe_bridges(bridge_domains, dc_ids)
    report = [
        {"kind": "bridge", "name": item["domain"], "latency": item["latency"]}
        for item in bridge_results
    ]
    direct_results = []
    if mode != "fast":
        emit("direct", "Проверка прямого соединения")
        dc_ips = parse_dc_ips(current.tg_proxy_dc_ip)
        direct_results = probe_direct(dc_ips)
        report.extend(
            {"kind": "direct", "name": item["ip"], "latency": item["latency"]}
            for item in direct_results
        )
    emit("decide", "Выбор настроек")
    decision = decide_tuning(bridge_results, direct_results)
    changes = dict(decision["changes"])
    changes["tg_proxy_tuning_done"] = True
    emit("apply", "Применение настроек")
    context.settings.update(**changes)
    states = {item.component_id: item for item in context.processes.list_states()}
    if states.get("tg-ws-proxy") and states["tg-ws-proxy"].status == "running":
        try:
            context.processes.stop_component("tg-ws-proxy")
        except Exception:
            pass
        try:
            context.processes.start_component("tg-ws-proxy")
        except Exception:
            pass
    result = {
        "status": "ok",
        "mode": decision["mode"],
        "summary": decision["summary"],
        "applied": changes,
        "report": report,
        "counts": {
            "bridges_tested": len(bridge_domains),
            "bridges_ok": len(bridge_results),
            "direct_ok": len(direct_results),
        },
    }
    result.update(_snapshot(context))
    return result


@_register_action("set_tg_proxy_tuning_done")
def _handle_set_tg_proxy_tuning_done(context, payload, emit_progress):
    done = bool(payload.get("done", True))
    current = context.settings.get()
    current.tg_proxy_tuning_done = done
    context.settings.save()
    return _snapshot(context)


@_register_action("check_component_updates")
def _handle_check_component_updates(context, payload, emit_progress):
    return {"updates": context.processes.check_component_updates()}


@_register_action("apply_dns_preset")
def _handle_apply_dns_preset(context, payload, emit_progress):
    preset = str(payload.get("preset", "")).strip()
    context.settings.update(selected_dns_preset=preset)
    states = {item.component_id: item for item in context.processes.list_states()}
    if states.get("dns-manager") and states["dns-manager"].status == "running":
        context.processes.stop_component("dns-manager")
        context.processes.start_component("dns-manager")
    result = _snapshot(context)
    result["dns_presets"] = context.processes.list_dns_presets()
    return result

class BackendWorkerClient(QObject):
    task_finished = Signal(dict)
    task_failed = Signal(dict)
    task_progress = Signal(dict)

    def __init__(
        self,
        parent: QObject | None = None,
        bootstrap_tasks: list[tuple[str, dict[str, Any] | None]] | None = None,
    ) -> None:
        super().__init__(parent)
        ctx = mp.get_context("spawn")
        self._task_queue = ctx.Queue()
        self._result_queue = ctx.Queue()
        # Enqueue bootstrap tasks before any other submit can land: FIFO
        # guarantees the worker executes them first thing after boot.
        for action, payload in bootstrap_tasks or []:
            self._task_queue.put({"id": uuid.uuid4().hex, "action": action, "payload": dict(payload or {})})
        self._process = ctx.Process(target=_worker_main, args=(self._task_queue, self._result_queue), daemon=True)
        self._process.start()
        self._cancel_paths: dict[str, str] = {}
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(40)
        self._poll_timer.timeout.connect(self._poll_results)
        self._poll_timer.start()

    def submit(self, action: str, payload: dict[str, Any] | None = None) -> str:
        task_id = uuid.uuid4().hex
        task_payload = dict(payload or {})
        if action in {"run_general_diagnostics", "run_general_diagnostic_single", "run_general_diagnostic_batch", "run_settings_diagnostics"}:
            cancel_path = os.path.join(tempfile.gettempdir(), f"zapret_zen_cancel_{task_id}.flag")
            try:
                if os.path.exists(cancel_path):
                    os.remove(cancel_path)
            except OSError:
                pass
            self._cancel_paths[task_id] = cancel_path
            task_payload["cancel_path"] = cancel_path
        self._task_queue.put({"id": task_id, "action": action, "payload": task_payload})
        return task_id

    def cancel(self, task_id: str) -> None:
        cancel_path = self._cancel_paths.get(task_id)
        if not cancel_path:
            return
        try:
            with open(cancel_path, "w", encoding="utf-8") as handle:
                handle.write("cancelled")
        except OSError:
            pass

    def _poll_results(self) -> None:
        while True:
            try:
                message = self._result_queue.get_nowait()
            except queue.Empty:
                break
            if str(message.get("kind", "")) == "progress":
                self.task_progress.emit(message)
                continue
            task_id = str(message.get("id", ""))
            cancel_path = self._cancel_paths.pop(task_id, None)
            if cancel_path:
                try:
                    if os.path.exists(cancel_path):
                        os.remove(cancel_path)
                except OSError:
                    pass
            if bool(message.get("ok")):
                self.task_finished.emit(message)
            else:
                self.task_failed.emit(message)

    def stop(self) -> None:
        try:
            self._task_queue.put({"id": uuid.uuid4().hex, "action": "shutdown", "payload": {}})
        except Exception:
            pass
        self._poll_timer.stop()
        if self._process.is_alive():
            self._process.join(timeout=3)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=2)

    def request_shutdown_background(self) -> None:
        try:
            self._task_queue.put({"id": uuid.uuid4().hex, "action": "shutdown", "payload": {}})
        except Exception:
            pass
        self._poll_timer.stop()
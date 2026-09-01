from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ThemeDefinition:
    id: str
    name: dict[str, str]
    is_light: bool
    stylesheet: str


@dataclass
class AppSettings:
    theme: str = "light"
    accent_color: str = "#7380ff"
    language: str = "en"
    start_in_tray: bool = False
    autostart_windows: bool = False
    auto_run_components: bool = False
    check_updates_on_start: bool = True
    update_branch: str = "release"
    notifications_enabled: bool = True
    work_root: str = ""
    active_profile_id: str = "default"
    enabled_component_ids: list[str] = field(default_factory=list)
    autostart_component_ids: list[str] = field(default_factory=list)
    component_selection_initialized: bool = False
    enabled_mod_ids: list[str] = field(default_factory=list)
    pending_mod_welcome: dict[str, str] = field(default_factory=dict)
    seen_mod_welcomes: dict[str, str] = field(default_factory=dict)
    mods_index_url: str = ""
    app_update_url: str = ""
    tg_proxy_host: str = "127.0.0.1"
    tg_proxy_port: int = 1443
    tg_proxy_secret: str = ""
    tg_proxy_dc_ip: str = "2:149.154.167.51\n4:149.154.167.91"
    tg_proxy_cfproxy_enabled: bool = True
    tg_proxy_cfproxy_priority: bool = True
    tg_proxy_cfproxy_domain: str = ""
    tg_proxy_fake_tls_domain: str = ""
    tg_proxy_buf_kb: int = 256
    tg_proxy_pool_size: int = 4
    tg_proxy_link_prompt_signature: str = ""
    tg_proxy_media_mode: str = "default"
    tg_proxy_tuning_done: bool = False
    selected_zapret_general: str = ""
    zapret_interface: str = "any"
    favorite_zapret_generals: list[str] = field(default_factory=list)
    general_autotest_done: bool = False
    selected_service_ids: list[str] = field(default_factory=list)
    selected_runtime_mode: str = "zapret"
    zapret_ipset_mode: str = "loaded"
    zapret_game_filter_mode: str = "disabled"
    zapret_udp_exclude_ports: str = "51820"
    zapret_block_quic: bool = False
    selected_dns_preset: str = ""
    apply_update_on_next_launch: bool = False
    dismissed_component_updates: dict[str, str] = field(default_factory=dict)
    discord_rpc_enabled: bool = True
    discord_rpc_detail: str = "Использует для обхода блокировок."


@dataclass(slots=True)
class ComponentDefinition:
    id: str
    name: str
    description: str
    version: str
    source: str
    command: list[str]
    enabled: bool = True
    autostart: bool = True


@dataclass(slots=True)
class ComponentState:
    component_id: str
    status: str = "stopped"
    pid: int | None = None
    last_error: str = ""


@dataclass(slots=True)
class ConfigProfile:
    id: str
    name: str
    description: str
    base_config_path: str
    settings_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModIndexItem:
    id: str
    name: str
    description: str
    author: str
    version: str
    source_url: str
    category: str = "general"
    tags: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    changelog: str = ""


@dataclass(slots=True)
class InstalledMod:
    id: str
    version: str
    path: str
    name: str = ""
    author: str = ""
    description: str = ""
    source_url: str = ""
    enabled: bool = False
    source_type: str = "generic"
    general_scripts: list[str] = field(default_factory=list)
    emoji: str = ""
    installed_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass(slots=True)
class MergeState:
    profile_id: str
    merged_path: str
    active_layers: list[str]
    rebuilt_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass(slots=True)
class BackupSnapshot:
    id: str
    created_at: str
    reason: str
    path: str


@dataclass(slots=True)
class UpdateInfo:
    target: str
    current_version: str
    latest_version: str
    status: str
    changelog: str = ""


@dataclass(slots=True)
class ReleaseEntry:
    version: str
    body: str
    html_url: str
    is_latest: bool = False


@dataclass(slots=True)
class DiagnosticResult:
    name: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LogEntry:
    timestamp: str
    level: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NotificationEntry:
    id: str
    level: str
    title: str
    message: str
    source: str = "app"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    read: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AppPaths:
    install_root: Path
    core_dir: Path
    runtime_dir: Path
    configs_dir: Path
    default_packs_dir: Path
    mods_dir: Path
    merged_runtime_dir: Path
    backups_dir: Path
    cache_dir: Path
    logs_dir: Path
    data_dir: Path
    ui_assets_dir: Path
    themes_dir: Path
    is_portable: bool = False


@dataclass(slots=True)
class FileRecord:
    path: str
    relative_path: str
    size: int

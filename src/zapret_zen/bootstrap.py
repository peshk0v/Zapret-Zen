from dataclasses import asdict, dataclass
from pathlib import Path
import shutil
import secrets
from typing import Any

from zapret_zen.domain import AppPaths
from zapret_zen.runtime_env import development_install_root, is_packaged_runtime, packaged_install_root, packaged_resource_root
from zapret_zen.ui.theme import load_theme_registry
from zapret_zen.services.autostart import AutostartManager
from zapret_zen.services.components import ProcessManager
from zapret_zen.services.diagnostics import DiagnosticsManager
from zapret_zen.services.files import FilesManager
from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.merge import MergeEngine
from zapret_zen.services.mods import ModsManager
from zapret_zen.services.notifications import NotificationManager
from zapret_zen.services.profiles import ProfilesManager
from zapret_zen.services.service_catalog import FORTNITE_GENERAL_PRIORITY
from zapret_zen.services.settings import SettingsManager
from zapret_zen.services.storage import StorageManager
from zapret_zen.services.updates import UpdatesManager

@dataclass(slots=True)
class ApplicationContext:
    paths: AppPaths
    storage: StorageManager
    settings: SettingsManager
    logging: LoggingManager
    autostart: AutostartManager
    processes: ProcessManager
    mods: ModsManager
    notifications: NotificationManager
    merge: MergeEngine
    diagnostics: DiagnosticsManager
    updates: UpdatesManager
    profiles: ProfilesManager
    files: FilesManager
    backend: Any | None = None


def bootstrap_application() -> ApplicationContext:
    if is_packaged_runtime():
        install_root = packaged_install_root()
        resource_root = packaged_resource_root()
    else:
        install_root = development_install_root(__file__)
        resource_root = install_root

    runtime_dir = install_root / "runtime"
    ui_assets_dir = install_root / "ui_assets"
    sample_data_dir = install_root / "sample_data"
    themes_dir = install_root / "themes"
    _hydrate_bundled_assets(
        resource_root=resource_root,
        install_root=install_root,
        runtime_dir=runtime_dir,
        ui_assets_dir=ui_assets_dir,
        sample_data_dir=sample_data_dir,
        themes_dir=themes_dir,
    )

    paths = AppPaths(
        install_root=install_root,
        core_dir=install_root / "core",
        runtime_dir=runtime_dir,
        configs_dir=install_root / "configs",
        default_packs_dir=install_root / "default_packs",
        mods_dir=install_root / "mods",
        merged_runtime_dir=install_root / "merged_runtime",
        backups_dir=install_root / "backups",
        cache_dir=install_root / "cache",
        logs_dir=install_root / "logs",
        data_dir=install_root / "data",
        ui_assets_dir=ui_assets_dir,
        themes_dir=install_root / "themes",
    )
    storage = StorageManager(paths)
    storage.ensure_layout()

    load_theme_registry(paths.themes_dir)

    settings = SettingsManager(storage)
    logging = LoggingManager(storage)
    autostart = AutostartManager(logging)
    processes = ProcessManager(storage, logging, settings)
    notifications = NotificationManager(storage)
    merge = MergeEngine(storage, logging, settings)
    mods = ModsManager(storage, logging, merge, settings, processes=processes)
    diagnostics = DiagnosticsManager(storage, logging, processes, mods, merge)
    updates = UpdatesManager(storage, logging, processes=processes)
    profiles = ProfilesManager(storage)
    files = FilesManager(storage, settings)
    _prime_first_run_state(settings, processes)

    return ApplicationContext(
        paths=paths,
        storage=storage,
        settings=settings,
        logging=logging,
        autostart=autostart,
        processes=processes,
        mods=mods,
        notifications=notifications,
        merge=merge,
        diagnostics=diagnostics,
        updates=updates,
        profiles=profiles,
        files=files,
        backend=None,
    )


def build_startup_snapshot(context: ApplicationContext) -> dict[str, Any]:
    current = context.settings.get()
    general_options = list(context.processes.list_zapret_generals())
    if not str(current.selected_zapret_general or "").strip() and general_options:
        context.settings.update(selected_zapret_general=str(general_options[0]["id"]))
        current = context.settings.get()
    return {
        "components": [asdict(item) for item in context.processes.list_components()],
        "states": [asdict(item) for item in context.processes.list_states()],
        "settings": {
            "selected_zapret_general": current.selected_zapret_general,
            "favorite_zapret_generals": list(current.favorite_zapret_generals or []),
            "enabled_mod_ids": list(current.enabled_mod_ids or []),
            "selected_runtime_mode": getattr(current, "selected_runtime_mode", "zapret"),
        },
        "general_options": general_options,
    }


def _prime_first_run_state(settings: SettingsManager, processes: ProcessManager) -> None:
    current = settings.get()
    changes: dict[str, Any] = {}
    if not (current.tg_proxy_secret or "").strip():
        changes["tg_proxy_secret"] = secrets.token_hex(16)
    if str(current.zapret_ipset_mode or "").strip() not in {"loaded", "none", "any"}:
        changes["zapret_ipset_mode"] = "loaded"
    if str(current.zapret_game_filter_mode or "").strip() not in {"disabled", "tcp", "udp", "tcpudp"}:
        changes["zapret_game_filter_mode"] = "disabled"
    if "fortnite" in set(current.selected_service_ids or []):
        changes["zapret_ipset_mode"] = "any"
        changes["zapret_game_filter_mode"] = "tcpudp"
        options = list(processes.list_zapret_generals())
        for wanted in FORTNITE_GENERAL_PRIORITY:
            match = next(
                (
                    option
                    for option in options
                    if str(option.get("name", "")).strip().lower() == wanted.lower()
                ),
                None,
            )
            if match is not None:
                changes["selected_zapret_general"] = str(match.get("id", "") or "")
                break
    if changes:
        settings.update(**changes)


def _hydrate_bundled_assets(
    resource_root: Path,
    install_root: Path,
    runtime_dir: Path,
    ui_assets_dir: Path,
    sample_data_dir: Path,
    themes_dir: Path,
) -> None:
    bundled_runtime = resource_root / "runtime"
    bundled_ui_assets = resource_root / "ui_assets"
    bundled_sample_data = resource_root / "sample_data"
    bundled_themes = resource_root / "themes"

    if bundled_runtime.exists() and not runtime_dir.exists():
        shutil.copytree(bundled_runtime, runtime_dir, dirs_exist_ok=True)

    if bundled_ui_assets.exists() and not ui_assets_dir.exists():
        shutil.copytree(bundled_ui_assets, ui_assets_dir, dirs_exist_ok=True)

    if bundled_sample_data.exists() and not sample_data_dir.exists():
        shutil.copytree(bundled_sample_data, sample_data_dir, dirs_exist_ok=True)

    if bundled_themes.exists() and not themes_dir.exists():
        shutil.copytree(bundled_themes, themes_dir, dirs_exist_ok=True)

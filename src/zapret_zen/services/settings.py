from __future__ import annotations

import locale
import sys
from collections.abc import Callable
from dataclasses import asdict

from zapret_zen.domain import AppSettings
from zapret_zen.platform import linux_default_native_window
from zapret_zen.services.service_catalog import SERVICE_PRESET_IDS
from zapret_zen.services.storage import StorageManager

if sys.platform.startswith("win"):
    import winreg


class SettingsManager:
    def __init__(self, storage: StorageManager) -> None:
        self.storage = storage
        self._settings_path = self.storage.paths.data_dir / "settings.json"
        self._settings = self.load()
        self._on_save_callbacks: list[Callable[[], None]] = []

    def add_on_save_callback(self, callback: Callable[[], None]) -> None:
        self._on_save_callbacks.append(callback)

    def remove_on_save_callback(self, callback: Callable[[], None]) -> None:
        try:
            self._on_save_callbacks.remove(callback)
        except ValueError:
            pass

    def load(self) -> AppSettings:
        raw = self.storage.read_json(self._settings_path, default={}) or {}
        allowed = {field.name for field in AppSettings.__dataclass_fields__.values()}
        raw = {key: value for key, value in raw.items() if key in allowed}
        settings = AppSettings(**raw)
        changed = False

        # One-time migration: preserve defaults from components.json on first load only.
        if not bool(raw.get("component_selection_initialized", False)):
            components_raw = self.storage.read_json(self.storage.paths.data_dir / "components.json", default=[]) or []
            if isinstance(components_raw, list):
                enabled_defaults: list[str] = []
                autostart_defaults: list[str] = []
                for item in components_raw:
                    if not isinstance(item, dict):
                        continue
                    cid = str(item.get("id", "")).strip()
                    if not cid:
                        continue
                    if bool(item.get("enabled", False)):
                        enabled_defaults.append(cid)
                    if bool(item.get("autostart", False)):
                        autostart_defaults.append(cid)
                settings.enabled_component_ids = enabled_defaults
                settings.autostart_component_ids = autostart_defaults
            settings.component_selection_initialized = True
            changed = True

        if not raw.get("language"):
            settings.language = self._detect_system_language()
            changed = True

        # First run / upgrade: prefer a compositor-managed (native) window on
        # Wayland compositors (e.g. Hyprland) where client-side decoration
        # conflicts with the compositor's own border/rounding.
        if "native_window_on_linux" not in raw:
            settings.native_window_on_linux = linux_default_native_window()
            changed = True

        old_theme = raw.get("theme", "")
        old_accent = raw.get("accent_color", "")
        if old_theme and old_theme not in {"light", "dark", "oled"}:
            # Migration from old theme system to new mode + accent
            old_theme_lower = old_theme.lower().replace(" ", "_")
            if old_theme == "midnight":
                settings.theme = "dark"
                if not old_accent:
                    settings.accent_color = "#7380ff"
                changed = True
            elif old_theme_lower in ("light_blue", "light blue"):
                settings.theme = "light"
                if not old_accent:
                    settings.accent_color = "#3b82f6"
                changed = True
            elif old_theme_lower in ("night", "dark"):
                settings.theme = "dark"
                if not old_accent:
                    settings.accent_color = "#7380ff"
                changed = True
            elif old_theme_lower == "oled":
                settings.theme = "oled"
                if not old_accent:
                    settings.accent_color = "#7380ff"
                changed = True
            else:
                # Other external themes -> light with default accent
                settings.theme = "light"
                if not old_accent:
                    settings.accent_color = "#7380ff"
                changed = True
        elif not old_theme:
            settings.theme = self._detect_system_theme()
            changed = True
        if old_accent and len(old_accent) == 7 and old_accent[0] == "#":
            settings.accent_color = old_accent

        if raw.get("zapret_ipset_mode") not in {"loaded", "none", "any"}:
            settings.zapret_ipset_mode = "loaded"
            changed = True

        if raw.get("zapret_game_filter_mode") == "all":
            settings.zapret_game_filter_mode = "tcpudp"
            changed = True
        elif raw.get("zapret_game_filter_mode") == "auto":
            settings.zapret_game_filter_mode = "disabled"
            changed = True
        elif raw.get("zapret_game_filter_mode") not in {"disabled", "tcp", "udp", "tcpudp"}:
            settings.zapret_game_filter_mode = "disabled"
            changed = True

        if raw.get("selected_runtime_mode") not in {"zapret"}:
            settings.selected_runtime_mode = "zapret"
            changed = True

        dc_ip_raw = str(settings.tg_proxy_dc_ip or "").strip()
        dc_ips_present = set()
        for part in dc_ip_raw.replace("\n", ",").split(","):
            token = part.strip()
            if token and ":" in token:
                dc_ips_present.add(token.split(":")[0])
        if "2" not in dc_ips_present and "4" in dc_ips_present:
            settings.tg_proxy_dc_ip = "2:149.154.167.51\n4:149.154.167.91"
            changed = True

        selected_service_ids = raw.get("selected_service_ids", [])
        if not isinstance(selected_service_ids, list):
            settings.selected_service_ids = []
            changed = True
        else:
            migrated_service_ids = [
                "clouds" if str(item).strip() == "steam" else "fortnite" if str(item).strip() == "twitch" else str(item).strip()
                for item in selected_service_ids
            ]
            normalized_service_ids = [item for item in migrated_service_ids if item in SERVICE_PRESET_IDS]
            if normalized_service_ids != list(settings.selected_service_ids):
                settings.selected_service_ids = normalized_service_ids
                changed = True
            if "fortnite" in normalized_service_ids:
                if settings.zapret_ipset_mode != "any":
                    settings.zapret_ipset_mode = "any"
                    changed = True
                if settings.zapret_game_filter_mode != "tcpudp":
                    settings.zapret_game_filter_mode = "tcpudp"
                    changed = True

        if changed:
            self.storage.write_json(self._settings_path, asdict(settings))
        return settings

    def get(self) -> AppSettings:
        return self._settings

    def reload(self) -> AppSettings:
        self._settings = self.load()
        return self._settings

    def update(self, **changes: object) -> AppSettings:
        for key, value in changes.items():
            setattr(self._settings, key, value)
        self.save()
        return self._settings

    def save(self) -> None:
        self.storage.write_json(self._settings_path, asdict(self._settings))
        for cb in self._on_save_callbacks:
            try:
                cb()
            except Exception:
                pass

    def _detect_system_language(self) -> str:
        if sys.platform.startswith("win"):
            try:
                import ctypes
                lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
                primary = lang_id & 0x3FF
                return "ru" if primary == 0x19 else "en"
            except Exception:
                pass
        try:
            locale_name = (locale.getdefaultlocale()[0] or "").lower()
        except Exception:
            locale_name = ""
        return "ru" if locale_name.startswith("ru") else "en"

    def _detect_system_theme(self) -> str:
        if not sys.platform.startswith("win"):
            return "dark"
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                0,
                winreg.KEY_READ,
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "light" if int(value) == 1 else "dark"
        except Exception:
            return "dark"

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from zapret_zen.domain import ConfigProfile
from zapret_zen.services.settings import SettingsManager
from zapret_zen.services.storage import StorageManager

_SNAPSHOT_SKIP = {
    "active_profile_id",
    "apply_update_on_next_launch",
    "dismissed_component_updates",
    "dismissed_app_update_key",
    "notifications_enabled",
    "work_root",
    "zapret_block_quic",
    "pending_mod_welcome",
    "seen_mod_welcomes",
    "selected_service_ids",
    "tg_proxy_host",
    "tg_proxy_port",
    "tg_proxy_secret",
    "tg_proxy_dc_ip",
    "tg_proxy_cfproxy_enabled",
    "tg_proxy_cfproxy_priority",
    "tg_proxy_cfproxy_domain",
    "tg_proxy_fake_tls_domain",
    "tg_proxy_buf_kb",
    "tg_proxy_pool_size",
    "tg_proxy_link_prompt_signature",
    "tg_proxy_media_mode",
}


def _slug(name: str) -> str:
    s = name.lower().strip().replace(" ", "-")
    s = re.sub(r"[^a-z0-9_-]", "", s)
    return s or "profile"


class ProfilesManager:
    def __init__(self, storage: StorageManager) -> None:
        self.storage = storage
        self._profiles_path = self.storage.paths.data_dir / "profiles.json"

    def list_profiles(self) -> list[ConfigProfile]:
        raw = self.storage.read_json(self._profiles_path, default=[]) or []
        return [ConfigProfile(**item) for item in raw]

    def _save(self, profiles: list[ConfigProfile]) -> None:
        self.storage.write_json(
            self._profiles_path,
            [asdict(p) for p in profiles],
        )

    def get_profile(self, profile_id: str) -> ConfigProfile | None:
        for p in self.list_profiles():
            if p.id == profile_id:
                return p
        return None

    def create_profile(self, name: str, snapshot: dict[str, Any]) -> ConfigProfile:
        profiles = self.list_profiles()
        pid = _slug(name)
        base = pid
        counter = 1
        while any(p.id == pid for p in profiles):
            pid = f"{base}-{counter}"
            counter += 1
        profile = ConfigProfile(
            id=pid,
            name=name,
            description="",
            base_config_path="",
            settings_snapshot=snapshot,
        )
        profiles.append(profile)
        self._save(profiles)
        return profile

    def update_profile(self, profile_id: str, **changes: Any) -> None:
        profiles = self.list_profiles()
        for p in profiles:
            if p.id == profile_id:
                for key, value in changes.items():
                    setattr(p, key, value)
                break
        self._save(profiles)

    def delete_profile(self, profile_id: str) -> None:
        if profile_id == "default":
            return
        profiles = self.list_profiles()
        profiles = [p for p in profiles if p.id != profile_id]
        self._save(profiles)

    def save_profile_snapshot(self, profile_id: str, settings_mgr: SettingsManager) -> None:
        snapshot = self._make_snapshot(settings_mgr)
        profiles = self.list_profiles()
        for p in profiles:
            if p.id == profile_id:
                p.settings_snapshot = snapshot
                break
        self._save(profiles)

    def switch_profile(self, profile_id: str, settings_mgr: SettingsManager) -> None:
        target = self.get_profile(profile_id)
        if target is None:
            return

        current_id = settings_mgr.get().active_profile_id
        if current_id and current_id != profile_id:
            self.save_profile_snapshot(current_id, settings_mgr)

        snapshot = target.settings_snapshot
        if snapshot:
            settings_mgr.update(**snapshot)

        settings_mgr.update(active_profile_id=profile_id)

    def ensure_default_exists(self, settings_mgr: SettingsManager) -> None:
        profiles = self.list_profiles()
        if any(p.id == "default" for p in profiles):
            return
        snapshot = self._make_snapshot(settings_mgr)
        default_profile = ConfigProfile(
            id="default",
            name="Основной",
            description="Основной профиль",
            base_config_path="",
            settings_snapshot=snapshot,
        )
        profiles.insert(0, default_profile)
        self._save(profiles)
        settings_mgr.update(active_profile_id="default")

    @staticmethod
    def _make_snapshot(settings_mgr: SettingsManager) -> dict[str, Any]:
        raw = asdict(settings_mgr.get())
        return {k: v for k, v in raw.items() if k not in _SNAPSHOT_SKIP}

from __future__ import annotations

import re
import shutil
import sys
import ctypes
from pathlib import Path
from typing import Any

from zapret_zen.platform_utils import IS_WINDOWS, system_hosts_path
from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.service_catalog import ALWAYS_APPLY_SERVICE_IDS
from zapret_zen.services.service_rules import SERVICE_RULES
from zapret_zen.services.settings import SettingsManager
from zapret_zen.services.storage import StorageManager


def _strip_zone_identifier(path: Path) -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.DeleteFileW(str(path) + ":Zone.Identifier")
    except Exception:
        pass


class ZapretRuntimeBuilder:
    """Builds merged zapret runtime from base + bundles + mods + service rules."""

    def __init__(
        self,
        storage: StorageManager,
        logging: LoggingManager,
        settings: SettingsManager,
    ) -> None:
        self.storage = storage
        self.logging = logging
        self.settings = settings

    def get_zapret_bundles(self, enabled_only: bool, *, include_hidden_generals: bool = False) -> list[dict[str, Any]]:
        bundles: list[dict[str, Any]] = []
        if IS_WINDOWS:
            base = self.storage.paths.runtime_dir / "zapret-discord-youtube"
        else:
            base = self.storage.paths.runtime_dir / "zapret-discord-youtube-rust"
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
            title = str(raw.get("name") or "").strip() or index_map.get(mod_id) or mod_id
            bundles.append({"id": mod_id, "title": title, "path": path})
        if base.exists():
            bundles.append({"id": "base", "title": "", "path": base})
        return bundles

    def general_option_sort_key(self, item: dict[str, str]) -> tuple[int, int, str]:
        bundle_id = str(item.get("bundle_id", ""))
        name = str(item.get("name", ""))
        lowered = name.lower()
        number = -1
        match = re.search(r"alt\s*(\d+)", lowered)
        if match:
            number = int(match.group(1))
        elif lowered == "general.bat":
            number = 0
        modified_rank = 0 if bundle_id != "base" else 2
        return (modified_rank, -number, lowered)

    def prepare_active_zapret_runtime(self, selected_bundle_root: Path, selected_bundle_id: str, selected_script_name: str) -> Path:
        self._cleanup_inactive_zapret_runtimes()
        active_root = self._next_active_runtime_dir()
        if IS_WINDOWS:
            base_root = self.storage.paths.runtime_dir / "zapret-discord-youtube"
        else:
            base_root = self.storage.paths.runtime_dir / "zapret-discord-youtube-rust"
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

        layered_bundles = self.get_zapret_bundles(enabled_only=True)
        for bundle in layered_bundles:
            bundle_id = bundle["id"]
            bundle_root = Path(bundle["path"])
            if bundle_id != "base":
                self._overlay_bundle_runtime(active_root, bundle_root)
            lists_source = bundle_root / "lists"
            if lists_source.exists():
                self._merge_lists_into_target(lists_target, lists_source)

        self._apply_selected_service_rules(active_root)

        selected_script = selected_bundle_root / selected_script_name
        if selected_script.exists():
            shutil.copy2(selected_script, active_root / selected_script.name)

        self._apply_user_collection_overrides(lists_target)
        self._apply_system_hosts_from_mods()
        self._materialize_visible_merged_runtime(active_root)
        return active_root

    def _overlay_bundle_runtime(self, active_root: Path, bundle_root: Path) -> None:
        for script in bundle_root.glob("*.bat"):
            if script.name.lower().startswith("service"):
                continue
            dest = active_root / script.name
            shutil.copy2(script, dest)
            _strip_zone_identifier(dest)
        for folder_name in ("bin", "utils"):
            source_dir = bundle_root / folder_name
            target_dir = active_root / folder_name
            if not source_dir.exists():
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            for source in source_dir.glob("*"):
                if source.is_file():
                    dest = target_dir / source.name
                    shutil.copy2(source, dest)
                    _strip_zone_identifier(dest)

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
        self, target_lists: Path, filename: str, existing: list[str], incoming: list[str],
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
                incoming_lines = self._read_list_lines(source)
                if not incoming_lines:
                    continue
                target = lists_dir / safe_name
                existing = self._read_list_lines(target)
                merged = self._merge_with_conflict_resolution(lists_dir, safe_name.lower(), existing, incoming_lines)
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

    def apply_service_command_extensions(self, command: list[str], *, lists_dir: Path) -> list[str]:
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

    def _read_hosts_lines(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        return [raw.rstrip() for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines()]

    def _collect_mod_hosts_entries(self) -> list[str]:
        entries: list[str] = []
        seen: set[str] = set()
        installed = self.storage.read_json(self.storage.paths.data_dir / "installed_mods.json", default=[]) or []
        for item in installed:
            if not isinstance(item, dict) or not bool(item.get("enabled")):
                continue
            mod_path = Path(str(item.get("path", "")))
            if not mod_path.exists():
                continue
            for candidate in [mod_path / "hosts", mod_path / "lists" / "hosts.txt", mod_path / "lists" / "hosts"]:
                if not candidate.exists() or not candidate.is_file():
                    continue
                for raw in candidate.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    key = " ".join(line.split()).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    entries.append(line)
        settings = self.settings.get()
        selected = set(settings.selected_service_ids or [])
        for service_id in sorted(selected | set(ALWAYS_APPLY_SERVICE_IDS)):
            rule = SERVICE_RULES.get(service_id)
            if rule is None:
                continue
            for entry in rule.hosts:
                key = " ".join(entry.split()).lower()
                if key in seen:
                    continue
                seen.add(key)
                entries.append(entry)
        return entries

    _SYSTEM_HOSTS_PATH = system_hosts_path()
    _MARKER_START = "# === Zapret Zen START ==="
    _MARKER_END = "# === Zapret Zen END ==="

    def _apply_system_hosts_from_mods(self) -> None:
        entries = self._collect_mod_hosts_entries()
        try:
            content = self._SYSTEM_HOSTS_PATH.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        lines = content.splitlines()
        start_idx = -1
        end_idx = -1
        for i, line in enumerate(lines):
            if line.strip() == self._MARKER_START:
                start_idx = i
            elif line.strip() == self._MARKER_END:
                end_idx = i
        if start_idx >= 0 and end_idx > start_idx:
            before = lines[:start_idx]
            after = lines[end_idx + 1:]
        elif start_idx >= 0:
            before = lines[:start_idx]
            after = []
        else:
            before = lines
            after = []
        while before and not before[-1].strip():
            before.pop()
        while after and not after[0].strip():
            after.pop(0)
        new_lines = list(before)
        if entries:
            if new_lines and new_lines[-1].strip():
                new_lines.append("")
            new_lines.append(self._MARKER_START)
            for entry in entries:
                new_lines.append(entry)
            new_lines.append(self._MARKER_END)
        if after:
            if new_lines and new_lines[-1].strip():
                new_lines.append("")
            new_lines.extend(after)
        result = "\n".join(new_lines)
        if not result.endswith("\n"):
            result += "\n"
        try:
            self._SYSTEM_HOSTS_PATH.write_text(result, encoding="utf-8")
        except PermissionError:
            self.logging.log("error", "System hosts write failed: no permission", path=str(self._SYSTEM_HOSTS_PATH))
        except Exception as exc:
            self.logging.log("error", "System hosts write failed", error=str(exc))

    def _ensure_zapret_user_lists(self, lists_dir: Path) -> None:
        for filename in ("list-general-user.txt", "list-exclude-user.txt", "ipset-all-user.txt", "ipset-exclude-user.txt"):
            target = lists_dir / filename
            if not target.exists():
                target.write_text("", encoding="utf-8")

    def _next_active_runtime_dir(self) -> Path:
        merged_root = self.storage.paths.merged_runtime_dir
        merged_root.mkdir(parents=True, exist_ok=True)
        for index in range(100):
            candidate = merged_root / f"_active_{index}"
            if not candidate.exists():
                return candidate
        return merged_root / "_active_fallback"

    def _cleanup_inactive_zapret_runtimes(self) -> None:
        merged_root = self.storage.paths.merged_runtime_dir
        if not merged_root.exists():
            return
        for entry in merged_root.iterdir():
            if entry.is_dir() and entry.name.startswith("_active_") and entry.name != "_active_0":
                try:
                    shutil.rmtree(entry, ignore_errors=True)
                except Exception:
                    pass

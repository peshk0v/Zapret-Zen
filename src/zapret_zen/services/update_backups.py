from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from zapret_zen.domain import UpdateBackupInfo
from zapret_zen.runtime_env import is_packaged_runtime
from zapret_zen.services.logging_service import LoggingManager
from zapret_zen.services.storage import StorageManager

# Каталоги и файлы, которые НЕ попадают в бэкап обновления и не удаляются при
# замене: это пользовательские данные/кэш/прочие бэкапы (тот же список, что
# preserve в PS-апдейтере). Бэкапим только replaceable-файлы приложения.
_PRESERVE_NAMES = ("data", "mods", "configs", "cache", "logs", "backups")
_PRESERVE_PREFIX = "unins000"

_UPDATE_BACKUP_SUBDIR = "update"
_META_FILE = "backup.json"
_PAYLOAD_DIR = "payload"

# Fallback-размер бэкапа, если ни одного ещё нет (≈ типичный portable payload).
_FALLBACK_BACKUP_SIZE = 400 * 1024 * 1024


def _is_preserved(name: str) -> bool:
    return name in _PRESERVE_NAMES or name.startswith(_PRESERVE_PREFIX)


class UpdateBackupManager:
    """Именованные бэкапы установочной директории перед обновлением приложения.

    Отдельно от generic ``StorageManager.create_backup``: здесь бэкап имеет
    версию/размер/метаданные, привязан к счётчику ``update_backup_keep_count``
    и умеет восстанавливаться поверх install_root (для авто- и ручного отката).
    """

    def __init__(self, storage: StorageManager, logging: LoggingManager) -> None:
        self.storage = storage
        self.logging = logging

    # ── Paths ────────────────────────────────────────────────────────────────
    @property
    def _update_root(self) -> Path:
        return self.storage.paths.backups_dir / _UPDATE_BACKUP_SUBDIR

    def _backup_dir(self, backup_id: str) -> Path:
        return self._update_root / backup_id

    def _payload_dir(self, backup_id: str) -> Path:
        return self._backup_dir(backup_id) / _PAYLOAD_DIR

    def _meta_path(self, backup_id: str) -> Path:
        return self._backup_dir(backup_id) / _META_FILE

    # ── Helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _backup_id(version_from: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe_version = "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in (version_from or "unknown"))
        return f"{stamp}_{safe_version}"

    @staticmethod
    def _dir_size(path: Path) -> int:
        total = 0
        try:
            for entry in path.rglob("*"):
                try:
                    if entry.is_file() and not entry.is_symlink():
                        total += entry.stat().st_size
                except OSError:
                    continue
        except OSError:
            pass
        return total

    def _copy_replaceable(self, source: Path, destination: Path) -> None:
        """Копирует install_root -> destination, пропуская preserve-элементы."""
        destination.mkdir(parents=True, exist_ok=True)
        try:
            entries = list(source.iterdir())
        except OSError as error:
            self.logging.log("warning", "Update backup: cannot list install root", error=str(error))
            return
        for entry in entries:
            if _is_preserved(entry.name):
                continue
            target = destination / entry.name
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.copytree(entry, target, dirs_exist_ok=True, symlinks=True)
                elif entry.is_file() or entry.is_symlink():
                    shutil.copy2(entry, target)
            except OSError as error:
                self.logging.log("warning", "Update backup: copy failed", item=str(entry), error=str(error))

    # ── Public API ───────────────────────────────────────────────────────────
    def create_update_backup(self, version_from: str) -> UpdateBackupInfo | None:
        """Создаёт бэкап replaceable-файлов install_root перед обновлением.

        Возвращает ``None`` в dev-режиме (нечего заменять поверх git-репо) либо
        при критической ошибке копирования — вызывающая сторона решает, продолжать
        обновление без бэкапа или нет.
        """
        if not is_packaged_runtime():
            return None
        install_root = self.storage.paths.install_root
        if not install_root.is_dir():
            return None

        backup_id = self._backup_id(version_from)
        payload = self._payload_dir(backup_id)
        try:
            payload.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self.logging.log("error", "Update backup: cannot create payload dir", error=str(error))
            return None

        self._copy_replaceable(install_root, payload)
        size_bytes = self._dir_size(payload)
        info = UpdateBackupInfo(
            id=backup_id,
            version_from=version_from or "unknown",
            created_at=datetime.now(timezone.utc).isoformat(),
            size_bytes=size_bytes,
            path=str(self._backup_dir(backup_id)),
        )
        try:
            self.storage.write_json(
                self._meta_path(backup_id),
                {
                    "id": info.id,
                    "version_from": info.version_from,
                    "created_at": info.created_at,
                    "size_bytes": info.size_bytes,
                    "kind": "update",
                },
            )
        except Exception as error:
            self.logging.log("warning", "Update backup: metadata write failed", error=str(error))

        self.logging.log(
            "info",
            "Update backup created",
            id=backup_id,
            version_from=info.version_from,
            size_bytes=size_bytes,
        )
        return info

    def list_update_backups(self) -> list[UpdateBackupInfo]:
        """Возвращает бэкапы, отсортированные от свежего к старому."""
        root = self._update_root
        if not root.is_dir():
            return []
        items: list[UpdateBackupInfo] = []
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            info = self._read_meta(entry)
            if info is not None:
                items.append(info)
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items

    def _read_meta(self, backup_dir: Path) -> UpdateBackupInfo | None:
        meta_path = backup_dir / _META_FILE
        try:
            data = self.storage.read_json(meta_path, default={}) or {}
        except Exception:
            data = {}
        if not data:
            # Метадата битая/отсутствует — реконструируем из имени и размера.
            payload = backup_dir / _PAYLOAD_DIR
            size = self._dir_size(payload) if payload.is_dir() else 0
            if size == 0 and not payload.is_dir():
                return None
            name = backup_dir.name
            version = name.split("_", 1)[1] if "_" in name else "unknown"
            try:
                created = datetime.fromtimestamp(backup_dir.stat().st_mtime, tz=timezone.utc).isoformat()
            except OSError:
                created = datetime.now(timezone.utc).isoformat()
            return UpdateBackupInfo(
                id=name,
                version_from=version,
                created_at=created,
                size_bytes=size,
                path=str(backup_dir),
            )
        return UpdateBackupInfo(
            id=str(data.get("id") or backup_dir.name),
            version_from=str(data.get("version_from") or "unknown"),
            created_at=str(data.get("created_at") or ""),
            size_bytes=int(data.get("size_bytes") or 0),
            path=str(backup_dir),
        )

    def get_backup(self, backup_id: str) -> UpdateBackupInfo | None:
        backup_dir = self._backup_dir(backup_id)
        if not backup_dir.is_dir():
            return None
        return self._read_meta(backup_dir)

    def prune(self, keep_count: int) -> int:
        """Оставляет ``keep_count`` свежих бэкапов, удаляет остальные."""
        keep_count = max(1, min(5, int(keep_count)))
        items = self.list_update_backups()
        removed = 0
        for info in items[keep_count:]:
            try:
                shutil.rmtree(info.path, ignore_errors=True)
                removed += 1
            except OSError:
                pass
        if removed:
            self.logging.log("info", "Update backups pruned", removed=removed, keep=keep_count)
        return removed

    def estimate_total_size(self, keep_count: int) -> int:
        """Оценка суммарного размера при ``keep_count`` бэкапах.

        Реальный размер умножаем на ``keep_count`` (бэкапы примерно одинакового
        размера). Если бэкапов ещё нет — берём fallback-оценку portable payload.
        """
        keep_count = max(1, min(5, int(keep_count)))
        items = self.list_update_backups()
        if items:
            avg = sum(item.size_bytes for item in items) / len(items)
        else:
            avg = float(_FALLBACK_BACKUP_SIZE)
        return int(avg * keep_count)

    def restore_update_backup(self, backup_id: str) -> bool:
        """Восстанавливает payload бэкапа поверх install_root (для отката).

        Копирует только replaceable-элементы, preserve-каталоги не трогает.
        Вызывается внешним откат-скриптом (Приложение не может заменить собственный
        запущенный exe) — здесь чистая логика восстановления.
        """
        if not is_packaged_runtime():
            return False
        payload = self._payload_dir(backup_id)
        install_root = self.storage.paths.install_root
        if not payload.is_dir():
            self.logging.log("error", "Update rollback: payload missing", id=backup_id)
            return False

        try:
            entries = list(payload.iterdir())
        except OSError as error:
            self.logging.log("error", "Update rollback: cannot list payload", error=str(error))
            return False

        for entry in entries:
            if _is_preserved(entry.name):
                continue
            target = install_root / entry.name
            try:
                if entry.is_dir() and not entry.is_symlink():
                    if target.exists():
                        shutil.rmtree(target, ignore_errors=True)
                    shutil.copytree(entry, target, dirs_exist_ok=True, symlinks=True)
                elif entry.is_file() or entry.is_symlink():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(entry, target)
            except OSError as error:
                self.logging.log("warning", "Update rollback: restore failed", item=str(entry), error=str(error))
                return False

        self.logging.log("info", "Update rollback restored", id=backup_id, install_root=str(install_root))
        return True

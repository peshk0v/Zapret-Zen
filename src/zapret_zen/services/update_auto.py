from __future__ import annotations

from zapret_zen import __version__
from zapret_zen.runtime_env import is_packaged_runtime
from zapret_zen.services.update_guard import write_update_state
from zapret_zen.services.updates import UpdatesManager

# Ранний apply-хук фонового авто-апдейта.
#
# Модель: фон на каждом старте тихо скачивает обновление в staging
# (cache/pending_update/), а установка происходит только на СЛЕДУЮЩЕМ запуске —
# здесь, до создания MainWindow: создаётся бэкап текущей версии, пишется фаза
# "installed" (см. update_guard) и делегируется launch_update (внешний PS:
# замена каталога + relaunch новой версии). Авто-рестарт garantируется PS-скриптом.


def apply_pending_staged_update(context) -> bool:
    """Применяет staged-обновление, если оно валидно.

    Возвращает True, если апдейтер запущен и приложение обязано молча выйти
    (саму замену и relaunch выполняет внешний скрипт). False — продолжаем
    обычный запуск.
    """
    # Dev-режим: заменять поверх git-репо нечего — staging считаем мусором.
    if not is_packaged_runtime():
        context.updates.discard_staged_update()
        return False

    settings = context.settings.get()
    if not settings.auto_update_enabled:
        # Пользователь выключил авто-обновку после скачивания — уважаем выбор.
        context.updates.discard_staged_update()
        return False

    prepared = context.updates.consume_staged_update()
    if not prepared:
        return False

    # Защита от петли: staged-версия обязана быть новее текущей (актуально после
    # авто-отката — битый пакет не должен ставиться снова и снова).
    staged_version = str(prepared.get("version", "") or "")
    if staged_version and UpdatesManager._version_key(staged_version) <= UpdatesManager._version_key(__version__):
        context.updates.discard_staged_update()
        context.logging.log(
            "info",
            "Staged update skipped (not newer than current)",
            staged=staged_version,
            current=__version__,
        )
        return False

    backup_id = ""
    try:
        info = context.update_backups.create_update_backup(__version__)
        if info is not None:
            backup_id = info.id
            context.update_backups.prune(settings.update_backup_keep_count)
    except Exception as backup_error:
        context.logging.log("warning", "Staged update backup failed", error=str(backup_error))

    write_update_state(
        phase="installed",
        backup_id=backup_id,
        version=staged_version,
        attempts=0,
    )
    context.updates.launch_update(prepared)
    context.logging.log("info", "Staged update applying", version=staged_version, backup_id=backup_id)
    return True

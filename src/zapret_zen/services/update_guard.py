from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from zapret_zen.runtime_env import (
    development_install_root,
    is_packaged_runtime,
    packaged_install_root,
)

# Валидация запуска после обновления и авто-откат.
#
# Файл-маркер update_state.json (в cache/, переживает апдейт) хранит фазу:
#   installed  -> обновление применено, ждём первого запуска новой версии
#   launching  -> запущены, но ещё не подтвердили здоровый старт
#   healthy    -> окно показано, старт подтверждён
#   rolled_back-> выполнен авто-откат (дальше обычный старт)
#   unhealthy  -> откат исчерпан/невозможен, продолжаем обычный запуск
#
# Если при старте видим "launching" — прошлая попытка стартовала, но не дошла
# до "healthy" (критический краш при запуске) -> откатываемся к бэкапу.

_STATE_FILE = "update_state.json"
_MAX_ROLLBACK_ATTEMPTS = 1


def _resolve_cache_dir() -> Path:
    """Локализует cache/ тем же способом, что и bootstrap — но без него.

    Guard должен работать даже если bootstrap/Qt падает, поэтому путь считаем
    на stdlib+runtime_env, без импорта тяжёлых модулей.
    """
    if is_packaged_runtime():
        install_root = packaged_install_root()
    else:
        install_root = development_install_root(__file__)
    portable_flag = install_root / "portable.flag"
    is_portable = not is_packaged_runtime() or portable_flag.exists()
    if is_portable:
        data_root = install_root
    else:
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        data_root = Path(appdata) / "Zapret-Zen"
    return data_root / "cache"


def _state_path() -> Path:
    return _resolve_cache_dir() / _STATE_FILE


def read_update_state() -> dict:
    try:
        raw = _state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_update_state(**fields: object) -> dict:
    state = read_update_state()
    state.update(fields)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        cache_dir = _resolve_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass
    return state


def clear_update_state() -> None:
    try:
        _state_path().unlink(missing_ok=True)  # type: ignore[arg-type]
    except TypeError:
        # Python<3.8 совместимость (нет missing_ok) — не критично.
        try:
            _state_path().unlink()
        except OSError:
            pass
    except OSError:
        pass


def confirm_launch_healthy() -> None:
    """Вызывается после успешного показа окна: старт подтверждён."""
    state = read_update_state()
    if str(state.get("phase", "")) == "launching":
        write_update_state(phase="healthy")


def start_manual_rollback(backup_id: str) -> bool:
    """Ручной откат к выбранному бэкапу (кнопка «Rollback to backup»).

    Явное действие пользователя: спавнит тот же внешний PS-скрипт, что и
    авто-откат, и помечает фазу rolling_back, чтобы state-machine не пересекалась
    с этим действием. Возвращает True, если скрипт запущен — вызывающая сторона
    должна молча завершить приложение (скрипт ждёт выхода PID, восстанавливает
    файлы и перезапускает восстановленную версию).
    """
    if not is_packaged_runtime() or not sys.platform.startswith("win"):
        return False
    if not backup_id:
        return False
    if not _spawn_rollback(backup_id):
        return False
    write_update_state(phase="rolling_back", backup_id=backup_id, manual=True)
    return True


def evaluate_startup_update_state() -> str:
    """Решает судьбу запуска по фазе обновления.

    Возвращает "continue" (обычный старт) либо "rollback" (спавнен внешний
    откат-скрипт, текущий процесс должен молча завершиться).
    """
    if not is_packaged_runtime() or not sys.platform.startswith("win"):
        return "continue"
    state = read_update_state()
    phase = str(state.get("phase", ""))
    if phase == "installed":
        write_update_state(phase="launching", attempts=int(state.get("attempts", 0) or 0) + 1)
        return "continue"
    if phase == "launching":
        backup_id = str(state.get("backup_id", "") or "")
        attempts = int(state.get("attempts", 0) or 0)
        if backup_id and attempts <= _MAX_ROLLBACK_ATTEMPTS:
            spawned = _spawn_rollback(backup_id)
            if spawned:
                write_update_state(phase="rolling_back")
                return "rollback"
        # Откат невозможен/исчерпан — не зацикливаемся.
        write_update_state(phase="unhealthy")
        return "continue"
    return "continue"


# ── External rollback (PowerShell) ──────────────────────────────────────────
# Приложение не может заменить собственный загруженный exe/DLL, поэтому откат,
# как и установка, выполняется внешним скриптом: ждёт выход процесса, заменяет
# каталог установки из payload бэкапа и перезапускает восстановленную версию.

_ROLLBACK_PS1_TEMPLATE = """\
$ErrorActionPreference = 'SilentlyContinue'
$pidToWait = {{PID}}
$src = '{{SRC}}'
$dst = '{{DST}}'
$logPath = '{{LOG_PATH}}'
$statePath = '{{STATE_PATH}}'
$preserve = @('data', 'mods', 'configs', 'cache', 'logs', 'backups')

function Add-RbLog([string]$message) {
  try { Add-Content -LiteralPath $logPath -Value ('[' + (Get-Date -Format s) + '] ' + $message) } catch {}
}
function Remove-PathRobust([string]$targetPath) {
  if (-not (Test-Path $targetPath)) { return $true }
  for ($i = 0; $i -lt 6; $i++) {
    try { attrib -r -s -h $targetPath /s /d *> $null } catch {}
    try { Remove-Item $targetPath -Recurse -Force -ErrorAction Stop; return $true } catch { Start-Sleep -Milliseconds 300 }
  }
  return $false
}
function Is-Preserved([string]$name) {
  if ($preserve -contains $name) { return $true }
  return $name.StartsWith('unins000')
}
function Get-PythonRuntimeDlls([string]$dir) {
  return @(Get-ChildItem -LiteralPath $dir -File -Force -Filter 'python3*.dll' -ErrorAction SilentlyContinue)
}
function Test-InstalledStandalone([string]$targetDir) {
  return (Test-Path (Join-Path $targetDir 'zapret_zen.exe')) -and
         (Test-Path (Join-Path $targetDir 'python3.dll')) -and
         (@(Get-PythonRuntimeDlls $targetDir).Count -gt 0)
}

# ── WARP: версия из манифеста и выбор победителя ────────────────────────────
# runtime/warp может прийти и из payload (предустановка WARP с приложением),
# и от пользователя (он мог обновиться через автообновление компонента).
# Оставляем более новую версию; неизвестные/нечитаемые версии означают
# «сохранить пользовательскую» (консервативно, как до предустановки).
function Get-WarpManifestVersion([string]$dir) {
  $manifest = Join-Path $dir 'version.json'
  if (-not (Test-Path -LiteralPath $manifest)) { return '' }
  try {
    $data = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    $value = [string]$data.version
    if ($value) { return $value.Trim() }
  } catch {}
  return ''
}

function Resolve-WarpWinner([string]$stagingDir, [string]$destDir) {
  # 'payload' — предустановка из payload новее или равна сохранённой;
  # 'keep-preserved' — оставить пользовательскую копию.
  $stagedRank = $null
  $payloadRank = $null
  $stagedOk = [version]::TryParse((Get-WarpManifestVersion $stagingDir), [ref]$stagedRank)
  $payloadOk = [version]::TryParse((Get-WarpManifestVersion $destDir), [ref]$payloadRank)
  if ($stagedOk -and $payloadOk -and ($payloadRank -ge $stagedRank)) { return 'payload' }
  return 'keep-preserved'
}
# ── end WARP helpers ────────────────────────────────────────────────────────

Add-RbLog 'rollback started'

for ($i = 0; $i -lt 40; $i++) {
  if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) { break }
  Start-Sleep -Milliseconds 250
}
if (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) {
  Add-RbLog 'forcing broken process stop'
  Stop-Process -Id $pidToWait -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 500
}
try { sc.exe stop zapret *> $null } catch {}
try { sc.exe delete zapret *> $null } catch {}
foreach ($image in @('zapret_zen.exe', 'TgWsProxy_windows.exe', 'winws.exe', 'warp-cli.exe', 'warp-svc.exe')) {
  try { taskkill /F /T /IM $image *> $null } catch {}
}

if (-not (Test-Path $src)) {
  Add-RbLog ('ERROR: backup payload missing: ' + $src)
  exit 2
}

New-Item -ItemType Directory -Path $dst -Force | Out-Null

# WARP-PRESERVE-START
# Бинарники Cloudflare WARP (runtime/warp) не входят в payload бэкапа —
# переносим их в карантин, чтобы откат их не удалил; при возврате
# сравниваем версии с предустановкой из payload (если она там есть).
$warpStaging = Join-Path $env:TEMP ('zapret_zen_warp_preserve_' + [guid]::NewGuid().ToString('N'))
$warpSource = Join-Path $dst 'runtime\\warp'
$warpStaged = $false
if (Test-Path -LiteralPath $warpSource) {
  try {
    Move-Item -LiteralPath $warpSource $warpStaging -Force -ErrorAction Stop
    $warpStaged = $true
    Add-RbLog ('warp binaries staged for preservation: ' + $warpStaging)
  } catch {
    Add-RbLog ('warp staging failed: ' + $_.Exception.Message)
  }
}
# WARP-PRESERVE-END

# Полная замена: удаляем всё непрезервированное, копируем payload бэкапа.
Get-ChildItem -LiteralPath $dst -Force -ErrorAction SilentlyContinue | ForEach-Object {
  if (Is-Preserved $_.Name) { return }
  [void](Remove-PathRobust $_.FullName)
}
Get-ChildItem -LiteralPath $src -Force -ErrorAction SilentlyContinue | ForEach-Object {
  if (Is-Preserved $_.Name) { return }
  $dest = Join-Path $dst $_.Name
  try {
    if ($_.PSIsContainer) { Copy-Item $_.FullName $dest -Recurse -Force -ErrorAction Stop }
    else { Copy-Item $_.FullName $dest -Force -ErrorAction Stop }
  } catch { Add-RbLog ('copy failed: ' + $_.FullName + ' | ' + $_.Exception.Message) }
}
Add-RbLog 'install directory restored from backup'

# WARP-RESTORE-START
$warpDest = Join-Path $dst 'runtime\\warp'
if ($warpStaged) {
  $warpWinner = 'keep-preserved'
  if (Test-Path -LiteralPath $warpDest) { $warpWinner = Resolve-WarpWinner $warpStaging $warpDest }
  if ($warpWinner -eq 'payload') {
    # Предустановка из payload новее или равна пользовательской — оставляем её.
    Remove-Item -LiteralPath $warpStaging -Recurse -Force -ErrorAction SilentlyContinue
    Add-RbLog ('warp bundled payload kept (same or newer than preserved): ' + $warpDest)
  } else {
    try {
      if (Test-Path -LiteralPath $warpDest) { [void](Remove-PathRobust $warpDest) }
      New-Item -ItemType Directory -Path (Split-Path $warpDest -Parent) -Force | Out-Null
      Move-Item -LiteralPath $warpStaging $warpDest -Force -ErrorAction Stop
      Add-RbLog ('warp binaries restored: ' + $warpDest)
    } catch {
      Add-RbLog ('warp restore failed: ' + $_.Exception.Message)
      Remove-Item -LiteralPath $warpStaging -Recurse -Force -ErrorAction SilentlyContinue
    }
  }
}
# WARP-RESTORE-END

if (-not (Test-InstalledStandalone $dst)) {
  Add-RbLog 'standalone validation failed after restore'
  exit 3
}

# Отмечаем в state, что выполнен откат (следующий старт — обычный).
try {
  $state = @{ phase = 'rolled_back'; rolled_back_at = (Get-Date -Format s) } | ConvertTo-Json
  Set-Content -LiteralPath $statePath -Value $state -Encoding UTF8
} catch {}

Start-Sleep -Milliseconds 300
$launch = Join-Path $dst 'zapret_zen.exe'
Start-Process -FilePath $launch -WorkingDirectory $dst
Add-RbLog 'restored app relaunched'
Remove-Item '{{SELF_DELETE}}' -Force -ErrorAction SilentlyContinue"""


def _spawn_rollback(backup_id: str) -> bool:
    """Генерит и спавнит внешний PS-скрипт отката к бэкапу backup_id."""
    if not sys.platform.startswith("win"):
        return False
    install_root = packaged_install_root() if is_packaged_runtime() else development_install_root(__file__)
    payload = install_root / "backups" / "update" / backup_id / "payload"
    # В portable/data_root layout бэкапи лежат в data_root/backups. Проверим оба.
    if not payload.is_dir():
        alt = _resolve_cache_dir().parent / "backups" / "update" / backup_id / "payload"
        if alt.is_dir():
            payload = alt
    if not payload.is_dir():
        return False

    current_pid = os.getpid()
    script_root = Path(tempfile.gettempdir()) / "zapret_zen_updates"
    script_root.mkdir(parents=True, exist_ok=True)
    stamp = int(datetime.now(timezone.utc).timestamp() * 1000)
    script_path = script_root / f"rollback_{stamp}.ps1"
    launcher_path = script_root / f"rollback_{stamp}.cmd"
    log_path = script_root / f"rollback_{stamp}.log"
    state_path = _state_path()

    def _esc(val: str) -> str:
        return str(val).replace("'", "''")

    script = (
        _ROLLBACK_PS1_TEMPLATE
        .replace("{{PID}}", str(current_pid))
        .replace("{{SRC}}", _esc(payload))
        .replace("{{DST}}", _esc(install_root))
        .replace("{{LOG_PATH}}", _esc(log_path))
        .replace("{{STATE_PATH}}", _esc(state_path))
        .replace("{{SELF_DELETE}}", _esc(script_path))
    )
    script_path.write_text(script, encoding="utf-8")
    launcher = textwrap.dedent(
        f"""
        @echo off
        start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script_path}"
        exit /b 0
        """
    ).strip() + "\n"
    launcher_path.write_text(launcher, encoding="utf-8")

    creationflags = 0
    startupinfo = None
    if sys.platform.startswith("win"):
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0

    try:
        subprocess.Popen(
            ["cmd.exe", "/c", str(launcher_path)],
            creationflags=creationflags,
            startupinfo=startupinfo,
            cwd=str(install_root),
        )
    except Exception:
        return False
    return True

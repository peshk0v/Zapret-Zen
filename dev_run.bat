@echo off
rem Zapret-Zen dev run: bootstrap окружения (.venv + зависимости) и запуск.
rem Флаги: -Auto (без меню), -Reinstall, -Rebuild, -Console. См. scripts\dev_run.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev_run.ps1" %*
exit /b %errorlevel%

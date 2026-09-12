import argparse
import ctypes
import hashlib
import multiprocessing
import os
import subprocess
import sys
import threading
import tempfile
from datetime import datetime
from pathlib import Path

if sys.platform.startswith("win"):
    import winreg

from PySide6.QtCore import QObject, QTimer, Qt, Slot
from PySide6.QtGui import QCloseEvent, QIcon, QImage, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from zapret_zen.runtime_env import development_install_root, is_packaged_runtime, packaged_install_root, packaged_resource_root
from zapret_zen.workers import run_tg_ws_proxy_worker

def _startup_trace(message: str) -> None:
    try:
        path = Path(tempfile.gettempdir()) / "zapret_zen_startup_trace.log"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    except Exception:
        pass


def _write_startup_error(message: str) -> None:
    try:
        path = Path(tempfile.gettempdir()) / "zapret_zen_startup_error.log"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    except Exception:
        pass


def _set_windows_app_id() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("peshk0v.ZapretZen")  # type: ignore[attr-defined]
    except Exception:
        return


def _configure_frozen_window_environment() -> None:
    if not (is_packaged_runtime() and sys.platform.startswith("win")):
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
        _startup_trace("win: DPI awareness set to per-monitor")
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            pass
    if not os.environ.get("QT_OPENGL"):
        os.environ["QT_OPENGL"] = "software"
        _startup_trace("win: forced QT_OPENGL=software for frozen build")


def _expected_python_dll_name() -> str:
    return f"python{sys.version_info.major}{sys.version_info.minor}.dll"


def _uninstall_registry_keys() -> list[tuple[int, str]]:
    return [
        (winreg.HKEY_LOCAL_MACHINE, _INNO_UNINSTALL_KEY),
        (winreg.HKEY_CURRENT_USER, _INNO_UNINSTALL_KEY),
        (winreg.HKEY_LOCAL_MACHINE, _LEGACY_UNINSTALL_KEY),
        (winreg.HKEY_CURRENT_USER, _LEGACY_UNINSTALL_KEY),
    ]


def _self_heal_windows_install() -> None:
    if not (is_packaged_runtime() and sys.platform.startswith("win")):
        return
    install_dir = packaged_install_root()
    try:
        if install_dir.is_dir():
            expected = _expected_python_dll_name()
            stale_dlls = [item for item in install_dir.glob("python3*.dll") if item.name.lower() != expected.lower()]
            if stale_dlls:
                bat = Path(tempfile.gettempdir()) / "zapret_zen_stale_runtime_cleanup.bat"
                lines = ["@echo off", "ping 127.0.0.1 -n 4 > nul"]
                for dll in stale_dlls:
                    lines.append(f'del /f /q "{dll}"')
                lines.append(f'del /f /q "{bat}"')
                bat.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
                subprocess.Popen(["cmd", "/c", str(bat)], creationflags=subprocess.CREATE_NO_WINDOW)
                _startup_trace("win: self-heal queued removal of stale python dlls: " + ", ".join(item.name for item in stale_dlls))

        inno_uninstaller = install_dir / "unins000.exe"
        if not inno_uninstaller.exists():
            exe = install_dir / "zapret_zen.exe"
            if exe.exists():
                uninstall_string = f'"{exe}" --uninstall --install-dir "{install_dir}"'
                quiet_string = f'{uninstall_string} --silent'
                repointed = False
                for hive, subkey in _uninstall_registry_keys():
                    try:
                        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_SET_VALUE) as key:
                            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, uninstall_string)
                            winreg.SetValueEx(key, "QuietUninstallString", 0, winreg.REG_SZ, quiet_string)
                            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, f'"{exe}",0')
                            repointed = True
                    except OSError:
                        continue
                if repointed:
                    _startup_trace("win: self-heal re-pointed uninstall registry entry to built-in uninstaller (unins000.exe missing)")
                else:
                    _startup_trace("win: self-heal: unins000.exe missing and no uninstall registry entry found")
    except Exception as error:
        _startup_trace(f"win: self-heal failed: {error}")


def _is_admin_windows() -> bool:
    if not sys.platform.startswith("win"):
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True


def _ensure_admin_windows(argv: list[str]) -> int:
    if _is_admin_windows():
        return 0

    if is_packaged_runtime():
        executable = sys.executable
        params_args = list(argv)
    else:
        executable = sys.executable
        src_root = development_install_root(__file__) / "src"
        current_pythonpath = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = str(src_root) if not current_pythonpath else f"{src_root}{os.pathsep}{current_pythonpath}"
        params_args = ["-m", "zapret_zen.main", *argv]
    params = " ".join(f'"{arg}"' if " " in arg else arg for arg in params_args)
    _startup_trace(f"run: relaunch elevated executable={executable} params={params}")
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
    if result <= 32:
        _write_startup_error(f"Failed to request administrator rights. ShellExecuteW returned {result}.")
        return 3
    return 2


def _single_instance_key() -> str:
    base = str(sys.executable if is_packaged_runtime() else __file__)
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    return f"zapret_zen_{digest}"


def _notify_existing_instance(server_name: str, message: bytes = b"SHOW") -> bool:
    socket = QLocalSocket()
    socket.connectToServer(server_name)
    if not socket.waitForConnected(220):
        return False
    socket.write(message)
    socket.flush()
    socket.waitForBytesWritten(220)
    socket.disconnectFromServer()
    return True


def _create_single_instance_server(server_name: str) -> QLocalServer | None:
    server = QLocalServer()
    if server.listen(server_name):
        return server
    QLocalServer.removeServer(server_name)
    if server.listen(server_name):
        return server
    return None


def _resolve_app_icon_path() -> Path | None:
    candidates: list[Path] = []
    if is_packaged_runtime():
        install_root = packaged_install_root()
        resource_root = packaged_resource_root()
        candidates.extend(
            [
                install_root / "ui_assets" / "icons" / "app.png",
                install_root / "ui_assets" / "icons" / "app.ico",
                resource_root / "ui_assets" / "icons" / "app.png",
                resource_root / "ui_assets" / "icons" / "app.ico",
            ]
        )
    else:
        install_root = development_install_root(__file__)
        candidates.extend(
            [
                install_root / "ui_assets" / "icons" / "app.png",
                install_root / "ui_assets" / "icons" / "app.ico",
            ]
        )
    for path in candidates:
        if path.exists():
            return path
    return None


def _load_app_icon() -> QIcon | None:
    path = _resolve_app_icon_path()
    if path is None:
        return None
    if path.suffix.lower() == ".png":
        image = QImage(str(path))
        if image.isNull():
            return None
        source = QPixmap.fromImage(image)
        if source.isNull():
            return None
        icon = QIcon()
        for size in (16, 20, 24, 32, 48, 64, 128, 256):
            icon.addPixmap(
                source.scaled(
                    size,
                    size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        return icon if not icon.isNull() else None
    icon = QIcon(str(path))
    return icon if not icon.isNull() else None


def _preload_startup_onboarding(context, *, launch_hidden: bool, startup_snapshot: dict[str, object] | None = None) -> bool:
    if launch_hidden:
        return False
    try:
        marker = context.paths.data_dir / ".services_onboarding_seen_v2"
        if marker.exists():
            return False
        if isinstance(startup_snapshot, dict):
            raw_options = startup_snapshot.get("general_options")
            if isinstance(raw_options, list):
                return any(isinstance(item, dict) and item.get("id") for item in raw_options)
        return False
    except Exception:
        return False


_INNO_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\F5A2C73E-9B11-4E6B-8C2D-1A7E5D0B3F91_is1"
_LEGACY_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ZapretZen"


def _registered_install_dir() -> Path | None:
    if not sys.platform.startswith("win"):
        return None
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for subkey in (_INNO_UNINSTALL_KEY, _LEGACY_UNINSTALL_KEY):
            try:
                with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
                    value, _ = winreg.QueryValueEx(key, "InstallLocation")
                    path = Path(str(value))
                    if path.exists():
                        return path
            except Exception:
                continue
    return None


def _run_uninstall(install_dir_arg: str, silent: bool = False) -> int:
    if not sys.platform.startswith("win"):
        return 0
    if not _is_admin_windows():
        args = ["--uninstall"]
        if install_dir_arg:
            args.extend(["--install-dir", install_dir_arg])
        if silent:
            args.append("--silent")
        return _ensure_admin_windows(args)

    if install_dir_arg:
        install_dir = Path(install_dir_arg)
    else:
        install_dir = _registered_install_dir() or Path("C:\\Program Files\\Zapret-Zen")
    inno_uninstaller = install_dir / "unins000.exe"
    if inno_uninstaller.exists():
        args = [str(inno_uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
        subprocess.Popen(args, cwd=str(install_dir))
        return 0

    legacy_uninstaller = install_dir / "uninstall_zapretzen.exe"
    if legacy_uninstaller.exists():
        args = [str(legacy_uninstaller), "--uninstall", "--install-dir", str(install_dir)]
        if silent:
            args.append("--silent")
        subprocess.Popen(args)
        return 0

    # fallback: delete the install dir via a delayed batch script and drop registry entries
    reg_delete_lines = []
    for hive, subkey in _uninstall_registry_keys():
        hive_path = "HKLM" if hive == winreg.HKEY_LOCAL_MACHINE else "HKCU"
        reg_delete_lines.append(f'reg delete "{hive_path}\\{subkey}" /f')
    cmd = (
        "@echo off\r\n"
        "ping 127.0.0.1 -n 4 > nul\r\n"
        f'rmdir /s /q "{install_dir}"\r\n'
        + "\r\n".join(reg_delete_lines)
        + "\r\n"
    )
    bat = Path(tempfile.gettempdir()) / "zapret_zen_cleanup.bat"
    bat.write_text(cmd, encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=subprocess.CREATE_NO_WINDOW)
    return 0


def run(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    _startup_trace("run: freeze_support passed")
    try:
        from zapret_zen import __version__
        _startup_trace(f"run: version={__version__}")
    except Exception:
        pass
    runtime_argv = list(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", choices=["tg-ws-proxy"], default="")
    parser.add_argument("--autostart-launch", action="store_true")
    parser.add_argument("--skip-autosettings", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--install-dir", default="")
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--diagnose-window", action="store_true")
    from zapret_zen.cli_args import build_worker_arg_group
    build_worker_arg_group(parser)
    known, _ = parser.parse_known_args(runtime_argv)

    if known.uninstall:
        return _run_uninstall(known.install_dir, known.silent)

    if known.diagnose_window:
        _startup_trace("run: diagnose-window start")
        _set_windows_app_id()
        _configure_frozen_window_environment()
        _self_heal_windows_install()
        app = QApplication(sys.argv)
        app.setApplicationName("Zapret-Zen")
        app.setOrganizationName("ZapretZen")
        _startup_trace("run: diagnose-window bootstrap")
        from zapret_zen.bootstrap import bootstrap_application
        from zapret_zen.ui.main_window import MainWindow
        from zapret_zen.window_diagnose import run_diagnose
        context = bootstrap_application()
        window = MainWindow(
            context,
            launch_hidden=False,
            startup_show_onboarding=False,
            startup_snapshot=None,
            skip_autosettings=True,
        )
        window.show()
        window.raise_()
        from PySide6.QtCore import QEventLoop
        settle_loop = QEventLoop()
        QTimer.singleShot(2500, settle_loop.quit)
        settle_loop.exec()
        target = Path(os.environ.get("TEMP", ".")) / "zapret_zen_window_diagnose.txt"
        code = run_diagnose(app, window, output=target)
        _startup_trace(f"run: diagnose-window wrote {target}")
        sys.stdout.flush()
        os._exit(code)

    if known.worker == "tg-ws-proxy":
        _startup_trace("run: worker=tg-ws-proxy")
        from zapret_zen.cli_args import parse_bool_flag
        return run_tg_ws_proxy_worker(
            host=known.tg_host,
            port=known.tg_port,
            secret=known.tg_secret,
            verbose=known.tg_verbose,
            dc_ip=list(known.tg_dc_ip or []),
            cfproxy_enabled=parse_bool_flag(known.tg_cfproxy_enabled),
            cfproxy_priority=parse_bool_flag(known.tg_cfproxy_priority),
            cfproxy_domain=known.tg_cfproxy_domain,
            fake_tls_domain=known.tg_fake_tls_domain,
            buf_kb=known.tg_buf_kb,
            pool_size=known.tg_pool_size,
        )
    if not known.autostart_launch:
        _startup_trace("run: ensure_admin start")
        elevate_result = _ensure_admin_windows(runtime_argv)
        _startup_trace(f"run: ensure_admin result={elevate_result}")
        if elevate_result in (2, 3):
            return elevate_result
    elif not _is_admin_windows():
        _startup_trace("run: autostart-launch without admin, requesting elevation")
        elevate_result = _ensure_admin_windows(runtime_argv)
        _startup_trace(f"run: autostart-launch elevate result={elevate_result}")
        if elevate_result in (2, 3):
            return elevate_result

    _set_windows_app_id()
    _configure_frozen_window_environment()
    _self_heal_windows_install()
    _startup_trace("run: before QApplication")
    app = QApplication(sys.argv)
    _startup_trace("run: QApplication created")
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Zapret-Zen")
    app.setOrganizationName("ZapretZen")
    app_icon = _load_app_icon()
    _startup_trace(f"run: app_icon loaded={app_icon is not None}")
    if app_icon is not None:
        app.setWindowIcon(app_icon)
    instance_key = _single_instance_key()
    notify_message = b"PING" if known.autostart_launch else b"SHOW"
    if _notify_existing_instance(instance_key, notify_message):
        _startup_trace("run: existing instance notified, exiting")
        return 0

    class _BootstrapBridge(QObject):
        @Slot(object)
        def finish_bootstrap(self, bundle: object) -> None:
            from zapret_zen.ui.main_window import MainWindow
            from zapret_zen.services.backend_worker import BackendWorkerClient

            _startup_trace("finish_bootstrap: entered")
            if not isinstance(bundle, dict):
                raise RuntimeError("Bootstrap result is invalid")
            context = bundle.get("context")
            startup_snapshot = bundle.get("startup_snapshot")
            startup_show_onboarding = bool(bundle.get("startup_show_onboarding"))
            if context is None:
                raise RuntimeError("Application context is missing")
            settings = context.settings.get()
            actual_autostart = bool(context.autostart.is_enabled())
            if actual_autostart:
                try:
                    context.autostart.ensure_runs_elevated()
                except Exception:
                    pass
            if bool(settings.autostart_windows) != actual_autostart:
                context.settings.update(autostart_windows=actual_autostart)
                settings = context.settings.get()
            launch_hidden = bool(known.autostart_launch and settings.start_in_tray)
            if launch_hidden:
                startup_show_onboarding = False
            context.backend = None
            _startup_trace("finish_bootstrap: before MainWindow")
            window = MainWindow(
                context,
                launch_hidden=launch_hidden,
                startup_show_onboarding=startup_show_onboarding,
                startup_snapshot=startup_snapshot if isinstance(startup_snapshot, dict) else None,
                skip_autosettings=bool(known.skip_autosettings),
            )
            _startup_trace("finish_bootstrap: MainWindow created")
            if app_icon is not None:
                try:
                    window.setWindowIcon(app_icon)
                except Exception:
                    pass
            server = _create_single_instance_server(instance_key)
            if server is not None:
                def _on_new_connection() -> None:
                    while server.hasPendingConnections():
                        client = server.nextPendingConnection()
                        if client is not None:
                            if client.bytesAvailable() <= 0:
                                client.waitForReadyRead(350)
                            payload = bytes(client.readAll()).strip()
                            client.disconnectFromServer()
                            if payload == b"SHOW":
                                window.restore_from_external_launch()

                server.newConnection.connect(_on_new_connection)
                app._single_instance_server = server  # type: ignore[attr-defined]
                app._single_instance_window = window  # type: ignore[attr-defined]

            def _cleanup_before_quit() -> None:
                try:
                    if context.backend is not None:
                        context.backend.request_shutdown_background()
                    else:
                        threading.Thread(target=context.processes.stop_all, daemon=True).start()
                except Exception:
                    pass
                if server is not None:
                    try:
                        server.close()
                    except Exception:
                        pass

            app.aboutToQuit.connect(_cleanup_before_quit)
            bootstrap_autostart = bool(known.autostart_launch and settings.auto_run_components)
            if launch_hidden:
                _startup_trace("finish_bootstrap: hide window")
                window.hide()
            else:
                _startup_trace("finish_bootstrap: show window")
                window.show()
            _startup_trace("finish_bootstrap: after window visible call")

            def _attach_backend_after_show() -> None:
                _startup_trace("attach_backend: start")
                bootstrap_tasks = (
                    [("start_enabled_components", {"autostart_only": True})] if bootstrap_autostart else None
                )
                backend = BackendWorkerClient(app, bootstrap_tasks=bootstrap_tasks)
                _startup_trace("attach_backend: client created")
                context.backend = backend
                window.attach_backend_client(backend)
                _startup_trace("attach_backend: attached")

            QTimer.singleShot(900, _attach_backend_after_show)
            _startup_trace("finish_bootstrap: backend attach scheduled")

        @Slot(str)
        def fail_bootstrap(self, message: str) -> None:
            _startup_trace(f"finish_bootstrap: failed {message}")
            _write_startup_error(message or "Failed to prepare the application")
            QMessageBox.critical(None, "Zapret-Zen", message or "Failed to prepare the application")
            app.quit()

    bootstrap_bridge = _BootstrapBridge()
    app._bootstrap_bridge = bootstrap_bridge  # type: ignore[attr-defined]

    def _bootstrap_on_main_thread() -> None:
        try:
            from zapret_zen.bootstrap import bootstrap_application, build_startup_snapshot

            _startup_trace("run: bootstrap main-thread start")
            context = bootstrap_application()
            startup_snapshot = build_startup_snapshot(context)
            startup_show_onboarding = _preload_startup_onboarding(
                context,
                launch_hidden=False,
                startup_snapshot=startup_snapshot,
            )
            _startup_trace("run: bootstrap main-thread ready")
            bootstrap_bridge.finish_bootstrap(
                {
                    "context": context,
                    "startup_snapshot": startup_snapshot,
                    "startup_show_onboarding": startup_show_onboarding,
                }
            )
        except Exception as error:
            bootstrap_bridge.fail_bootstrap(str(error))

    QTimer.singleShot(0, _bootstrap_on_main_thread)
    _startup_trace("run: bootstrap scheduled")
    return app.exec()


from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def get_creation_flags() -> int:
    if IS_WINDOWS:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    return 0


def get_startup_info() -> subprocess.STARTUPINFO | None:
    if IS_WINDOWS:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return si
    return None


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


def kill_process_by_pid(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True,
            check=False,
            creationflags=get_creation_flags(),
        )
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def kill_image_by_name(image_name: str) -> None:
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/IM", image_name, "/F", "/T"],
            capture_output=True,
            check=False,
            creationflags=get_creation_flags(),
        )
    else:
        subprocess.run(
            ["pkill", "-9", "-f", image_name],
            capture_output=True,
            check=False,
        )


def is_image_running(image_name: str) -> bool:
    if IS_WINDOWS:
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=get_creation_flags(),
        )
        return image_name.lower() in (proc.stdout or "").lower()
    else:
        proc = subprocess.run(
            ["pgrep", "-x", image_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0


# ---------------------------------------------------------------------------
# File system
# ---------------------------------------------------------------------------


def system_hosts_path() -> Path:
    if IS_WINDOWS:
        return Path(r"C:\Windows\System32\drivers\etc\hosts")
    return Path("/etc/hosts")


def strip_zone_identifier(path: Path) -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.DeleteFileW(str(path) + ":Zone.Identifier")  # type: ignore[attr-defined]
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Privilege checks
# ---------------------------------------------------------------------------


def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return True
    return os.geteuid() == 0


# ---------------------------------------------------------------------------
# Zapret engine binary names
# ---------------------------------------------------------------------------


def zapret_binary_name() -> str:
    if IS_WINDOWS:
        return "winws.exe"
    return "nfqws"


def zapret_rust_binary_name() -> str:
    if IS_WINDOWS:
        return "zapret-rust.exe"
    return "zapret-rust"


def has_zapret_rust(runtime_dir: Path) -> bool:
    binary = runtime_dir / "zapret-discord-youtube-rust" / zapret_rust_binary_name()
    if binary.exists():
        return True
    binary = runtime_dir / "bin" / zapret_rust_binary_name()
    return binary.exists()


# ---------------------------------------------------------------------------
# Null device
# ---------------------------------------------------------------------------


def dev_null() -> str:
    if IS_WINDOWS:
        return "NUL"
    return "/dev/null"

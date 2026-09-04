from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# Linux compositor / windowing detection
# ---------------------------------------------------------------------------


def is_wayland() -> bool:
    """True when the current display server is Wayland."""
    if IS_WINDOWS:
        return False
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland"


def is_hyprland() -> bool:
    """True when the running Wayland compositor is Hyprland."""
    if not is_wayland():
        return False
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
    return "hyprland" in desktop or "hypr" in desktop


def linux_default_native_window() -> bool:
    """Whether a compositor-managed (native) window should be the default.

    Custom client-side (frameless + translucent) decoration conflicts with
    Wayland compositors that add their own border/rounding (Hyprland in
    particular).  Native mode lets the compositor draw the frame/rounding.
    """
    if not IS_LINUX:
        return False
    return is_hyprland()


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


def _pkill_pattern(image_name: str) -> str:
    """Return a pkill -f pattern that won't match the pkill/sudo command itself.

    ``pkill -9 -f <name>`` matches every process whose command line contains ``<name>``,
    including the ``sudo``/``pkill`` invocation itself.  Killing ``sudo`` mid-flight
    hangs the call.  The classic ``[n]ame`` regex trick prevents self-matching while
    still matching the target process.
    """
    escaped = image_name.replace("[", "[[]").replace("]", "[]]")
    if escaped:
        escaped = f"[{escaped[0]}]{escaped[1:]}"
    return escaped


def kill_image_by_name(image_name: str) -> None:
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/IM", image_name, "/F", "/T"],
            capture_output=True,
            check=False,
            creationflags=get_creation_flags(),
        )
    else:
        pattern = _pkill_pattern(image_name)
        if not is_admin():
            _run_elevated_stop(["/usr/bin/pkill", "-9", "-f", pattern])
        subprocess.run(
            ["pkill", "-9", "-f", pattern],
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
        if proc.returncode != 0:
            return False
        for pid in proc.stdout.split():
            if not _pid_is_zombie(pid):
                return True
        return False


def _pid_is_zombie(pid: str) -> bool:
    """Return True if the given PID is a zombie (defunct) process.

    A stopped zapret child (nfqws) can become an orphaned zombie in WSL/containers
    where it is never reaped. ``pgrep -x`` still reports zombies, so a plain
    ``pgrep -x`` success would wrongly make nfqws look "running" forever.  Here we
    inspect ``/proc/<pid>/status`` and ignore processes whose State starts with
    ``Z``.
    """
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if line.startswith("State:"):
                    return line.split()[1].startswith("Z")
    except Exception:
        return False
    return False


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
# Linux privilege handling (elevation)
# ---------------------------------------------------------------------------

_SUDO_NOPASSWD_OK: bool | None = None
_SUDO_TTY_CHECKED: bool = False


def _probe_sudo_tty_requirement() -> None:
    """Log a warning if sudo requires a TTY (use_pty/requiretty defaults)."""
    global _SUDO_TTY_CHECKED
    if _SUDO_TTY_CHECKED:
        return
    _SUDO_TTY_CHECKED = True
    if not IS_LINUX or is_admin():
        return
    try:
        proc = subprocess.run(
            ["sudo", "-n", "/usr/bin/true"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=5,
        )
        stderr = (proc.stderr or b"").decode("utf-8", "replace").lower()
        if "a terminal is required" in stderr or "sorry, you are not allowed" in stderr:
            import logging
            logging.getLogger("zapret_zen").warning(
                "sudo requires a TTY (use_pty/requiretty). "
                "Passwordless sudo may not work from a GUI application. "
                "Consider adding 'Defaults !use_pty' to /etc/sudoers.d/zapret-zen-defaults."
            )
    except Exception:
        pass


def set_prefer_sudo(prefer: bool) -> None:
    """(Deprecated compat) Clear the cached sudo -n probe result.

    The sudo -n availability is now auto-detected on every elevation call, so this
    only resets the cached probe so reconfiguration is honoured immediately.
    """
    global _SUDO_NOPASSWD_OK
    _SUDO_NOPASSWD_OK = None


def sudo_nopasswd_works(probe_command: list[str]) -> bool:
    """Return True if ``sudo -n <probe>`` runs without asking for a password."""
    global _SUDO_NOPASSWD_OK
    if _SUDO_NOPASSWD_OK is not None:
        return _SUDO_NOPASSWD_OK
    if not IS_LINUX or is_admin() or not probe_command:
        _SUDO_NOPASSWD_OK = True
        return True
    _probe_sudo_tty_requirement()
    try:
        proc = subprocess.run(["sudo", "-n", *probe_command], capture_output=True, check=False, timeout=30)
        _SUDO_NOPASSWD_OK = proc.returncode == 0
    except Exception:
        _SUDO_NOPASSWD_OK = False
    return _SUDO_NOPASSWD_OK


def _use_sudo_nopasswd(binary: str) -> bool:
    """True when a passwordless sudoers rule covers the given zapret binary.

    First checks whether *any* NOPASSWD rule is active (via ``/usr/bin/true``).
    If so, verifies the *specific* binary path is also covered by attempting a
    safe ``sudo -n <binary> --help`` invocation.  The zapret binary does not
    accept ``--help``; a timeout means it started successfully (path covered),
    while an instant failure with stderr means auth failure (path not covered).
    """
    if not IS_LINUX or is_admin() or not binary:
        return True
    if not sudo_nopasswd_works(["/usr/bin/true"]):
        return False
    try:
        proc = subprocess.run(
            ["sudo", "-n", binary, "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=5,
        )
        stderr = (proc.stderr or b"").decode("utf-8", "replace").lower()
        if proc.returncode == 0 or not stderr:
            return True
        return False
    except subprocess.TimeoutExpired:
        return True
    except Exception:
        return sudo_nopasswd_works(["/usr/bin/true"])


def _elevation_prefix(command: list[str]) -> tuple[list[str], str]:
    """Choose the elevation prefix for a command based on current state."""
    if command and str(command[0]).endswith(("pkexec", "sudo")):
        return (list(command), "")
    if command and _use_sudo_nopasswd(command[0]):
        return (["sudo", "-n", *command], "")
    return (
        list(command),
        "Для запуска Zapret требуются права root, но sudoers-правило не настроено. "
        "Настройте автоматические права в настройках или запустите приложение от root.",
    )


def build_elevated_command(command: list[str], *, chdir: str | None = None) -> tuple[list[str], str]:
    """Return ``(launch_command, error_message)`` for a possibly-root command.

    On Linux, when not root, the command is wrapped in ``sudo -n`` if a passwordless
    sudoers rule is configured.  If no NOPASSWD rule exists, an error message is
    returned instead of showing an interactive password dialog (which would break
    automated execution flows).

    On Windows, or when already running as root, the command is returned unchanged.
    """
    if not IS_LINUX or is_admin():
        return (list(command), "")
    return _elevation_prefix(command)


def configure_zapret_sudoers(binary: str) -> bool:
    """Install a passwordless sudoers rule for the given binary (requires Linux).

    Writes a rule to ``/etc/sudoers.d/zapret-zen`` so the current user can run the
    binary, ``kill``/``pkill``/``pgrep`` without a password afterward. The rule is
    written through root elevation (polkit/pkexec if available, else zenity+sudo),
    showing the password prompt at most once. Returns True on success.
    """
    if not IS_LINUX:
        return True
    if is_admin():
        return True
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        return False
    binary_paths = [binary]
    bin_fallback = str(Path(binary).parent.parent / "bin" / Path(binary).name)
    if bin_fallback != binary and Path(bin_fallback).exists():
        binary_paths.append(bin_fallback)
    commands = ", ".join(binary_paths) + (
        ", /usr/bin/kill, /usr/bin/pkill, /usr/bin/pgrep, /usr/bin/true, "
        "/usr/bin/install, /usr/bin/cp, /bin/sh, /usr/bin/sh, "
        "/usr/bin/nft, /usr/sbin/nft, /usr/bin/iptables, /usr/sbin/iptables, /usr/bin/ip, /usr/sbin/ip"
    )
    reason = f"ALL=(ALL) NOPASSWD: {commands}"
    rule = f"{user} {reason}\n"
    script = (
        "set -e; "
        f"if [ -f /etc/sudoers.d/zapret-zen ]; then rm -f /etc/sudoers.d/zapret-zen; fi; "
        f"echo '{rule}' > /etc/sudoers.d/zapret-zen && "
        f"chmod 0440 /etc/sudoers.d/zapret-zen && "
        "if command -v visudo >/dev/null 2>&1; then visudo -cf /etc/sudoers.d/zapret-zen; "
        "elif [ -x /usr/sbin/visudo ]; then /usr/sbin/visudo -cf /etc/sudoers.d/zapret-zen; fi"
    )

    # 1) Prefer zenity on a desktop session: a single clean password prompt piped to
    #    sudo -S. This is reliable when a display / WAYLAND_DISPLAY / DISPLAY is set,
    #    and avoids the double prompt that can occur when pkexec falls back to zenity.
    if shutil.which("zenity"):
        try:
            prompt = subprocess.Popen(
                ["zenity", "--password", "--title=Пароль для настройки Zapret (один раз)"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if prompt.stdout is not None:
                pwd = prompt.stdout.readline().decode("utf-8", "ignore").strip()
                prompt.stdout.close()
                if pwd:
                    sudo = subprocess.run(
                        ["sudo", "-S", "-p", "", "sh", "-c", script],
                        input=pwd + "\n",
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if sudo.returncode == 0:
                        return True
        except Exception:
            pass

    # 2) Fallback: pkexec/polkit (GUI password dialog via the session's polkit agent).
    if shutil.which("pkexec"):
        try:
            proc = subprocess.run(["pkexec", "sh", "-c", script], capture_output=True, text=True, timeout=60)
            if proc.returncode == 0:
                return True
        except Exception:
            pass
    return False


def sudoers_configured(binary: str) -> bool:
    """Return True if the passwordless sudoers rule for this binary is active."""
    if not IS_LINUX or is_admin():
        return True
    return _use_sudo_nopasswd(binary)


def _run_elevated_stop(cmd: list[str]) -> None:
    """Try to run an elevated stop command: sudo -n first (never prompts), then pkexec.

    A non-zero return code from the command itself (e.g. ``pkill`` returning 1 when no
    process matches) must not be mistaken for a failed privilege escalation.  When
    ``sudo_nopasswd_works`` has already confirmed the NOPASSWD rule exists, we skip
    the pkexec fallback entirely — the command simply returned non-zero because it
    had nothing to kill.
    """
    if IS_LINUX and not is_admin():
        if sudo_nopasswd_works(["/usr/bin/true"]):
            try:
                subprocess.run(["sudo", "-n", *cmd], capture_output=True, check=False, timeout=30)
            except Exception:
                pass
            return
        try:
            if shutil.which("pkexec"):
                subprocess.run(["pkexec", *cmd], capture_output=True, check=False, timeout=30)
        except Exception:
            pass


def terminate_privileged_process(process: subprocess.Popen[Any]) -> None:
    """Stop a possibly root-owned child process (e.g. launched via pkexec/sudo)."""
    if process is None or process.poll() is not None:
        return
    _run_elevated_stop(["/usr/bin/kill", "-9", str(process.pid)])
    try:
        process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except Exception:
            pass


def stop_image_elevated(image: str) -> None:
    """Stop all processes matching an image name, elevating when needed."""
    _run_elevated_stop(["/usr/bin/pkill", "-9", "-f", _pkill_pattern(image)])
    kill_image_by_name(image)


def flush_zapret_firewall() -> dict[str, Any]:
    """Best-effort flush of firewall rules left behind by zapret-rust (Linux only).

    ``zapret-rust`` installs nftables chains (``zapret-rust-rule-*``) that redirect
    matching packets into netfilter queue 200, plus an ``ip rule`` for fwmark
    ``0x40000000``.  When the app stops the engine it kills ``nfqws``/``zapret-rust``
    (the only consumer of queue 200), so any leftover rule stalls the matched traffic
    until it is removed -> total internet loss.  This helper removes those rules so a
    normal stop leaves the firewall clean.  Every step is best-effort and tolerates the
    tools being absent or the rules already gone.

    Returns a small status dict describing what happened (useful for logging), e.g.::

        {"ok": True, "nft_permission_ok": True, "chains_removed": 0, "tables_removed": 0}
    """
    if not IS_LINUX:
        return {"ok": True, "nft_permission_ok": True, "chains_removed": 0, "tables_removed": 0}
    status: dict[str, Any] = {
        "ok": True,
        "nft_permission_ok": True,
        "chains_removed": 0,
        "tables_removed": 0,
    }
    nft_cmd = shutil.which("nft") or "/usr/sbin/nft"
    try:
        ruleset = subprocess.run(
            [nft_cmd, "-j", "list", "ruleset"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except Exception:
        return status
    # A non-zero return (e.g. "a password is required") means the current sudoers rule
    # does not cover nft, so the explicit flush cannot run here.  This is expected when
    # the on-disk sudoers.d rule predates the nft entry; the graceful SIGTERM to the
    # real zapret-rust process is the primary cleanup in that case.
    if ruleset.returncode != 0:
        status["ok"] = False
        status["nft_permission_ok"] = False
        return status

    chain_refs = _extract_zapret_chain_names(ruleset.stdout)
    tables = _extract_zapret_tables(ruleset.stdout)
    for table_ref in tables:
        _run_elevated_or_direct(["nft", "delete", "table", *table_ref])
        status["tables_removed"] += 1
    for chain_ref in chain_refs:
        _run_elevated_or_direct(["nft", "delete", "chain", *chain_ref])
        status["chains_removed"] += 1
    _run_elevated_or_direct(["/usr/sbin/ip", "rule", "del", "fwmark", "0x40000000"])
    status["ok"] = True
    return status


def _extract_zapret_tables(ruleset_json_text: str) -> list[list[str]]:
    """Return ``[family, table]`` refs for tables that contain only zapret chains.

    If a table is dedicated to zapret (every named chain is ``zapret-rust-rule-*`` or
    the table name contains ``zapret``), deleting the whole table atomically removes
    all of its zapret chains/rules.  System tables (filter/nat/mangle) are left alone.
    """
    refs: list[list[str]] = []
    try:
        parsed = json.loads(ruleset_json_text)
    except Exception:
        return refs
    tables: dict[tuple[str, str], list[str]] = {}
    for ns in parsed.get("nftables", []):
        chain = ns.get("chain")
        if not chain:
            continue
        family = chain.get("family", "ip")
        table = chain.get("table", "")
        name = chain.get("name", "")
        if table:
            tables.setdefault((family, table), []).append(name)
    for (family, table), _chain_names in tables.items():
        if not table:
            continue
        if "zapret" in table.lower():
            refs.append([family, table])
    return refs


def _extract_zapret_chain_names(ruleset_json_text: str) -> list[list[str]]:
    """Return ``[family, table, name]`` refs for zapret-rust-owned nftables chains."""
    refs: list[list[str]] = []
    try:
        parsed = json.loads(ruleset_json_text)
    except Exception:
        return refs
    for ns in parsed.get("nftables", []):
        chain = ns.get("chain")
        if not chain:
            continue
        name = chain.get("name", "")
        if "zapret-rust" in name and chain.get("table"):
            refs.append([chain.get("family", "ip"), chain["table"], name])
    return refs


def _run_elevated_or_direct(command: list[str]) -> None:
    """Run ``command`` as root: directly if already root, else via sudo -n then pkexec.

    Unlike :func:`_run_elevated_stop` (which assumes the target is a stop command and
    only elevates when not root), this also runs the command directly when the current
    user is already root, and is a strict best-effort no-op on any failure.
    """
    try:
        if IS_LINUX and not is_admin():
            if sudo_nopasswd_works(["/usr/bin/true"]):
                subprocess.run(
                    ["sudo", "-n", *command], capture_output=True, check=False, timeout=30
                )
                return
            if shutil.which("pkexec"):
                subprocess.run(["pkexec", *command], capture_output=True, check=False, timeout=30)
            return
        subprocess.run(command, capture_output=True, check=False, timeout=30)
    except Exception:
        pass


def terminate_zapret_runtime_gracefully() -> bool:
    """Send SIGTERM to the REAL ``zapret-rust`` process (not the sudo wrapper).

    ``zapret-rust`` installs its nftables rules and runs the "Clearing nftables
    rules..." cleanup when it receives SIGTERM (Ctrl-C handler).  When the app launches
    it via ``sudo -n``, ``process.pid`` is the *sudo* process, and signalling that PID
    does not reliably reach the actual ``zapret-rust`` child.  Instead we locate real
    ``zapret-rust`` processes with ``pgrep -x`` and signal those directly.

    Returns True if at least one real ``zapret-rust`` process received SIGTERM.
    Requires only ``pgrep``/``kill``, both of which are already in the sudoers rule.
    """
    if not IS_LINUX:
        return False
    pids = _image_pids("zapret-rust")
    if not pids:
        return False
    for pid in pids:
        _run_elevated_or_direct(["/usr/bin/kill", "-15", str(pid)])
    return True


def _image_pids(image_name: str) -> list[int]:
    """Return PIDs of processes whose executable name matches ``image_name``."""
    try:
        proc = subprocess.run(
            ["pgrep", "-x", image_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return [int(p) for p in proc.stdout.split() if p.strip().isdigit()]


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

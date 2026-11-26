"""Sidecar process: owns the orchestrator, speaks JSON-RPC over a named pipe.

The GUI never touches the observer, the prober or SQLite directly.  It talks to
this process through :class:`zapret_zen.orchestrator.client.OrchestratorClient`,
which keeps the UI thread free and lets the whole engine be replaced by a Rust
binary that speaks the same protocol.

Process lifetime rules:

* started by the app (or by ``python -m zapret_zen.orchestrator.cli serve``);
* dies when its parent dies (``--parent-pid``), so no orphan keeps probing;
* dies when it has no client for ``--idle-timeout`` seconds and the parent is
  gone, which covers crash-restart loops.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import hashlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from zapret_zen.orchestrator import __version__
from zapret_zen.orchestrator.app.service import OrchestratorService
from zapret_zen.orchestrator.domain.models import jsonable
from zapret_zen.orchestrator.ipc.named_pipe import NamedPipeServer, default_pipe_name
from zapret_zen.orchestrator.ipc.token import ensure_token

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class _FileLogger:
    def __init__(self, path: Path | None, *, echo: bool = False) -> None:
        self.path = Path(path) if path else None
        self.echo = bool(echo)
        self._lock = threading.Lock()
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.path = None

    def __call__(self, message: str) -> None:
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] {message}"
        if self.echo:
            try:
                print(line, file=sys.stderr, flush=True)
            except Exception:
                pass
        if self.path is None:
            return
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8", errors="replace") as stream:
                    stream.write(line + "\n")
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Parent watchdog
# ---------------------------------------------------------------------------


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if not sys.platform.startswith("win"):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return int(code.value) == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


class OrchestratorSidecar:
    def __init__(
        self,
        service: OrchestratorService,
        *,
        pipe_name: str | None = None,
        token: str = "",
        parent_pid: int = 0,
        idle_timeout_s: float = 0.0,
        log: Callable[[str], None] | None = None,
        max_instances: int = 8,
    ) -> None:
        self.service = service
        self.pipe_name = pipe_name or default_pipe_name()
        self.token = token or ""
        self.parent_pid = int(parent_pid or 0)
        self.idle_timeout_s = max(0.0, float(idle_timeout_s or 0.0))
        self.log = log or (lambda _message: None)
        self._stop = threading.Event()
        self._clients_seen = 0
        self._last_request_at = time.time()
        self._started_at = time.time()
        self._server = NamedPipeServer(
            self.pipe_name,
            self.handle,
            max_instances=max_instances,
            auth_token=self.token,
            logger=self.log,
        )
        self._monitor_thread: threading.Thread | None = None
        self._tick_errors = 0

    # -- lifecycle ---------------------------------------------------------

    def serve_forever(self) -> int:
        self.log(f"sidecar {__version__} starting on {self.pipe_name} (pid {os.getpid()})")
        try:
            self.service.start()
        except Exception as error:
            self.log(f"service.start failed: {error!r}")
        self._server.start()
        self._monitor_thread = threading.Thread(target=self._monitor, name="orchestrator-monitor", daemon=True)
        self._monitor_thread.start()
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            self.log("interrupted")
        finally:
            self.shutdown()
        return 0

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        self.log("sidecar stopping")
        try:
            self._server.stop()
        except Exception:
            pass
        monitor = self._monitor_thread
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=2.0)
        try:
            self.service.stop()
        except Exception as error:
            self.log(f"service.stop failed: {error!r}")

    # -- monitoring --------------------------------------------------------

    def _monitor(self) -> None:
        last_prune = time.time()
        last_tick = 0.0
        while not self._stop.wait(0.25):
            now = time.time()
            interval = max(0.2, float(self.service.policy.observe_interval_s or 1.0))
            try:
                # The tick schedule is independent of RPC traffic: a chatty UI
                # must not starve observation by refreshing the request clock.
                if self.service.started and now - last_tick >= interval:
                    last_tick = now
                    self.service.tick()
            except Exception as error:
                self._tick_errors += 1
                if self._tick_errors <= 5:
                    self.log(f"tick failed: {error!r}")
            if now - last_prune > 3600:
                last_prune = now
                try:
                    self.service.journal.prune(int(self.service.policy.retention_days))
                except Exception:
                    pass
            if self.parent_pid and not _process_alive(self.parent_pid):
                self.log("parent process is gone, exiting")
                self._stop.set()
                break
            if self.idle_timeout_s and now - self._started_at > self.idle_timeout_s:
                if now - self._last_request_at > self.idle_timeout_s:
                    self.log("idle timeout reached, exiting")
                    self._stop.set()
                    break

    # -- RPC ---------------------------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        method = str(request.get("method") or "").strip().lower()
        params = request.get("params")
        if not isinstance(params, dict):
            params = {}
        self._clients_seen += 1
        self._last_request_at = time.time()
        handler = self._methods().get(method)
        if handler is None:
            return {"ok": False, "error": f"unknown method: {method}"}
        try:
            result = handler(params)
        except Exception as error:
            self.log(f"method {method} failed: {error!r}")
            return {"ok": False, "error": str(error) or error.__class__.__name__}
        if isinstance(result, dict):
            return result
        return {"ok": True, "result": result}

    def _methods(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "ping": self._ping,
            "status": self._status,
            "snapshot": lambda _params: self.service.snapshot(),
            "connections": self._connections,
            "tick": lambda _params: self.service.tick(),
            "probe": self._probe,
            "rules": lambda _params: {"ok": True, "rules": self.service.rules()},
            "recipes": self._recipes,
            "forget": self._forget,
            "pin": self._pin,
            "set_mode": self._set_mode,
            "policy": lambda _params: {"ok": True, "policy": jsonable(self.service.policy.to_dict())},
            "update_policy": self._update_policy,
            "apply": lambda _params: self.service.apply_rules(),
            "ack_restart": lambda _params: self.service.ack_restart(),
            "journal_stats": self._journal_stats,
            "shutdown": self._shutdown,
        }

    def _ping(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "pong": True,
            "version": __version__,
            "pid": os.getpid(),
            "uptime_s": int(time.time() - self._started_at),
            "service_started": self.service.started,
        }

    def _status(self, _params: dict[str, Any]) -> dict[str, Any]:
        snapshot = self.service.snapshot()
        snapshot.pop("stats", None)
        snapshot.pop("rules", None)
        snapshot.pop("connections", None)
        snapshot.pop("decisions", None)
        snapshot["pid"] = os.getpid()
        snapshot["version"] = __version__
        snapshot["pipe"] = self.pipe_name
        snapshot["requests"] = self._clients_seen
        return snapshot

    def _connections(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = int(params.get("limit", 200) or 200)
        problematic = bool(params.get("problematic_only", False))
        items = self.service.connections(limit, problematic_only=problematic)
        return {"ok": True, "connections": items}

    def _probe(self, params: dict[str, Any]) -> dict[str, Any]:
        host = str(params.get("host") or "").strip()
        if not host:
            return {"ok": False, "error": "host is required"}
        trials = bool(params.get("trials", True))
        return self.service.probe_host(host, trials=trials)

    def _recipes(self, _params: dict[str, Any]) -> dict[str, Any]:
        recipes = self.service.catalog.list_recipes()
        return {
            "ok": True,
            "recipes": [jsonable(item) for item in recipes],
            "trial_order": [item.id for item in self.service.catalog.trial_order("tcp")],
            "quic_order": [item.id for item in self.service.catalog.trial_order("udp")],
        }

    def _forget(self, params: dict[str, Any]) -> dict[str, Any]:
        host = str(params.get("host") or "").strip()
        if not host:
            return {"ok": False, "error": "host is required"}
        return self.service.forget(host)

    def _pin(self, params: dict[str, Any]) -> dict[str, Any]:
        host = str(params.get("host") or "").strip()
        recipe = str(params.get("recipe") or "").strip()
        mode = str(params.get("mode") or "desync").strip().lower()
        if not host or not recipe:
            return {"ok": False, "error": "host and recipe are required"}
        return self.service.pin(host, recipe, mode)

    def _set_mode(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.service.set_mode(str(params.get("mode") or ""))

    def _update_policy(self, params: dict[str, Any]) -> dict[str, Any]:
        patch = params.get("patch")
        if patch is None:
            patch = {key: value for key, value in params.items() if key != "patch"}
        return self.service.update_policy(patch)

    def _journal_stats(self, _params: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"ok": True, "stats": dict(self.service.journal.stats())}
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def _shutdown(self, _params: dict[str, Any]) -> dict[str, Any]:
        # The reply is written by the session thread *after* this handler
        # returns, and ``NamedPipeServer.stop`` cancels in-flight session I/O.
        # A short grace period lets the acknowledgement reach the client before
        # the pipe is torn down, otherwise ``shutdown`` looks like a timeout.
        timer = threading.Timer(0.4, self.stop)
        timer.name = "orchestrator-shutdown"
        timer.daemon = True
        timer.start()
        return {"ok": True, "stopping": True}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_sidecar(
    *,
    install_root: Path,
    data_dir: Path,
    seed_hosts: list[str] | None = None,
    pipe_name: str | None = None,
    parent_pid: int = 0,
    idle_timeout_s: float = 0.0,
    verbose: bool = False,
) -> OrchestratorSidecar:
    from zapret_zen.orchestrator.factory import build_service

    data_root = Path(data_dir) / "orchestrator"
    data_root.mkdir(parents=True, exist_ok=True)
    log = _FileLogger(data_root / "sidecar.log", echo=verbose)
    service = build_service(
        install_root=Path(install_root),
        data_dir=Path(data_dir),
        seed_hosts=list(seed_hosts or ()),
    )
    token = ensure_token(data_root / "ipc.token")
    return OrchestratorSidecar(
        service,
        pipe_name=pipe_name or default_pipe_name(),
        token=token,
        parent_pid=parent_pid,
        idle_timeout_s=idle_timeout_s,
        log=log,
    )


def _acquire_single_instance(pipe_name: str) -> object:
    """Return a mutex handle, ``False`` if another sidecar already owns it.

    Two processes may race to autostart the sidecar (the UI and the backend
    worker).  A named mutex makes the loser exit immediately instead of adding a
    second pipe server instance and a second observer writing the same SQLite
    database.
    """

    if not sys.platform.startswith("win"):
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        digest = hashlib.sha256(str(pipe_name).encode("utf-8")).hexdigest()[:16]
        handle = kernel32.CreateMutexW(None, 0, f"Local\\ZapretZenOrchestrator-{digest}")
        if not handle:
            return None
        if int(kernel32.GetLastError()) == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        return handle
    except Exception:
        return None


def _detach_console() -> None:
    """Makes sure the sidecar never owns a visible console window.

    The client already spawns it with ``CREATE_NO_WINDOW|DETACHED_PROCESS``,
    but a stray window was once observed anyway; detaching here covers every
    other way this module can be started (``-m``, a shell, a wrapper script).
    """
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zapret-zen-orchestrator", description="Zapret-Zen smart orchestrator sidecar")
    parser.add_argument("--install-root", default="", help="installation root (defaults to the current directory)")
    parser.add_argument("--data-dir", default="", help="writable data directory (defaults to <install-root>/data)")
    parser.add_argument("--pipe", default="", help="override the named-pipe name")
    parser.add_argument("--parent-pid", type=int, default=0, help="exit when this process disappears")
    parser.add_argument("--idle-timeout", type=float, default=0.0, help="exit after being idle for N seconds")
    parser.add_argument("--seed-host", action="append", default=[], help="host to probe even without traffic (repeatable)")
    parser.add_argument("--verbose", action="store_true", help="echo log lines to stderr")
    args, _unknown = parser.parse_known_args(argv)

    if not args.verbose:
        # `--verbose` keeps stderr alive to echo log lines, so only detach when
        # nothing was asked from the console.
        _detach_console()

    install_root = Path(args.install_root).resolve() if args.install_root else Path.cwd()
    data_dir = Path(args.data_dir).resolve() if args.data_dir else install_root / "data"
    pipe_name = args.pipe or default_pipe_name()
    instance = _acquire_single_instance(pipe_name)
    if instance is False:
        print("zapret-zen orchestrator is already running", file=sys.stderr)
        return 0
    sidecar = build_sidecar(
        install_root=install_root,
        data_dir=data_dir,
        seed_hosts=list(args.seed_host or []),
        pipe_name=pipe_name,
        parent_pid=int(args.parent_pid or 0),
        idle_timeout_s=float(args.idle_timeout or 0.0),
        verbose=bool(args.verbose),
    )
    return sidecar.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())

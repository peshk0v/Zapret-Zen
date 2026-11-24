"""Client for the orchestrator sidecar.

Used by the backend worker (actions + periodic restart tick), by the CLI and by
troubleshooting scripts.  The client is synchronous and thread-safe; every call
is a named-pipe round trip, so callers must never invoke it from the Qt GUI
thread with a long timeout.

Autostart: if the pipe is not there, the client launches the sidecar itself
(``--orchestrator-sidecar`` in a packaged build, ``-m ...sidecar`` otherwise)
and waits for the first successful ``ping``.  A short circuit breaker stops
spawn storms when the sidecar keeps dying.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from zapret_zen.runtime_env import is_packaged_runtime
from zapret_zen.orchestrator.ipc.named_pipe import PipeClient, PipeError, PipeTimeout, default_pipe_name
from zapret_zen.orchestrator.ipc.token import ensure_token, read_token

_SPAWN_BREAKER_S = 10.0


class OrchestratorUnavailable(PipeError):
    """The sidecar is not running and could not be started."""


class OrchestratorClient:
    def __init__(
        self,
        *,
        install_root: Path,
        data_dir: Path,
        pipe_name: str | None = None,
        seed_hosts: Sequence[str] = (),
        autostart: bool = True,
        connect_timeout_s: float = 1.5,
        spawn_timeout_s: float = 12.0,
    ) -> None:
        self.install_root = Path(install_root)
        self.data_dir = Path(data_dir)
        self.pipe_name = pipe_name or default_pipe_name()
        self.token_path = self.data_dir / "orchestrator" / "ipc.token"
        self._token = ensure_token(self.token_path) if autostart else read_token(self.token_path)
        self.seed_hosts = [str(item) for item in seed_hosts if str(item).strip()]
        self.autostart = bool(autostart)
        self.connect_timeout_s = max(0.2, float(connect_timeout_s))
        self.spawn_timeout_s = max(1.0, float(spawn_timeout_s))
        self.last_error = ""
        self._lock = threading.RLock()
        self._client: PipeClient | None = None
        self._process: subprocess.Popen[Any] | None = None
        self._last_spawn_at = 0.0

    # -- public API --------------------------------------------------------

    @property
    def connected(self) -> bool:
        client = self._client
        return bool(client is not None and client.connected)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float = 30.0,
        ensure: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            client = self._ensure_client(ensure=ensure)
            try:
                response = client.request(method, params, timeout_s=timeout_s)
            except PipeTimeout as error:
                # A timeout may just be a busy sidecar; keep the handle, but
                # report the failure so callers can surface it.
                self.last_error = str(error)
                raise
            except PipeError:
                self._reset_client()
                raise
        if not isinstance(response, dict):
            raise PipeError("invalid response")
        if not response.get("ok", False):
            self.last_error = str(response.get("error") or "")
        return response

    def try_request(self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float = 5.0) -> dict[str, Any]:
        """Like :meth:`request` but never raises; returns ``{"ok": False}``."""
        try:
            return self.request(method, params, timeout_s=timeout_s)
        except (PipeError, OSError) as error:
            self.last_error = str(error)
            return {"ok": False, "error": str(error)}

    def status(self) -> dict[str, Any]:
        return self.try_request("status")

    def snapshot(self) -> dict[str, Any]:
        return self.try_request("snapshot")

    def ping(self, *, timeout_s: float = 2.0) -> bool:
        client = self._client
        if client is not None and client.connected:
            return client.ping(timeout_s=timeout_s)
        return bool(self.try_request("ping", timeout_s=timeout_s).get("ok"))

    def ensure_running(self) -> bool:
        with self._lock:
            try:
                self._ensure_client(ensure=True)
                return True
            except PipeError:
                return False

    def close(self) -> None:
        with self._lock:
            self._reset_client()

    def shutdown(self, *, timeout_s: float = 3.0) -> bool:
        """Ask the sidecar to exit, then make sure it is gone."""
        ok = False
        try:
            response = self.request("shutdown", timeout_s=timeout_s)
            ok = bool(response.get("ok"))
        except PipeError:
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            process = self._process
            if process is not None and process.poll() is not None:
                break
            if process is None and not self.connected:
                break
            time.sleep(0.05)
        self._terminate_process()
        self._reset_client()
        return ok

    # -- internals ---------------------------------------------------------

    def _ensure_client(self, *, ensure: bool) -> PipeClient:
        client = self._client
        if client is None:
            client = PipeClient(self.pipe_name, auth_token=self._token)
            self._client = client
        if client.connected:
            return client
        if client.connect(timeout_s=self.connect_timeout_s):
            return client
        if not ensure:
            raise OrchestratorUnavailable(f"orchestrator pipe is not available: {self.pipe_name}")
        if not self.autostart:
            raise OrchestratorUnavailable(f"orchestrator pipe is not available: {self.pipe_name}")
        if not self._spawn():
            raise OrchestratorUnavailable(self.last_error or "cannot start the orchestrator sidecar")
        deadline = time.monotonic() + self.spawn_timeout_s
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            if client.connect(timeout_s=0.5):
                if client.ping(timeout_s=1.5):
                    self.last_error = ""
                    return client
            process = self._process
            if process is not None and process.poll() is not None:
                code = process.returncode
                self._process = None
                raise OrchestratorUnavailable(f"orchestrator sidecar exited with code {code}")
            time.sleep(min(0.05 * attempt, 0.4))
        self._terminate_process()
        raise OrchestratorUnavailable(self.last_error or "orchestrator sidecar did not become ready")

    def _spawn(self) -> bool:
        now = time.monotonic()
        if now - self._last_spawn_at < _SPAWN_BREAKER_S:
            self.last_error = "orchestrator sidecar was started recently, not retrying yet"
            return False
        process = self._process
        if process is not None and process.poll() is None:
            return True
        command = self._build_command()
        env = self._build_env()
        flags = 0
        startupinfo = None
        if sys.platform.startswith("win"):
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        try:
            self._process = subprocess.Popen(
                command,
                cwd=str(self.install_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                startupinfo=startupinfo,
                close_fds=True,
            )
        except OSError as error:
            self.last_error = f"cannot spawn orchestrator sidecar: {error}"
            return False
        self._last_spawn_at = now
        return True

    def _build_command(self) -> list[str]:
        arguments = [
            "--install-root",
            str(self.install_root),
            "--data-dir",
            str(self.data_dir),
            "--pipe",
            self.pipe_name,
            "--parent-pid",
            str(os.getpid()),
        ]
        for host in self.seed_hosts:
            arguments.extend(["--seed-host", host])
        if is_packaged_runtime():
            return [sys.executable, "--orchestrator-sidecar", *arguments]
        return [_python_executable(self.install_root), "-m", "zapret_zen.orchestrator.sidecar", *arguments]

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if not is_packaged_runtime():
            src_root = str(self.install_root / "src")
            parts = [item for item in str(env.get("PYTHONPATH", "") or "").split(os.pathsep) if item]
            if src_root not in parts:
                parts.insert(0, src_root)
            env["PYTHONPATH"] = os.pathsep.join(parts)
            env.setdefault("PYTHONUTF8", "1")
            env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def _terminate_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _reset_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def _python_executable(install_root: Path) -> str:
    candidates = [
        install_root / ".venv" / "Scripts" / "python.exe",
        install_root / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                return str(candidate)
        except OSError:
            continue
    return sys.executable

"""Windows named-pipe transport for the orchestrator sidecar.

The transport is deliberately boring: a byte-mode named pipe with a per-user
DACL, carrying length-prefixed JSON frames (see :mod:`...ipc.framing`).  It has
no dependency on Qt, ``multiprocessing`` or asyncio, so the sidecar can be a
plain console process today and a Rust binary tomorrow — only the pipe name and
the frame layout are contract.

Why a named pipe instead of a TCP socket:

* no listening port, no Windows Firewall prompt;
* access is controlled by the object DACL (see ``_pipe_security``);
* ``CreateNamedPipeW`` with ``PIPE_REJECT_REMOTE_CLIENTS`` in the *pipe mode*
  blocks SMB access (the flag is a ``dwPipeMode`` bit, not ``dwOpenMode``).

The client never blocks indefinitely: reads go through ``PeekNamedPipe`` with a
deadline, so a hung sidecar surfaces as ``PipeTimeout`` instead of freezing the
UI or the backend worker.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import os
import sys
import threading
import time
import uuid
from typing import Any, Callable

from zapret_zen.orchestrator.ipc import framing

PIPE_PREFIX = r"\\.\pipe"

PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3

ERROR_PIPE_CONNECTED = 535
ERROR_PIPE_BUSY = 231
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_MORE_DATA = 234
ERROR_OPERATION_ABORTED = 995

THREAD_TERMINATE = 0x0001

BUFFER_SIZE = 64 * 1024
_READ_CHUNK = 8192
_POLL_SLICE = 0.005

_INVALID_HANDLE = ctypes.c_void_p(-1).value


class PipeError(RuntimeError):
    """Transport-level failure (pipe missing, broken or malformed)."""


class PipeTimeout(PipeError):
    """The peer did not answer within the allotted time."""


def default_pipe_name(suffix: str = "") -> str:
    """Per-user pipe name, stable across processes on this machine.

    The digest keeps two Windows users (and two portable copies with different
    profile paths) from fighting over a single pipe instance.
    """
    identity = "|".join(
        [
            os.environ.get("USERNAME", "user"),
            os.environ.get("USERPROFILE", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ]
    )
    digest = hashlib.sha1(identity.encode("utf-8", "replace")).hexdigest()[:12]
    name = f"zapret-zen-orchestrator-{digest}"
    if suffix:
        name = f"{name}-{suffix}"
    return f"{PIPE_PREFIX}\\{name}"


def pipe_name(suffix: str = "") -> str:
    """Alias kept for readability at call sites."""
    return default_pipe_name(suffix)


# ---------------------------------------------------------------------------
# ctypes plumbing
# ---------------------------------------------------------------------------


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


_kernel32: Any = None
_advapi32: Any = None
_configured = False


def _configure() -> None:
    global _kernel32, _advapi32, _configured
    if _configured:
        return
    if not sys.platform.startswith("win"):
        raise PipeError("named pipes are only available on Windows")
    _kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)

    _kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes),
    ]
    _kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    _kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    _kernel32.ConnectNamedPipe.restype = wintypes.BOOL
    _kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    _kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL
    _kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.PeekNamedPipe.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    _kernel32.CancelSynchronousIo.restype = wintypes.BOOL

    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _configured = True


def _k32() -> Any:
    _configure()
    return _kernel32


def _last_error() -> int:
    return int(ctypes.get_last_error() or 0)


def _is_invalid(handle: Any) -> bool:
    if handle in (None, 0):
        return True
    try:
        return int(handle) == int(_INVALID_HANDLE)
    except (TypeError, ValueError):
        return False


def _close(handle: Any) -> None:
    if _is_invalid(handle):
        return
    try:
        _k32().CloseHandle(handle)
    except Exception:
        pass


def _cancel_synchronous_io(thread: threading.Thread | None) -> None:
    """Interrupt a thread blocked in ``ConnectNamedPipe``/``ReadFile``.

    Closing the handle from another thread does not help: ``CloseHandle`` itself
    waits for the pending synchronous I/O and deadlocks.  ``CancelSynchronousIo``
    is the documented way to break such a call, after which the blocked call
    fails with ``ERROR_OPERATION_ABORTED``.
    """
    if thread is None or thread is threading.current_thread():
        return
    native_id = getattr(thread, "native_id", None)
    if not native_id:
        return
    kernel32 = _k32()
    try:
        handle = kernel32.OpenThread(THREAD_TERMINATE, False, int(native_id))
    except Exception:
        return
    if _is_invalid(handle):
        return
    try:
        kernel32.CancelSynchronousIo(handle)
    except Exception:
        pass
    finally:
        _close(handle)


def _sddl_security() -> tuple[_SecurityAttributes | None, wintypes.LPVOID]:
    """Allow only this user, local system and administrators.

    ``OW`` is the object owner (the creating user), which is what makes the
    pipe invisible to other interactive sessions.  If the conversion fails we
    fall back to the default DACL rather than refusing to start.
    """
    sddl = "D:P(A;;GA;;;OW)(A;;GA;;;SY)(A;;GA;;;BA)"
    descriptor = wintypes.LPVOID()
    size = wintypes.DWORD(0)
    try:
        _configure()
    except PipeError:
        return None, wintypes.LPVOID()
    try:
        ok = _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)
        )
    except Exception:
        ok = 0
    if not ok or not descriptor:
        return None, wintypes.LPVOID()
    attributes = _SecurityAttributes()
    attributes.nLength = ctypes.sizeof(_SecurityAttributes)
    attributes.lpSecurityDescriptor = descriptor
    attributes.bInheritHandle = False
    return attributes, descriptor


def _release_security(descriptor: wintypes.LPVOID) -> None:
    if not descriptor:
        return
    try:
        ctypes.WinDLL("kernel32.dll").LocalFree(descriptor)
    except Exception:
        pass


def _write_all(handle: Any, data: bytes) -> None:
    kernel32 = _k32()
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        chunk = bytes(view[offset : offset + _READ_CHUNK])
        buffer = ctypes.create_string_buffer(chunk, len(chunk))
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(handle, ctypes.byref(buffer), len(chunk), ctypes.byref(written), None)
        if not ok:
            raise PipeError(f"WriteFile failed: {_last_error()}")
        if written.value <= 0:
            raise PipeError("WriteFile wrote nothing")
        offset += written.value
    try:
        kernel32.FlushFileBuffers(handle)
    except Exception:
        pass


def _read_some(handle: Any, size: int) -> bytes:
    kernel32 = _k32()
    buffer = ctypes.create_string_buffer(max(1, size))
    read = wintypes.DWORD(0)
    ok = kernel32.ReadFile(handle, ctypes.byref(buffer), size, ctypes.byref(read), None)
    if not ok:
        error = _last_error()
        if error in (ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED, ERROR_NO_DATA, ERROR_OPERATION_ABORTED):
            return b""
        raise PipeError(f"ReadFile failed: {error}")
    return buffer.raw[: read.value]


def _read_exact(handle: Any, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = _read_some(handle, size - len(chunks))
        if not part:
            break
        chunks.extend(part)
    return bytes(chunks)


def _available(handle: Any) -> int:
    kernel32 = _k32()
    available = wintypes.DWORD(0)
    ok = kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None)
    if not ok:
        error = _last_error()
        if error in (ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED, ERROR_NO_DATA):
            return -1
        return -1
    return int(available.value)


def _read_frame(handle: Any) -> Any:
    header = _read_exact(handle, framing.header_size())
    if len(header) < framing.header_size():
        return None
    length, _version = framing.decode_header(header)
    body = _read_exact(handle, length)
    if len(body) < length:
        return None
    return framing.decode(body)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

RequestHandler = Callable[[dict[str, Any]], dict[str, Any]]


class NamedPipeServer:
    """Threaded named-pipe server with one session thread per client.

    The handler receives a decoded request dict and must return a JSON-safe
    dict.  Exceptions are caught and returned as ``{"ok": False, "error": ...}``
    so a bug in one method never takes the sidecar down.
    """

    def __init__(
        self,
        name: str,
        handler: RequestHandler,
        *,
        max_instances: int = 8,
        auth_token: str = "",
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.name = name if name.startswith(PIPE_PREFIX) else f"{PIPE_PREFIX}\\{name}"
        self.handler = handler
        self.max_instances = max(1, int(max_instances))
        self.auth_token = auth_token or ""
        self._log = logger or (lambda _message: None)
        self._stop = threading.Event()
        self._sessions: set[threading.Thread] = set()
        self._sessions_lock = threading.Lock()
        self._pending: Any = None
        self._pending_lock = threading.Lock()
        self._served = 0
        self._serve_thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        thread = threading.Thread(target=self.serve_forever, name="orchestrator-pipe", daemon=True)
        self._serve_thread = thread
        thread.start()

    def serve_forever(self) -> None:
        _k32()
        self._serve_thread = threading.current_thread()
        while not self._stop.is_set():
            handle = self._create_instance()
            if _is_invalid(handle):
                error = _last_error()
                if error == ERROR_PIPE_BUSY:
                    time.sleep(0.05)
                    continue
                self._log(f"pipe instance failed: {error}")
                time.sleep(0.5)
                continue
            with self._pending_lock:
                self._pending = handle
            if not self._accept(handle):
                self._close_pending(handle)
                continue
            with self._pending_lock:
                self._pending = None
            session = threading.Thread(target=self._session, args=(handle,), name="orchestrator-pipe-session", daemon=True)
            with self._sessions_lock:
                self._sessions.add(session)
            session.start()

    def stop(self) -> None:
        self._stop.set()
        # ``CloseHandle`` and ``DisconnectNamedPipe`` block while another thread
        # is parked inside ``ConnectNamedPipe``/``ReadFile``.  Cancelling the
        # synchronous I/O first is what actually unblocks those threads, so the
        # handles can be released right after.
        current = threading.current_thread()
        with self._sessions_lock:
            sessions = [item for item in self._sessions if item is not current]
        serve_thread = self._serve_thread
        if serve_thread is not None and serve_thread is not current:
            _cancel_synchronous_io(serve_thread)
        for session in sessions:
            _cancel_synchronous_io(session)
        for session in sessions:
            session.join(timeout=1.0)
        if serve_thread is not None and serve_thread is not current:
            serve_thread.join(timeout=1.0)
        self._close_pending()

    @property
    def served_requests(self) -> int:
        return self._served

    # -- internals ---------------------------------------------------------

    def _create_instance(self) -> Any:
        attributes, descriptor = _sddl_security()
        try:
            handle = _k32().CreateNamedPipeW(
                self.name,
                PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                self.max_instances,
                BUFFER_SIZE,
                BUFFER_SIZE,
                0,
                ctypes.byref(attributes) if attributes is not None else None,
            )
        finally:
            if descriptor:
                _release_security(descriptor)
        return handle

    def _accept(self, handle: Any) -> bool:
        kernel32 = _k32()
        ok = kernel32.ConnectNamedPipe(handle, None)
        if ok:
            return True
        error = _last_error()
        if error == ERROR_PIPE_CONNECTED:
            return True
        if self._stop.is_set():
            return False
        self._log(f"ConnectNamedPipe failed: {error}")
        return False

    def _close_pending(self, handle: Any | None = None) -> None:
        with self._pending_lock:
            target = handle if handle is not None else self._pending
            if self._pending is target:
                self._pending = None
        if target is not None:
            _close(target)

    def _session(self, handle: Any) -> None:
        try:
            while not self._stop.is_set():
                request = _read_frame(handle)
                if request is None:
                    break
                response = self._dispatch(request)
                _write_all(handle, framing.encode(response))
        except PipeError as error:
            self._log(f"session closed: {error}")
        except Exception as error:  # pragma: no cover - defensive
            self._log(f"session crashed: {error!r}")
        finally:
            try:
                _k32().DisconnectNamedPipe(handle)
            except Exception:
                pass
            _close(handle)
            with self._sessions_lock:
                self._sessions.discard(threading.current_thread())

    def _dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": "request must be an object"}
        request_id = request.get("id")
        if self.auth_token and str(request.get("token") or "") != self.auth_token:
            return {"id": request_id, "ok": False, "error": "unauthorized"}
        self._served += 1
        try:
            response = self.handler(request)
        except Exception as error:  # pragma: no cover - defensive
            self._log(f"handler error for {request.get('method')!r}: {error!r}")
            return {"id": request_id, "ok": False, "error": str(error) or error.__class__.__name__}
        if not isinstance(response, dict):
            return {"id": request_id, "ok": False, "error": "handler returned a non-object"}
        response.setdefault("id", request_id)
        return response


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PipeClient:
    """Blocking request/response client, safe to share between threads."""

    def __init__(self, name: str, *, auth_token: str = "", connect_timeout_s: float = 5.0) -> None:
        self.name = name if name.startswith(PIPE_PREFIX) else f"{PIPE_PREFIX}\\{name}"
        self.auth_token = auth_token or ""
        self.connect_timeout_s = max(0.1, float(connect_timeout_s))
        self._handle: Any = None
        self._lock = threading.RLock()

    # -- connection --------------------------------------------------------

    @property
    def connected(self) -> bool:
        return not _is_invalid(self._handle)

    def connect(self, timeout_s: float | None = None) -> bool:
        kernel32 = _k32()
        with self._lock:
            if self.connected:
                return True
            deadline = time.monotonic() + (self.connect_timeout_s if timeout_s is None else max(0.1, float(timeout_s)))
            while True:
                handle = kernel32.CreateFileW(
                    self.name,
                    GENERIC_READ | GENERIC_WRITE,
                    0,
                    None,
                    OPEN_EXISTING,
                    0,
                    None,
                )
                if not _is_invalid(handle):
                    self._handle = handle
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    wait_ms = int(min(remaining, 0.25) * 1000)
                    kernel32.WaitNamedPipeW(self.name, max(1, wait_ms))
                except Exception:
                    pass
                time.sleep(0.02)

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
        _close(handle)

    def __enter__(self) -> "PipeClient":
        if not self.connect():
            raise PipeError(f"cannot connect to {self.name}")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- requests ----------------------------------------------------------

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float = 30.0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        identifier = request_id or uuid.uuid4().hex
        payload: dict[str, Any] = {
            "id": identifier,
            "method": str(method),
            "params": dict(params or {}),
        }
        if self.auth_token:
            payload["token"] = self.auth_token
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        with self._lock:
            if not self.connected and not self.connect():
                raise PipeError(f"cannot connect to {self.name}")
            handle = self._handle
            try:
                _write_all(handle, framing.encode(payload))
                # A previous call may have timed out while its answer was still
                # in flight.  Match on the request id and drop such stale frames
                # instead of returning another call's result.
                while True:
                    response = self._read_response(handle, deadline)
                    if not isinstance(response, dict):
                        raise PipeError("invalid response frame")
                    if str(response.get("id") or "") == identifier:
                        return response
                    if time.monotonic() >= deadline:
                        raise PipeTimeout(f"no response from {self.name}")
            except BaseException:
                self.close()
                raise

    def ping(self, timeout_s: float = 2.0) -> bool:
        try:
            response = self.request("ping", timeout_s=timeout_s)
        except PipeError:
            return False
        return bool(response.get("ok"))

    def _read_response(self, handle: Any, deadline: float) -> Any:
        header = self._take(handle, framing.header_size(), deadline, allow_empty=False)
        if header is None:
            raise PipeError("pipe closed by server")
        length, _version = framing.decode_header(header)
        body = self._take(handle, length, deadline, allow_empty=False)
        if body is None:
            raise PipeError("pipe closed by server")
        return framing.decode(body)

    def _take(self, handle: Any, size: int, deadline: float, *, allow_empty: bool = True) -> bytes | None:
        kernel32 = _k32()
        chunks = bytearray()
        while len(chunks) < size:
            available = _available(handle)
            if available < 0:
                if not allow_empty and not chunks:
                    raise PipeError("pipe closed by server")
                return bytes(chunks)
            if available == 0:
                if time.monotonic() >= deadline:
                    raise PipeTimeout(f"no response from {self.name}")
                time.sleep(_POLL_SLICE)
                continue
            want = min(available, size - len(chunks))
            buffer = ctypes.create_string_buffer(max(1, want))
            read = wintypes.DWORD(0)
            ok = kernel32.ReadFile(handle, ctypes.byref(buffer), want, ctypes.byref(read), None)
            if not ok:
                error = _last_error()
                if error in (ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED, ERROR_NO_DATA, ERROR_MORE_DATA):
                    raise PipeError(f"pipe closed while reading ({error})")
                raise PipeError(f"ReadFile failed: {error}")
            if read.value:
                chunks.extend(buffer.raw[: read.value])
        return bytes(chunks)

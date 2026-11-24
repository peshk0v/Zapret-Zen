"""Passive connection observer built on the Windows IP helper API.

``GetExtendedTcpTable`` needs no driver, no admin rights and no ETW session,
which makes it the safe degraded observer. It is polled once per second and
reports new TCP 5-tuples with the owning PID. UDP/QUIC attribution requires
ETW and is intentionally unavailable in this adapter.

Higher-fidelity ETW observation can be plugged in later behind the same
``TrafficObserver`` port without touching the application layer.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import socket
import struct
import sys
import time
from typing import Any

from zapret_zen.orchestrator.domain.models import ConnectionEvent

AF_INET = 2
AF_INET6 = 23
TCP_TABLE_OWNER_PID_ALL = 5
ERROR_INSUFFICIENT_BUFFER = 122
NO_ERROR = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_TCP_ESTABLISHED = 5
_TCP_SYN_SENT = 3
_SEEN_TTL_SECONDS = 900.0


class _MibTcpRowOwnerPid(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class _MibTcp6RowOwnerPid(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("ucRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwState", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class TcpTableObserver:
    """Polling observer over the kernel TCP/UDP tables."""

    name = "tcp-table"

    def __init__(
        self,
        *,
        probe_lane: tuple[int, int] | None = None,
        process_allowlist: list[str] | None = None,
        dns_cache: Any = None,
    ) -> None:
        self._available = sys.platform == "win32"
        self._started = False
        self._dropped = 0
        self._seen: dict[tuple[object, ...], float] = {}
        self._snapshot: list[ConnectionEvent] = []
        self._probe_lane = probe_lane
        self._allowlist = {item.lower() for item in (process_allowlist or []) if item}
        self._process_cache: dict[int, str] = {}
        self._dns_cache = dns_cache
        self._detail = "" if self._available else "Windows only"
        self._reported: set[tuple[object, ...]] = set()
        self._functions: dict[tuple[bool, int, int], Any] = {}
        if self._available:
            try:
                self._iphlpapi = ctypes.WinDLL("iphlpapi.dll")
                self._kernel32 = ctypes.WinDLL("kernel32.dll")
                self._configure_signatures()
                self._detail = "TCP table"
            except Exception as error:  # pragma: no cover - depends on host
                self._available = False
                self._detail = f"unavailable: {error}"

    def _configure_signatures(self) -> None:
        self._iphlpapi.GetExtendedTcpTable.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
        ]
        self._iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    # -- port contract -----------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def detail(self) -> str:
        return self._detail

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def snapshot(self) -> list[ConnectionEvent]:
        return list(self._snapshot)

    def poll(self, max_events: int = 512) -> list[ConnectionEvent]:
        if not self._available or not self._started:
            return []
        now = time.time()
        rows: list[ConnectionEvent] = []
        try:
            rows.extend(self._read_tcp4())
            rows.extend(self._read_tcp6())
        except Exception as error:  # pragma: no cover - defensive
            self._detail = f"read error: {error}"
            return []
        current: list[ConnectionEvent] = []
        emitted: list[ConnectionEvent] = []
        active_keys: set[tuple[object, ...]] = set()
        for event in rows:
            if self._is_ignored(event):
                continue
            current.append(event)
            key = event.key
            active_keys.add(key)
            if key not in self._reported and len(emitted) < max_events:
                emitted.append(event)
                self._reported.add(key)
            self._seen[key] = now
        self._reported.intersection_update(active_keys)
        self._snapshot = current
        self._prune(now)
        return emitted

    # -- readers -----------------------------------------------------------

    def _read_tcp4(self) -> list[ConnectionEvent]:
        data = self._table(True, AF_INET, TCP_TABLE_OWNER_PID_ALL)
        if not data:
            return []
        count, offset = self._count(data)
        size = ctypes.sizeof(_MibTcpRowOwnerPid)
        result: list[ConnectionEvent] = []
        for index in range(count):
            row = _MibTcpRowOwnerPid.from_buffer_copy(data[offset + index * size : offset + (index + 1) * size])
            if int(row.dwState) not in (_TCP_ESTABLISHED, _TCP_SYN_SENT):
                continue
            result.append(
                ConnectionEvent(
                    ts=time.time(),
                    pid=int(row.dwOwningPid),
                    process=self._process_name(int(row.dwOwningPid)),
                    protocol="tcp",
                    local_address=socket.inet_ntoa(struct.pack("<L", int(row.dwLocalAddr))),
                    local_port=socket.ntohs(int(row.dwLocalPort) & 0xFFFF),
                    remote_address=socket.inet_ntoa(struct.pack("<L", int(row.dwRemoteAddr))),
                    remote_port=socket.ntohs(int(row.dwRemotePort) & 0xFFFF),
                    hostname=self._hostname(socket.inet_ntoa(struct.pack("<L", int(row.dwRemoteAddr)))),
                )
            )
        return result

    def _read_tcp6(self) -> list[ConnectionEvent]:
        data = self._table(True, AF_INET6, TCP_TABLE_OWNER_PID_ALL)
        if not data:
            return []
        count, offset = self._count(data)
        size = ctypes.sizeof(_MibTcp6RowOwnerPid)
        result: list[ConnectionEvent] = []
        for index in range(count):
            row = _MibTcp6RowOwnerPid.from_buffer_copy(data[offset + index * size : offset + (index + 1) * size])
            if int(row.dwState) not in (_TCP_ESTABLISHED, _TCP_SYN_SENT):
                continue
            result.append(
                ConnectionEvent(
                    ts=time.time(),
                    pid=int(row.dwOwningPid),
                    process=self._process_name(int(row.dwOwningPid)),
                    protocol="tcp",
                    local_address=socket.inet_ntop(socket.AF_INET6, bytes(row.ucLocalAddr)),
                    local_port=socket.ntohs(int(row.dwLocalPort) & 0xFFFF),
                    remote_address=socket.inet_ntop(socket.AF_INET6, bytes(row.ucRemoteAddr)),
                    remote_port=socket.ntohs(int(row.dwRemotePort) & 0xFFFF),
                    hostname=self._hostname(socket.inet_ntop(socket.AF_INET6, bytes(row.ucRemoteAddr))),
                )
            )
        return result

    # -- helpers -----------------------------------------------------------

    def _hostname(self, address: str) -> str:
        if self._dns_cache is None:
            return ""
        try:
            return str(self._dns_cache.host_for(address) or "")
        except Exception:
            return ""

    def _table(self, tcp: bool, family: int, table_class: int) -> bytes:
        if not tcp:
            return b""
        size = wintypes.DWORD(0)
        function = self._iphlpapi.GetExtendedTcpTable
        code = function(None, ctypes.byref(size), False, family, table_class, 0)
        if code not in (ERROR_INSUFFICIENT_BUFFER, NO_ERROR) or size.value == 0:
            return b""
        buffer = ctypes.create_string_buffer(size.value)
        code = function(buffer, ctypes.byref(size), False, family, table_class, 0)
        if code != NO_ERROR:
            return b""
        return buffer.raw[: size.value]

    @staticmethod
    def _count(data: bytes) -> tuple[int, int]:
        if len(data) < 4:
            return 0, 4
        count = int.from_bytes(data[0:4], "little")
        return count, 4

    def _process_name(self, pid: int) -> str:
        if pid <= 0:
            return ""
        cached = self._process_cache.get(pid)
        if cached is not None:
            return cached
        name = ""
        handle = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            try:
                buffer = ctypes.create_unicode_buffer(1024)
                length = wintypes.DWORD(len(buffer))
                if self._kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                    name = buffer.value.rsplit("\\", 1)[-1]
            finally:
                self._kernel32.CloseHandle(handle)
        if len(self._process_cache) > 4096:
            self._process_cache.clear()
        self._process_cache[pid] = name
        return name

    def _is_ignored(self, event: ConnectionEvent) -> bool:
        if event.remote_port <= 0:
            return True
        if event.remote_address.startswith(("127.", "0.0.0.0", "::1", "::")):
            return True
        if self._probe_lane:
            start, end = self._probe_lane
            if start <= event.local_port <= end:
                return True
        if self._allowlist and event.process.lower() not in self._allowlist:
            return True
        return False

    def _prune(self, now: float) -> None:
        if len(self._seen) > 20000:
            cutoff = now - _SEEN_TTL_SECONDS
            self._seen = {key: value for key, value in self._seen.items() if value >= cutoff}

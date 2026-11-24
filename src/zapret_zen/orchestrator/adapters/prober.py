"""Active prober with the failure taxonomy borrowed from NetZapret.

Passive observation can only produce a suspicion.  A verdict is produced here:
TCP connect -> TLS handshake -> HTTP response -> bulk transfer, with a hard
timeout, a stall window and a minimum number of bytes, exactly like
``BlockCheck`` in the C# project (4 s / 3 s / 256 KiB by default).
"""

from __future__ import annotations

import random
import re
import socket
import ssl
import struct
import time

from zapret_zen.orchestrator.domain.models import BlockKind, ProbeResult, ProbeTiming

_STATUS_RE = re.compile(rb"HTTP/\d(?:\.\d)?\s+(\d{3})")
_DEFAULT_PATH = "/"


class SocketProber:
    def __init__(
        self,
        *,
        timeout_s: float = 4.0,
        stall_s: float = 3.0,
        min_bytes: int = 262144,
        lane: tuple[int, int] = (40000, 40100),
    ) -> None:
        self.timeout_s = max(0.5, float(timeout_s))
        self.stall_s = max(0.5, float(stall_s))
        self.min_bytes = max(1024, int(min_bytes))
        self.lane = lane

    # -- public API --------------------------------------------------------

    def probe(
        self,
        host: str,
        *,
        port: int = 443,
        path: str = _DEFAULT_PATH,
        recipe: str | None = None,
        transport: str = "direct",
        source_port: int | None = None,
        lane: bool = True,
    ) -> ProbeResult:
        host = (host or "").strip().lower().rstrip(".")
        if not host:
            return ProbeResult(host="", port=port, kind=BlockKind.NO_ADDRESS.value, ts=time.time())
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            return ProbeResult(host=host, port=port, kind=BlockKind.DNS.value, ts=time.time(), error=str(error), recipe=recipe, transport=transport)
        addresses: list[str] = []
        for info in infos:
            address = str(info[4][0])
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            return ProbeResult(host=host, port=port, kind=BlockKind.NO_ADDRESS.value, ts=time.time(), recipe=recipe, transport=transport)

        # Try a second address when the first one is unreachable at the TCP
        # level: dual-stack hosts often return an IPv6 address first while the
        # network only routes IPv4 (or the site is blocked per address family).
        # TLS-level failures are meaningful on their own and are not retried.
        result: ProbeResult | None = None
        for address in addresses[:2]:
            attempt = self._attempt(
                host,
                address,
                port,
                path,
                recipe=recipe,
                transport=transport,
                source_port=source_port,
                lane=lane,
            )
            result = attempt
            if attempt.ok or attempt.kind not in {
                BlockKind.FULL.value,
                BlockKind.TIMEOUT.value,
                BlockKind.ERROR.value,
            }:
                break
        if result is None:
            return ProbeResult(host=host, port=port, kind=BlockKind.NO_ADDRESS.value, ts=time.time(), recipe=recipe, transport=transport)
        return _with_addresses(result, addresses)

    def probe_confirmed(self, host: str, *, attempts: int = 2, **kwargs) -> ProbeResult:
        attempts = max(1, int(attempts))
        last: ProbeResult | None = None
        for index in range(attempts):
            last = self.probe(host, **kwargs)
            if last.ok:
                return last
            if index + 1 < attempts:
                time.sleep(0.25)
        assert last is not None
        return last

    # -- internals ---------------------------------------------------------

    def _attempt(
        self,
        host: str,
        address: str,
        port: int,
        path: str,
        *,
        recipe: str | None,
        transport: str,
        source_port: int | None,
        lane: bool = True,
    ) -> ProbeResult:
        started = time.monotonic()
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        bound = False
        try:
            sock.settimeout(self.timeout_s)
            try:
                # Reset instead of FIN: a probe port must not sit in TIME_WAIT
                # for two minutes, or the small lane would run out under load.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except OSError:
                pass
            bound = self._bind_lane(sock, family, source_port, use_lane=lane)
            if not bound:
                return self._result(
                    host,
                    port,
                    BlockKind.ERROR,
                    started,
                    recipe,
                    transport,
                    error="probe source-port lane is unavailable",
                )
            target = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            try:
                sock.connect(target)
            except socket.timeout:
                return self._result(host, port, BlockKind.TIMEOUT, started, recipe, transport, error="TCP connect timed out")
            except ConnectionResetError as error:
                return self._result(host, port, BlockKind.FULL, started, recipe, transport, error=f"TCP reset: {error}")
            except OSError as error:
                return self._result(host, port, BlockKind.FULL, started, recipe, transport, error=f"TCP connect failed: {error}")
            tcp_ms = _elapsed_ms(started)

            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            try:
                context.minimum_version = ssl.TLSVersion.TLSv1_2
            except Exception:
                pass
            handshake_started = time.monotonic()
            try:
                tls = context.wrap_socket(sock, server_hostname=host)
            except socket.timeout:
                return self._result(
                    host,
                    port,
                    BlockKind.TLS_DPI,
                    started,
                    recipe,
                    transport,
                    tcp_ms=tcp_ms,
                    error="TLS ClientHello sent, no ServerHello",
                )
            except (ConnectionResetError, ConnectionAbortedError) as error:
                return self._result(host, port, BlockKind.TLS_DPI, started, recipe, transport, tcp_ms=tcp_ms, error=f"TLS reset: {error}")
            except ssl.SSLError as error:
                return self._result(host, port, BlockKind.HANDSHAKE, started, recipe, transport, tcp_ms=tcp_ms, error=f"TLS handshake failed: {error}")
            except OSError as error:
                return self._result(host, port, BlockKind.TLS_DPI, started, recipe, transport, tcp_ms=tcp_ms, error=f"TLS error: {error}")
            tls_ms = _elapsed_ms(handshake_started)

            try:
                return self._read_response(
                    tls,
                    host,
                    port,
                    path,
                    started,
                    tcp_ms=tcp_ms,
                    tls_ms=tls_ms,
                    recipe=recipe,
                    transport=transport,
                )
            finally:
                try:
                    tls.close()
                except Exception:
                    pass
        except OSError as error:
            return self._result(host, port, BlockKind.ERROR, started, recipe, transport, error=str(error))
        finally:
            if bound:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                except Exception:
                    pass
            try:
                sock.close()
            except Exception:
                pass

    def _bind_lane(self, sock: socket.socket, family: int, source_port: int | None, *, use_lane: bool = True) -> bool:
        # Health checks must travel the same path as real traffic, otherwise a
        # rule that production already applies would be measured as if it were
        # absent (the production instance excludes the probe lane on purpose).
        if not use_lane:
            return True
        candidates: list[int] = []
        if source_port:
            start, end = self.lane
            if not start <= int(source_port) <= end:
                return False
            candidates.append(int(source_port))
        start, end = self.lane
        if start < 1 or end > 65535 or end < start:
            return False
        pool = list(range(start, end + 1))
        random.shuffle(pool)
        candidates.extend(pool)
        for candidate in candidates:
            if not (1 <= candidate <= 65535):
                continue
            try:
                if family == socket.AF_INET6:
                    sock.bind(("::", candidate, 0, 0))
                else:
                    sock.bind(("0.0.0.0", candidate))
                return True
            except OSError:
                continue
        return False

    def _read_response(
        self,
        tls: ssl.SSLSocket,
        host: str,
        port: int,
        path: str,
        started: float,
        *,
        tcp_ms: int,
        tls_ms: int,
        recipe: str | None,
        transport: str,
    ) -> ProbeResult:
        request = (
            f"GET {path or _DEFAULT_PATH} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) ZapretZen/1.0\r\n"
            "Accept: */*\r\n"
            "Accept-Language: en-US,en;q=0.9\r\n"
            "Connection: close\r\n\r\n"
        )
        try:
            tls.sendall(request.encode("ascii", "replace"))
        except (OSError, ssl.SSLError) as error:
            return self._result(host, port, BlockKind.STALL, started, recipe, transport, tcp_ms=tcp_ms, tls_ms=tls_ms, error=f"request send failed: {error}")

        deadline = time.monotonic() + self.timeout_s
        received = 0
        header_buffer = b""
        status: int | None = None
        ttfb_ms: int | None = None
        last_data = time.monotonic()
        stalled_at: int | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                tls.settimeout(min(self.stall_s, remaining))
                chunk = tls.recv(65536)
            except socket.timeout:
                gap = time.monotonic() - last_data
                if gap >= self.stall_s:
                    stalled_at = int((time.monotonic() - started) * 1000)
                    break
                continue
            except (ConnectionResetError, ConnectionAbortedError) as error:
                return self._result(host, port, BlockKind.STALL, started, recipe, transport, tcp_ms=tcp_ms, tls_ms=tls_ms, bytes_read=received, error=f"reset while reading: {error}")
            except ssl.SSLError as error:
                return self._result(host, port, BlockKind.STALL, started, recipe, transport, tcp_ms=tcp_ms, tls_ms=tls_ms, bytes_read=received, error=f"TLS read failed: {error}")
            except OSError as error:
                return self._result(host, port, BlockKind.STALL, started, recipe, transport, tcp_ms=tcp_ms, tls_ms=tls_ms, bytes_read=received, error=str(error))
            if not chunk:
                break
            if ttfb_ms is None:
                ttfb_ms = _elapsed_ms(started)
            last_data = time.monotonic()
            received += len(chunk)
            if status is None:
                header_buffer += chunk[:8192]
                match = _STATUS_RE.search(header_buffer)
                if match:
                    status = int(match.group(1))
            if received >= self.min_bytes:
                break

        total_ms = _elapsed_ms(started)
        timing = ProbeTiming(
            tcp_connect_ms=tcp_ms,
            tls_handshake_ms=tls_ms,
            ttfb_ms=ttfb_ms,
            total_ms=total_ms,
            bytes_read=received,
            stalled_at_ms=stalled_at,
        )
        kind = BlockKind.OK
        error = ""
        if status == 451:
            kind = BlockKind.GEO_BLOCK
            error = "HTTP 451"
        elif status == 403:
            kind = BlockKind.FORBIDDEN
            error = "HTTP 403"
        elif status is not None and status >= 500:
            kind = BlockKind.ERROR
            error = f"HTTP {status}"
        elif stalled_at is not None and received < self.min_bytes:
            kind = BlockKind.STALL
            error = f"data stalled at {received} bytes"
        elif status is None and received == 0:
            kind = BlockKind.TIMEOUT
            error = "no HTTP response"
        return ProbeResult(
            host=host,
            port=port,
            kind=kind.value,
            ts=time.time(),
            recipe=recipe,
            status_code=status,
            error=error,
            timing=timing,
            transport=transport,
        )

    def _result(
        self,
        host: str,
        port: int,
        kind: BlockKind,
        started: float,
        recipe: str | None,
        transport: str,
        *,
        tcp_ms: int | None = None,
        tls_ms: int | None = None,
        bytes_read: int = 0,
        error: str = "",
    ) -> ProbeResult:
        return ProbeResult(
            host=host,
            port=port,
            kind=kind.value,
            ts=time.time(),
            recipe=recipe,
            error=error,
            timing=ProbeTiming(tcp_connect_ms=tcp_ms, tls_handshake_ms=tls_ms, total_ms=_elapsed_ms(started), bytes_read=bytes_read),
            transport=transport,
        )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _with_addresses(result: ProbeResult, addresses: list[str]) -> ProbeResult:
    return ProbeResult(
        host=result.host,
        port=result.port,
        kind=result.kind,
        ts=result.ts,
        recipe=result.recipe,
        status_code=result.status_code,
        error=result.error,
        addresses=tuple(addresses),
        timing=result.timing,
        transport=result.transport,
    )

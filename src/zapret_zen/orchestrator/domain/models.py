"""Typed DTOs shared by the orchestrator core, IPC and the UI.

Everything here is a plain dataclass/enum with no OS or Qt dependencies.  The
same shapes are serialized over the named-pipe IPC and (later) over the Rust
boundary, so keep field names stable and JSON-friendly.
"""

from __future__ import annotations

import dataclasses
import enum
import time
from dataclasses import dataclass, field
from typing import Any


class Protocol(str, enum.Enum):
    TCP = "tcp"
    UDP = "udp"


class Verdict(str, enum.Enum):
    UNKNOWN = "unknown"
    OK = "ok"
    SUSPECT = "suspect"
    BLOCKED = "blocked"
    ROUTED = "routed"
    UNMATCHABLE = "unmatchable"


class BlockKind(str, enum.Enum):
    """Failure taxonomy (mirrors NetZapret's ``BlockKind`` where it fits)."""

    OK = "ok"
    DNS = "dns"
    NO_ADDRESS = "no_address"
    FULL = "full"
    HTTPS_PORT = "https_port"
    TLS_DPI = "tls_dpi"
    HANDSHAKE = "handshake"
    STALL = "stall"
    GEO_BLOCK = "geo_block"
    FORBIDDEN = "forbidden"
    TUNNEL_FAILED = "tunnel_failed"
    TIMEOUT = "timeout"
    ERROR = "error"

    @property
    def is_failure(self) -> bool:
        return self is not BlockKind.OK


class OrchestratorMode(str, enum.Enum):
    OFF = "off"
    OBSERVE = "observe"
    SUGGEST = "suggest"
    AUTO = "auto"


class ActionKind(str, enum.Enum):
    NONE = "none"
    PROBE = "probe"
    APPLY_DESYNC = "apply_desync"
    EXCLUDE = "exclude"
    ROUTE_PROXY = "route_proxy"
    ROLLBACK = "rollback"


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    ts: float
    pid: int
    process: str
    protocol: str
    local_address: str
    local_port: int
    remote_address: str
    remote_port: int
    hostname: str | None = None
    source: str = "tcp-table"
    verdict: str = Verdict.UNKNOWN.value

    @property
    def key(self) -> tuple[int, str, str, int, str, int]:
        return (
            self.pid,
            self.protocol,
            self.local_address,
            self.local_port,
            self.remote_address,
            self.remote_port,
        )

    @property
    def endpoint(self) -> str:
        return f"{self.remote_address}:{self.remote_port}"


@dataclass(frozen=True, slots=True)
class ProbeTiming:
    tcp_connect_ms: int | None = None
    tls_handshake_ms: int | None = None
    ttfb_ms: int | None = None
    total_ms: int = 0
    bytes_read: int = 0
    stalled_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    host: str
    port: int
    kind: str
    ts: float
    recipe: str | None = None
    status_code: int | None = None
    error: str = ""
    addresses: tuple[str, ...] = ()
    timing: ProbeTiming = field(default_factory=ProbeTiming)
    transport: str = "direct"

    @property
    def ok(self) -> bool:
        return self.kind == BlockKind.OK.value

    @property
    def is_failure(self) -> bool:
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True, slots=True)
class Recipe:
    id: str
    title: str
    args: tuple[str, ...]
    description: str = ""
    source: str = "catalog"
    # ``tcp`` recipes desync TLS on 80/443; ``udp`` recipes desync QUIC on
    # UDP/443.  The two halves of a rule are learned and applied independently.
    transport: str = "tcp"


@dataclass(slots=True)
class DomainRule:
    host: str
    recipe: str
    mode: str = "desync"  # desync | direct | proxy
    confidence: float = 0.5
    source: str = "probe"
    network: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    last_ok: float | None = None
    fails: int = 0
    pinned: bool = False
    enabled: bool = True
    # ``quic_recipe`` adds a second, UDP/443 profile for the same host.  Browsers
    # prefer QUIC and silently fall back to TCP, so a domain can look healthy in
    # a TLS probe while video/voice still stutters; QUIC needs its own desync.
    quic_recipe: str = ""


@dataclass(frozen=True, slots=True)
class Decision:
    ts: float
    host: str
    action: str
    reason: str
    recipe: str | None = None
    confidence: float = 0.0
    applied: bool = False


@dataclass(slots=True)
class HostStats:
    host: str
    process: str = ""
    connections: int = 0
    failures: int = 0
    last_seen: float = 0.0
    verdict: str = Verdict.UNKNOWN.value
    last_kind: str = ""
    recipe: str = ""


def jsonable(value: Any) -> Any:
    """Recursively convert DTOs/enums into JSON-safe primitives."""
    if dataclasses.is_dataclass(value):
        return {item.name: jsonable(getattr(value, item.name)) for item in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)

"""Length-prefixed JSON framing for the orchestrator IPC.

Frame layout::

    [u32 little-endian payload length][u8 protocol version][JSON UTF-8]

Length-prefixing (instead of JSON-lines) keeps large snapshots and batched
connection events fast and immune to embedded newlines.
"""

from __future__ import annotations

import json
import struct
from typing import Any

PROTOCOL_VERSION = 1
HEADER = struct.Struct("<IB")
MAX_FRAME_BYTES = 16 * 1024 * 1024


class FrameError(ValueError):
    pass


def encode(payload: Any, *, version: int = PROTOCOL_VERSION) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise FrameError(f"frame too large: {len(body)} bytes")
    return HEADER.pack(len(body), version) + body


def header_size() -> int:
    return HEADER.size


def decode_header(header: bytes) -> tuple[int, int]:
    if len(header) != HEADER.size:
        raise FrameError("short frame header")
    length, version = HEADER.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise FrameError(f"frame too large: {length} bytes")
    if version != PROTOCOL_VERSION:
        raise FrameError(f"unsupported protocol version: {version}")
    return length, version


def decode(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FrameError(f"invalid JSON frame: {error}") from error

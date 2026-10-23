"""Helpers for capturing output of Windows console utilities.

Python 3.14+ enables UTF-8 mode by default (PEP 686), so ``subprocess``
with ``text=True`` decodes child-process output as UTF-8.  Legacy
Windows tools (tasklist, taskkill, sc, Windows PowerShell) still emit
the OEM codepage (cp866 on Russian systems).  Decoding their output as
UTF-8 raises ``UnicodeDecodeError`` inside the pipe-reader thread,
leaves ``stdout=None`` and silently breaks health checks.

Capture raw bytes instead and decode them explicitly with the OEM
codepage (UTF-8 and cp1251 as fallbacks).
"""

from __future__ import annotations

import sys


def windows_console_encoding() -> str:
    """Best-guess encoding for output of native Windows console tools."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            return "cp" + str(ctypes.windll.kernel32.GetOEMCP())
        except Exception:
            return "mbcs"
    return "utf-8"


def decode_process_output(data: bytes | str | None, encoding: str | None = None) -> str:
    """Decode captured subprocess output without ever raising.

    ``text=True`` output (already decoded by subprocess) is passed
    through unchanged.  Bytes are decoded with the OEM codepage first,
    then UTF-8 and cp1251; as a last resort, undecodable bytes are
    replaced so ASCII content (process names, exit codes) stays usable.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    candidates = [encoding or windows_console_encoding(), "utf-8", "cp1251"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return data.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")

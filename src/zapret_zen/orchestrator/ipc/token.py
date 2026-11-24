"""Shared IPC secret.

The named-pipe DACL already restricts the endpoint to the current user, so the
token is defence in depth: it keeps two copies of the app (a portable build and
an installed one) from accidentally driving each other's sidecar, and it makes
stale client handles fail loudly instead of silently issuing commands.

The token lives next to the orchestrator database and is regenerated whenever
the file is missing or too short.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

TOKEN_BYTES = 24


def ensure_token(path: Path) -> str:
    """Return the token stored at ``path``, creating it when needed."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if len(text) >= 16:
        return text
    token = secrets.token_hex(TOKEN_BYTES)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        return token
    return token


def read_token(path: Path) -> str:
    """Read a token without creating one (used by short-lived clients)."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""

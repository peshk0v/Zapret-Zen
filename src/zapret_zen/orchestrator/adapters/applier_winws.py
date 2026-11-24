"""Writes per-domain desync overrides for the main winws process.

The orchestrator never edits user presets.  It materializes a small, generated
overlay (``overrides.json`` + hostlists) which ``ProcessManager`` appends to the
winws command line on the next start.  That keeps user configuration intact and
makes rollback a matter of deleting one directory.

Each rule can produce **two** profiles: a TCP one on 80/443 (TLS ClientHello
desync) and a UDP one on 443 (QUIC Initial desync).  They are separate winws
filters, and a domain can be healthy over TCP while QUIC is throttled, so the
two halves are written independently.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

from zapret_zen.orchestrator.domain.models import DomainRule

# ``{bin}`` is resolved by ``ProcessManager`` when it builds the real command:
# only that layer knows where ``winws.exe`` (and its ``.bin`` patterns) lives.
DEFAULT_QUIC_RECIPE = "quic-fake6"


class WinwsOverrideApplier:
    def __init__(
        self,
        data_dir: Path,
        *,
        enabled: bool = True,
        recipe_args: dict[str, tuple[str, ...]] | None = None,
        probe_lane: tuple[int, int] = (40000, 40100),
        quic_enabled: bool = True,
        quic_recipe: str = DEFAULT_QUIC_RECIPE,
    ) -> None:
        self.root = Path(data_dir) / "orchestrator"
        self.hostlists = self.root / "hostlists"
        self.overrides_path = self.root / "overrides.json"
        self.enabled = bool(enabled)
        self.quic_enabled = bool(quic_enabled)
        self.quic_recipe = str(quic_recipe or DEFAULT_QUIC_RECIPE)
        self._recipe_args = dict(recipe_args or {})
        self._probe_lane = (int(probe_lane[0]), int(probe_lane[1]))

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    # -- port contract -----------------------------------------------------

    def apply(self, rules: Sequence[DomainRule]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.hostlists.mkdir(parents=True, exist_ok=True)
        now = time.time()
        sections: list[dict[str, Any]] = []
        ordered = sorted(
            (
                rule
                for rule in rules
                if rule.enabled and rule.mode == "desync" and not _expired(rule, now)
            ),
            key=lambda rule: (not rule.pinned, -float(rule.confidence)),
        )
        quic_sections = 0
        for rule in ordered:
            host = (rule.host or "").strip().lower().rstrip(".")
            if not host:
                continue
            hostlist = self.hostlists / f"{_safe_name(host)}.txt"
            hostlist.write_text(host + "\n", encoding="utf-8")
            sections.append(
                {
                    "host": host,
                    "transport": "tcp",
                    "mode": "desync",
                    "recipe": rule.recipe,
                    "hostlist": str(hostlist),
                    "args": list(self._recipe_args.get(rule.recipe, ())),
                    "pinned": bool(rule.pinned),
                }
            )
            quic_recipe = self._quic_recipe_for(rule)
            if not quic_recipe:
                continue
            quic_hostlist = self.hostlists / f"{_safe_name(host)}-quic.txt"
            quic_hostlist.write_text(host + "\n", encoding="utf-8")
            sections.append(
                {
                    "host": host,
                    "transport": "udp",
                    "mode": "desync",
                    "recipe": quic_recipe,
                    "hostlist": str(quic_hostlist),
                    "args": list(self._recipe_args.get(quic_recipe, ())),
                    "pinned": bool(rule.pinned),
                }
            )
            quic_sections += 1
        payload = {
            "version": 1,
            "enabled": self.enabled,
            "updated_at": time.time(),
            "pending_restart": True,
            "probe_lane": [self._probe_lane[0], self._probe_lane[1]],
            "sections": sections,
        }
        _atomic_write_json(self.overrides_path, payload)
        return {
            "ok": True,
            "count": len(sections),
            "tcp": len(sections) - quic_sections,
            "quic": quic_sections,
            "fingerprint": self.fingerprint(),
            "pending_restart": True,
            "path": str(self.overrides_path),
        }

    def _quic_recipe_for(self, rule: DomainRule) -> str:
        """QUIC profile for a rule, or ``""`` when it must not be written.

        The *policy* is the single source of truth here: switching
        ``quic_recipe`` at runtime must move every learned rule with it, so a
        per-rule copy (``rule.quic_recipe``) is recorded for the journal and the
        UI but never overrides the policy.  ``direct`` rules are excluded on
        purpose: "this host needs no bypass" must not become "send fake QUIC
        packets to this host".
        """
        if not self.quic_enabled:
            return ""
        if rule.recipe == "direct" or rule.mode != "desync":
            return ""
        return self.quic_recipe if self._recipe_args.get(self.quic_recipe) else ""

    def clear(self) -> dict[str, Any]:
        payload = {
            "version": 1,
            "enabled": self.enabled,
            "updated_at": time.time(),
            "pending_restart": True,
            "probe_lane": [self._probe_lane[0], self._probe_lane[1]],
            "sections": [],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.overrides_path, payload)
        return {"ok": True, "count": 0, "fingerprint": self.fingerprint(), "pending_restart": True}

    def fingerprint(self) -> str:
        try:
            data = self.overrides_path.read_bytes()
        except OSError:
            return ""
        return hashlib.sha1(data).hexdigest()

    def read(self) -> dict[str, Any]:
        try:
            with self.overrides_path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def acknowledge_restart(self) -> dict[str, Any]:
        payload = self.read()
        if not payload:
            return {"ok": True}
        payload["pending_restart"] = False
        _atomic_write_json(self.overrides_path, payload)
        return {"ok": True}


def read_overrides(overrides_path: Path) -> dict[str, Any]:
    """Read the overlay from a plain path (used by ProcessManager)."""
    try:
        with Path(overrides_path).open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _safe_name(host: str) -> str:
    return hashlib.sha1(host.encode("utf-8")).hexdigest()[:16]


def _expired(rule: DomainRule, now: float) -> bool:
    """Learned rules carry a TTL; pinned rules carry none and never expire."""

    expires = getattr(rule, "expires_at", None)
    if expires is None:
        return False
    try:
        return float(expires) <= now
    except (TypeError, ValueError):
        return False


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="overrides-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise

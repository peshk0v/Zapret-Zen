"""Orchestrator policy: the user-tunable knobs, stored as TOML.

TOML is used because ``tomllib`` ships with Python 3.11, it is human-editable
and diff-friendly, and the file can later be consumed by a Rust sidecar
without pulling a YAML dependency into the app.
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Policy:
    # Observation
    mode: str = "auto"  # off | observe | suggest | auto
    observe_interval_s: float = 1.0
    max_live_events: int = 500
    retention_days: int = 7
    process_allowlist: list[str] = field(default_factory=list)
    ignore_hosts: list[str] = field(default_factory=list)
    seed_hosts: list[str] = field(default_factory=list)

    # Probing
    probe_timeout_s: float = 4.0
    probe_stall_s: float = 3.0
    probe_min_bytes: int = 262144
    probe_confirmations: int = 2
    probe_concurrency: int = 4
    probe_cooldown_s: int = 60
    probe_lane_start: int = 40000
    probe_lane_end: int = 40100

    # Recipes / applying
    recipe_budget: int = 8
    apply_enabled: bool = True
    restart_debounce_s: float = 3.0
    network_scope: bool = True
    # QUIC (UDP/443) needs its own desync profile: browsers prefer it and fall
    # back to TCP silently, so a TCP-only overlay leaves video/voice broken.
    quic_enabled: bool = True
    quic_recipe: str = "quic-fake6"

    def lane(self) -> tuple[int, int]:
        start = max(1024, min(int(self.probe_lane_start), 65535))
        end = max(start, min(int(self.probe_lane_end), 65535))
        return start, end

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        known = {item.name for item in fields(cls)}
        payload = {key: value for key, value in (data or {}).items() if key in known}
        return cls(**payload)


def default_policy() -> Policy:
    return Policy()


def load_policy(path: Path) -> Policy:
    """Load policy, returning defaults when the file is absent or broken."""
    try:
        with Path(path).open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return default_policy()
    merged: dict[str, Any] = {}
    for section in ("observer", "probe", "apply", "recipes", "quic"):
        values = raw.get(section)
        if isinstance(values, dict):
            merged.update(values)
    if "mode" in raw:
        merged["mode"] = raw["mode"]
    policy = Policy.from_dict(merged)
    if policy.mode not in {"off", "observe", "suggest", "auto"}:
        policy.mode = "auto"
    policy.process_allowlist = _clean_list(policy.process_allowlist)
    policy.ignore_hosts = _clean_list(policy.ignore_hosts)
    policy.seed_hosts = _clean_list(policy.seed_hosts)
    policy.quic_recipe = str(policy.quic_recipe or "").strip() or "quic-fake6"
    return policy


def save_policy(path: Path, policy: Policy) -> None:
    """Write policy atomically as TOML (no third-party writer needed)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# Zapret-Zen Smart Orchestrator policy",
        "# Edit while the orchestrator is stopped, or use the UI.",
        "",
        f"mode = {_toml_value(policy.mode)}",
        "",
        "[observer]",
        f"observe_interval_s = {_toml_value(policy.observe_interval_s)}",
        f"max_live_events = {_toml_value(policy.max_live_events)}",
        f"retention_days = {_toml_value(policy.retention_days)}",
        f"process_allowlist = {_toml_value(policy.process_allowlist)}",
        f"ignore_hosts = {_toml_value(policy.ignore_hosts)}",
        f"seed_hosts = {_toml_value(policy.seed_hosts)}",
        "",
        "[probe]",
        f"probe_timeout_s = {_toml_value(policy.probe_timeout_s)}",
        f"probe_stall_s = {_toml_value(policy.probe_stall_s)}",
        f"probe_min_bytes = {_toml_value(policy.probe_min_bytes)}",
        f"probe_confirmations = {_toml_value(policy.probe_confirmations)}",
        f"probe_concurrency = {_toml_value(policy.probe_concurrency)}",
        f"probe_cooldown_s = {_toml_value(policy.probe_cooldown_s)}",
        f"probe_lane_start = {_toml_value(policy.probe_lane_start)}",
        f"probe_lane_end = {_toml_value(policy.probe_lane_end)}",
        "",
        "[recipes]",
        f"recipe_budget = {_toml_value(policy.recipe_budget)}",
        "",
        "[apply]",
        f"apply_enabled = {_toml_value(policy.apply_enabled)}",
        f"restart_debounce_s = {_toml_value(policy.restart_debounce_s)}",
        f"network_scope = {_toml_value(policy.network_scope)}",
        "",
        "[quic]",
        f"quic_enabled = {_toml_value(policy.quic_enabled)}",
        f"quic_recipe = {_toml_value(policy.quic_recipe)}",
        "",
    ]
    text = "\n".join(lines)
    fd, temp_name = tempfile.mkstemp(prefix="policy-", suffix=".toml", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _clean_list(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in values:
        text = str(item or "").strip().lower()
        if text and text not in result:
            result.append(text)
    return result


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)

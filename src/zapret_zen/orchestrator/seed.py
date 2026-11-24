"""Seed hosts derived from the service catalogue.

The orchestrator needs something to probe during the first seconds of a
session, before the user has opened anything interesting.  Instead of a
hard-coded list it reuses the service catalogue's test targets, so the seeds
follow whatever the user actually enabled.
"""

from __future__ import annotations

from typing import Iterable, Sequence

_DEFAULT_LIMIT = 16
_SKIP_SCHEMES = ("ping:", "tcp:", "udp:", "icmp:")


def seed_hosts(service_ids: Sequence[str] | None = None, *, limit: int = _DEFAULT_LIMIT) -> list[str]:
    try:
        from zapret_zen.services.service_rules import SERVICE_RULES
    except Exception:
        return []
    if service_ids:
        candidates: Iterable[str] = [str(item) for item in service_ids]
    else:
        candidates = list(SERVICE_RULES.keys())
    hosts: list[str] = []
    for service_id in candidates:
        rule = SERVICE_RULES.get(service_id)
        if rule is None:
            continue
        for _label, target in getattr(rule, "test_targets", ()) or ():
            host = host_from_target(str(target))
            if host and host not in hosts:
                hosts.append(host)
            if len(hosts) >= max(1, int(limit)):
                return hosts
    return hosts


def host_from_target(target: str) -> str:
    value = str(target or "").strip().lower()
    if not value:
        return ""
    for prefix in _SKIP_SCHEMES:
        if value.startswith(prefix):
            # Numeric targets (PING:1.1.1.1) are not probeable over TLS.
            return ""
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    if value.startswith("["):
        # IPv6 literal: keep it without the brackets.
        return value[1:].split("]", 1)[0]
    value = value.split(":", 1)[0]
    value = value.strip(".")
    if not value or value.replace(".", "").isdigit():
        return ""
    return value

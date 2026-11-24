"""Ports (interfaces) of the orchestrator.

Adapters live in ``zapret_zen.orchestrator.adapters``.  The application layer
depends only on these protocols, which is what makes the future PyO3/IPC
swap a matter of writing a new adapter rather than touching the UI.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from zapret_zen.orchestrator.domain.models import (
    ConnectionEvent,
    Decision,
    DomainRule,
    ProbeResult,
    Recipe,
)


class ObserverUnavailable(RuntimeError):
    """Raised when a traffic observer cannot be started on this machine."""


@runtime_checkable
class TrafficObserver(Protocol):
    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def poll(self, max_events: int = 512) -> list[ConnectionEvent]: ...

    def snapshot(self) -> list[ConnectionEvent]: ...

    @property
    def available(self) -> bool: ...

    @property
    def dropped(self) -> int: ...

    @property
    def detail(self) -> str: ...


@runtime_checkable
class Prober(Protocol):
    def probe(
        self,
        host: str,
        *,
        port: int = 443,
        path: str = "/",
        recipe: str | None = None,
        transport: str = "direct",
        source_port: int | None = None,
    ) -> ProbeResult: ...

    def probe_confirmed(self, host: str, *, attempts: int = 2, **kwargs: Any) -> ProbeResult: ...


@runtime_checkable
class Journal(Protocol):
    def record_probe(self, result: ProbeResult) -> None: ...

    def record_decision(self, decision: Decision) -> None: ...

    def recent_probes(self, host: str, limit: int = 20) -> list[ProbeResult]: ...

    def recent_decisions(self, limit: int = 50) -> list[Decision]: ...

    def upsert_rule(self, rule: DomainRule) -> None: ...

    def get_rule(self, host: str, network: str) -> DomainRule | None: ...

    def list_rules(self, network: str = "") -> list[DomainRule]: ...

    def forget_rule(self, host: str, network: str = "") -> int: ...

    def prune(self, retention_days: int) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class StrategyApplier(Protocol):
    def apply(self, rules: Sequence[DomainRule]) -> dict[str, Any]: ...

    def clear(self) -> dict[str, Any]: ...

    def fingerprint(self) -> str: ...


@runtime_checkable
class Router(Protocol):
    def route(self, host: str, mode: str) -> dict[str, Any]: ...

    def health(self) -> dict[str, Any]: ...


class RecipeProvider(Protocol):
    def list_recipes(self) -> list[Recipe]: ...

    def get(self, recipe_id: str) -> Recipe | None: ...

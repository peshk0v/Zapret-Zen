"""Wires adapters into the application service.

Kept in one place so the sidecar, the CLI and unit tests build the same
object graph and only swap individual adapters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from zapret_zen.orchestrator.adapters.applier_winws import WinwsOverrideApplier
from zapret_zen.orchestrator.adapters.dns_cache import WindowsDnsCache
from zapret_zen.orchestrator.adapters.journal_sqlite import SqliteJournal
from zapret_zen.orchestrator.adapters.observer_tcp_table import TcpTableObserver
from zapret_zen.orchestrator.adapters.prober import SocketProber
from zapret_zen.orchestrator.adapters.recipes import RecipeCatalog
from zapret_zen.orchestrator.adapters.router_noop import NoopRouter
from zapret_zen.orchestrator.adapters.trial_winws import WinwsTrialRunner
from zapret_zen.orchestrator.app.service import OrchestratorService
from zapret_zen.orchestrator.domain.policy import load_policy


def build_service(
    *,
    install_root: Path,
    data_dir: Path,
    seed_hosts: Sequence[str] = (),
    policy_path: Path | None = None,
) -> OrchestratorService:
    install_root = Path(install_root)
    data_root = Path(data_dir) / "orchestrator"
    data_root.mkdir(parents=True, exist_ok=True)
    policy_file = Path(policy_path) if policy_path else data_root / "policy.toml"
    policy = load_policy(policy_file)
    lane = policy.lane()
    catalog = RecipeCatalog()
    journal = SqliteJournal(data_root / "state.db")
    observer = TcpTableObserver(
        probe_lane=lane,
        process_allowlist=list(policy.process_allowlist),
        dns_cache=WindowsDnsCache(),
    )
    prober = SocketProber(
        timeout_s=policy.probe_timeout_s,
        stall_s=policy.probe_stall_s,
        min_bytes=policy.probe_min_bytes,
        lane=lane,
    )
    applier = WinwsOverrideApplier(
        Path(data_dir),
        enabled=policy.apply_enabled,
        recipe_args={recipe.id: recipe.args for recipe in catalog.list_recipes()},
        probe_lane=lane,
        quic_enabled=policy.quic_enabled,
        quic_recipe=policy.quic_recipe,
    )
    trial = WinwsTrialRunner(install_root, Path(data_dir), lane=lane)
    return OrchestratorService(
        install_root=install_root,
        data_dir=Path(data_dir),
        policy=policy,
        policy_path=policy_file,
        observer=observer,
        prober=prober,
        journal=journal,
        applier=applier,
        catalog=catalog,
        trial=trial,
        router=NoopRouter(),
        seed_hosts=seed_hosts,
    )

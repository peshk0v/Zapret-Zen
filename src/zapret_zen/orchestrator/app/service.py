"""Application layer: observation -> detection -> probing -> decision -> apply.

This module is deliberately free of Qt and of ``ProcessManager``.  It only
talks to ports, so it can run in a sidecar process today and inside a Rust
runtime later.  Engine restarts are requested indirectly: applying a rule
writes the override overlay with ``pending_restart = true`` and the backend
consumes that flag.
"""

from __future__ import annotations

import socket
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from zapret_zen.orchestrator.domain.models import (
    ActionKind,
    BlockKind,
    ConnectionEvent,
    Decision,
    DomainRule,
    HostStats,
    OrchestratorMode,
    ProbeResult,
    Recipe,
    Verdict,
    jsonable,
)
from zapret_zen.orchestrator.domain.policy import Policy, save_policy
from zapret_zen.orchestrator.ports import Prober, StrategyApplier, TrafficObserver

_RULE_TTL_SECONDS = 30 * 86400
_RULE_FAIL_LIMIT = 3
_STATS_TTL_SECONDS = 3600.0
_INDEX_TTL_SECONDS = 900.0


class OrchestratorService:
    def __init__(
        self,
        *,
        install_root: Path,
        data_dir: Path,
        policy: Policy,
        policy_path: Path,
        observer: TrafficObserver,
        prober: Prober,
        journal: Any,
        applier: StrategyApplier,
        catalog: Any,
        trial: Any,
        router: Any,
        seed_hosts: Sequence[str] = (),
    ) -> None:
        self.install_root = Path(install_root)
        self.data_dir = Path(data_dir)
        self.policy = policy
        self.policy_path = Path(policy_path)
        self.observer = observer
        self.prober = prober
        self.journal = journal
        self.applier = applier
        self.catalog = catalog
        self.trial = trial
        self.router = router
        self._seed_hosts = _clean_hosts(seed_hosts)
        self._network = network_key() if policy.network_scope else ""
        self._network_checked_at = time.time()
        self._live: deque[dict[str, Any]] = deque(maxlen=max(50, int(policy.max_live_events)))
        self._stats: dict[str, HostStats] = {}
        self._index: dict[str, tuple[str, float]] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(policy.probe_concurrency)), thread_name_prefix="orch-probe")
        self._futures: dict[Future[Any], str] = {}
        self._inflight: set[str] = set()
        self._last_probe: dict[str, float] = {}
        self._suspect_counts: dict[str, int] = {}
        self._started = False
        self._started_at = 0.0
        self._pending_restart = False
        self._last_error = ""
        self._probes_run = 0
        self._failures_seen = 0
        self._healed = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> dict[str, Any]:
        if self._started:
            return {"ok": True, "already": True}
        try:
            self.observer.start()
        except Exception as error:
            self._last_error = f"observer: {error}"
        self._started = True
        self._started_at = time.time()
        try:
            self.journal.prune(int(self.policy.retention_days))
        except Exception:
            pass
        return {"ok": True, "observer": self.observer.name, "network": self._network}

    def stop(self) -> dict[str, Any]:
        self._started = False
        try:
            self.observer.stop()
        except Exception:
            pass
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            self.journal.close()
        except Exception:
            pass
        return {"ok": True}

    @property
    def started(self) -> bool:
        return self._started

    # -- periodic work -----------------------------------------------------

    def tick(self) -> dict[str, Any]:
        if not self._started:
            return {"ok": False, "error": "orchestrator is not running"}
        now = time.time()
        self._refresh_network(now)
        events: list[ConnectionEvent] = []
        try:
            events = list(self.observer.poll(512))
        except Exception as error:
            self._last_error = f"poll: {error}"
        resolved: list[ConnectionEvent] = []
        if events:
            for event in events:
                host = event.hostname or self._host_for_ip(event.remote_address)
                if host:
                    event = replace(event, hostname=host)
                resolved.append(event)
            self._ingest(resolved, now)
        self._schedule_probes(now)
        self._drain_futures()
        self._prune(now)
        return {"ok": True, "events": len(resolved), "inflight": len(self._inflight)}

    def _refresh_network(self, now: float) -> None:
        """Re-key rules when the user switches network (Wi-Fi, VPN, hotspot).

        Rules are bound to the network they were learned on, so a stale key
        would silently hide every previously learned rule and keep writing new
        ones into the old bucket.  A cheap /24 probe every half minute is
        enough; the actual UDP socket is created and closed immediately.
        """
        if not self.policy.network_scope or now - self._network_checked_at < 30.0:
            return
        self._network_checked_at = now
        current = network_key()
        if current == self._network:
            return
        previous, self._network = self._network, current
        self._last_error = f"network changed: {previous or 'unknown'} -> {current or 'unknown'}"
        try:
            self.apply_rules()
        except Exception:
            pass

    def _ingest(self, events: Sequence[ConnectionEvent], now: float) -> None:
        with self._lock:
            for event in events:
                self._live.append(jsonable(event))
                host = event.hostname or event.remote_address
                stats = self._stats.get(host)
                if stats is None:
                    stats = HostStats(host=host)
                    self._stats[host] = stats
                stats.connections += 1
                stats.last_seen = now
                if event.protocol.lower() == "tcp":
                    # TCP-table is degraded passive evidence. A repeated
                    # connection failure pattern can raise suspicion, but a
                    # separate probe still confirms the verdict.
                    self._suspect_counts[host] = self._suspect_counts.get(host, 0) + 1
                    if self._suspect_counts[host] >= 2 and stats.verdict == Verdict.UNKNOWN.value:
                        stats.verdict = Verdict.SUSPECT.value
                if event.process and event.process.lower() not in {"", "system"}:
                    stats.process = event.process
                rule = self.journal.get_rule(host, self._network) if _looks_like_host(host) and stats.connections <= 1 else None
                if rule is not None:
                    stats.recipe = rule.recipe

    def _schedule_probes(self, now: float) -> None:
        if self.policy.mode == OrchestratorMode.OFF.value:
            return
        with self._lock:
            since = time.monotonic() - float(self.policy.probe_cooldown_s)
            candidates: list[str] = []
            for host, stats in self._stats.items():
                if not _looks_like_host(host) or host in self.policy.ignore_hosts:
                    continue
                if now - stats.last_seen > 300:
                    continue
                if self._last_probe.get(host, 0.0) > since:
                    continue
                candidates.append(host)
            for host in self._seed_hosts:
                if host in self.policy.ignore_hosts:
                    continue
                if self._last_probe.get(host, 0.0) > time.monotonic() - 300:
                    continue
                candidates.append(host)
            capacity = max(1, int(self.policy.probe_concurrency)) - len(self._inflight)
            for host in candidates:
                if capacity <= 0:
                    break
                if host in self._inflight:
                    continue
                stats = self._stats.get(host)
                is_seed = host in self._seed_hosts and (stats is None or stats.connections == 0)
                if not is_seed and self._suspect_counts.get(host, 0) < 2:
                    continue
                self._submit(host)
                capacity -= 1

    def _submit(self, host: str) -> None:
        self._inflight.add(host)
        self._last_probe[host] = time.monotonic()
        try:
            future = self._executor.submit(self._evaluate, host)
        except Exception as error:
            self._inflight.discard(host)
            self._last_error = f"submit: {error}"
            return
        self._futures[future] = host

    def _drain_futures(self) -> None:
        done: list[Future[Any]] = []
        with self._lock:
            for future in list(self._futures):
                if future.done():
                    done.append(future)
            for future in done:
                host = self._futures.pop(future, "")
                self._inflight.discard(host)
        for future in done:
            try:
                future.result()
            except Exception as error:
                self._last_error = f"probe: {error}"

    # -- evaluation / healing ---------------------------------------------

    def _evaluate(self, host: str) -> dict[str, Any]:
        baseline = self.prober.probe_confirmed(
            host,
            attempts=max(1, int(self.policy.probe_confirmations)),
            lane=False,
        )
        self._record_probe(baseline)
        self._probes_run += 1
        if baseline.ok:
            self._mark_ok(host)
            return {"host": host, "ok": True, "baseline": jsonable(baseline)}
        self._failures_seen += 1
        self._mark_blocked(host, baseline)
        return self._heal(host, baseline)

    def _heal(self, host: str, baseline: ProbeResult, *, manual: bool = False) -> dict[str, Any]:
        trials: list[ProbeResult] = []
        winner: ProbeResult | None = None
        winner_recipe: Recipe | None = None
        if not manual and self.policy.mode == OrchestratorMode.OBSERVE.value:
            # Observation must not touch anything: record the verdict, but do
            # not spin up a trial engine or invent a rule.  "Suggest" is the
            # first mode allowed to prepare a rule (without applying it).
            decision = Decision(
                ts=time.time(),
                host=host,
                action=ActionKind.NONE.value,
                reason=f"probe failed ({baseline.kind}); observe mode only",
                confidence=0.2,
                applied=False,
            )
            self.journal.record_decision(decision)
            return {
                "host": host,
                "ok": True,
                "healed": False,
                "baseline": jsonable(baseline),
                "trials": [],
                "decision": jsonable(decision),
            }
        if self.trial is not None and getattr(self.trial, "available", False):
            budget = max(0, int(self.policy.recipe_budget))
            for recipe in self.catalog.trial_order("tcp")[:budget]:
                result = self.trial.run(host, recipe, self.prober, port=baseline.port)
                if result is None:
                    continue
                self._record_probe(result)
                trials.append(result)
                if result.ok:
                    winner = result
                    winner_recipe = recipe
                    break

        if winner is not None and winner_recipe is not None:
            mode = "direct" if winner_recipe.id == "direct" else "desync"
            rule = DomainRule(
                host=host,
                recipe=winner_recipe.id,
                mode=mode,
                confidence=0.65 if mode == "desync" else 0.55,
                source="probe",
                network=self._network,
                expires_at=time.time() + _RULE_TTL_SECONDS,
                last_ok=time.time(),
                quic_recipe=self._quic_recipe_for(mode),
            )
            self.journal.upsert_rule(rule)
            applied = self._auto_apply()
            decision = Decision(
                ts=time.time(),
                host=host,
                action=ActionKind.APPLY_DESYNC.value,
                reason=f"probe failed ({baseline.kind}); recipe {winner_recipe.id} works",
                recipe=winner_recipe.id,
                confidence=rule.confidence,
                applied=applied,
            )
            self.journal.record_decision(decision)
            self._healed += 1
            return {
                "host": host,
                "ok": True,
                "healed": True,
                "baseline": jsonable(baseline),
                "trials": [jsonable(item) for item in trials],
                "rule": jsonable(rule),
                "decision": jsonable(decision),
            }

        decision = Decision(
            ts=time.time(),
            host=host,
            action=ActionKind.ROUTE_PROXY.value,
            reason=f"probe failed ({baseline.kind}); no desync recipe helped",
            confidence=0.3,
            applied=False,
        )
        self.journal.record_decision(decision)
        return {
            "host": host,
            "ok": True,
            "healed": False,
            "baseline": jsonable(baseline),
            "trials": [jsonable(item) for item in trials],
            "decision": jsonable(decision),
        }

    def probe_host(self, host: str, *, trials: bool = True) -> dict[str, Any]:
        host = _normalize_host(host)
        if not host:
            return {"ok": False, "error": "empty host"}
        baseline = self.prober.probe_confirmed(
            host,
            attempts=max(1, int(self.policy.probe_confirmations)),
            lane=False,
        )
        self._record_probe(baseline)
        if baseline.ok:
            self._mark_ok(host)
            return {"ok": True, "host": host, "baseline": jsonable(baseline), "trials": [], "decision": None}
        self._mark_blocked(host, baseline)
        if trials:
            return self._heal(host, baseline, manual=True)
        return {"ok": False, "host": host, "baseline": jsonable(baseline), "trials": [], "decision": None}

    def _record_probe(self, result: ProbeResult) -> None:
        try:
            self.journal.record_probe(result)
        except Exception:
            pass
        if result.addresses:
            self._index_host(result.host, result.addresses)

    def _index_host(self, host: str, addresses: Sequence[str]) -> None:
        now = time.time()
        with self._lock:
            for address in addresses:
                if address:
                    self._index[address] = (host, now)

    def _host_for_ip(self, address: str) -> str | None:
        with self._lock:
            entry = self._index.get(address)
            if entry is None:
                return None
            host, ts = entry
            if time.time() - ts > _INDEX_TTL_SECONDS:
                return None
            return host

    def _mark_ok(self, host: str) -> None:
        with self._lock:
            self._suspect_counts.pop(host, None)
            stats = self._stats.get(host) or HostStats(host=host)
            stats.verdict = Verdict.OK.value
            stats.last_kind = BlockKind.OK.value
            stats.last_seen = time.time()
            self._stats[host] = stats
        rule = self.journal.get_rule(host, self._network) if _looks_like_host(host) else None
        if rule is not None:
            rule.last_ok = time.time()
            rule.fails = 0
            try:
                self.journal.upsert_rule(rule)
            except Exception:
                pass

    def _mark_blocked(self, host: str, result: ProbeResult) -> None:
        with self._lock:
            stats = self._stats.get(host) or HostStats(host=host)
            stats.verdict = Verdict.BLOCKED.value
            stats.last_kind = result.kind
            stats.failures += 1
            stats.last_seen = time.time()
            self._stats[host] = stats
        self._penalize_rule(host)

    def _penalize_rule(self, host: str) -> None:
        """Auto-rollback for a rule that no longer proves itself.

        A healthy rule makes the baseline probe succeed, which resets the
        counter.  If the baseline keeps failing while the rule is in place, the
        rule is useless: disable it and let the next healing round try a fresh
        recipe instead of pinning the domain to a dead profile.
        """
        if not _looks_like_host(host):
            return
        # Only an applied rule can be blamed for a failing baseline.  In
        # observe/suggest mode, or while the overlay waits for a winws restart,
        # the probe measured the old (or no) configuration.
        if self.policy.mode != OrchestratorMode.AUTO.value or not self.policy.apply_enabled:
            return
        if self._pending_restart:
            return
        try:
            rule = self.journal.get_rule(host, self._network)
        except Exception:
            return
        if rule is None or not rule.enabled or rule.pinned or rule.mode != "desync":
            return
        rule.fails = int(rule.fails or 0) + 1
        if rule.fails < _RULE_FAIL_LIMIT:
            try:
                self.journal.upsert_rule(rule)
            except Exception:
                pass
            return
        rule.enabled = False
        try:
            self.journal.upsert_rule(rule)
        except Exception:
            return
        decision = Decision(
            ts=time.time(),
            host=host,
            action=ActionKind.ROLLBACK.value,
            reason=f"rule {rule.recipe} failed {rule.fails} baselines; disabled",
            recipe=rule.recipe,
            confidence=0.2,
            applied=False,
        )
        try:
            self.journal.record_decision(decision)
        except Exception:
            pass
        self.apply_rules()

    def _quic_recipe_for(self, mode: str) -> str:
        """QUIC profile for a freshly learned rule.

        The TCP probe says nothing about the UDP path, so QUIC is attached by
        policy rather than by measurement: every desynced host also gets the
        configured QUIC profile.  ``direct`` hosts get nothing, because "no
        bypass needed" must not be turned into "send fake QUIC packets".
        """
        if not getattr(self.policy, "quic_enabled", False):
            return ""
        if mode != "desync":
            return ""
        recipe = str(getattr(self.policy, "quic_recipe", "") or "").strip()
        return recipe if self.catalog.get(recipe) is not None else ""

    def _auto_apply(self) -> bool:
        if self.policy.mode != OrchestratorMode.AUTO.value or not self.policy.apply_enabled:
            return False
        if not getattr(self.observer, "available", False):
            # Without a working connection observer there is no attribution, and
            # guessing what to desync is worse than doing nothing.
            return False
        result = self.apply_rules()
        return bool(result.get("ok"))

    def apply_rules(self) -> dict[str, Any]:
        try:
            rules = self.journal.list_rules(self._network)
            result = self.applier.apply(rules)
            if result.get("ok"):
                self._pending_restart = True
            return result
        except Exception as error:
            self._last_error = f"apply: {error}"
            return {"ok": False, "error": str(error)}

    def ack_restart(self) -> dict[str, Any]:
        self._pending_restart = False
        try:
            return self.applier.acknowledge_restart()
        except Exception as error:
            return {"ok": False, "error": str(error)}

    # -- management API ----------------------------------------------------

    def set_mode(self, mode: str) -> dict[str, Any]:
        value = str(mode or "").strip().lower()
        if value not in {item.value for item in OrchestratorMode}:
            return {"ok": False, "error": f"unknown mode: {mode}"}
        self.policy.mode = value
        self._save_policy()
        applied: dict[str, Any] | None = None
        if value == OrchestratorMode.AUTO.value and self.policy.apply_enabled:
            # Entering Auto should not wait for the next suspicious connection:
            # rules learned earlier (or in Suggest) become effective right away.
            applied = self.apply_rules()
        return {"ok": True, "mode": value, "applied": applied}

    def update_policy(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            return {"ok": False, "error": "policy patch must be an object"}
        allowed = {
            "mode",
            "observe_interval_s",
            "max_live_events",
            "retention_days",
            "probe_timeout_s",
            "probe_stall_s",
            "probe_min_bytes",
            "probe_confirmations",
            "probe_concurrency",
            "probe_cooldown_s",
            "recipe_budget",
            "apply_enabled",
            "restart_debounce_s",
            "network_scope",
            "quic_enabled",
            "quic_recipe",
            "seed_hosts",
            "ignore_hosts",
            "process_allowlist",
        }
        for key, value in patch.items():
            if key not in allowed:
                continue
            if key == "mode":
                value = str(value or "").strip().lower()
                if value not in {item.value for item in OrchestratorMode}:
                    continue
            setattr(self.policy, key, value)
        # The applier materializes the master switch and the QUIC defaults into
        # the overlay, so a runtime toggle must reach it; they are otherwise
        # fixed at construction.
        try:
            self.applier.set_enabled(bool(self.policy.apply_enabled))
        except AttributeError:
            pass
        try:
            self.applier.quic_enabled = bool(self.policy.quic_enabled)
            self.applier.quic_recipe = str(self.policy.quic_recipe or "")
        except AttributeError:
            pass
        self._save_policy()
        applied: dict[str, Any] | None = None
        # A toggle that changes the shape of the overlay (the master switch, the
        # QUIC half, the network scope) must rewrite it now: leaving the file
        # stale means the next winws start silently applies the old policy.
        if set(patch) & {"apply_enabled", "quic_enabled", "quic_recipe", "network_scope"}:
            if self.policy.mode == OrchestratorMode.AUTO.value or self.rules():
                self._resync_quic_rules()
                applied = self.apply_rules()
        return {"ok": True, "policy": jsonable(self.policy.to_dict()), "applied": applied}

    def _resync_quic_rules(self) -> None:
        """Keep the journal's ``quic_recipe`` in step with the policy.

        The applier always reads the policy, but the rule rows are what the UI
        and the CLI show, and a stale value there would advertise a QUIC profile
        that is not actually written.
        """
        try:
            rules = self.journal.list_rules(self._network)
        except Exception:
            return
        for rule in rules:
            expected = self._quic_recipe_for(rule.mode)
            if str(getattr(rule, "quic_recipe", "") or "") == expected:
                continue
            rule.quic_recipe = expected
            try:
                self.journal.upsert_rule(rule)
            except Exception:
                pass

    def _save_policy(self) -> None:
        try:
            save_policy(self.policy_path, self.policy)
        except Exception as error:
            self._last_error = f"policy: {error}"

    def forget(self, host: str) -> dict[str, Any]:
        removed = self.journal.forget_rule(_normalize_host(host), self._network)
        applied = self.apply_rules() if removed else {"ok": True}
        return {"ok": True, "removed": removed, "applied": applied}

    def pin(self, host: str, recipe: str, mode: str = "desync") -> dict[str, Any]:
        host = _normalize_host(host)
        item = self.catalog.get(recipe)
        if not host or (item is None and recipe != "direct"):
            return {"ok": False, "error": "unknown host or recipe"}
        rule = self.journal.get_rule(host, self._network) or DomainRule(host=host, recipe=recipe, network=self._network)
        rule.recipe = "direct" if recipe == "direct" else recipe
        rule.mode = "direct" if recipe == "direct" else (mode if mode in {"desync", "direct", "proxy"} else "desync")
        rule.pinned = True
        rule.enabled = True
        rule.confidence = 1.0
        rule.source = "manual"
        rule.expires_at = None
        rule.quic_recipe = self._quic_recipe_for(rule.mode)
        self.journal.upsert_rule(rule)
        applied = self.apply_rules()
        return {"ok": True, "rule": jsonable(rule), "applied": applied}

    def rules(self) -> list[dict[str, Any]]:
        return [jsonable(rule) for rule in self.journal.list_rules(self._network)]

    def connections(self, limit: int = 200, *, problematic_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            # Stored events are frozen at capture time, when nothing is known
            # yet; the verdict lives in the per-host stats.  Overlay it so the
            # feed (and its "problematic only" filter) shows the current state.
            items = [dict(item) for item in self._live]
            for item in items:
                host = str(item.get("hostname") or item.get("remote_address") or "")
                stats = self._stats.get(host)
                if stats is not None:
                    item["verdict"] = stats.verdict
        if problematic_only:
            items = [
                item
                for item in items
                if str(item.get("verdict")) in {Verdict.BLOCKED.value, Verdict.SUSPECT.value}
            ]
        return items[-max(1, min(int(limit), 2000)) :]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            stats = sorted(self._stats.values(), key=lambda item: item.last_seen, reverse=True)[:60]
            rules = self.journal.list_rules(self._network)
            decisions = self.journal.recent_decisions(20)
        return {
            "ok": True,
            "started": self._started,
            "mode": self.policy.mode,
            "network": self._network,
            "install_root": str(self.install_root),
            "observer": {
                "name": self.observer.name,
                "available": bool(self.observer.available),
                "detail": self.observer.detail,
                "dropped": int(self.observer.dropped),
                "live": len(self._live),
            },
            "trial": {
                "available": bool(getattr(self.trial, "available", False)),
                "executable": getattr(self.trial, "executable", ""),
            },
            "router": self.router.health() if self.router is not None else {"enabled": False},
            "applier": {
                "enabled": bool(self.policy.apply_enabled),
                "pending_restart": self._pending_restart,
                "fingerprint": self.applier.fingerprint(),
                "quic_enabled": bool(getattr(self.policy, "quic_enabled", False)),
                "quic_recipe": str(getattr(self.policy, "quic_recipe", "") or ""),
            },
            "policy": jsonable(self.policy.to_dict()),
            "counters": {
                "probes": self._probes_run,
                "failures": self._failures_seen,
                "healed": self._healed,
                "rules": len(rules),
                "hosts": len(self._stats),
            },
            "uptime_s": max(0, int(time.time() - self._started_at)) if self._started_at else 0,
            "error": self._last_error,
            "stats": [jsonable(item) for item in stats],
            "rules": [jsonable(item) for item in rules],
            "decisions": [jsonable(item) for item in decisions],
        }

    def _prune(self, now: float) -> None:
        mono = time.monotonic()
        with self._lock:
            stale = [host for host, stats in self._stats.items() if now - stats.last_seen > _STATS_TTL_SECONDS]
            for host in stale:
                self._stats.pop(host, None)
                self._suspect_counts.pop(host, None)
            # ``_last_probe`` is keyed by monotonic time, so it needs its own
            # clock and its own cutoff; it otherwise grows one entry per host
            # ever seen, for the whole life of the sidecar.
            stale_probes = [host for host, ts in self._last_probe.items() if mono - ts > _STATS_TTL_SECONDS]
            for host in stale_probes:
                self._last_probe.pop(host, None)
            if len(self._index) > 20000:
                cutoff = now - _INDEX_TTL_SECONDS
                self._index = {key: value for key, value in self._index.items() if value[1] >= cutoff}


def network_key() -> str:
    """Cheap network fingerprint: local /24 of the default route."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("1.1.1.1", 80))
            address = probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return ""
    if not address or address.startswith("127."):
        return ""
    parts = address.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3])
    return address


def _normalize_host(host: str) -> str:
    value = str(host or "").strip().lower().rstrip(".")
    if value.startswith("*."):
        value = value[2:]
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    if value.startswith("["):
        end = value.find("]")
        if end > 0:
            return value[1:end]
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value


def _clean_hosts(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        host = _normalize_host(value)
        if host and host not in result:
            result.append(host)
    return result


def _looks_like_host(value: str) -> bool:
    return bool(value) and not _is_ip(value)


def _is_ip(value: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, value)
            return True
        except OSError:
            continue
    return False

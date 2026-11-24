"""SQLite journal for probes, decisions and learned rules.

Raw connections are deliberately absent: they live only in the sidecar memory,
so the database holds aggregates-free, small, retention-friendly history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from zapret_zen.orchestrator.domain.models import (
    Decision,
    DomainRule,
    ProbeResult,
    ProbeTiming,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    kind TEXT NOT NULL,
    recipe TEXT,
    status_code INTEGER,
    error TEXT NOT NULL DEFAULT '',
    timing TEXT NOT NULL DEFAULT '{}',
    transport TEXT NOT NULL DEFAULT 'direct'
);
CREATE INDEX IF NOT EXISTS idx_probes_host ON probes(host, ts DESC);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    host TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    recipe TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    applied INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts DESC);

CREATE TABLE IF NOT EXISTS rules (
    host TEXT NOT NULL,
    network TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'desync',
    recipe TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    source TEXT NOT NULL DEFAULT 'probe',
    created_at REAL NOT NULL,
    expires_at REAL,
    last_ok REAL,
    fails INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    quic_recipe TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (host, network)
);
"""


class SqliteJournal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(_SCHEMA)
            self._migrate()
            self._connection.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release.

        ``CREATE TABLE IF NOT EXISTS`` does not touch an existing table, so a
        database written by an older sidecar would silently lose the new QUIC
        column and every ``upsert_rule`` would fail.
        """
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(rules)").fetchall()
        }
        if "quic_recipe" not in columns:
            self._connection.execute("ALTER TABLE rules ADD COLUMN quic_recipe TEXT NOT NULL DEFAULT ''")

    # -- writes ------------------------------------------------------------

    def record_probe(self, result: ProbeResult) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO probes(ts,host,port,kind,recipe,status_code,error,timing,transport)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    result.ts,
                    result.host,
                    result.port,
                    result.kind,
                    result.recipe,
                    result.status_code,
                    result.error,
                    json.dumps(_timing_dict(result.timing), ensure_ascii=False),
                    result.transport,
                ),
            )
            self._connection.commit()

    def record_decision(self, decision: Decision) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO decisions(ts,host,action,reason,recipe,confidence,applied) VALUES(?,?,?,?,?,?,?)",
                (
                    decision.ts,
                    decision.host,
                    decision.action,
                    decision.reason,
                    decision.recipe,
                    decision.confidence,
                    1 if decision.applied else 0,
                ),
            )
            self._connection.commit()

    def upsert_rule(self, rule: DomainRule) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO rules(host,network,mode,recipe,confidence,source,created_at,expires_at,last_ok,fails,pinned,enabled,quic_recipe)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(host,network) DO UPDATE SET mode=excluded.mode, recipe=excluded.recipe,"
                " confidence=excluded.confidence, source=excluded.source, expires_at=excluded.expires_at,"
                " last_ok=excluded.last_ok, fails=excluded.fails, pinned=excluded.pinned, enabled=excluded.enabled,"
                " quic_recipe=excluded.quic_recipe",
                (
                    rule.host,
                    rule.network,
                    rule.mode,
                    rule.recipe,
                    rule.confidence,
                    rule.source,
                    rule.created_at,
                    rule.expires_at,
                    rule.last_ok,
                    rule.fails,
                    1 if rule.pinned else 0,
                    1 if rule.enabled else 0,
                    str(getattr(rule, "quic_recipe", "") or ""),
                ),
            )
            self._connection.commit()

    def forget_rule(self, host: str, network: str = "") -> int:
        host = (host or "").strip().lower()
        if not host:
            return 0
        with self._lock:
            if network:
                cursor = self._connection.execute("DELETE FROM rules WHERE host=? AND network=?", (host, network))
            else:
                cursor = self._connection.execute("DELETE FROM rules WHERE host=?", (host,))
            self._connection.commit()
            return int(cursor.rowcount or 0)

    # -- reads -------------------------------------------------------------

    def recent_probes(self, host: str, limit: int = 20) -> list[ProbeResult]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM probes WHERE host=? ORDER BY ts DESC LIMIT ?", ((host or "").strip().lower(), int(limit))
            ).fetchall()
        return [_probe_from_row(row) for row in rows]

    def recent_decisions(self, limit: int = 50) -> list[Decision]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        return [
            Decision(
                ts=float(row["ts"]),
                host=str(row["host"]),
                action=str(row["action"]),
                reason=str(row["reason"]),
                recipe=row["recipe"],
                confidence=float(row["confidence"]),
                applied=bool(row["applied"]),
            )
            for row in rows
        ]

    def get_rule(self, host: str, network: str) -> DomainRule | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM rules WHERE host=? AND network=?", ((host or "").strip().lower(), network)
            ).fetchone()
        return _rule_from_row(row) if row is not None else None

    def list_rules(self, network: str = "") -> list[DomainRule]:
        with self._lock:
            if network:
                rows = self._connection.execute(
                    "SELECT * FROM rules WHERE network IN (?, '') ORDER BY host", (network,)
                ).fetchall()
            else:
                rows = self._connection.execute("SELECT * FROM rules ORDER BY host").fetchall()
        return [_rule_from_row(row) for row in rows]

    def stats(self) -> dict[str, int]:
        with self._lock:
            probes = int(self._connection.execute("SELECT COUNT(*) FROM probes").fetchone()[0])
            rules = int(self._connection.execute("SELECT COUNT(*) FROM rules").fetchone()[0])
        return {"probes": probes, "rules": rules}

    def prune(self, retention_days: int) -> None:
        cutoff = time.time() - max(1, int(retention_days)) * 86400
        with self._lock:
            self._connection.execute("DELETE FROM probes WHERE ts < ?", (cutoff,))
            self._connection.execute("DELETE FROM decisions WHERE ts < ?", (cutoff,))
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except Exception:
                pass


def _timing_dict(timing: ProbeTiming) -> dict[str, object]:
    return {
        "tcp_connect_ms": timing.tcp_connect_ms,
        "tls_handshake_ms": timing.tls_handshake_ms,
        "ttfb_ms": timing.ttfb_ms,
        "total_ms": timing.total_ms,
        "bytes_read": timing.bytes_read,
        "stalled_at_ms": timing.stalled_at_ms,
    }


def _probe_from_row(row: sqlite3.Row) -> ProbeResult:
    try:
        timing_data = json.loads(row["timing"] or "{}")
    except (TypeError, ValueError):
        timing_data = {}
    return ProbeResult(
        host=str(row["host"]),
        port=int(row["port"]),
        kind=str(row["kind"]),
        ts=float(row["ts"]),
        recipe=row["recipe"],
        status_code=row["status_code"],
        error=str(row["error"] or ""),
        timing=ProbeTiming(
            tcp_connect_ms=timing_data.get("tcp_connect_ms"),
            tls_handshake_ms=timing_data.get("tls_handshake_ms"),
            ttfb_ms=timing_data.get("ttfb_ms"),
            total_ms=int(timing_data.get("total_ms") or 0),
            bytes_read=int(timing_data.get("bytes_read") or 0),
            stalled_at_ms=timing_data.get("stalled_at_ms"),
        ),
        transport=str(row["transport"] or "direct"),
    )


def _rule_from_row(row: sqlite3.Row) -> DomainRule:
    keys = set(row.keys())
    return DomainRule(
        host=str(row["host"]),
        recipe=str(row["recipe"] or ""),
        mode=str(row["mode"] or "desync"),
        confidence=float(row["confidence"]),
        source=str(row["source"]),
        network=str(row["network"]),
        created_at=float(row["created_at"]),
        expires_at=row["expires_at"],
        last_ok=row["last_ok"],
        fails=int(row["fails"]),
        pinned=bool(row["pinned"]),
        enabled=bool(row["enabled"]),
        quic_recipe=str(row["quic_recipe"] or "") if "quic_recipe" in keys else "",
    )

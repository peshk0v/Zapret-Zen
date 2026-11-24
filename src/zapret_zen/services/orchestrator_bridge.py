"""Thin service-layer wrapper around the orchestrator sidecar.

The backend worker and the UI both need the orchestrator, but neither should
know how it is started or where its files live.  This bridge owns that:

* lazily creates one :class:`OrchestratorClient` per process;
* seeds the sidecar with hosts derived from the selected services;
* exposes the few operations the UI needs, always as plain dicts;
* watches ``overrides.json`` so the worker knows when to restart zapret.

Nothing here raises during normal operation: a missing or crashing sidecar must
never break the rest of the application.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from zapret_zen.orchestrator.client import OrchestratorClient
from zapret_zen.orchestrator.seed import seed_hosts
from zapret_zen.services.service_catalog import ALWAYS_APPLY_SERVICE_IDS

_RESTART_STABLE_S = 3.0


class OrchestratorBridge:
    def __init__(self, storage: Any, settings: Any, logging: Any = None, *, autostart: bool = True) -> None:
        self.storage = storage
        self.settings = settings
        self.logging = logging
        self.autostart = bool(autostart)
        self.install_root = Path(storage.paths.install_root)
        self.data_dir = Path(storage.paths.data_dir)
        self.overrides_path = self.data_dir / "orchestrator" / "overrides.json"
        self._lock = threading.RLock()
        self._client: OrchestratorClient | None = None
        self._restart_debounce_s = _RESTART_STABLE_S
        self._overlay_mtime = 0.0
        self._overlay_stable_since = 0.0
        self._last_error = ""

    # -- client management -------------------------------------------------

    @property
    def client(self) -> OrchestratorClient:
        with self._lock:
            if self._client is None:
                self._client = OrchestratorClient(
                    install_root=self.install_root,
                    data_dir=self.data_dir,
                    seed_hosts=self._seed_hosts(),
                    autostart=self.autostart,
                )
            return self._client

    def reset(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def close(self) -> None:
        self.reset()

    def shutdown_sidecar(self) -> bool:
        """Stop the sidecar process (used on application exit)."""
        client = self._client
        if client is None:
            return False
        try:
            return client.shutdown()
        finally:
            self.reset()

    # -- operations --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        result = self._call("snapshot", timeout_s=15.0)
        policy = result.get("policy") if isinstance(result.get("policy"), dict) else None
        if policy is not None:
            try:
                self._restart_debounce_s = max(1.0, float(policy.get("restart_debounce_s") or _RESTART_STABLE_S))
            except (TypeError, ValueError):
                pass
        return result

    def status(self) -> dict[str, Any]:
        return self._call("status", timeout_s=10.0)

    def tick(self) -> dict[str, Any]:
        return self._call("tick", timeout_s=15.0)

    def connections(self, limit: int = 120, *, problematic_only: bool = False) -> dict[str, Any]:
        return self._call("connections", {"limit": int(limit), "problematic_only": bool(problematic_only)}, timeout_s=15.0)

    def rules(self) -> dict[str, Any]:
        return self._call("rules", timeout_s=15.0)

    def recipes(self) -> dict[str, Any]:
        return self._call("recipes", timeout_s=10.0)

    def probe(self, host: str, *, trials: bool = True) -> dict[str, Any]:
        return self._call("probe", {"host": str(host), "trials": bool(trials)}, timeout_s=180.0)

    def forget(self, host: str) -> dict[str, Any]:
        return self._call("forget", {"host": str(host)}, timeout_s=30.0)

    def pin(self, host: str, recipe: str, mode: str = "desync") -> dict[str, Any]:
        return self._call("pin", {"host": str(host), "recipe": str(recipe), "mode": str(mode)}, timeout_s=30.0)

    def set_mode(self, mode: str) -> dict[str, Any]:
        return self._call("set_mode", {"mode": str(mode)}, timeout_s=15.0)

    def update_policy(self, patch: dict[str, Any]) -> dict[str, Any]:
        return self._call("update_policy", {"patch": dict(patch or {})}, timeout_s=15.0)

    def apply(self) -> dict[str, Any]:
        return self._call("apply", timeout_s=30.0)

    def ack_restart(self) -> dict[str, Any]:
        return self._call("ack_restart", timeout_s=15.0)

    def ping(self) -> bool:
        with self._lock:
            client = self._client
        if client is None:
            return False
        try:
            return client.ping()
        except Exception:
            return False

    # -- overlay / restart -------------------------------------------------

    def overlay(self) -> dict[str, Any]:
        try:
            with self.overrides_path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def overlay_sections(self) -> list[dict[str, Any]]:
        items = self.overlay().get("sections")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def probe_lane(self) -> tuple[int, int] | None:
        lane = self.overlay().get("probe_lane")
        if isinstance(lane, (list, tuple)) and len(lane) == 2:
            try:
                start, end = int(lane[0]), int(lane[1])
            except (TypeError, ValueError):
                return None
            if 1 <= start < end <= 65535:
                return start, end
        return None

    def poll_restart(self) -> dict[str, Any]:
        """Return ``{"needed": bool, ...}`` once the overlay has settled.

        The debounce coalesces bursts of rule writes (one host healing is often
        followed by a second one a moment later) into a single zapret restart.
        """
        payload = self.overlay()
        if not payload or not payload.get("pending_restart"):
            self._overlay_mtime = 0.0
            self._overlay_stable_since = 0.0
            return {"needed": False}
        try:
            mtime = self.overrides_path.stat().st_mtime
        except OSError:
            return {"needed": False}
        now = time.monotonic()
        if mtime != self._overlay_mtime:
            self._overlay_mtime = mtime
            self._overlay_stable_since = now
            return {"needed": False, "debouncing": True}
        if now - self._overlay_stable_since < self._restart_debounce_s:
            return {"needed": False, "debouncing": True}
        return {
            "needed": True,
            "sections": len(self.overlay_sections()),
            "fingerprint": str(payload.get("updated_at") or ""),
        }

    def mark_restart_done(self) -> None:
        self.ack_restart()
        self._overlay_mtime = 0.0
        self._overlay_stable_since = 0.0

    # -- internals ---------------------------------------------------------

    def _seed_hosts(self) -> list[str]:
        try:
            current = self.settings.get()
            ids: Sequence[str] = list(getattr(current, "selected_service_ids", []) or [])
        except Exception:
            ids = []
        selected = list(dict.fromkeys([*ALWAYS_APPLY_SERVICE_IDS, *ids]))
        try:
            return seed_hosts(selected, limit=16)
        except Exception:
            return []

    def _call(self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float = 30.0) -> dict[str, Any]:
        try:
            client = self.client
        except Exception as error:  # pragma: no cover - defensive
            self._last_error = str(error)
            return {"ok": False, "error": str(error)}
        try:
            result = client.request(method, params, timeout_s=timeout_s)
        except Exception as error:
            self._last_error = str(error)
            self._log("warning", "orchestrator request failed", method=method, error=str(error))
            return {"ok": False, "error": str(error)}
        if not result.get("ok"):
            self._last_error = str(result.get("error") or "")
        return result

    def _log(self, level: str, message: str, **fields: Any) -> None:
        logger = self.logging
        if logger is None:
            return
        try:
            logger.log(level, message, **fields)
        except Exception:
            pass

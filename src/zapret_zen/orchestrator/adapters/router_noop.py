"""No-op router adapter.

The ``Router`` port exists from day one so the orchestrator never hard-codes
"desync is the only remedy".  When Xray support lands, only this adapter is
replaced; the decision engine already emits ``route_proxy`` intents.
"""

from __future__ import annotations

from typing import Any


class NoopRouter:
    enabled = False

    def route(self, host: str, mode: str) -> dict[str, Any]:
        return {
            "ok": False,
            "host": host,
            "mode": mode,
            "reason": "router adapter is not configured yet",
        }

    def health(self) -> dict[str, Any]:
        return {"enabled": False, "status": "disabled", "engine": "none"}

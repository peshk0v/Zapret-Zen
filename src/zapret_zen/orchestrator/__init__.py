"""Smart Orchestrator core.

The package is intentionally UI-agnostic: it must not import PySide6 so the
same code can run inside the backend worker, a standalone sidecar process
(today) or a future Rust implementation behind the same IPC contract.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"

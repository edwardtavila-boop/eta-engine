"""Compatibility shim — paper_soak_tracker moved to ``eta_engine.scripts``.

All canonical logic lives in ``eta_engine/scripts/paper_soak_tracker.py``
(duplicate-window detection, unique-session aggregation in --status).
This module re-exports the public surface so older callers keep working.

Deprecated: import from ``eta_engine.scripts.paper_soak_tracker`` directly.
"""
from __future__ import annotations

from eta_engine.scripts.paper_soak_tracker import (  # noqa: F401
    LEDGER_PATH,
    MIN_DAYS,
    MIN_TRADES,
    main,
    run_session,
    show_status,
)

__all__ = ["LEDGER_PATH", "MIN_DAYS", "MIN_TRADES", "main", "run_session", "show_status"]

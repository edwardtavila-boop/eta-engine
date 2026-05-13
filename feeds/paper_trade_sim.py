"""Compatibility shim — paper_trade_sim moved to ``eta_engine.scripts``.

All canonical logic lives in ``eta_engine/scripts/paper_trade_sim.py``
(realistic fills, instrument-aware multipliers, session bucketing,
walk-forward).  This module re-exports the public surface so callers
that imported from ``eta_engine.feeds.paper_trade_sim`` keep working
without copy-paste drift between two implementations.

Deprecated: import from ``eta_engine.scripts.paper_trade_sim`` directly.
"""

from __future__ import annotations

from eta_engine.scripts.paper_trade_sim import (  # noqa: F401
    PaperPosition,
    PaperTrade,
    SimResult,
    main,
    run_simulation,
)

__all__ = ["PaperPosition", "PaperTrade", "SimResult", "main", "run_simulation"]

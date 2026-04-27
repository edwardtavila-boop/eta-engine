"""ETA Engine // brain.jarvis_v3.sage
=====================================
JARVIS multi-school market-theory consultation layer (2026-04-27).

JARVIS doesn't pick ONE technical school -- it consults ALL of them, then
weights their verdicts into a composite confluence score. This package
encodes the canonical wisdom of the major schools as queryable
``SchoolBase`` analyzers:

CLASSICAL (10):
  * dow_theory      -- primary/secondary/minor trends + averages confirm
  * wyckoff         -- accumulation/distribution + spring/upthrust + 3 laws
  * elliott_wave    -- 5 impulsive + 3 corrective wave structure
  * fibonacci       -- retracement (23.6/38.2/50/61.8/78.6) + extension
  * gann            -- 1x1 angles, square of nine, time/price geometry
  * support_resistance -- pivot-based S/R + breakout/rejection
  * trend_following -- MA stack, ADX, trendline alignment
  * vpa             -- volume confirms (or contradicts) price moves
  * market_profile  -- value area, POC, HVN/LVN
  * risk_management -- 1-2% per trade, R-multiple sizing, capital preservation

MODERN (4):
  * smc_ict         -- order blocks, FVG, liquidity sweep, BOS/ChoCH
  * order_flow      -- delta, absorption, bid/ask imbalance
  * neowave         -- structured Elliott extension (stricter rules)
  * weis_wyckoff    -- modern Wyckoff (intraday wave + S/R + price-volume)

Each school exposes::

    class XSchool(SchoolBase):
        NAME: str = "school_name"
        KNOWLEDGE: str = "canonical text knowledge..."

        def analyze(self, ctx: MarketContext) -> SchoolVerdict: ...

The ``confluence`` module aggregates per-school verdicts into a single
``SageReport`` with composite bias (long/short/neutral) + conviction
(0-1) + per-school breakdown. The ``consultation`` module is the
JARVIS entry point: ``consult_sage(ctx) -> SageReport``.

A new candidate policy ``v22_sage_confluence`` uses the sage report to
modulate JARVIS's verdicts: tighter caps when sage conviction is low or
disagrees with the entry direction.

Usage::

    from eta_engine.brain.jarvis_v3.sage import consult_sage, MarketContext

    ctx = MarketContext(bars=last_50_bars, side="long",
                        entry_price=21450.0)
    report = consult_sage(ctx)
    print(report.composite_bias)         # "long" | "short" | "neutral"
    print(report.conviction)             # 0.0-1.0
    print(report.per_school)             # {"dow": SchoolVerdict, ...}
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    MarketContext,
    SageReport,
    SchoolBase,
    SchoolVerdict,
)
from eta_engine.brain.jarvis_v3.sage.consultation import (
    SCHOOLS,
    consult_sage,
)

__all__ = [
    "MarketContext",
    "SCHOOLS",
    "SageReport",
    "SchoolBase",
    "SchoolVerdict",
    "consult_sage",
]

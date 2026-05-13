"""Live-vs-backtest divergence detector (Wave-15, 2026-04-27).

Backtest-claimed Sharpe is meaningless if live performance diverges
from it. This detector compares live realized R per (bot_id, regime)
cell to the backtest-expected R for the same cell, and flags
statistically significant gaps.

Why this matters: when divergence persists, ONE of these is true:
  1. Backtest is overfit (drop the strategy)
  2. Live execution adds slippage/latency the backtest didn't model
  3. Market regime has shifted from the backtest period

In all 3 cases, JARVIS needs to know -- and conditionally shrink size
or pull the bot.

The backtest expected R is supplied by the caller (we don't run
backtests here -- that's wave-16). This module is the comparison
layer: live vs whatever-baseline-you-supply.

Pure stdlib + math.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRADE_LOG = ROOT / "state" / "jarvis_intel" / "trade_closes.jsonl"


@dataclass
class CellComparison:
    """One (bot_id, regime) comparison."""

    bot_id: str
    regime: str
    n_live_trades: int
    live_avg_r: float
    backtest_expected_r: float
    delta_r: float  # live - backtest
    z_score: float  # SE-based z-stat
    severity: str  # "info" / "warning" / "critical"
    note: str = ""


@dataclass
class DivergenceReport:
    """Aggregated divergence report."""

    ts: str
    n_cells_compared: int
    n_warnings: int
    n_criticals: int
    cells: list[CellComparison] = field(default_factory=list)
    overall_status: str = "OK"
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "n_cells_compared": self.n_cells_compared,
            "n_warnings": self.n_warnings,
            "n_criticals": self.n_criticals,
            "overall_status": self.overall_status,
            "summary": self.summary,
            "cells": [
                {
                    "bot_id": c.bot_id,
                    "regime": c.regime,
                    "n_live_trades": c.n_live_trades,
                    "live_avg_r": c.live_avg_r,
                    "backtest_expected_r": c.backtest_expected_r,
                    "delta_r": c.delta_r,
                    "z_score": c.z_score,
                    "severity": c.severity,
                    "note": c.note,
                }
                for c in self.cells
            ],
        }


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        return None


def _stats(xs: list[float]) -> tuple[float, float]:
    """(mean, sample-stddev). (0,0) for fewer than 2."""
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else 0.0), 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(var)


def detect_divergence(
    *,
    backtest_baselines: dict[tuple[str, str], float],
    n_days_back: float = 30,
    min_trades_per_cell: int = 5,
    z_warning: float = 1.5,
    z_critical: float = 2.5,
    log_path: Path = DEFAULT_TRADE_LOG,
) -> DivergenceReport:
    """Compare live trades to backtest baselines.

    ``backtest_baselines`` is a dict mapping (bot_id, regime) -> the
    expected average R from the backtest. Caller supplies these
    values from their walk-forward harness output.
    """
    cutoff = datetime.now(UTC) - timedelta(days=n_days_back)
    trades = [t for t in _read_jsonl(log_path) if (dt := _parse_ts(t.get("ts"))) is not None and dt >= cutoff]

    # Group trades by (bot_id, regime)
    grouped: dict[tuple[str, str], list[float]] = {}
    for t in trades:
        bot = str(t.get("bot_id", ""))
        regime = str(t.get("regime", "neutral"))
        if not bot:
            continue
        grouped.setdefault((bot, regime), []).append(
            float(t.get("realized_r", 0.0)),
        )

    cells: list[CellComparison] = []
    n_warnings = 0
    n_criticals = 0

    for (bot_id, regime), rs in grouped.items():
        if len(rs) < min_trades_per_cell:
            continue
        backtest_expected = backtest_baselines.get((bot_id, regime))
        if backtest_expected is None:
            continue
        m, s = _stats(rs)
        # Standard error of the mean
        se = s / math.sqrt(len(rs)) if s > 0 else 0.0
        z = (m - backtest_expected) / se if se > 0 else 0.0
        delta = m - backtest_expected

        if abs(z) >= z_critical:
            severity = "critical"
            n_criticals += 1
        elif abs(z) >= z_warning:
            severity = "warning"
            n_warnings += 1
        else:
            severity = "info"

        note = f"live {m:+.2f}R vs backtest {backtest_expected:+.2f}R ({delta:+.2f}R; z={z:+.2f})"
        cells.append(
            CellComparison(
                bot_id=bot_id,
                regime=regime,
                n_live_trades=len(rs),
                live_avg_r=round(m, 3),
                backtest_expected_r=round(backtest_expected, 3),
                delta_r=round(delta, 3),
                z_score=round(z, 2),
                severity=severity,
                note=note,
            )
        )

    cells.sort(key=lambda c: abs(c.z_score), reverse=True)

    if n_criticals > 0:
        overall = "CRITICAL"
    elif n_warnings > 0:
        overall = "WARNING"
    else:
        overall = "OK"
    summary = f"divergence: {overall}; {len(cells)} cells, {n_warnings} warnings, {n_criticals} critical"

    return DivergenceReport(
        ts=datetime.now(UTC).isoformat(),
        n_cells_compared=len(cells),
        n_warnings=n_warnings,
        n_criticals=n_criticals,
        cells=cells,
        overall_status=overall,
        summary=summary,
    )

"""
EVOLUTIONARY TRADING ALGO  //  strategies.l2_per_symbol_ensemble
================================================================
Per-(strategy, symbol) ensemble weights — refinement of
l2_strategy_ensemble that recognizes a strategy's edge can vary
by symbol.

Why this exists
---------------
The global ensemble in l2_strategy_ensemble weights book_imbalance
the same way whether it's trading MNQ or GC.  But the realized
sharpe can differ wildly by symbol:
  book_imbalance on MNQ → 0.8
  book_imbalance on GC  → -0.3

Using one global weight either over-weights GC trades (where edge
is negative) or under-weights MNQ trades (where edge is real).

Per-symbol weights fix this.  Each (strategy, symbol) tuple gets
its own learned weight from historical performance.

Mechanic
--------
1. Read l2_backtest_runs.jsonl
2. Group by (strategy, symbol)
3. Compute weight = max(min_floor, latest_sharpe) per group
4. ensemble.vote_per_symbol(signals, symbol) looks up weights by
   (strategy_id, symbol)

Fallback
--------
When a (strategy, symbol) tuple has no history, fall back to the
strategy's global weight (from l2_strategy_ensemble).  This way new
symbols don't get muted immediately on launch.
"""
from __future__ import annotations

# ruff: noqa: ANN401, PLR2004
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

from eta_engine.strategies.l2_strategy_ensemble import (
    EnsembleSignal,
    compute_weights_from_history,
)

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
L2_BACKTEST_LOG = LOG_DIR / "l2_backtest_runs.jsonl"


@dataclass
class PerSymbolWeights:
    """Nested dict: weights[(strategy_id, symbol)] -> weight."""
    weights: dict[tuple[str, str], float] = field(default_factory=dict)
    global_fallback: dict[str, float] = field(default_factory=dict)
    ts: str = ""


def compute_per_symbol_weights(*, since_days: int = 30,
                                  min_sharpe_floor: float = 0.0,
                                  _path: Path | None = None) -> PerSymbolWeights:
    """Read backtest log, return per-(strategy, symbol) weights.

    A symbol's weight is its strategy's latest sharpe_proxy on that
    symbol, clipped at min_sharpe_floor.  When the (strategy, symbol)
    pair has no history, the per-symbol weight is omitted — callers
    should fall back to the strategy's global weight.
    """
    path = _path if _path is not None else L2_BACKTEST_LOG
    weights_by_pair: dict[tuple[str, str], float] = {}
    if not path.exists():
        global_weights = compute_weights_from_history(
            since_days=since_days, min_sharpe_floor=min_sharpe_floor,
            _path=path)
        return PerSymbolWeights(
            weights={}, global_fallback=global_weights.weights,
            ts=datetime.now(UTC).isoformat())
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt < cutoff:
                    continue
                strategy = rec.get("strategy")
                symbol = rec.get("symbol")
                sharpe = rec.get("sharpe_proxy")
                if not strategy or not symbol or sharpe is None:
                    continue
                if not rec.get("sharpe_proxy_valid", False):
                    continue
                key = (strategy, symbol)
                weights_by_pair[key] = max(min_sharpe_floor, float(sharpe))
    except OSError:
        pass

    global_weights = compute_weights_from_history(
        since_days=since_days, min_sharpe_floor=min_sharpe_floor,
        _path=path)
    return PerSymbolWeights(
        weights=weights_by_pair,
        global_fallback=global_weights.weights,
        ts=datetime.now(UTC).isoformat(),
    )


def vote_per_symbol(signals: Iterable[Any], weights: PerSymbolWeights,
                     *, symbol: str,
                     ensemble_threshold: float = 0.5) -> EnsembleSignal | None:
    """Weighted vote using per-(strategy, symbol) weights with global
    fallback.  Same semantics as l2_strategy_ensemble.vote() otherwise."""
    constituents: list[dict] = []
    weighted_sum = 0.0
    total_weight = 0.0
    for sig in signals:
        if sig is None:
            continue
        strategy = getattr(sig, "strategy_id", None) or "unknown"
        # Try (strategy, symbol) first
        weight = weights.weights.get((strategy, symbol))
        weight_source = "per_symbol"
        if weight is None:
            # Fall back to global
            weight = weights.global_fallback.get(strategy, 0.0)
            weight_source = "global_fallback"
        if weight <= 0:
            continue
        side = str(getattr(sig, "side", "")).upper()
        is_long = side in ("LONG", "BUY")
        confidence = float(getattr(sig, "confidence", 0.5))
        signed = confidence if is_long else -confidence
        weighted_sum += weight * signed
        total_weight += weight
        constituents.append({
            "strategy_id": strategy,
            "symbol": symbol,
            "side": "LONG" if is_long else "SHORT",
            "confidence": round(confidence, 3),
            "weight": round(weight, 3),
            "weight_source": weight_source,
            "signed_contribution": round(weight * signed, 3),
        })

    if total_weight <= 0:
        return None
    vote_val = weighted_sum / total_weight
    if abs(vote_val) < ensemble_threshold:
        return None
    side = "LONG" if vote_val > 0 else "SHORT"
    sig_ids = [getattr(s, "signal_id", "")
                for s in signals if s is not None]
    sig_ids_clean = [s for s in sig_ids if s]
    composite_id = (f"ENSEMBLE-PER-SYMBOL-{symbol}-{side}-"
                      + "_".join(sig_ids_clean[:3])
                      if sig_ids_clean else f"ENSEMBLE-PER-SYMBOL-{symbol}-{side}")
    return EnsembleSignal(
        side=side,
        weighted_vote=round(vote_val, 4),
        confidence=round(abs(vote_val), 3),
        constituent_signals=constituents,
        signal_id=composite_id[:200],
        rationale=(f"per-symbol ensemble vote={vote_val:+.3f} "
                    f"(symbol={symbol}, threshold={ensemble_threshold}, "
                    f"n_constituents={len(constituents)})"),
    )

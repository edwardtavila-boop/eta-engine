"""
EVOLUTIONARY TRADING ALGO  //  strategies.l2_strategy_ensemble
==============================================================
Combine signals from the 4 L2 strategies (book_imbalance,
footprint_absorption, aggressor_flow, microprice_drift) with
data-driven weights derived from each strategy's historical
sharpe_proxy.

Why this exists
---------------
Each L2 strategy has different edge characteristics:
  - book_imbalance: medium-frequency, persistent imbalance
  - footprint_absorption: low-frequency, large prints
  - aggressor_flow: low-frequency, bar-paced
  - microprice_drift: high-frequency, fast scalp

Trading each in isolation captures the per-strategy edge but
ignores diversification.  An ensemble that votes — or weighted-
averages — across strategies catches edges that survive across
multiple signal generation mechanisms while ignoring noise.

Mechanic
--------
1. Each constituent strategy produces a signal:
     None | LONG(confidence) | SHORT(confidence)
2. Ensemble computes weighted vote:
     vote = sum(weight_i * signed_confidence_i)
     where weight_i = max(0, sharpe_proxy_i)  (recent history)
3. Fire LONG if vote >= +ensemble_threshold
   Fire SHORT if vote <= -ensemble_threshold
   Else None
4. Confidence of ensemble signal = |vote| / sum_weights

The weights are read from l2_backtest_runs.jsonl — strategies with
no history or negative sharpe get weight 0 (effectively muted).

Limitations
-----------
- Correlation between constituent signals is NOT modeled (a
  full mean-variance approach would need a covariance matrix
  estimated from realized returns)
- Weights are recomputed on each invocation; for production, pin
  them weekly + freeze
- The ensemble is risk-additive: if all 4 signals fire LONG MNQ,
  this module returns ONE signal — but the operator's order router
  needs to ensure portfolio limits aren't violated by parallel
  per-strategy fires (use l2_portfolio_limits)
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

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
L2_BACKTEST_LOG = LOG_DIR / "l2_backtest_runs.jsonl"


@dataclass
class EnsembleSignal:
    """What the ensemble emits when its weighted vote crosses threshold."""

    side: str  # "LONG" | "SHORT"
    weighted_vote: float
    confidence: float  # |vote| / sum_weights
    constituent_signals: list[dict] = field(default_factory=list)
    # Each dict: {strategy_id, side, confidence, weight}
    signal_id: str = ""
    rationale: str = ""


@dataclass
class EnsembleWeights:
    """Per-strategy weights derived from recent sharpe history."""

    weights: dict[str, float] = field(default_factory=dict)
    ts: str = ""


def compute_weights_from_history(
    *, since_days: int = 30, min_sharpe_floor: float = 0.0, _path: Path | None = None
) -> EnsembleWeights:
    """Read l2_backtest_runs.jsonl and compute per-strategy weights.

    Weights are clipped at min_sharpe_floor (default 0 = mute losers).
    A strategy with no history gets weight 0 (will be ignored by
    the ensemble vote).
    """
    path = _path if _path is not None else L2_BACKTEST_LOG
    if not path.exists():
        return EnsembleWeights(weights={}, ts=datetime.now(UTC).isoformat())
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    latest_sharpe: dict[str, float] = {}
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
                if not strategy:
                    continue
                sharpe = rec.get("sharpe_proxy")
                if sharpe is None or not rec.get("sharpe_proxy_valid", False):
                    continue
                latest_sharpe[strategy] = float(sharpe)
    except OSError:
        return EnsembleWeights(weights={}, ts=datetime.now(UTC).isoformat())
    weights = {s: max(min_sharpe_floor, v) for s, v in latest_sharpe.items()}
    return EnsembleWeights(weights=weights, ts=datetime.now(UTC).isoformat())


def vote(
    signals: Iterable[Any], weights: dict[str, float], *, ensemble_threshold: float = 0.5
) -> EnsembleSignal | None:
    """Aggregate constituent signals with weighted vote.

    ``signals`` is an iterable of objects with attributes:
      - strategy_id (or symbol-as-fallback)
      - side ("LONG" | "SHORT" or "BUY" | "SELL")
      - confidence (float in [0, 1])

    None entries are skipped (strategy didn't fire).

    Returns an EnsembleSignal if |weighted_vote| >= ensemble_threshold,
    else None.
    """
    constituents: list[dict] = []
    weighted_sum = 0.0
    total_weight = 0.0
    for sig in signals:
        if sig is None:
            continue
        strategy = getattr(sig, "strategy_id", None) or getattr(sig, "_strategy", None) or "unknown"
        weight = weights.get(strategy, 0.0)
        if weight <= 0:
            continue  # this strategy has no positive history → ignore
        side = str(getattr(sig, "side", "")).upper()
        is_long = side in ("LONG", "BUY")
        confidence = float(getattr(sig, "confidence", 0.5))
        # Signed confidence: +confidence for LONG, -confidence for SHORT
        signed = confidence if is_long else -confidence
        weighted_sum += weight * signed
        total_weight += weight
        constituents.append(
            {
                "strategy_id": strategy,
                "side": "LONG" if is_long else "SHORT",
                "confidence": round(confidence, 3),
                "weight": round(weight, 3),
                "signed_contribution": round(weight * signed, 3),
            }
        )

    if total_weight <= 0:
        return None
    vote_val = weighted_sum / total_weight  # normalized to [-1, +1]
    if abs(vote_val) < ensemble_threshold:
        return None
    side = "LONG" if vote_val > 0 else "SHORT"
    # Generate ensemble signal_id from constituent IDs
    sig_ids = [getattr(s, "signal_id", "") for s in signals if s is not None]
    sig_ids_clean = [s for s in sig_ids if s]
    composite_id = "ENSEMBLE-" + side + "-" + "_".join(sig_ids_clean[:3]) if sig_ids_clean else f"ENSEMBLE-{side}"
    return EnsembleSignal(
        side=side,
        weighted_vote=round(vote_val, 4),
        confidence=round(abs(vote_val), 3),
        constituent_signals=constituents,
        signal_id=composite_id[:200],
        rationale=(
            f"ensemble vote={vote_val:+.3f} (threshold={ensemble_threshold}, n_constituents={len(constituents)})"
        ),
    )


def make_ensemble(*, since_days: int = 30, ensemble_threshold: float = 0.5, min_sharpe_floor: float = 0.0) -> Any:
    """Factory: returns a callable wrapper that loads weights at
    construction time and exposes ``decide(signals)`` for callers."""
    weights = compute_weights_from_history(since_days=since_days, min_sharpe_floor=min_sharpe_floor)

    class _Ensemble:
        def __init__(self) -> None:
            self.weights = weights.weights
            self.weights_ts = weights.ts
            self.ensemble_threshold = ensemble_threshold

        def decide(self, signals: Iterable[Any]) -> EnsembleSignal | None:
            return vote(signals, self.weights, ensemble_threshold=self.ensemble_threshold)

    return _Ensemble()

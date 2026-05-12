"""
JARVIS v3 // kelly_optimizer (T13)

Per-bot fractional-Kelly sizing recommendation with a drawdown penalty.
Reads recent trade closes for each bot, fits a normal distribution to
the R-returns, computes the Kelly fraction, then scales it down for
the operator's drawdown tolerance.

The output is a RECOMMENDATION — never auto-applied. Operator reviews
and pins each via ``jarvis_set_size_modifier``.

Math
----

Continuous Kelly for a normally-distributed return R~N(µ, σ²):

    f_kelly = µ / σ²

Fractional Kelly (operator's "what fraction of full-Kelly to size at"):

    f_target = kelly_fraction × f_kelly

Drawdown-penalty adjustment (penalizes bots with bigger losing tails):

    f_adjusted = f_target × exp(-α × (min_R / σ))
                 with α=0.15 by default — gentle but real penalty

Final clamp to [_SIZE_MOD_LOW, _SIZE_MOD_HIGH] = [0.0, 1.0] to match
hermes_overrides' de-risk-only contract.

Public interface
----------------

* ``recommend_sizing(lookback_days=30, kelly_fraction=0.25,
                      drawdown_penalty=0.15)`` — list of
  ``SizingRecommendation`` per bot.
* ``SizingRecommendation`` dataclass.

NEVER raises. Bots with insufficient data (n_trades < MIN_OBS) are
flagged ``insufficient_data=True`` and skipped from sizing math.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.kelly_optimizer")

_WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
_STATE_ROOT = _WORKSPACE / "var" / "eta_engine" / "state"
DEFAULT_TRADE_CLOSES_PATH = _STATE_ROOT / "jarvis_intel" / "trade_closes.jsonl"

MIN_OBS = 20  # bots with fewer than 20 closed trades over the lookback window get insufficient_data
SIZE_MOD_LOW, SIZE_MOD_HIGH = 0.0, 1.0
DEFAULT_KELLY_FRACTION = 0.25  # quarter-Kelly is the typical operator's prior
DEFAULT_DRAWDOWN_PENALTY = 0.15

EXPECTED_HOOKS = ("recommend_sizing",)


@dataclass(frozen=True)
class SizingRecommendation:
    bot_id: str
    n_trades: int
    insufficient_data: bool
    avg_r: float
    std_r: float
    min_r: float
    f_kelly: float          # raw Kelly fraction (can be > 1)
    f_target: float         # kelly_fraction × f_kelly
    f_adjusted: float       # after drawdown penalty
    recommended_size_modifier: float  # final clamped to [0, 1]
    rationale: str


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------


def _read_trade_closes(
    path: Path | None = None,
    since_dt: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read trade_closes.jsonl, filtered to records newer than since_dt."""
    import json
    target = path or DEFAULT_TRADE_CLOSES_PATH
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with target.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_dt is not None:
                    ts_str = rec.get("ts") or rec.get("closed_at")
                    if not isinstance(ts_str, str):
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts < since_dt:
                        continue
                out.append(rec)
    except OSError as exc:
        logger.warning("kelly_optimizer._read_trade_closes failed: %s", exc)
    return out


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


def _stats(rs: list[float]) -> tuple[float, float, float, float]:
    """Return (mean, std, min, max) for a list of R-values. Degenerate
    inputs return (0, 0, 0, 0)."""
    n = len(rs)
    if n == 0:
        return 0.0, 0.0, 0.0, 0.0
    mean = sum(rs) / n
    if n < 2:
        return mean, 0.0, rs[0], rs[0]
    variance = sum((r - mean) ** 2 for r in rs) / (n - 1)
    std = math.sqrt(variance)
    return mean, std, min(rs), max(rs)


def _kelly_fraction(mean: float, std: float) -> float:
    """Continuous Kelly = µ / σ². Returns 0 if std=0 (no variance)."""
    if std <= 0:
        return 0.0
    return mean / (std ** 2)


def _drawdown_penalty_factor(min_r: float, std: float, alpha: float) -> float:
    """Return a multiplier in (0, 1] that shrinks sizing for bots with
    fat lower tails. Bots whose worst trade was within 1σ of zero get
    factor ~= 1; bots whose worst trade was -3σ get factor ~= 0.64.
    """
    if std <= 0:
        return 1.0
    # min_r is typically negative; how-bad ratio is its magnitude in stds
    tail_severity = abs(min(0.0, min_r)) / std
    return math.exp(-alpha * tail_severity)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def recommend_sizing(
    lookback_days: int = 30,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
    drawdown_penalty: float = DEFAULT_DRAWDOWN_PENALTY,
    trade_closes_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return a list of sizing recommendations, one per bot with enough data.

    Args:
      lookback_days: trade-closes window (default 30 days).
      kelly_fraction: scalar applied to raw Kelly (default 0.25 = quarter-Kelly).
      drawdown_penalty: exponential decay constant in the tail penalty
                         (default 0.15 — gentle penalty).

    Returns: list of dict-serialized ``SizingRecommendation``.
    """
    if lookback_days <= 0:
        lookback_days = 30
    try:
        kf = float(kelly_fraction)
    except (TypeError, ValueError):
        kf = DEFAULT_KELLY_FRACTION
    try:
        dp = float(drawdown_penalty)
    except (TypeError, ValueError):
        dp = DEFAULT_DRAWDOWN_PENALTY

    since = datetime.now(UTC) - timedelta(days=lookback_days)
    trades = _read_trade_closes(trade_closes_path, since)

    by_bot: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        bid = t.get("bot_id")
        if not bid:
            continue
        try:
            r = float(t.get("r", t.get("r_value", 0.0)))
        except (TypeError, ValueError):
            continue
        by_bot[str(bid)].append(r)

    recs: list[SizingRecommendation] = []
    for bid, rs in by_bot.items():
        n = len(rs)
        if n < MIN_OBS:
            recs.append(SizingRecommendation(
                bot_id=bid,
                n_trades=n,
                insufficient_data=True,
                avg_r=0.0, std_r=0.0, min_r=0.0,
                f_kelly=0.0, f_target=0.0, f_adjusted=0.0,
                recommended_size_modifier=1.0,
                rationale=f"only {n} trades in {lookback_days}d (need ≥ {MIN_OBS}); keeping baseline 1.0×",
            ))
            continue
        mean, std, lo, _hi = _stats(rs)
        fk = _kelly_fraction(mean, std)
        ft = kf * fk
        penalty = _drawdown_penalty_factor(lo, std, dp)
        fa = ft * penalty
        clamped = _clamp(fa, SIZE_MOD_LOW, SIZE_MOD_HIGH)

        # Build a 1-line rationale for the operator
        rationale_bits = [
            f"µ={mean:+.3f}R σ={std:.3f}R worst={lo:+.3f}R n={n}",
            f"raw_Kelly={fk:.2f}×",
            f"×{kf:.2f}_kelly_frac → target={ft:.2f}",
            f"×{penalty:.2f}_tail_penalty → adj={fa:.2f}",
        ]
        rationale = " | ".join(rationale_bits)

        recs.append(SizingRecommendation(
            bot_id=bid,
            n_trades=n,
            insufficient_data=False,
            avg_r=round(mean, 4),
            std_r=round(std, 4),
            min_r=round(lo, 4),
            f_kelly=round(fk, 4),
            f_target=round(ft, 4),
            f_adjusted=round(fa, 4),
            recommended_size_modifier=round(clamped, 4),
            rationale=rationale,
        ))

    # Sort by recommended_size_modifier descending so operator sees the
    # confident bots first
    recs.sort(key=lambda r: (r.insufficient_data, -r.recommended_size_modifier))
    return [asdict(r) for r in recs]

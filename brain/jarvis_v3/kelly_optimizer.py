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
_LEGACY_STATE_ROOT = _WORKSPACE / "eta_engine" / "state"
# Primary canonical writer path.  Kept as a module-level export for
# back-compat — tests and external callers can override via the
# ``trade_closes_path=`` parameter on ``recommend_sizing``.
DEFAULT_TRADE_CLOSES_PATH = _STATE_ROOT / "jarvis_intel" / "trade_closes.jsonl"
# Legacy archive path.  Despite the "legacy" name, this is where 99% of
# historical trade-close records actually live (22.8 MB / 43,450 rows
# vs the canonical's 180 KB / 422 rows as of 2026-05-12).  The closed
# trade ledger reads from BOTH and dedupes; kelly_optimizer must do
# the same or it silently sees only the recent shim and reports
# ``insufficient_data`` for bots that actually have thousands of trades.
_LEGACY_TRADE_CLOSES_PATH = (
    _LEGACY_STATE_ROOT / "jarvis_intel" / "trade_closes.jsonl"
)

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


def _read_single_jsonl(
    target: Path,
    since_dt: datetime | None,
) -> list[dict[str, Any]]:
    """Read one trade_closes.jsonl file with optional since-filter."""
    import json
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
        logger.warning(
            "kelly_optimizer._read_single_jsonl failed (%s): %s", target, exc,
        )
    return out


def _read_trade_closes(
    path: Path | None = None,
    since_dt: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read trade-close records from BOTH the canonical and legacy paths,
    deduping on (signal_id, bot_id, ts, realized_r). This mirrors the
    ``closed_trade_ledger.load_close_records`` pattern.

    Pre-fix behavior: this function read only the canonical path
    (``var/eta_engine/state/jarvis_intel/trade_closes.jsonl``), which on
    most installations is a thin recent shim. The bulk of historical
    trade data lives in the so-called "legacy" archive at
    ``eta_engine/state/jarvis_intel/trade_closes.jsonl``. The kelly  # HISTORICAL-PATH-OK
    optimizer silently reported ``insufficient_data`` for the entire
    diamond fleet because the recent shim only held ~400 trades while
    the archive held 43,450+.

    If ``path`` is provided, it overrides the canonical path but the
    legacy path is still consulted (with dedupe) unless ``path`` is the
    explicit legacy path. This means tests that pass a tmp_path get
    behavior they expect.
    """
    if path is not None:
        # Caller-supplied path: behave as the legacy single-source reader.
        # Tests rely on this exact semantics (tmp_path with curated rows).
        return _read_single_jsonl(path, since_dt)

    primary = _read_single_jsonl(DEFAULT_TRADE_CLOSES_PATH, since_dt)
    legacy = _read_single_jsonl(_LEGACY_TRADE_CLOSES_PATH, since_dt)

    # Dedupe on (signal_id, bot_id, ts, realized_r) — same key the
    # closed_trade_ledger uses. Primary wins when there's a collision.
    def _key(r: dict[str, Any]) -> str:
        return "|".join([
            str(r.get("signal_id") or ""),
            str(r.get("bot_id") or ""),
            str(r.get("ts") or ""),
            str(r.get("realized_r") or ""),
        ])

    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for source in (primary, legacy):
        for r in source:
            k = _key(r)
            if k in seen:
                continue
            seen.add(k)
            merged.append(r)
    return merged


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
        # Canonical field is `realized_r` (see jarvis_intel/trade_closes.jsonl);
        # `r` and `r_value` are legacy aliases preserved for back-compat.
        raw_r = t.get("realized_r")
        if raw_r is None:
            raw_r = t.get("r", t.get("r_value", 0.0))
        try:
            r = float(raw_r)
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

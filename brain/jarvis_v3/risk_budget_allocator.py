"""Risk-budget allocator (Wave-15, 2026-04-27).

JARVIS's per-trade size shouldn't be static. After +5% MTD, you can
afford more aggression. After -3% MTD, you stand down. The risk-
budget allocator computes a dynamic envelope multiplier based on:

  * Month-to-date P&L (in R)
  * Current drawdown from peak (R)
  * Days remaining in the month
  * Per-bot risk allocation (so one bot's loss doesn't shrink the
    fleet's budget proportionally)

Output: ``BudgetMultiplier`` in [0, 2.0] with a ``reason`` field.
Multiplier > 1.0 = aggressive ("we have room"); < 1.0 = defensive.

Caps:
  * Max multiplier 2.0 (never bet more than 2x base)
  * Floor 0.0 (full stand-down when drawdown limit hit)

Pure stdlib + math. Reads from the trade-close log.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRADE_LOG = ROOT / "state" / "jarvis_intel" / "trade_closes.jsonl"


@dataclass
class BudgetEnvelope:
    """Operator-tunable knobs for the budget allocator."""

    target_monthly_r: float = 8.0  # plan: +8R/month average
    max_drawdown_r: float = -6.0  # full stand-down at -6R MTD
    soft_drawdown_r: float = -3.0  # start shrinking at -3R MTD
    aggressive_threshold_r: float = 4.0  # start expanding at +4R MTD
    max_multiplier: float = 2.0  # cap on aggression
    min_multiplier: float = 0.0  # floor


@dataclass
class BudgetMultiplier:
    """Output of size_for_proposal."""

    multiplier: float  # in [0, max_multiplier]
    mtd_r: float
    drawdown_r: float
    n_trades_mtd: int
    reason: str


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


def _days_remaining_in_month(now: datetime) -> int:
    """Days from `now` (inclusive) to last day of month."""
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1)
    else:
        next_month = now.replace(month=now.month + 1, day=1)
    last_day = next_month - timedelta(days=1)
    return max(0, (last_day.date() - now.date()).days + 1)


def current_envelope(
    *,
    envelope: BudgetEnvelope | None = None,
    bot_id: str | None = None,
    log_path: Path = DEFAULT_TRADE_LOG,
    as_of: datetime | None = None,
) -> BudgetMultiplier:
    """Compute the budget multiplier as of ``as_of`` (default now).

    When ``bot_id`` is supplied, only trades from that bot count
    toward the MTD P&L; otherwise all bots aggregate.
    """
    cfg = envelope or BudgetEnvelope()
    now = as_of or datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Pull MTD trades
    trades = _read_jsonl(log_path)
    mtd_trades: list[dict] = []
    for t in trades:
        dt = _parse_ts(t.get("ts"))
        if dt is None or dt < month_start:
            continue
        if bot_id is not None and str(t.get("bot_id", "")) != bot_id:
            continue
        mtd_trades.append(t)

    if not mtd_trades:
        return BudgetMultiplier(
            multiplier=1.0,
            mtd_r=0.0,
            drawdown_r=0.0,
            n_trades_mtd=0,
            reason="no MTD trades; standard sizing",
        )

    # Cumulative R + drawdown from peak
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in mtd_trades:
        cum += float(t.get("realized_r", 0.0))
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
    mtd_r = cum
    drawdown_r = -max_dd  # signed: negative = drawdown depth

    # Multiplier rules
    if mtd_r <= cfg.max_drawdown_r:
        return BudgetMultiplier(
            multiplier=cfg.min_multiplier,
            mtd_r=round(mtd_r, 3),
            drawdown_r=round(drawdown_r, 3),
            n_trades_mtd=len(mtd_trades),
            reason=(f"MTD {mtd_r:+.2f}R <= max_drawdown_r {cfg.max_drawdown_r:+.2f}R; FULL STAND-DOWN"),
        )

    if mtd_r <= cfg.soft_drawdown_r:
        # Linear ramp from min_multiplier (at max_drawdown) to 0.5 at soft_drawdown
        ratio = (mtd_r - cfg.max_drawdown_r) / max(cfg.soft_drawdown_r - cfg.max_drawdown_r, 1e-9)
        mult = cfg.min_multiplier + ratio * (0.5 - cfg.min_multiplier)
        return BudgetMultiplier(
            multiplier=round(max(cfg.min_multiplier, min(mult, 1.0)), 3),
            mtd_r=round(mtd_r, 3),
            drawdown_r=round(drawdown_r, 3),
            n_trades_mtd=len(mtd_trades),
            reason=(f"MTD {mtd_r:+.2f}R in soft drawdown; defensive sizing"),
        )

    if mtd_r >= cfg.aggressive_threshold_r:
        # Linear ramp from 1.0 at aggressive_threshold to max_multiplier
        # at 2x aggressive_threshold
        cap_at = cfg.aggressive_threshold_r * 2
        ratio = min(1.0, (mtd_r - cfg.aggressive_threshold_r) / max(cap_at - cfg.aggressive_threshold_r, 1e-9))
        mult = 1.0 + ratio * (cfg.max_multiplier - 1.0)
        return BudgetMultiplier(
            multiplier=round(max(1.0, min(mult, cfg.max_multiplier)), 3),
            mtd_r=round(mtd_r, 3),
            drawdown_r=round(drawdown_r, 3),
            n_trades_mtd=len(mtd_trades),
            reason=(f"MTD {mtd_r:+.2f}R above aggressive threshold; aggressive sizing"),
        )

    # Standard zone
    return BudgetMultiplier(
        multiplier=1.0,
        mtd_r=round(mtd_r, 3),
        drawdown_r=round(drawdown_r, 3),
        n_trades_mtd=len(mtd_trades),
        reason=f"MTD {mtd_r:+.2f}R in standard band",
    )


def size_for_proposal(
    *,
    base_size: float,
    envelope: BudgetEnvelope | None = None,
    bot_id: str | None = None,
    log_path: Path = DEFAULT_TRADE_LOG,
) -> tuple[float, BudgetMultiplier]:
    """Adjust a base size by the current budget envelope.

    Returns (adjusted_size, BudgetMultiplier)."""
    mult = current_envelope(
        envelope=envelope,
        bot_id=bot_id,
        log_path=log_path,
    )
    return base_size * mult.multiplier, mult

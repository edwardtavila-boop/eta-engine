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
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.scripts import workspace_roots

logger = logging.getLogger(__name__)

DEFAULT_TRADE_LOG = workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH
DEFAULT_SNAPSHOT_PATH = workspace_roots.ETA_JARVIS_RISK_BUDGET_SNAPSHOT_PATH


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


def _load_snapshot(path: Path, *, bot_id: str | None = None) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if bot_id:
        bot_entry = payload.get("bots", {}).get(bot_id)
        if isinstance(bot_entry, dict):
            return bot_entry
    fleet = payload.get("fleet")
    return fleet if isinstance(fleet, dict) else None


def _budget_from_summary(
    *,
    cfg: BudgetEnvelope,
    mtd_r: float,
    drawdown_r: float,
    n_trades_mtd: int,
    source: str,
) -> BudgetMultiplier:
    if n_trades_mtd <= 0:
        return BudgetMultiplier(
            multiplier=1.0,
            mtd_r=0.0,
            drawdown_r=0.0,
            n_trades_mtd=0,
            reason=f"no MTD trades; standard sizing [{source}]",
        )

    if mtd_r <= cfg.max_drawdown_r:
        return BudgetMultiplier(
            multiplier=cfg.min_multiplier,
            mtd_r=round(mtd_r, 3),
            drawdown_r=round(drawdown_r, 3),
            n_trades_mtd=n_trades_mtd,
            reason=(
                f"MTD {mtd_r:+.2f}R <= max_drawdown_r {cfg.max_drawdown_r:+.2f}R; "
                f"FULL STAND-DOWN [{source}]"
            ),
        )

    if mtd_r <= cfg.soft_drawdown_r:
        ratio = (mtd_r - cfg.max_drawdown_r) / max(cfg.soft_drawdown_r - cfg.max_drawdown_r, 1e-9)
        mult = cfg.min_multiplier + ratio * (0.5 - cfg.min_multiplier)
        return BudgetMultiplier(
            multiplier=round(max(cfg.min_multiplier, min(mult, 1.0)), 3),
            mtd_r=round(mtd_r, 3),
            drawdown_r=round(drawdown_r, 3),
            n_trades_mtd=n_trades_mtd,
            reason=f"MTD {mtd_r:+.2f}R in soft drawdown [{source}]",
        )

    if mtd_r >= cfg.aggressive_threshold_r:
        cap_at = cfg.aggressive_threshold_r * 2
        ratio = min(1.0, (mtd_r - cfg.aggressive_threshold_r) / max(cap_at - cfg.aggressive_threshold_r, 1e-9))
        mult = 1.0 + ratio * (cfg.max_multiplier - 1.0)
        return BudgetMultiplier(
            multiplier=round(max(1.0, min(mult, cfg.max_multiplier)), 3),
            mtd_r=round(mtd_r, 3),
            drawdown_r=round(drawdown_r, 3),
            n_trades_mtd=n_trades_mtd,
            reason=f"MTD {mtd_r:+.2f}R above aggressive threshold [{source}]",
        )

    return BudgetMultiplier(
        multiplier=1.0,
        mtd_r=round(mtd_r, 3),
        drawdown_r=round(drawdown_r, 3),
        n_trades_mtd=n_trades_mtd,
        reason=f"MTD {mtd_r:+.2f}R in standard band [{source}]",
    )


def current_envelope(
    *,
    envelope: BudgetEnvelope | None = None,
    bot_id: str | None = None,
    log_path: Path = DEFAULT_TRADE_LOG,
    snapshot_path: Path = DEFAULT_SNAPSHOT_PATH,
    as_of: datetime | None = None,
) -> BudgetMultiplier:
    """Compute the budget multiplier as of ``as_of`` (default now).

    When ``bot_id`` is supplied, only trades from that bot count
    toward the MTD P&L; otherwise all bots aggregate.
    """
    cfg = envelope or BudgetEnvelope()
    snapshot = _load_snapshot(snapshot_path, bot_id=bot_id)
    if snapshot is not None:
        return _budget_from_summary(
            cfg=cfg,
            mtd_r=float(snapshot.get("mtd_r", 0.0)),
            drawdown_r=float(snapshot.get("drawdown_r", 0.0)),
            n_trades_mtd=int(snapshot.get("n_trades_mtd", 0)),
            source="snapshot",
        )

    now = as_of or datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
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
        return _budget_from_summary(
            cfg=cfg,
            mtd_r=0.0,
            drawdown_r=0.0,
            n_trades_mtd=0,
            source="log",
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

    return _budget_from_summary(
        cfg=cfg,
        mtd_r=round(mtd_r, 3),
        drawdown_r=round(drawdown_r, 3),
        n_trades_mtd=len(mtd_trades),
        source="log",
    )


def size_for_proposal(
    *,
    base_size: float,
    envelope: BudgetEnvelope | None = None,
    bot_id: str | None = None,
    log_path: Path = DEFAULT_TRADE_LOG,
    snapshot_path: Path = DEFAULT_SNAPSHOT_PATH,
) -> tuple[float, BudgetMultiplier]:
    """Adjust a base size by the current budget envelope.

    Returns (adjusted_size, BudgetMultiplier)."""
    mult = current_envelope(
        envelope=envelope,
        bot_id=bot_id,
        log_path=log_path,
        snapshot_path=snapshot_path,
    )
    return base_size * mult.multiplier, mult

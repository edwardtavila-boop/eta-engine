"""
JARVIS v3 // pnl_summary — operator-facing PnL aggregation.

Reads trade_closes.jsonl (canonical and legacy paths, dedupe by signal_id)
and produces a multi-window PnL summary the operator actually wants
to see in Telegram briefings.

Why this exists
---------------

The original morning_briefing prompt rendered fleet_status + wiring_audit
output — useful for an engineer, but the operator wants TRADING info:

  * Total R today / this week / this month
  * Number of wins vs losses
  * Top 3 winners + worst 3 losers
  * Biggest single win + biggest single loss in the window
  * Win rate (with sample size caveat)
  * Whether anything material happened since last briefing

This module computes all of that from a single read of trade_closes.

Material-event detection
------------------------

``has_material_events_since(asof)`` returns True iff:
  * Any trade closed since asof
  * Total R changed by ≥ 0.5
  * Any new override applied
  * Any anomaly threshold tripped (>2R single trade, >3R drawdown)

The Telegram cron uses this to SUPPRESS delivery on quiet windows —
no more "everything is fine, here's a status snapshot" spam.

Public interface
----------------

* ``summarize(window_hours=24)`` → ``PnLSummary`` dataclass
* ``multi_window_summary()`` → today / 7d / 30d combined
* ``recent_trades(n=5)`` → last N closes with W/L tag
* ``has_material_events_since(asof_iso)`` → bool

NEVER raises. Returns empty/zero shapes on read failure.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.pnl_summary")

_WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
_STATE_ROOT = _WORKSPACE / "var" / "eta_engine" / "state"
_LEGACY_STATE_ROOT = _WORKSPACE / "eta_engine" / "state"
DEFAULT_TRADE_CLOSES_PATH = _STATE_ROOT / "jarvis_intel" / "trade_closes.jsonl"
LEGACY_TRADE_CLOSES_PATH = _LEGACY_STATE_ROOT / "jarvis_intel" / "trade_closes.jsonl"

# Material-event thresholds — when has_material_events_since() trips True.
BIG_WIN_R = 2.0  # single trade ≥ +2R is "celebrate-worthy"
BIG_LOSS_R = 2.0  # single trade ≤ -2R is "alert-worthy"
DRAWDOWN_R = 3.0  # cumulative R ≤ -3R is "investigate-worthy"
TOTAL_R_DELTA = 0.5  # half-R move in the window is "worth surfacing"

EXPECTED_HOOKS = (
    "summarize",
    "multi_window_summary",
    "recent_trades",
    "has_material_events_since",
)


@dataclass(frozen=True)
class TradeRow:
    bot_id: str
    asset: str
    r: float
    ts: str
    consult_id: str
    win: bool  # True iff r > 0


@dataclass(frozen=True)
class BotRollup:
    bot_id: str
    n_trades: int
    total_r: float
    win_rate: float
    best_trade: float
    worst_trade: float


@dataclass(frozen=True)
class PnLSummary:
    """Aggregated PnL over a time window."""

    window_hours: float
    asof: str
    n_trades: int
    n_wins: int
    n_losses: int
    total_r: float
    win_rate: float
    best_trade: TradeRow | None
    worst_trade: TradeRow | None
    top_performers: list[BotRollup] = field(default_factory=list)
    worst_performers: list[BotRollup] = field(default_factory=list)
    recent: list[TradeRow] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["best_trade"] = asdict(self.best_trade) if self.best_trade else None
        d["worst_trade"] = asdict(self.worst_trade) if self.worst_trade else None
        d["top_performers"] = [asdict(b) for b in self.top_performers]
        d["worst_performers"] = [asdict(b) for b in self.worst_performers]
        d["recent"] = [asdict(t) for t in self.recent]
        return d


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------


def _parse_iso(s: Any) -> datetime | None:  # noqa: ANN401
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_jsonl(path: Path, since_dt: datetime | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_dt is not None:
                    ts = _parse_iso(rec.get("ts") or rec.get("closed_at"))
                    if ts is None or ts < since_dt:
                        continue
                out.append(rec)
    except OSError as exc:
        logger.warning("pnl_summary._read_jsonl failed: %s", exc)
    return out


def _read_trades_deduped(
    override_path: Path | None,
    since_dt: datetime | None,
) -> list[dict[str, Any]]:
    """Read from canonical + legacy paths, dedupe on (signal_id, bot_id,
    ts, realized_r) — same pattern as attribution_cube / kelly_optimizer.

    When override_path is supplied (tests), behave as single-source.
    """
    if override_path is not None:
        return _read_jsonl(override_path, since_dt)
    primary = _read_jsonl(DEFAULT_TRADE_CLOSES_PATH, since_dt)
    legacy = _read_jsonl(LEGACY_TRADE_CLOSES_PATH, since_dt)

    def _key(r: dict[str, Any]) -> str:
        return "|".join(
            [
                str(r.get("signal_id") or ""),
                str(r.get("bot_id") or ""),
                str(r.get("ts") or r.get("closed_at") or ""),
                str(r.get("realized_r") or ""),
            ]
        )

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


def _extract_r(rec: dict[str, Any]) -> float | None:
    """Canonical field is realized_r; r and r_value are legacy aliases."""
    raw = rec.get("realized_r")
    if raw is None:
        raw = rec.get("r", rec.get("r_value"))
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _to_trade_row(rec: dict[str, Any]) -> TradeRow | None:
    r = _extract_r(rec)
    if r is None:
        return None
    bot_id = str(rec.get("bot_id") or "?")
    asset = str(rec.get("asset_class") or rec.get("asset") or "?")
    ts = str(rec.get("ts") or rec.get("closed_at") or "")
    consult_id = str(rec.get("consult_id") or "")
    return TradeRow(
        bot_id=bot_id,
        asset=asset,
        r=round(r, 4),
        ts=ts,
        consult_id=consult_id,
        win=r > 0,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def summarize(
    window_hours: float = 24.0,
    trade_closes_path: Path | None = None,
    asof: datetime | None = None,
) -> PnLSummary:
    """Aggregate PnL over the last ``window_hours``.

    Returns an empty-ish summary (n_trades=0) when no trades fall in
    the window — operator's briefing layer renders this as "(no trades)"
    rather than a wall of zeros.
    """
    now = asof or datetime.now(UTC)
    if window_hours <= 0:
        window_hours = 24.0
    since = now - timedelta(hours=window_hours)

    try:
        records = _read_trades_deduped(trade_closes_path, since)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pnl_summary.summarize read failed: %s", exc)
        return PnLSummary(
            window_hours=window_hours,
            asof=now.isoformat(),
            n_trades=0,
            n_wins=0,
            n_losses=0,
            total_r=0.0,
            win_rate=0.0,
            best_trade=None,
            worst_trade=None,
            error=str(exc)[:200],
        )

    rows: list[TradeRow] = []
    for rec in records:
        row = _to_trade_row(rec)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda t: t.ts)  # chronological

    n = len(rows)
    if n == 0:
        return PnLSummary(
            window_hours=window_hours,
            asof=now.isoformat(),
            n_trades=0,
            n_wins=0,
            n_losses=0,
            total_r=0.0,
            win_rate=0.0,
            best_trade=None,
            worst_trade=None,
        )

    n_wins = sum(1 for r in rows if r.win)
    n_losses = sum(1 for r in rows if r.r < 0)
    total_r = round(sum(r.r for r in rows), 4)
    win_rate = round(n_wins / n, 4) if n else 0.0
    best = max(rows, key=lambda t: t.r)
    worst = min(rows, key=lambda t: t.r)

    # Bot rollups
    by_bot: dict[str, list[TradeRow]] = defaultdict(list)
    for r in rows:
        by_bot[r.bot_id].append(r)
    rollups: list[BotRollup] = []
    for bot_id, trades in by_bot.items():
        wins = sum(1 for t in trades if t.win)
        total = round(sum(t.r for t in trades), 4)
        rollups.append(
            BotRollup(
                bot_id=bot_id,
                n_trades=len(trades),
                total_r=total,
                win_rate=round(wins / len(trades), 4),
                best_trade=round(max(t.r for t in trades), 4),
                worst_trade=round(min(t.r for t in trades), 4),
            )
        )
    rollups.sort(key=lambda b: b.total_r, reverse=True)
    top_3 = rollups[:3]
    worst_3 = list(reversed(rollups[-3:])) if len(rollups) >= 1 else []

    # Most recent 5 trades (already chronological; take tail then reverse)
    recent = list(reversed(rows[-5:]))

    return PnLSummary(
        window_hours=window_hours,
        asof=now.isoformat(),
        n_trades=n,
        n_wins=n_wins,
        n_losses=n_losses,
        total_r=total_r,
        win_rate=win_rate,
        best_trade=best,
        worst_trade=worst,
        top_performers=top_3,
        worst_performers=worst_3,
        recent=recent,
    )


def multi_window_summary(
    trade_closes_path: Path | None = None,
    asof: datetime | None = None,
) -> dict[str, Any]:
    """Convenience: today (24h) + this week (168h) + this month (720h)
    bundled in one dict for the operator's briefing template.
    """
    today = summarize(24.0, trade_closes_path, asof)
    week = summarize(168.0, trade_closes_path, asof)
    month = summarize(720.0, trade_closes_path, asof)
    return {
        "asof": (asof or datetime.now(UTC)).isoformat(),
        "today": today.to_dict(),
        "week": week.to_dict(),
        "month": month.to_dict(),
    }


def recent_trades(
    n: int = 5,
    trade_closes_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return the last ``n`` trade closes (newest first)."""
    if n <= 0:
        return []
    # Pull a generous window (30d) then take tail
    s = summarize(720.0, trade_closes_path)
    return [asdict(t) for t in s.recent[:n]]


def has_material_events_since(
    asof_iso: str,
    trade_closes_path: Path | None = None,
) -> dict[str, Any]:
    """Detect whether anything operator-material happened since ``asof_iso``.

    Used by Telegram cron tasks to SUPPRESS delivery on quiet windows.
    Returns a dict with:
      * ``has_material``: bool
      * ``reasons``: list of strings (which triggers fired)
      * ``trades_since``: int
      * ``r_since``: float
      * ``biggest_single_r``: float
    """
    reasons: list[str] = []
    since_dt = _parse_iso(asof_iso) or (datetime.now(UTC) - timedelta(hours=24))
    try:
        records = _read_trades_deduped(trade_closes_path, since_dt)
    except Exception as exc:  # noqa: BLE001
        return {
            "has_material": False,
            "reasons": [f"read_failed:{exc}"],
            "trades_since": 0,
            "r_since": 0.0,
            "biggest_single_r": 0.0,
        }

    rows = [_to_trade_row(rec) for rec in records]
    rows = [r for r in rows if r is not None]
    n = len(rows)
    if n > 0:
        reasons.append(f"trades_since:{n}")

    total_r = sum(r.r for r in rows)
    if abs(total_r) >= TOTAL_R_DELTA:
        reasons.append(f"r_delta_{total_r:+.2f}")

    biggest = 0.0
    if rows:
        biggest = max((r.r for r in rows), key=abs)
        if biggest >= BIG_WIN_R:
            reasons.append(f"big_win_{biggest:+.2f}R")
        if biggest <= -BIG_LOSS_R:
            reasons.append(f"big_loss_{biggest:+.2f}R")

    if total_r <= -DRAWDOWN_R:
        reasons.append(f"drawdown_{total_r:+.2f}R")

    # Also surface override changes — check hermes_overrides activity
    try:
        from eta_engine.brain.jarvis_v3 import hermes_overrides

        active = hermes_overrides.active_overrides_summary()
        size_pins = active.get("size_modifiers") or {}
        for bot_id, entry in size_pins.items():
            applied_at = _parse_iso((entry or {}).get("applied_at"))
            if applied_at and applied_at >= since_dt:
                reasons.append(f"override_applied:{bot_id}")
                break
    except Exception:  # noqa: BLE001 — best-effort
        pass

    has_material = len(reasons) > 0

    return {
        "has_material": has_material,
        "reasons": reasons,
        "trades_since": n,
        "r_since": round(total_r, 4),
        "biggest_single_r": round(biggest, 4),
    }

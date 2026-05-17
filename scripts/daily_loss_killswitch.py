"""Daily loss kill switch — halts all entries when the day's
realized PnL drops below the configured floor.

Designed to be called from JarvisStrategySupervisor._tick_bot before
_maybe_enter. Read-only: never modifies state, just returns a boolean
+ reason. The supervisor logs and skips the entry; existing positions
are unaffected (they exit via brackets / supervisor exit logic
normally).

Loss is computed across the trade_closes.jsonl stream filtered to
"today" in the operator's local timezone (defaults to America/New_York
for Atlanta/ET operations; override via ETA_KILLSWITCH_TIMEZONE). Reset
is automatic at the next local midnight tick.

Env knobs:
  ETA_KILLSWITCH_DAILY_LIMIT_USD      default -300.0  (halt at -$300 day)
  ETA_KILLSWITCH_DAILY_LIMIT_PCT      default None   (alt spec as %% of equity)
  ETA_KILLSWITCH_EQUITY_USD           default 5000.0 (denominator for %% spec)
  ETA_KILLSWITCH_DISABLED             default unset (set to "1" to disable)
  ETA_KILLSWITCH_TIMEZONE             default "America/New_York"
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, time, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from eta_engine.scripts import workspace_roots

logger = logging.getLogger(__name__)

_TRADE_CLOSES_PATH = workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH
_DEFAULT_TIMEZONE = "America/New_York"


def _operator_timezone_name() -> str:
    return os.getenv("ETA_KILLSWITCH_TIMEZONE", _DEFAULT_TIMEZONE).strip() or _DEFAULT_TIMEZONE


def _operator_timezone() -> tzinfo:
    tz_name = _operator_timezone_name()
    if tz_name == "UTC":
        return UTC
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        return UTC


def _operator_now() -> datetime:
    return datetime.now(_operator_timezone())


def _trade_closes_path() -> Path:
    override = os.getenv("ETA_JARVIS_TRADE_CLOSES_PATH", "").strip()
    if override:
        return Path(override)
    state_override = os.getenv("ETA_STATE_DIR", "").strip()
    if state_override:
        return Path(state_override) / "jarvis_intel" / "trade_closes.jsonl"
    return _TRADE_CLOSES_PATH


def _next_reset_at(now: datetime | None = None) -> datetime:
    local_now = now or _operator_now()
    return datetime.combine(local_now.date() + timedelta(days=1), time.min, tzinfo=local_now.tzinfo)


def _today_utc_date_str() -> str:
    """Operator-tz "today" as YYYY-MM-DD."""
    return _operator_now().date().isoformat()


def _parse_trade_close_ts(ts_str: str, *, operator_tz: tzinfo | None = None) -> datetime | None:
    if not ts_str:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
    except ValueError:
        return None

    local_tz = operator_tz or _operator_timezone()
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


def _is_today(ts_str: str, *, today: str | None = None, operator_tz: tzinfo | None = None) -> bool:
    parsed = _parse_trade_close_ts(ts_str, operator_tz=operator_tz)
    if parsed is None:
        return False
    return parsed.date().isoformat() == (today or _today_utc_date_str())


def _today_realized_pnl_usd() -> float:
    """Sum realized_pnl across LIVE trade closes timestamped today.

    Wave-25 (2026-05-13): only counts records with ``data_source="live"``
    so paper / backtest emissions do not trip the killswitch. Without
    this filter the killswitch could fire on backfilled historical
    losses or paper-sim runs.
    """
    from eta_engine.scripts.closed_trade_ledger import (
        DATA_SOURCE_LIVE,
        load_close_records,
    )

    rows = load_close_records(
        source_paths=[_trade_closes_path()],
        data_sources=frozenset({DATA_SOURCE_LIVE}),
    )
    total = 0.0
    today = _today_utc_date_str()
    operator_tz = _operator_timezone()
    for rec in rows:
        extra = rec.get("extra") or {}
        ts = (
            rec.get("close_ts")
            or (extra.get("close_ts") if isinstance(extra, dict) else None)
            or rec.get("ts")
            or rec.get("fill_ts")
            or ""
        )
        if not _is_today(ts, today=today, operator_tz=operator_tz):
            continue
        pnl = rec.get("realized_pnl")
        if pnl is None and isinstance(extra, dict):
            pnl = extra.get("realized_pnl")
        if pnl is None:
            continue
        try:
            total += float(pnl)
        except (TypeError, ValueError):
            continue
    return total


def _resolve_limit_usd() -> float:
    """USD limit (negative number = max allowed loss).

    ETA_KILLSWITCH_DAILY_LIMIT_PCT (if set) takes precedence and is
    converted via ETA_KILLSWITCH_EQUITY_USD. Otherwise the literal
    USD value is used. Returns the SIGNED floor — typically negative.
    """
    pct_raw = os.getenv("ETA_KILLSWITCH_DAILY_LIMIT_PCT", "").strip()
    if pct_raw:
        try:
            pct = float(pct_raw)
            equity = float(os.getenv("ETA_KILLSWITCH_EQUITY_USD", "5000"))
            return -abs(pct) * equity / 100.0
        except (TypeError, ValueError):
            pass
    raw = os.getenv("ETA_KILLSWITCH_DAILY_LIMIT_USD", "-300.0").strip()
    try:
        v = float(raw)
        # Treat positive input as "this much loss tolerated" (operator-friendly).
        return -abs(v) if v >= 0 and v != 0 else v
    except (TypeError, ValueError):
        return -300.0


def is_killswitch_tripped() -> tuple[bool, str]:
    """Return (tripped, reason).

    Tripped means: today's realized PnL has crossed the loss floor.
    Caller (supervisor) logs the reason once per tick and refuses
    new entries. Existing positions continue to manage themselves
    via brackets / exit logic — we do NOT close them on trip.
    """
    if os.getenv("ETA_KILLSWITCH_DISABLED", "").lower() in {"1", "true", "yes", "on"}:
        return False, "disabled"

    limit_usd = _resolve_limit_usd()
    today_pnl = _today_realized_pnl_usd()
    if today_pnl <= limit_usd:
        return True, (f"day_pnl=${today_pnl:+.2f} <= limit=${limit_usd:+.2f} (date={_today_utc_date_str()})")
    return False, f"day_pnl=${today_pnl:+.2f} (limit=${limit_usd:+.2f})"


def killswitch_status() -> dict:
    """Snapshot for dashboards / heartbeat."""
    tripped, reason = is_killswitch_tripped()
    now = _operator_now()
    reset_at = _next_reset_at(now)
    tz_name = _operator_timezone_name()
    return {
        "tripped": tripped,
        "reason": reason,
        "limit_usd": _resolve_limit_usd(),
        "today_pnl_usd": _today_realized_pnl_usd(),
        "date": now.date().isoformat(),
        "timezone": tz_name,
        "reset_at": reset_at.isoformat(),
        "reset_at_utc": reset_at.astimezone(UTC).isoformat(),
        "reset_display": reset_at.strftime("%Y-%m-%d %H:%M %Z"),
        "reset_in_s": max(0, int((reset_at - now).total_seconds())),
        "disabled": os.getenv("ETA_KILLSWITCH_DISABLED", "").lower() in {"1", "true", "yes", "on"},
    }


if __name__ == "__main__":
    print(json.dumps(killswitch_status(), indent=2))

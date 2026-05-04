"""Daily loss kill switch — halts all entries when the day's
realized PnL drops below the configured floor.

Designed to be called from JarvisStrategySupervisor._tick_bot before
_maybe_enter. Read-only: never modifies state, just returns a boolean
+ reason. The supervisor logs and skips the entry; existing positions
are unaffected (they exit via brackets / supervisor exit logic
normally).

Loss is computed across the trade_closes.jsonl stream filtered to
"today" in the operator's local timezone (defaults to UTC; override
via ETA_KILLSWITCH_TIMEZONE). Reset is automatic at the next
midnight tick.

Env knobs:
  ETA_KILLSWITCH_DAILY_LIMIT_USD      default -300.0  (halt at -$300 day)
  ETA_KILLSWITCH_DAILY_LIMIT_PCT      default None   (alt spec as %% of equity)
  ETA_KILLSWITCH_EQUITY_USD           default 5000.0 (denominator for %% spec)
  ETA_KILLSWITCH_DISABLED             default unset (set to "1" to disable)
  ETA_KILLSWITCH_TIMEZONE             default "UTC"
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_TRADE_CLOSES_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\trade_closes.jsonl"
)


def _today_utc_date_str() -> str:
    """Operator-tz "today" as YYYY-MM-DD. Default UTC."""
    tz_name = os.getenv("ETA_KILLSWITCH_TIMEZONE", "UTC")
    if tz_name == "UTC":
        return datetime.now(UTC).date().isoformat()
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name)).date().isoformat()
    except Exception:  # noqa: BLE001
        return datetime.now(UTC).date().isoformat()


def _is_today(ts_str: str) -> bool:
    today = _today_utc_date_str()
    if not ts_str:
        return False
    return str(ts_str).startswith(today)


def _today_realized_pnl_usd() -> float:
    """Sum realized_pnl across all trade closes timestamped today.

    Reads the JSONL fully each call. The file is small (one record per
    closed trade) and typically <1MB, so this is cheap. If it ever
    grows, add a cache+offset.
    """
    if not _TRADE_CLOSES_PATH.exists():
        return 0.0
    total = 0.0
    try:
        with _TRADE_CLOSES_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # close_trade writes top-level fields plus extra={}.
                # Newer records put realized_pnl in extra; older ones
                # in top-level. Read both for backwards compatibility.
                extra = rec.get("extra") or {}
                ts = (
                    rec.get("close_ts")
                    or (extra.get("close_ts") if isinstance(extra, dict) else None)
                    or rec.get("ts") or rec.get("fill_ts") or ""
                )
                if not _is_today(ts):
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
    except OSError as exc:
        logger.debug("killswitch read failed: %s", exc)
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
        return True, (
            f"day_pnl=${today_pnl:+.2f} ≤ limit=${limit_usd:+.2f} "
            f"(date={_today_utc_date_str()})"
        )
    return False, f"day_pnl=${today_pnl:+.2f} (limit=${limit_usd:+.2f})"


def killswitch_status() -> dict:
    """Snapshot for dashboards / heartbeat."""
    tripped, reason = is_killswitch_tripped()
    return {
        "tripped": tripped,
        "reason": reason,
        "limit_usd": _resolve_limit_usd(),
        "today_pnl_usd": _today_realized_pnl_usd(),
        "date": _today_utc_date_str(),
        "disabled": os.getenv("ETA_KILLSWITCH_DISABLED", "").lower() in {"1", "true", "yes", "on"},
    }


if __name__ == "__main__":
    print(json.dumps(killswitch_status(), indent=2))

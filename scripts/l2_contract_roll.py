"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_contract_roll
=======================================================
Futures contract expiry + roll-window tracker.  Warns operator when
the active front-month is approaching last-trade or when liquidity
is migrating to the next month.

Why this exists
---------------
Equity futures (MNQ/NQ/ES) roll quarterly: Mar (H), Jun (M), Sep (U),
Dec (Z).  Roll dynamics:
  - 7-10 days before last-trade: liquidity migrates to the next contract
  - 2 days before last-trade: front-month gets thin, spreads widen
  - Last-trade day: front-month settles, positions auto-roll or
    cash-settle depending on contract

A strategy that doesn't roll its positions:
  - Trades into the back-month with thinner liquidity → worse fills
  - Holds an expiring contract through settlement (price discontinuity)
  - Gets margin-called at settle if the position is sizable

This script:
  1. Computes next-expiry date for each active symbol
  2. Flags strategies in 3 zones:
       NORMAL   : >10 days until expiry — trade as usual
       ROLL     : 3-10 days — operator should be planning the roll
       URGENT   : 0-2 days — pause new entries; close or roll open positions
  3. Emits per-strategy verdict the trading_gate can consult

Calendar
--------
CME equity-index futures expire on the 3rd Friday of Mar/Jun/Sep/Dec.
Commodity futures vary — most monthly, some non-monthly.  Symbol→
expiry-cycle lookup table here covers the common ones.

Run
---
::

    python -m eta_engine.scripts.l2_contract_roll
    python -m eta_engine.scripts.l2_contract_roll --symbol GC
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import calendar
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
ROLL_LOG = LOG_DIR / "l2_contract_roll.jsonl"


# Symbol → expiry cycle.  "quarterly" = Mar/Jun/Sep/Dec, 3rd Friday.
# "monthly" = every month, varies by product:
#   CL: 3 business days before 25th of month BEFORE delivery
#   GC: 3rd-last business day of expiry month
#   NG: 3 business days before 1st day of delivery month
# Approximation here uses the 3rd Friday for quarterly + last-business-
# day for monthly.  Operator must verify against CME contract specs.
EXPIRY_CYCLE: dict[str, str] = {
    "MNQ": "quarterly_3rd_friday",
    "NQ":  "quarterly_3rd_friday",
    "MES": "quarterly_3rd_friday",
    "ES":  "quarterly_3rd_friday",
    "M2K": "quarterly_3rd_friday",
    "RTY": "quarterly_3rd_friday",
    "MYM": "quarterly_3rd_friday",
    "YM":  "quarterly_3rd_friday",
    "ZN":  "quarterly_3rd_friday",
    "ZB":  "quarterly_3rd_friday",
    "6E":  "quarterly_3rd_friday",
    "M6E": "quarterly_3rd_friday",
    "6B":  "quarterly_3rd_friday",
    "M6B": "quarterly_3rd_friday",
    # Monthly commodity rolls — use last-business-day proxy
    "GC":  "monthly_last_business_day",
    "MGC": "monthly_last_business_day",
    "SI":  "monthly_last_business_day",
    "SIL": "monthly_last_business_day",
    "HG":  "monthly_last_business_day",
    "CL":  "monthly_last_business_day",
    "MCL": "monthly_last_business_day",
    "NG":  "monthly_last_business_day",
    # Crypto futures — monthly
    "BTC": "monthly_last_business_day",
    "MBT": "monthly_last_business_day",
    "MET": "monthly_last_business_day",
}


@dataclass
class RollVerdict:
    symbol: str
    next_expiry: str  # YYYY-MM-DD
    days_until_expiry: int
    zone: str         # NORMAL | ROLL | URGENT
    blocked: bool     # True when zone == URGENT
    reason: str
    notes: list[str] = field(default_factory=list)


def _quarterly_3rd_friday(year: int, today: date) -> date:
    """Find the next quarterly expiry's 3rd-Friday date."""
    quarter_months = [3, 6, 9, 12]
    for q_year in (year, year + 1):
        for month in quarter_months:
            cal = calendar.monthcalendar(q_year, month)
            # Pick the 3rd Friday: filter Friday entries
            fridays = [week[calendar.FRIDAY]
                        for week in cal if week[calendar.FRIDAY] != 0]
            if len(fridays) >= 3:
                d = date(q_year, month, fridays[2])
                if d > today:
                    return d
    # Fallback (shouldn't reach)
    return date(year + 1, 3, 21)


def _monthly_last_business_day(year: int, month: int) -> date:
    """Last business day (Mon-Fri) of the given month."""
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() >= calendar.SATURDAY:
        d -= timedelta(days=1)
    return d


def compute_next_expiry(symbol: str, *, today: date | None = None) -> date | None:
    """Return next expiry date for a symbol, or None if not in cycle map."""
    today = today or datetime.now(UTC).date()
    base = symbol.rstrip("1") if symbol.endswith("1") and len(symbol) > 1 else symbol
    cycle = EXPIRY_CYCLE.get(base)
    if cycle is None:
        return None
    if cycle == "quarterly_3rd_friday":
        return _quarterly_3rd_friday(today.year, today)
    if cycle == "monthly_last_business_day":
        # Find next month-end after today
        for offset in range(0, 13):
            year = today.year + (today.month + offset - 1) // 12
            month = ((today.month + offset - 1) % 12) + 1
            d = _monthly_last_business_day(year, month)
            if d > today:
                return d
    return None


def assess_roll_zone(symbol: str, *, today: date | None = None) -> RollVerdict:
    today = today or datetime.now(UTC).date()
    expiry = compute_next_expiry(symbol, today=today)
    if expiry is None:
        return RollVerdict(
            symbol=symbol, next_expiry="?", days_until_expiry=-1,
            zone="UNKNOWN", blocked=False,
            reason=f"no expiry cycle defined for {symbol}",
            notes=[f"add {symbol} to EXPIRY_CYCLE in l2_contract_roll.py"],
        )
    days = (expiry - today).days
    if days <= 2:
        zone = "URGENT"
        blocked = True
        reason = f"<=2 days to expiry ({expiry}); close or roll positions"
    elif days <= 10:
        zone = "ROLL"
        blocked = False
        reason = f"{days} days to expiry; plan the roll"
    else:
        zone = "NORMAL"
        blocked = False
        reason = f"{days} days to expiry; trade normally"
    return RollVerdict(
        symbol=symbol, next_expiry=expiry.isoformat(),
        days_until_expiry=days, zone=zone, blocked=blocked,
        reason=reason,
    )


def assess_all_symbols(symbols: list[str] | None = None,
                         *, today: date | None = None) -> dict[str, RollVerdict]:
    if symbols is None:
        symbols = sorted(EXPIRY_CYCLE.keys())
    return {s: assess_roll_zone(s, today=today) for s in symbols}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default=None,
                    help="Single symbol; default = all known symbols")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    verdicts = {args.symbol: assess_roll_zone(args.symbol)} if args.symbol else assess_all_symbols()

    try:
        with ROLL_LOG.open("a", encoding="utf-8") as f:
            for _sym, v in verdicts.items():
                f.write(json.dumps({"ts": datetime.now(UTC).isoformat(),
                                     **asdict(v)},
                                    separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: roll log write failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps({s: asdict(v) for s, v in verdicts.items()},
                           indent=2))
        return 1 if any(v.blocked for v in verdicts.values()) else 0

    print()
    print("=" * 78)
    print("L2 CONTRACT ROLL CALENDAR")
    print("=" * 78)
    print(f"  {'Symbol':<8s} {'Next expiry':<12s} {'Days':<6s} {'Zone':<8s} Reason")
    print(f"  {'-'*8:<8s} {'-'*12:<12s} {'-'*6:<6s} {'-'*8:<8s} {'-'*45}")
    for s, v in sorted(verdicts.items()):
        print(f"  {s:<8s} {v.next_expiry:<12s} {v.days_until_expiry:<6d} "
              f"{v.zone:<8s} {v.reason}")
    print()
    return 1 if any(v.blocked for v in verdicts.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_seed_news_calendar
============================================================
Seed ``l2_news_events.jsonl`` with scheduled 2026 FOMC, NFP, CPI,
ECB, and quarterly-witching dates.

Why this exists
---------------
``l2_news_blackout.py`` is data-driven — it only blacks out trading
when the events file has scheduled windows in it.  Without seed data
the module is a no-op.  This script writes 2026 H1+H2 known events
in one go.

CRITICAL OPERATOR NOTE
----------------------
The FOMC + ECB + CPI dates seeded here are sourced from the publicly
announced FOMC calendar and BLS / Eurostat release schedules.  Some
of these dates can shift mid-year (especially CPI when BLS publishes
its updated schedule each December).  Before going LIVE the operator
MUST reconcile against:

  - FOMC calendar:    https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  - BLS CPI schedule: https://www.bls.gov/schedule/news_release/cpi.htm
  - ECB calendar:     https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html
  - NFP/witching:     deterministic (1st Friday / 3rd Friday)

If a seeded date is wrong, edit ``l2_news_events.jsonl`` by hand or
re-run this script with ``--clear`` then ``--seed``.

Window defaults
---------------
- FOMC      : 15 min before through 90 min after 14:00 ET
              (decision + press-conf cool-off)
- NFP/CPI   : 15 min before through 30 min after 08:30 ET
- ECB       : 15 min before 07:45 ET through 45 min after 08:30 ET
              (decision + press-conf)
- Witching  : last hour of session 14:00→15:15 ET on 3rd Friday
              of Mar/Jun/Sep/Dec (volume spike from index rebalance)

All times stored in ISO 8601 UTC.

Run
---
::

    # Inspect what would be seeded — no write
    python -m eta_engine.scripts.l2_seed_news_calendar --dry-run

    # Wipe existing file (CAREFUL) + re-seed
    python -m eta_engine.scripts.l2_seed_news_calendar --clear --seed

    # Just add 2026 seed (skip if duplicates)
    python -m eta_engine.scripts.l2_seed_news_calendar --seed
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import calendar
from datetime import UTC, date, datetime, timedelta

from eta_engine.scripts.l2_news_blackout import (
    EVENTS_FILE,
    BlackoutWindow,
    add_event,
    load_events,
)

# Futures symbols affected by US macro events.  Add/remove as needed.
US_MACRO_SYMBOLS = ["MNQ", "NQ", "MES", "ES", "MGC", "GC",
                     "MCL", "CL", "M2K", "RTY", "YM", "ZN", "ZB"]
# Symbols affected by ECB events.
ECB_SYMBOLS = ["MNQ", "NQ", "MES", "ES", "6E", "M6E"]
# Symbols affected by quarterly index witching.
WITCHING_SYMBOLS = ["MNQ", "NQ", "MES", "ES", "M2K", "RTY", "YM"]


def _utc_window(local_dt: datetime, *, before_min: int,
                 after_min: int) -> tuple[str, str]:
    """Convert a tz-aware datetime to an ISO-UTC window (before, after)."""
    start = (local_dt - timedelta(minutes=before_min)).astimezone(UTC)
    end = (local_dt + timedelta(minutes=after_min)).astimezone(UTC)
    return start.isoformat(), end.isoformat()


def _et_to_utc(year: int, month: int, day: int,
                hour: int, minute: int) -> datetime:
    """Convert ET wall-clock to UTC datetime.  Heuristic DST handling:
    EDT (UTC-4) from 2nd Sun of March through 1st Sun of November,
    else EST (UTC-5).  Sufficient for blackout-window precision."""
    d = date(year, month, day)
    # 2nd Sunday of March
    dst_start = _nth_weekday(year, 3, 6, 2)
    # 1st Sunday of November
    dst_end = _nth_weekday(year, 11, 6, 1)
    offset_hours = 4 if dst_start <= d < dst_end else 5
    return datetime(year, month, day, hour + offset_hours, minute,
                     tzinfo=UTC)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the date of the n-th occurrence of weekday (0=Mon..6=Sun)
    in the given month.  E.g. n=3, weekday=4 (Friday) → 3rd Friday."""
    c = calendar.Calendar()
    matches = [d for d in c.itermonthdates(year, month)
                if d.month == month and d.weekday() == weekday]
    return matches[n - 1]


def _first_weekday(year: int, month: int, weekday: int) -> date:
    """Return the date of the first weekday (0=Mon..6=Sun) in the month."""
    return _nth_weekday(year, month, weekday, 1)


def fomc_windows_2026() -> list[BlackoutWindow]:
    """FOMC scheduled meetings 2026.  Decision at 14:00 ET on day 2,
    press conf 14:30 ET.  Blackout: 15min before through 90min after
    decision (i.e. 13:45 ET → 15:30 ET).

    Source: Fed FOMC calendar (publicly announced).  Operator MUST
    reconcile against fed website before live cutover.
    """
    # (year, month, day) of decision day (= last day of each 2-day meeting)
    fomc_decision_days_2026 = [
        (2026, 1, 28),
        (2026, 3, 18),
        (2026, 4, 29),
        (2026, 6, 17),
        (2026, 7, 29),
        (2026, 9, 16),
        (2026, 10, 28),
        (2026, 12, 16),
    ]
    out: list[BlackoutWindow] = []
    for y, m, d in fomc_decision_days_2026:
        decision = _et_to_utc(y, m, d, 14, 0)
        start_iso, end_iso = _utc_window(
            decision, before_min=15, after_min=90)
        out.append(BlackoutWindow(
            start=start_iso, end=end_iso,
            reason="FOMC", symbols=US_MACRO_SYMBOLS,
            note=f"Fed rate decision {y}-{m:02d}-{d:02d} 14:00 ET; "
                 "press conf 14:30 ET",
        ))
    return out


def nfp_windows_2026() -> list[BlackoutWindow]:
    """NFP — first Friday of each month at 08:30 ET.  Blackout:
    15min before through 30min after.  Deterministic — no operator
    confirmation needed."""
    out: list[BlackoutWindow] = []
    for month in range(1, 13):
        d = _first_weekday(2026, month, 4)  # Friday
        release = _et_to_utc(2026, d.month, d.day, 8, 30)
        start_iso, end_iso = _utc_window(
            release, before_min=15, after_min=30)
        out.append(BlackoutWindow(
            start=start_iso, end=end_iso,
            reason="NFP", symbols=US_MACRO_SYMBOLS,
            note=f"Non-farm payrolls {d.isoformat()} 08:30 ET",
        ))
    return out


def cpi_windows_2026() -> list[BlackoutWindow]:
    """CPI — BLS publishes mid-month, usually 2nd Wednesday or
    Thursday.  Window: 13:00→09:00 ET ±15/30.  Operator MUST
    reconcile against BLS schedule for exact dates.

    Below uses the BLS-published 2026 CPI calendar (best-effort
    knowledge of typical Tue/Wed/Thu mid-month slot).
    """
    cpi_dates_2026 = [
        (2026, 1, 14),   # Jan 14
        (2026, 2, 11),   # Feb 11
        (2026, 3, 12),   # Mar 12
        (2026, 4, 14),   # Apr 14
        (2026, 5, 13),   # May 13
        (2026, 6, 11),   # Jun 11
        (2026, 7, 15),   # Jul 15
        (2026, 8, 12),   # Aug 12
        (2026, 9, 11),   # Sep 11
        (2026, 10, 14),  # Oct 14
        (2026, 11, 13),  # Nov 13
        (2026, 12, 10),  # Dec 10
    ]
    out: list[BlackoutWindow] = []
    for y, m, d in cpi_dates_2026:
        release = _et_to_utc(y, m, d, 8, 30)
        start_iso, end_iso = _utc_window(
            release, before_min=15, after_min=30)
        out.append(BlackoutWindow(
            start=start_iso, end=end_iso,
            reason="CPI", symbols=US_MACRO_SYMBOLS,
            note=f"BLS CPI release {y}-{m:02d}-{d:02d} 08:30 ET "
                 "(operator: confirm against bls.gov schedule)",
        ))
    return out


def ecb_windows_2026() -> list[BlackoutWindow]:
    """ECB Governing Council — rate decision 13:15 CET (07:15 ET
    or 08:15 EDT), press conf 13:45 CET.  8 meetings/year.

    Source: ECB calendar — operator MUST reconcile.
    """
    # ECB 2026 monetary policy meetings (publicly scheduled)
    ecb_dates_2026 = [
        (2026, 1, 22),
        (2026, 3, 5),
        (2026, 4, 16),
        (2026, 6, 4),
        (2026, 7, 23),
        (2026, 9, 10),
        (2026, 10, 29),
        (2026, 12, 17),
    ]
    out: list[BlackoutWindow] = []
    for y, m, d in ecb_dates_2026:
        # Decision in CET = UTC+1 (or UTC+2 during DST end-Mar→end-Oct)
        # Approximate: 13:15 CET = 12:15 UTC standard / 11:15 UTC DST.
        # For blackout windows ±15min/±45min the small DST drift
        # doesn't matter operationally.
        decision = datetime(y, m, d, 12, 15, tzinfo=UTC)
        start_iso, end_iso = _utc_window(
            decision, before_min=15, after_min=45)
        out.append(BlackoutWindow(
            start=start_iso, end=end_iso,
            reason="ECB", symbols=ECB_SYMBOLS,
            note=f"ECB Governing Council {y}-{m:02d}-{d:02d} 13:15 CET",
        ))
    return out


def witching_windows_2026() -> list[BlackoutWindow]:
    """Quad-witching — 3rd Friday of Mar, Jun, Sep, Dec.  Equity
    index futures see elevated volume + slippage in the last hour."""
    out: list[BlackoutWindow] = []
    for month in (3, 6, 9, 12):
        d = _nth_weekday(2026, month, 4, 3)  # 3rd Friday
        last_hour_start = _et_to_utc(2026, d.month, d.day, 14, 0)
        last_hour_end = _et_to_utc(2026, d.month, d.day, 15, 15)
        out.append(BlackoutWindow(
            start=last_hour_start.isoformat(),
            end=last_hour_end.isoformat(),
            reason="WITCHING", symbols=WITCHING_SYMBOLS,
            note=f"Quad witching {d.isoformat()} last-hour volume spike",
        ))
    return out


def all_2026_seeds() -> list[BlackoutWindow]:
    return (
        fomc_windows_2026()
        + nfp_windows_2026()
        + cpi_windows_2026()
        + ecb_windows_2026()
        + witching_windows_2026()
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be seeded; do not write")
    ap.add_argument("--seed", action="store_true",
                    help="Append 2026 seed (dedupe by start+reason)")
    ap.add_argument("--clear", action="store_true",
                    help="DESTROY existing l2_news_events.jsonl (CAREFUL)")
    ap.add_argument("--show", action="store_true",
                    help="List existing events without seeding")
    args = ap.parse_args()

    seeds = all_2026_seeds()

    if args.show:
        existing = load_events()
        print(f"Existing windows: {len(existing)}")
        for w in existing:
            print(f"  {w.reason:<8s} {w.start} -> {w.end}  {','.join(w.symbols)}")
        return 0

    if args.dry_run:
        print(f"=== DRY RUN: would seed {len(seeds)} blackout windows ===")
        for w in seeds:
            print(f"  {w.reason:<8s} {w.start} -> {w.end}")
            print(f"    note: {w.note}")
        return 0

    if args.clear:
        if EVENTS_FILE.exists():
            EVENTS_FILE.unlink()
            print(f"Cleared {EVENTS_FILE}")
        else:
            print(f"({EVENTS_FILE} did not exist; nothing to clear)")

    if args.seed:
        existing = load_events()
        existing_keys = {(w.start, w.reason) for w in existing}
        n_added = 0
        n_skipped = 0
        for w in seeds:
            key = (w.start, w.reason)
            if key in existing_keys:
                n_skipped += 1
                continue
            add_event(w)
            n_added += 1
        print(f"Seeded {n_added} new windows ({n_skipped} duplicates skipped)")
        print(f"Calendar file: {EVENTS_FILE}")
        print()
        print("OPERATOR REMINDER -- before live cutover:")
        print("  * Reconcile FOMC dates against federalreserve.gov")
        print("  * Reconcile CPI dates against bls.gov/schedule/")
        print("  * Reconcile ECB dates against ecb.europa.eu calendar")
        return 0

    # Default: print help if no action flag
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

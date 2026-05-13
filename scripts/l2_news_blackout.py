"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_news_blackout
=======================================================
Pre/post news-event trading pause windows for high-volatility
scheduled events.

Why this exists
---------------
Strategies optimized in normal regimes can blow up on FOMC, NFP,
CPI, OPEC, and earnings.  Spread_regime_filter catches the regime
shift AFTER it happens — by then the damage is done.

This module is the BEFORE side: blacklist trading windows that the
operator knows are dangerous, regardless of what real-time regime
detection says.

Event types
-----------
- FOMC (8 per year): 14:00 ET rate decision + 14:30 ET press conf
- NFP (1st Friday): 08:30 ET
- CPI (mid-month): 08:30 ET
- ECB (12 per year): 07:45 ET decision + 08:30 ET press conf
- OPEC (irregular): varies
- Index rebalances (quarterly)
- Triple/quad witching (3rd Friday of Mar/Jun/Sep/Dec, last hour)

Window defaults
---------------
- Before:  15 min lead-in (start blackout)
- After:   30 min cool-off (end blackout)
These can be tightened per event type.

Mechanic
--------
Each blackout entry is a (start_ts, end_ts, reason, affected_symbols).
``is_in_blackout(symbol, when)`` returns True if any window covers
the symbol at the given time.

This module is data-only — operator (or a daily cron) populates the
events.jsonl file with upcoming scheduled events.

Run
---
::

    # Check if MNQ is currently in a blackout
    python -m eta_engine.scripts.l2_news_blackout --check MNQ

    # Add a scheduled FOMC blackout
    python -m eta_engine.scripts.l2_news_blackout --add \\
        --start "2026-05-15T18:00:00+00:00" \\
        --end   "2026-05-15T19:30:00+00:00" \\
        --reason FOMC --symbols MNQ NQ ES MES

    # List all upcoming blackouts
    python -m eta_engine.scripts.l2_news_blackout --list
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
EVENTS_FILE = LOG_DIR / "l2_news_events.jsonl"


@dataclass
class BlackoutWindow:
    start: str  # ISO 8601 UTC
    end: str  # ISO 8601 UTC
    reason: str  # "FOMC" | "NFP" | "CPI" | ...
    symbols: list[str]  # symbols affected; ["*"] for all
    note: str = ""


@dataclass
class BlackoutCheck:
    symbol: str
    when: str
    in_blackout: bool
    reason: str | None = None
    until: str | None = None
    affected_by: list[str] = field(default_factory=list)


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def load_events(*, _path: Path | None = None) -> list[BlackoutWindow]:
    path = _path if _path is not None else EVENTS_FILE
    if not path.exists():
        return []
    out: list[BlackoutWindow] = []
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
                try:
                    out.append(BlackoutWindow(**rec))
                except TypeError:
                    continue
    except OSError:
        return []
    return out


def add_event(window: BlackoutWindow, *, _path: Path | None = None) -> None:
    path = _path if _path is not None else EVENTS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(window), separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: blackout add failed: {e}", file=sys.stderr)


def is_in_blackout(symbol: str, when: datetime | None = None, *, _path: Path | None = None) -> BlackoutCheck:
    when = when or datetime.now(UTC)
    when_iso = when.isoformat()
    windows = load_events(_path=_path)
    affecting: list[BlackoutWindow] = []
    for w in windows:
        start = _parse_ts(w.start)
        end = _parse_ts(w.end)
        if start is None or end is None:
            continue
        if start <= when <= end and ("*" in w.symbols or symbol in w.symbols):
            affecting.append(w)
    if not affecting:
        return BlackoutCheck(symbol=symbol, when=when_iso, in_blackout=False)
    # Pick the latest end-time across affecting windows
    latest_end = max(_parse_ts(w.end) for w in affecting if _parse_ts(w.end))
    return BlackoutCheck(
        symbol=symbol,
        when=when_iso,
        in_blackout=True,
        reason=", ".join(w.reason for w in affecting),
        until=latest_end.isoformat() if latest_end else None,
        affected_by=[w.reason for w in affecting],
    )


def list_upcoming(*, after: datetime | None = None, _path: Path | None = None) -> list[BlackoutWindow]:
    after = after or datetime.now(UTC)
    windows = load_events(_path=_path)
    upcoming = []
    for w in windows:
        end = _parse_ts(w.end)
        if end and end >= after:
            upcoming.append(w)
    upcoming.sort(key=lambda w: w.start)
    return upcoming


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", metavar="SYMBOL", help="Check if SYMBOL is currently in a blackout")
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--start", default=None, help="ISO 8601 UTC; required for --add")
    ap.add_argument("--end", default=None, help="ISO 8601 UTC; required for --add")
    ap.add_argument("--reason", default=None, help="event tag (FOMC/NFP/CPI/...)")
    ap.add_argument("--symbols", nargs="+", default=None, help="symbols affected; or '*' for all")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.check:
        result = is_in_blackout(args.check)
        if args.json:
            print(json.dumps(asdict(result), indent=2))
            return 1 if result.in_blackout else 0
        if result.in_blackout:
            print(f"[BLACKOUT] {args.check} blocked: {result.reason} (until {result.until})")
            return 1
        print(f"[CLEAR] {args.check} not in any blackout window")
        return 0

    if args.add:
        if not all([args.start, args.end, args.reason, args.symbols]):
            print("--add requires --start --end --reason --symbols", file=sys.stderr)
            return 2
        add_event(
            BlackoutWindow(
                start=args.start,
                end=args.end,
                reason=args.reason,
                symbols=args.symbols,
            )
        )
        print(f"Added blackout: {args.reason} {args.start}→{args.end} for {args.symbols}")
        return 0

    # Default: list
    upcoming = list_upcoming()
    if args.json:
        print(json.dumps([asdict(w) for w in upcoming], indent=2))
        return 0
    print()
    print("=" * 78)
    print("L2 NEWS BLACKOUT CALENDAR (upcoming)")
    print("=" * 78)
    if not upcoming:
        print("  (no scheduled blackouts)")
        return 0
    for w in upcoming:
        print(f"  {w.reason:<8s}  {w.start}  →  {w.end}  symbols={','.join(w.symbols)}")
        if w.note:
            print(f"    note: {w.note}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

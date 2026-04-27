"""Stuck-killswitch detector for eta_engine.

Reads ``docs/alerts_log.jsonl`` and detects the silent-failure mode
where a kill-switch fired hours/days ago and trading never resumed.
Without this, a stuck killswitch silently zeros every ``risk_mult``
forever and the operator only finds out from the absence of trades.

Detection logic
---------------
1. Find the most recent record with ``event == "kill_switch"``.
2. Find the most recent record with ``event in {"runtime_start",
   "runtime_resume"}`` AFTER that kill timestamp.
3. If no resume-style event has fired since the kill:

     hours_since_kill > 4   AND  weekday  -> YELLOW
     hours_since_kill > 24                -> RED
     hours_since_kill > 96                -> CRITICAL

The thresholds reflect the operator's own re-arm cadence: under normal
conditions, a kill triggered intra-day is followed by a manual re-arm
within hours. A multi-day quiet stretch is the signature of forgotten
state.

Output
------
* Exit code 0 = green (kill is fresh and resume happened, OR no kill
  in the file)
* Exit code 1 = YELLOW (4h+ on weekday, no resume)
* Exit code 2 = RED (24h+, no resume)
* Exit code 3 = CRITICAL (96h+, no resume)
* Exit code 9 = malformed alerts_log

A one-line diagnostic is always printed to stdout. The log is NEVER
modified by this script.

Why this exists
---------------
The HIGH_VOL OOS work hardened risk_mult against false positives. This
script hardens the OPPOSITE direction -- it watches for risk_mult
silently stuck at zero from a stale kill state.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "docs" / "alerts_log.jsonl"

KILL_EVENTS = {"kill_switch"}
RESUME_EVENTS = {"runtime_start", "runtime_resume", "kill_clear", "rearm"}


def _load_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, TypeError):
            # Skip malformed lines but keep parsing.
            continue
    return out


def _latest_with_event(records: list[dict], events: set[str]) -> dict | None:
    """Return the latest record whose 'event' is in `events`."""
    matches = [r for r in records if r.get("event") in events]
    if not matches:
        return None
    matches.sort(key=lambda r: float(r.get("ts", 0)))
    return matches[-1]


def _classify(hours_since_kill: float, *, is_weekday: bool) -> tuple[str, int]:
    """Return (level, exit_code)."""
    if hours_since_kill > 96:
        return "CRITICAL", 3
    if hours_since_kill > 24:
        return "RED", 2
    if hours_since_kill > 4 and is_weekday:
        return "YELLOW", 1
    return "GREEN", 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help=f"path to alerts log (default: {DEFAULT_LOG})",
    )
    p.add_argument(
        "--now-utc",
        type=float,
        default=None,
        help="override 'now' as a unix timestamp (for testing)",
    )
    args = p.parse_args(argv)

    records = _load_records(args.log)
    if not records:
        print(f"kill-switch-drift: GREEN -- no alerts log entries at {args.log}")
        return 0

    latest_kill = _latest_with_event(records, KILL_EVENTS)
    if latest_kill is None:
        print("kill-switch-drift: GREEN -- no kill_switch events in log")
        return 0

    kill_ts = float(latest_kill.get("ts", 0))
    latest_resume = _latest_with_event(records, RESUME_EVENTS)
    resume_ts = float(latest_resume["ts"]) if latest_resume else 0.0

    now_ts = args.now_utc if args.now_utc is not None else datetime.now(UTC).timestamp()
    hours_since_kill = (now_ts - kill_ts) / 3600.0

    if resume_ts > kill_ts:
        # Bot resumed after the kill -- not stuck.
        print(
            f"kill-switch-drift: GREEN -- last kill at {datetime.fromtimestamp(kill_ts, UTC).isoformat()}, "
            f"resumed at {datetime.fromtimestamp(resume_ts, UTC).isoformat()}",
        )
        return 0

    is_weekday = datetime.fromtimestamp(now_ts, UTC).weekday() < 5
    level, code = _classify(hours_since_kill, is_weekday=is_weekday)

    kill_reason = (
        latest_kill.get("payload", {}).get("reason")
        or latest_kill.get("payload", {}).get("verdict", {}).get("reason")
        or "<unknown>"
    )
    print(
        f"kill-switch-drift: {level} -- "
        f"{hours_since_kill:.1f}h since kill (reason: {kill_reason!r}), "
        f"no resume event since",
    )
    return code


if __name__ == "__main__":
    sys.exit(main())

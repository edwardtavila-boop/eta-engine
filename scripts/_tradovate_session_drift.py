"""Tradovate OAuth session keep-alive monitor.

Reads ``docs/tradovate_auth_status.json`` (overwritten by
``scripts/authorize_tradovate.py`` on every auth attempt) and surfaces:

1. **Expired or near-expired access token** -- token_expires_at is in
   the past, or within ``--warn-h`` hours.

2. **Auth never succeeded** -- ``result`` is not ``AUTHORIZED``.
   ``STUBBED`` (creds missing) is YELLOW; ``FAILED`` (creds present but
   API call broken) is RED.

3. **Stale auth report** -- the status file itself hasn't been
   regenerated within ``--stale-h`` hours, meaning the upstream auth
   keep-alive script isn't running on schedule.

This script does NOT attempt to refresh -- that is
``authorize_tradovate.py``'s responsibility, and the cloud trigger that
calls THIS script should chain a refresh-attempt step BEFORE invoking
this monitor when refresh is wanted.

Exit codes
----------
0  GREEN -- token healthy, fresh status file, AUTHORIZED
1  YELLOW -- creds missing (STUBBED), or stale report, or token < warn-h
2  RED -- token expired, FAILED auth, or report > 2*stale-h hours old
9  data missing (no status file)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATUS = ROOT / "docs" / "tradovate_auth_status.json"


def _parse_iso(s: str) -> float | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _classify_result(result: str) -> tuple[str, str]:
    if result == "AUTHORIZED":
        return ("GREEN", "AUTHORIZED")
    if result == "STUBBED":
        return ("YELLOW", "STUBBED -- creds missing, populate keyring/.env")
    if result == "FAILED":
        return ("RED", "FAILED -- creds present but auth call broke")
    return ("RED", f"unknown result: {result!r}")


def _classify_expiry(expires_ts: float | None, now_ts: float, warn_h: float) -> tuple[str, str]:
    if expires_ts is None:
        return ("RED", "token_expires_at missing or unparseable")
    delta_h = (expires_ts - now_ts) / 3600.0
    if delta_h <= 0:
        return ("RED", f"token expired {-delta_h:.1f}h ago")
    if delta_h <= warn_h:
        return ("YELLOW", f"token expires in {delta_h:.1f}h (warn cap {warn_h:.0f}h)")
    return ("GREEN", f"token expires in {delta_h:.1f}h")


def _classify_freshness(report_mtime: float, now_ts: float, stale_h: float) -> tuple[str, str]:
    age_h = (now_ts - report_mtime) / 3600.0
    if age_h > stale_h * 2:
        return (
            "RED",
            f"auth report {age_h:.1f}h old (>{stale_h * 2:.0f}h) -- keep-alive dead",
        )
    if age_h > stale_h:
        return ("YELLOW", f"auth report {age_h:.1f}h old (>{stale_h:.0f}h)")
    return ("GREEN", f"auth report {age_h:.1f}h old")


def _severity(level: str) -> int:
    return {"GREEN": 0, "YELLOW": 1, "RED": 2}.get(level, 0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    p.add_argument("--warn-h", type=float, default=6.0)
    p.add_argument("--stale-h", type=float, default=8.0)
    p.add_argument("--now-utc", type=float, default=None, help="override 'now' for testing")
    args = p.parse_args(argv)

    if not args.status.exists():
        print(f"tradovate-session: data-missing -- {args.status} not found")
        return 9

    try:
        data = json.loads(args.status.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        print(f"tradovate-session: data-missing -- parse failed: {e}")
        return 9

    now_ts = args.now_utc if args.now_utc is not None else datetime.now(UTC).timestamp()
    expires_ts = _parse_iso(data.get("token_expires_at", ""))

    result_lvl, result_msg = _classify_result(data.get("result", ""))
    expiry_lvl, expiry_msg = _classify_expiry(expires_ts, now_ts, args.warn_h)
    fresh_lvl, fresh_msg = _classify_freshness(args.status.stat().st_mtime, now_ts, args.stale_h)

    # If creds are missing (STUBBED), expiry doesn't matter -- the token is fake.
    if result_lvl == "YELLOW" and result_msg.startswith("STUBBED"):
        expiry_lvl, expiry_msg = ("GREEN", "expiry moot for STUBBED auth")

    levels = [result_lvl, expiry_lvl, fresh_lvl]
    overall = max(levels, key=_severity)
    code = _severity(overall)

    print(f"tradovate-session: {overall} -- demo={data.get('demo', '?')}")
    print(f"  [{result_lvl:6}] result: {result_msg}")
    print(f"  [{expiry_lvl:6}] expiry: {expiry_msg}")
    print(f"  [{fresh_lvl:6}] freshness: {fresh_msg}")
    return code


if __name__ == "__main__":
    sys.exit(main())

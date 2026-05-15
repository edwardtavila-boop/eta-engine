"""
EVOLUTIONARY TRADING ALGO  //  scripts.capture_health_monitor
=============================================================
Verifies the Phase-1 tick + depth capture daemons are alive and
producing data.  Read-only audit -- runs daily via cloud cron.

Why this exists
---------------
``capture_tick_stream.py`` and ``capture_depth_snapshots.py`` run as
long-lived processes on the VPS (next to the TWS Gateway).  If
either crashes silently, every day uncaptured is irrecoverable
history loss.  This monitor checks:

1. Today's tick file exists for every expected symbol AND its
   mtime advanced in the last 30 minutes.
2. Today's depth file exists AND its mtime advanced in the last
   5 minutes (depth snapshots are 1Hz so the file should be very
   fresh during market hours).
3. Yesterday's files are non-trivially sized (>10KB for ticks,
   >1MB for depth) -- sanity check that capture wasn't bare-token.
4. Subscription verifier (``verify_ibkr_subscriptions.py``) ran in
   the last 24h and reported all-realtime.

Output
------
* JSONL append to logs/eta_engine/capture_health.jsonl
* Optional alert append to logs/eta_engine/alerts_log.jsonl when
  capture is stalled or yesterday's file is suspiciously small.

Run
---
::

    python -m eta_engine.scripts.capture_health_monitor
    python -m eta_engine.scripts.capture_health_monitor --json
    python -m eta_engine.scripts.capture_health_monitor \
        --symbols MNQ NQ M2K 6E MCL MYM NG MBT
    python -m eta_engine.scripts.capture_health_monitor \
        --tick-symbols MNQ NQ M2K 6E MCL MYM NG MBT --depth-symbols MNQ NQ ES M2K MYM 6E MBT
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR = ROOT.parent / "var" / "eta_engine" / "state"
TICKS_DIR = ROOT.parent / "mnq_data" / "ticks"
DEPTH_DIR = ROOT.parent / "mnq_data" / "depth"
HEALTH_LOG = LOG_DIR / "capture_health.jsonl"
ALERT_LOG = LOG_DIR / "alerts_log.jsonl"
SUB_STATUS_LOG = LOG_DIR / "ibkr_subscription_status.jsonl"
TICK_STATUS_FILE = STATE_DIR / "capture_tick_status.json"

DEFAULT_TICK_SYMBOLS = ["MNQ", "NQ", "M2K", "6E", "MCL", "MYM", "NG", "MBT"]
DEFAULT_DEPTH_SYMBOLS = ["MNQ", "NQ", "ES", "M2K", "MYM", "6E", "MBT"]

TICK_STALE_SECONDS = 30 * 60  # ticks should land within 30min during RTH
DEPTH_STALE_SECONDS = 5 * 60  # depth snapshots are 1Hz, very fresh expected
TICK_MIN_SIZE_BYTES = 10_000  # ~10KB minimum for a full RTH session
DEPTH_MIN_SIZE_BYTES = 1_000_000  # ~1MB minimum (1Hz snapshots add up)
SUB_AUDIT_MAX_AGE_HOURS = 24
DEPTH_MIN_LEVELS_PER_SIDE = 3


def _check_capture_file(d: Path, symbol: str, today: date, stale_seconds: int, min_size_bytes: int) -> dict:
    """Return health status for one symbol's capture file today + yesterday."""
    # File naming: <SYMBOL>_<YYYYMMDD>.jsonl
    today_path = d / f"{symbol}_{today.strftime('%Y%m%d')}.jsonl"
    yest = today - timedelta(days=1)
    yest_path = d / f"{symbol}_{yest.strftime('%Y%m%d')}.jsonl"

    out = {"symbol": symbol, "dir": str(d.name)}
    now_utc = datetime.now(UTC).timestamp()

    # Today's file
    if not today_path.exists():
        out["today_status"] = "MISSING"
        out["today_path"] = str(today_path)
    else:
        size = today_path.stat().st_size
        mtime_age = now_utc - today_path.stat().st_mtime
        out["today_size_bytes"] = size
        out["today_mtime_age_seconds"] = round(mtime_age, 1)
        out["today_status"] = "STALE" if mtime_age > stale_seconds else "FRESH"

    # Yesterday's file (sanity check on prior day's full session)
    if not yest_path.exists():
        out["yesterday_status"] = "MISSING"
    else:
        size = yest_path.stat().st_size
        out["yesterday_size_bytes"] = size
        out["yesterday_status"] = "TOO_SMALL" if size < min_size_bytes else "OK"

    return out


def _read_last_jsonl_record(path: Path) -> dict | None:
    """Read the last non-empty JSON object from a JSONL file without loading the full file."""
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size <= 0:
                return None
            tail_bytes = min(size, 64 * 1024)
            f.seek(-tail_bytes, 2)
            chunk = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _check_depth_book_quality(symbol: str, today: date) -> dict:
    """Inspect the latest depth snapshot for empty/thin books."""
    today_path = DEPTH_DIR / f"{symbol}_{today.strftime('%Y%m%d')}.jsonl"
    if not today_path.exists():
        return {}
    latest = _read_last_jsonl_record(today_path)
    if latest is None:
        return {"today_book_status": "PARSE_ERROR"}
    bids = latest.get("bids") if isinstance(latest.get("bids"), list) else []
    asks = latest.get("asks") if isinstance(latest.get("asks"), list) else []
    bid_levels = len(bids)
    ask_levels = len(asks)
    if bid_levels == 0 and ask_levels == 0:
        status = "EMPTY_BOOK"
    elif bid_levels < DEPTH_MIN_LEVELS_PER_SIDE or ask_levels < DEPTH_MIN_LEVELS_PER_SIDE:
        status = "THIN_BOOK"
    else:
        status = "OK"
    return {
        "today_book_status": status,
        "today_bid_levels": bid_levels,
        "today_ask_levels": ask_levels,
        "today_snapshot_ts": latest.get("ts"),
    }


def _check_tick_daemon_status() -> dict:
    """Read the daemon-side blocker surface written by capture_tick_stream."""
    if not TICK_STATUS_FILE.exists():
        return {"status": "NEVER_WRITTEN"}
    try:
        payload = json.loads(TICK_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "PARSE_ERROR"}
    return payload if isinstance(payload, dict) else {"status": "PARSE_ERROR"}


def _check_subscription_audit_age() -> dict:
    """Look at the most recent line of ibkr_subscription_status.jsonl."""
    if not SUB_STATUS_LOG.exists():
        return {"status": "NEVER_RUN", "note": "verify_ibkr_subscriptions has never run"}
    try:
        with SUB_STATUS_LOG.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return {"status": "READ_ERROR"}
    if not lines:
        return {"status": "EMPTY"}
    try:
        last = json.loads(lines[-1])
    except json.JSONDecodeError:
        return {"status": "PARSE_ERROR"}
    last_ts = last.get("ts")
    if not last_ts:
        return {"status": "NO_TS"}
    try:
        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
    except ValueError:
        return {"status": "BAD_TS"}
    age_h = (datetime.now(UTC) - last_dt).total_seconds() / 3600
    return {
        "status": "STALE" if age_h > SUB_AUDIT_MAX_AGE_HOURS else "FRESH",
        "age_hours": round(age_h, 1),
        "all_realtime": bool(last.get("all_realtime")),
        "last_ts": last_ts,
    }


def _emit_alert(level: str, message: str, payload: dict) -> None:
    record = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "source": "capture_health_monitor",
        "level": level,
        "message": message,
        "payload": payload,
    }
    try:
        with ALERT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as e:
        # D6: surface to stderr — silent swallow meant disk-full
        # incidents went un-recorded when the alert log itself
        # couldn't be written.
        print(f"capture_health_monitor WARN: could not append alert to {ALERT_LOG}: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+", default=None, help="legacy override: check same symbols for ticks/depth")
    ap.add_argument("--tick-symbols", nargs="+", default=None, help="tick symbols to check")
    ap.add_argument("--depth-symbols", nargs="+", default=None, help="depth symbols to check")
    ap.add_argument("--json", action="store_true", help="JSON output (machine-readable)")
    args = ap.parse_args()
    tick_symbols = list(args.symbols or args.tick_symbols or DEFAULT_TICK_SYMBOLS)
    depth_symbols = list(args.symbols or args.depth_symbols or DEFAULT_DEPTH_SYMBOLS)

    today = datetime.now(UTC).date()
    tick_results: list[dict] = []
    depth_results: list[dict] = []
    for sym in tick_symbols:
        tick_results.append(_check_capture_file(TICKS_DIR, sym, today, TICK_STALE_SECONDS, TICK_MIN_SIZE_BYTES))
    for sym in depth_symbols:
        depth_entry = _check_capture_file(DEPTH_DIR, sym, today, DEPTH_STALE_SECONDS, DEPTH_MIN_SIZE_BYTES)
        depth_entry.update(_check_depth_book_quality(sym, today))
        depth_results.append(depth_entry)

    sub_audit = _check_subscription_audit_age()
    tick_daemon = _check_tick_daemon_status()
    tick_blocker = tick_daemon.get("status") == "BLOCKED"

    # Roll-up verdict
    issues: list[str] = []
    for tr in tick_results:
        if tr.get("today_status") in {"MISSING", "STALE"} and not tick_blocker:
            issues.append(f"ticks {tr['symbol']}: {tr.get('today_status')}")
        if tr.get("yesterday_status") == "TOO_SMALL":
            issues.append(f"ticks {tr['symbol']}: yesterday too small")
    for dr in depth_results:
        if dr.get("today_status") in {"MISSING", "STALE"}:
            issues.append(f"depth {dr['symbol']}: {dr.get('today_status')}")
        if dr.get("today_book_status") in {"EMPTY_BOOK", "THIN_BOOK", "PARSE_ERROR"}:
            if dr.get("today_book_status") == "PARSE_ERROR":
                issues.append(f"depth {dr['symbol']}: PARSE_ERROR")
            else:
                issues.append(
                    f"depth {dr['symbol']}: {dr.get('today_book_status')} "
                    f"({dr.get('today_bid_levels', 0)}x{dr.get('today_ask_levels', 0)} levels)"
                )
        if dr.get("yesterday_status") == "TOO_SMALL":
            issues.append(f"depth {dr['symbol']}: yesterday too small")
    if sub_audit.get("status") == "STALE":
        issues.append(f"sub audit stale ({sub_audit.get('age_hours')}h old)")
    if sub_audit.get("status") == "NEVER_RUN":
        issues.append("sub audit never run")
    if sub_audit.get("status") == "FRESH" and not sub_audit.get("all_realtime"):
        issues.append("sub audit FAIL -- at least one exchange not realtime")
    if tick_blocker:
        blocker = tick_daemon.get("blocked_reason") or {}
        summary = blocker.get("summary") or tick_daemon.get("note") or "tick daemon blocked"
        if blocker.get("code"):
            issues.append(f"tick daemon blocked: Error {blocker['code']} -- {summary}")
        else:
            issues.append(f"tick daemon blocked: {summary}")

    verdict = "GREEN" if not issues else ("RED" if tick_blocker or any("MISSING" in i for i in issues) else "YELLOW")

    digest = {
        "ts": datetime.now(UTC).isoformat(),
        "today": str(today),
        "n_symbols": len(set(tick_symbols) | set(depth_symbols)),
        "tick_symbols": tick_symbols,
        "depth_symbols": depth_symbols,
        "verdict": verdict,
        "issues": issues,
        "ticks": tick_results,
        "depth": depth_results,
        "tick_daemon": tick_daemon,
        "subscription_audit": sub_audit,
    }
    try:
        with HEALTH_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(digest, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"capture_health_monitor WARN: could not append digest to {HEALTH_LOG}: {e}", file=sys.stderr)

    if verdict != "GREEN":
        _emit_alert(verdict, f"capture health {verdict}: {len(issues)} issue(s)", digest)

    if args.json:
        print(json.dumps(digest, indent=2))
    else:
        print(f"capture-health: {verdict}  ({len(issues)} issues)")
        for i in issues:
            print(f"  - {i}")
        if not issues:
            print("  all symbols capturing freshly; subscription audit current")

    return 0 if verdict == "GREEN" else (1 if verdict == "YELLOW" else 2)


if __name__ == "__main__":
    raise SystemExit(main())

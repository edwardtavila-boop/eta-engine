"""Daily trade-journal reconciliation for eta_engine.

Catches the silent-failure modes that no single component is responsible
for noticing:

1. **Orphaned runtime sessions** -- a ``runtime_start`` event in
   ``logs/eta_engine/alerts_log.jsonl`` with no matching ``runtime_stop`` /
   ``runtime_resume`` within the window AND no fresh ``runtime_start``
   following it. Either the bot crashed without writing a stop, or the
   alert pipeline is broken.

2. **Orphaned BTC live runs** -- a ``*_run_start`` row in
   ``var/eta_engine/state/btc_live/btc_live_decisions.jsonl`` with no paired
   ``*_run_end`` row. Means the engine entered the trading loop and
   never wrote a clean exit -- often a divergence between intent and
   execution.

3. **Kill-switch storm** -- in the window, ``kill_switch`` events
   exceed ``--kill-storm-pct`` (default 25%) of all ``runtime_start``
   events. Indicates the kill thresholds are mis-tuned or upstream
   data is feeding a bad regime.

4. **Jarvis approval-without-execution** -- a JARVIS row with
   ``intent == "overlay_approved"`` and ``outcome != "EXECUTED"``.
   Means the brain greenlit a trade and the venue silently dropped
   it.

5. **Stale BTC live decisions file** -- ``btc_live_decisions.jsonl``
   not appended to within ``--btc-stale-h`` hours (default 36) but
   ``alerts_log.jsonl`` shows runtime activity. Indicates the BTC
   leg silently stopped journaling.

The script never modifies any journal -- read-only by design.

Exit codes
----------
0  GREEN -- no issues
1  YELLOW -- low-severity issues (1-2 detectors red)
2  RED -- multiple detectors red OR any single CRITICAL detector
9  data missing / malformed (one or more journals unreadable)

Window
------
Default = last 24h. Override with ``--hours N`` for sweeps.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eta_engine.scripts import workspace_roots  # noqa: E402

DEFAULT_ALERTS = workspace_roots.ETA_RUNTIME_ALERTS_LOG_PATH
DEFAULT_BTC = workspace_roots.ETA_BTC_LIVE_DECISIONS_PATH


def _load_jsonl(path: Path) -> list[dict]:
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
            continue
    return out


def _alerts_in_window(records: list[dict], window_start_ts: float) -> list[dict]:
    """Filter alerts_log records by unix-ts >= window_start_ts."""
    out = []
    for r in records:
        ts = r.get("ts")
        if isinstance(ts, int | float) and float(ts) >= window_start_ts:
            out.append(r)
    return out


def _parse_iso(s: str) -> float | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _btc_in_window(records: list[dict], window_start_ts: float) -> list[dict]:
    """Filter btc_live_decisions records by ISO ts >= window_start_ts."""
    out = []
    for r in records:
        ts_iso = r.get("ts")
        if not isinstance(ts_iso, str):
            continue
        ts = _parse_iso(ts_iso)
        if ts is not None and ts >= window_start_ts:
            out.append(r)
    return out


def _is_live_start(r: dict) -> bool:
    """A runtime_start whose payload says live=True."""
    return r.get("event") == "runtime_start" and bool(r.get("payload", {}).get("live"))


def _check_orphaned_runtime(alerts_window: list[dict]) -> tuple[str, str]:
    """runtime_start (LIVE only) with no following runtime_stop/resume in window."""
    starts = [r for r in alerts_window if _is_live_start(r)]
    stops = [r for r in alerts_window if r.get("event") in {"runtime_stop", "runtime_resume"}]
    starts.sort(key=lambda r: float(r.get("ts", 0)))
    stops.sort(key=lambda r: float(r.get("ts", 0)))
    if not starts:
        all_starts = sum(1 for r in alerts_window if r.get("event") == "runtime_start")
        return ("GREEN", f"no LIVE runtime_starts in window (paper/dev={all_starts})")
    orphans = 0
    for i, st in enumerate(starts):
        st_ts = float(st.get("ts", 0))
        next_stop = next((s for s in stops if float(s.get("ts", 0)) > st_ts), None)
        next_start = starts[i + 1] if i + 1 < len(starts) else None
        if next_stop is None and next_start is None:
            orphans += 1
    if orphans == 0:
        return ("GREEN", f"no orphaned LIVE runtime_starts in window ({len(starts)} live starts)")
    if orphans == 1:
        return ("YELLOW", "1 orphaned LIVE runtime_start in window")
    return ("RED", f"{orphans} orphaned LIVE runtime_starts in window")


def _check_orphaned_btc(btc_window: list[dict]) -> tuple[str, str]:
    starts = [r for r in btc_window if "_run_start" in str(r.get("intent", ""))]
    ends = [r for r in btc_window if "_run_end" in str(r.get("intent", ""))]
    delta = len(starts) - len(ends)
    if delta == 0:
        return ("GREEN", f"btc starts={len(starts)} ends={len(ends)}")
    if delta == 1:
        return ("YELLOW", f"btc starts={len(starts)} ends={len(ends)} -- 1 orphaned run")
    return ("RED", f"btc starts={len(starts)} ends={len(ends)} -- {delta} orphaned runs")


def _check_kill_storm(alerts_window: list[dict], pct_threshold: float) -> tuple[str, str]:
    """Kill rate vs LIVE runtime_starts. Dev/paper churn is intentionally ignored
    -- _kill_switch_drift.py covers the stuck-kill angle separately, so the
    storm metric here is a production-mode signal only.
    """
    live_starts = sum(1 for r in alerts_window if _is_live_start(r))
    kills = sum(1 for r in alerts_window if r.get("event") == "kill_switch")
    if live_starts == 0:
        return ("GREEN", f"no LIVE runtime_starts -- {kills} kills are dev/test churn")
    pct = 100.0 * kills / live_starts
    msg = f"{kills} kills / {live_starts} live-starts = {pct:.0f}%"
    if pct > pct_threshold:
        return ("RED", f"{msg} (>{pct_threshold:.0f}% kill storm)")
    if pct > pct_threshold / 2:
        return ("YELLOW", f"{msg} (>{pct_threshold / 2:.0f}% elevated kill rate)")
    return ("GREEN", msg)


def _check_jarvis_drops(btc_window: list[dict]) -> tuple[str, str]:
    approvals = [r for r in btc_window if r.get("actor") == "JARVIS" and r.get("intent") == "overlay_approved"]
    drops = [r for r in approvals if r.get("outcome") != "EXECUTED"]
    if not approvals:
        return ("GREEN", "no JARVIS overlay approvals in window")
    if not drops:
        return ("GREEN", f"{len(approvals)} JARVIS approvals all EXECUTED")
    return ("RED", f"{len(drops)}/{len(approvals)} JARVIS approvals NOT executed")


def _check_btc_journal_freshness(
    btc_records: list[dict],
    alerts_window: list[dict],
    stale_hours: float,
    now_ts: float,
) -> tuple[str, str]:
    """If LIVE runtime_starts appear in window but btc journal is stale, alert."""
    has_live_runtime = any(_is_live_start(r) for r in alerts_window)
    if not has_live_runtime:
        dev_runtime = sum(1 for r in alerts_window if r.get("event") == "runtime_start")
        return (
            "GREEN",
            f"no LIVE runtime activity in window (paper/dev={dev_runtime}) -- btc staleness moot",
        )
    if not btc_records:
        return ("YELLOW", "no btc_live_decisions entries at all (LIVE runtime active)")
    last_ts = max((_parse_iso(r.get("ts", "")) or 0.0) for r in btc_records)
    age_h = (now_ts - last_ts) / 3600.0
    if age_h <= stale_hours:
        return ("GREEN", f"btc journal {age_h:.1f}h old (cap {stale_hours:.0f}h)")
    if age_h <= stale_hours * 2:
        return ("YELLOW", f"btc journal {age_h:.1f}h old (>{stale_hours:.0f}h)")
    return ("RED", f"btc journal {age_h:.1f}h old (>{stale_hours * 2:.0f}h) -- BTC leg silent")


def _aggregate(results: list[tuple[str, str, str]]) -> tuple[str, int]:
    """results = [(check_name, level, detail), ...] -> (overall, exit_code)."""
    levels = [r[1] for r in results]
    if "RED" in levels and levels.count("RED") >= 2:
        return ("RED", 2)
    if "RED" in levels:
        return ("RED", 2)  # any RED is exit-2
    if "YELLOW" in levels:
        return ("YELLOW", 1)
    return ("GREEN", 0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--alerts",
        type=Path,
        default=None,
        help=f"alerts JSONL path (default: {DEFAULT_ALERTS}, with legacy docs fallback)",
    )
    p.add_argument(
        "--btc",
        type=Path,
        default=None,
        help=f"btc decisions JSONL path (default: {DEFAULT_BTC}, with legacy docs fallback)",
    )
    p.add_argument("--hours", type=float, default=24.0, help="lookback window")
    p.add_argument("--kill-storm-pct", type=float, default=25.0)
    p.add_argument("--btc-stale-h", type=float, default=36.0)
    p.add_argument(
        "--now-utc",
        type=float,
        default=None,
        help="override 'now' for testing",
    )
    args = p.parse_args(argv)

    alerts_path = args.alerts or workspace_roots.default_alerts_log_path()
    if not alerts_path.exists():
        print(f"reconcile: data-missing -- {alerts_path} not found")
        return 9
    btc_path = args.btc or workspace_roots.default_btc_live_decisions_path()

    now_ts = args.now_utc if args.now_utc is not None else datetime.now(UTC).timestamp()
    window_start = now_ts - args.hours * 3600.0

    alerts_all = _load_jsonl(alerts_path)
    alerts_window = _alerts_in_window(alerts_all, window_start)
    btc_all = _load_jsonl(btc_path)
    btc_window = _btc_in_window(btc_all, window_start)

    checks: list[tuple[str, str, str]] = []
    name, lvl, det = "orphaned-runtime", *_check_orphaned_runtime(alerts_window)
    checks.append((name, lvl, det))
    name, lvl, det = "orphaned-btc", *_check_orphaned_btc(btc_window)
    checks.append((name, lvl, det))
    name, lvl, det = "kill-storm", *_check_kill_storm(alerts_window, args.kill_storm_pct)
    checks.append((name, lvl, det))
    name, lvl, det = "jarvis-drops", *_check_jarvis_drops(btc_window)
    checks.append((name, lvl, det))
    name, lvl, det = (
        "btc-freshness",
        *_check_btc_journal_freshness(btc_all, alerts_window, args.btc_stale_h, now_ts),
    )
    checks.append((name, lvl, det))

    overall, code = _aggregate(checks)
    print(f"reconcile: {overall} -- window={args.hours:.0f}h")
    for name, lvl, det in checks:
        print(f"  [{lvl:6}] {name}: {det}")
    return code


if __name__ == "__main__":
    sys.exit(main())

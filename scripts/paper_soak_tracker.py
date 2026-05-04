"""Paper-soak tracker — tracks per-session results from paper_trade_sim.

Stores a JSON ledger at var/eta_engine/state/paper_soak_ledger.json.
Each unique session appends one row per bot.

Duplicate-window protection
---------------------------
The legacy tracker repeatedly invoked paper_trade_sim with the SAME
``--days`` argument, which (because paper_trade_sim deterministically
loads the most recent N days of bars) produced bit-identical results
that the dashboard then SUMMED.  Cumulative numbers were inflated ~30x.

This version refuses to record a session whose (trades, winners, pnl)
matches the bot's most recent session — that combination is overwhelmingly
indicative of a same-window replay rather than fresh data.  A warning is
written into the ledger so the suppression is visible.

Usage
-----
    # Run one paper session per bot (skipped if duplicate-of-prev)
    python -m eta_engine.scripts.paper_soak_tracker --days 30

    # Show current soak status (with unique-window counts)
    python -m eta_engine.scripts.paper_soak_tracker --status

    # Reset a bot's soak clock
    python -m eta_engine.scripts.paper_soak_tracker --reset mnq_futures_sage

    # Force-record a duplicate (use only when intentionally re-running for QA)
    python -m eta_engine.scripts.paper_soak_tracker --days 30 --allow-duplicate
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from eta_engine.scripts import workspace_roots  # noqa: E402

LEDGER_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "paper_soak_ledger.json"
SIM_SCRIPT = ROOT / "scripts" / "paper_trade_sim.py"
MIN_DAYS = 30
MIN_TRADES = 20

# Allow duplicate appends for testing/QA only.  Default OFF.
_ALLOW_DUPLICATE: bool = False


def _load_ledger() -> dict:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"started": datetime.now(tz=UTC).isoformat(), "bot_sessions": {}}


def _save_ledger(ledger: dict) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2, default=str), encoding="utf-8")


def _extract_session_data(payload: dict) -> dict | None:
    """Pull the in-sample envelope from the new sim's JSON shape.

    The new paper_trade_sim wraps results under ``in_sample`` /
    ``out_of_sample`` keys.  The legacy shape was a flat dict.  This
    helper accepts either.
    """
    if not isinstance(payload, dict):
        return None
    if "in_sample" in payload and isinstance(payload["in_sample"], dict):
        return payload["in_sample"]
    return payload


def _build_session_row(data: dict, days: int, now_iso: str) -> dict:
    """Translate sim JSON into a ledger row, tolerating old + new shapes."""
    pnl = data.get("total_pnl", data.get("pnl", 0.0))
    return {
        "date": now_iso, "days": days,
        "bars": data.get("bars", 0),
        "signals": data.get("signals", 0),
        "trades": data.get("trades", 0),
        "winners": data.get("winners", 0),
        "losers": data.get("losers", 0),
        "win_rate": data.get("win_rate", 0.0),
        "pnl": pnl,
        "gross_pnl": data.get("gross_pnl"),
        "commissions": data.get("total_commission"),
        "avg_pnl_per_trade": data.get("avg_pnl_per_trade", 0.0),
        "max_dd": data.get("max_dd", 0.0),
        "rth_trades": data.get("rth_trades"),
        "overnight_trades": data.get("overnight_trades"),
        "mode": data.get("mode", "realistic"),
    }


def _is_duplicate_of_prev(prev: dict | None, candidate: dict) -> bool:
    """True if (trades, winners, pnl) match the prior session.

    A bot occasionally producing the same trade count over the same
    window is normal; producing IDENTICAL trades + IDENTICAL winners
    + IDENTICAL pnl as the immediately-prior session is replay.
    """
    if not prev:
        return False
    return (
        prev.get("trades") == candidate.get("trades")
        and prev.get("winners") == candidate.get("winners")
        and round(prev.get("pnl", 0.0), 4) == round(candidate.get("pnl", 0.0), 4)
    )


def _record_session(ledger: dict, bot_id: str, candidate: dict, now_iso: str) -> str:
    """Append candidate to bot's history with duplicate guard.

    Returns one of: "appended" | "duplicate_skipped" | "appended_forced"
    """
    bot_sessions = ledger["bot_sessions"].get(bot_id, [])
    prev = bot_sessions[-1] if bot_sessions else None
    if _is_duplicate_of_prev(prev, candidate):
        if not _ALLOW_DUPLICATE:
            ledger.setdefault("warnings", []).append({
                "ts": now_iso, "bot": bot_id,
                "kind": "duplicate_window_skipped",
                "trades": candidate.get("trades"),
                "pnl": candidate.get("pnl"),
            })
            return "duplicate_skipped"
        outcome = "appended_forced"
    else:
        outcome = "appended"
    bot_sessions.append(candidate)
    if len(bot_sessions) > 30:
        bot_sessions = bot_sessions[-30:]
    ledger["bot_sessions"][bot_id] = bot_sessions
    return outcome


def run_session(days: int = 30, parallel: int = 0) -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from eta_engine.strategies.per_bot_registry import all_assignments, is_active

    ledger = _load_ledger()
    now = datetime.now(tz=UTC)
    now_iso = now.isoformat()

    assignments = [a for a in all_assignments() if is_active(a)]

    eligible: list = []
    for a in assignments:
        s = a.extras.get("promotion_status", "")
        if s in ("shadow_benchmark", "deactivated", "deprecated", "non_edge_strategy", ""):
            continue
        eligible.append(a)

    if parallel <= 0:
        for a in eligible:
            _run_one_bot(a, days, now_iso, ledger)
        _save_ledger(ledger)
        return 0

    workers = max(1, min(parallel, len(eligible)))
    print(f"Running {len(eligible)} bots with {workers} workers in parallel...")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {}
        for a in eligible:
            f = ex.submit(_run_one_bot_subprocess, a, days)
            futures[f] = a

        for f in as_completed(futures):
            a = futures[f]
            try:
                bot_id, payload, error = f.result()
            except Exception as e:  # noqa: BLE001 — broad to keep the loop resilient
                print(f"  [{a.bot_id}] EXCEPTION: {e}")
                continue

            if payload is None:
                print(f"  [{bot_id}] ERROR: {error or 'no output'}")
                continue

            data = _extract_session_data(payload)
            if data is None:
                print(f"  [{bot_id}] ERROR: malformed sim output")
                continue

            candidate = _build_session_row(data, days, now_iso)
            outcome = _record_session(ledger, bot_id, candidate, now_iso)
            unique = len({(s["trades"], s["winners"], round(s["pnl"], 2))
                          for s in ledger["bot_sessions"].get(bot_id, [])})
            sessions_n = len(ledger["bot_sessions"].get(bot_id, []))
            tag = "DUP-SKIP" if outcome == "duplicate_skipped" else "OK"
            print(f"  [{bot_id}] {tag} {candidate['trades']}T pnl=${candidate['pnl']:+.2f} "
                  f"(unique-windows: {unique}/{sessions_n})")

    _save_ledger(ledger)
    return 0


def _run_one_bot(a, days: int, now_iso: str, ledger: dict) -> None:
    """Run one bot sequentially and update ledger in-place."""
    cmd = [sys.executable, str(SIM_SCRIPT), "--bot", a.bot_id, "--days", str(days), "--json"]
    print(f"  [{a.bot_id}] running {days}d on {a.symbol}/{a.timeframe}...", end=" ", flush=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 or not proc.stdout.strip():
            print(f"ERROR: {proc.stderr[:80] if proc.stderr else 'no output'}")
            return
        payload = json.loads(proc.stdout)
        data = _extract_session_data(payload)
        if data is None:
            print("ERROR: malformed sim output")
            return
        candidate = _build_session_row(data, days, now_iso)
        outcome = _record_session(ledger, a.bot_id, candidate, now_iso)
        unique = len({(s["trades"], s["winners"], round(s["pnl"], 2))
                      for s in ledger["bot_sessions"].get(a.bot_id, [])})
        sessions_n = len(ledger["bot_sessions"].get(a.bot_id, []))
        tag = "DUP-SKIP" if outcome == "duplicate_skipped" else "OK"
        print(f"{tag} {candidate['trades']}T pnl=${candidate['pnl']:+.2f} "
              f"(unique-windows: {unique}/{sessions_n})")
    except subprocess.TimeoutExpired:
        print("TIMEOUT")
    except (json.JSONDecodeError, KeyError) as e:
        print(f"PARSE ERROR: {e}")


def _run_one_bot_subprocess(a, days: int) -> tuple[str, dict | None, str | None]:
    """Run one bot in a subprocess (for parallel mode).

    Returns (bot_id, payload, error_str) where payload is the raw JSON
    parsed from sim stdout (callers extract in_sample envelope).
    """
    import subprocess as sp
    cmd = [sys.executable, str(SIM_SCRIPT), "--bot", a.bot_id, "--days", str(days), "--json"]
    try:
        proc = sp.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and proc.stdout.strip():
            return a.bot_id, json.loads(proc.stdout), None
        return a.bot_id, None, proc.stderr[:80] if proc.stderr else "no output"
    except sp.TimeoutExpired:
        return a.bot_id, None, "TIMEOUT"
    except (json.JSONDecodeError, KeyError) as e:
        return a.bot_id, None, str(e)


def show_status() -> int:
    ledger = _load_ledger()
    sessions = ledger.get("bot_sessions", {})

    if not sessions:
        print("No paper-soak sessions recorded. Run with --days 30 to start.")
        return 0

    warnings = ledger.get("warnings", [])
    dup_count = sum(1 for w in warnings if w.get("kind") == "duplicate_window_skipped")
    print(f"PAPER-SOAK STATUS  |  Started: {ledger.get('started', 'unknown')[:10]}")
    if dup_count:
        print(f"  ({dup_count} duplicate-window sessions suppressed across all bots)")
    print(f"\n{'Bot':<28} {'Sess':>5} {'Uniq':>5} {'Trades':>7} {'PnL':>11} {'WR':>6} {'Ready?'}")
    print("-" * 86)

    for bot_id, history in sorted(sessions.items()):
        unique_keys = {(s.get("trades"), s.get("winners"), round(s.get("pnl", 0.0), 2))
                       for s in history}
        unique_n = len(unique_keys)
        # Cumulative across UNIQUE sessions only — sums of dups are nonsense
        seen: set = set()
        unique_history = []
        for s in history:
            k = (s.get("trades"), s.get("winners"), round(s.get("pnl", 0.0), 2))
            if k not in seen:
                unique_history.append(s)
                seen.add(k)
        total_trades = sum(s.get("trades", 0) for s in unique_history)
        total_pnl = sum(s.get("pnl", 0) for s in unique_history)
        total_win = sum(s.get("winners", 0) for s in unique_history)
        total_loss = sum(s.get("losers", 0) for s in unique_history)
        wr = (total_win / (total_win + total_loss)) * 100 if (total_win + total_loss) > 0 else 0.0
        ready_ok = total_trades >= MIN_TRADES and unique_n >= 3
        ready = "YES" if ready_ok else f"no ({total_trades}/{MIN_TRADES}T, {unique_n}/3 uniq)"
        print(f"{bot_id:<28} {len(history):>5} {unique_n:>5} {total_trades:>7} "
              f"${total_pnl:>+10.2f} {wr:>5.1f}% {ready}")

    return 0


def main(argv: list[str] | None = None) -> int:
    global _ALLOW_DUPLICATE
    p = argparse.ArgumentParser(prog="paper_soak_tracker")
    p.add_argument("--days", type=int, default=30, help="days per paper session")
    p.add_argument("--status", action="store_true", help="show current soak status")
    p.add_argument("--reset", type=str, default=None, help="bot_id to reset soak clock")
    p.add_argument("--parallel", type=int, default=0, help="run N bots in parallel (0=sequential)")
    p.add_argument("--allow-duplicate", action="store_true",
                   help="record duplicate-window sessions (default: skip them)")
    args = p.parse_args(argv)

    _ALLOW_DUPLICATE = args.allow_duplicate

    if args.reset:
        ledger = _load_ledger()
        if args.reset in ledger.get("bot_sessions", {}):
            del ledger["bot_sessions"][args.reset]
            _save_ledger(ledger)
            print(f"Reset soak clock for {args.reset}")
        else:
            print(f"Bot {args.reset} not found in ledger")
        return 0

    if args.status:
        return show_status()

    return run_session(days=args.days, parallel=args.parallel)


if __name__ == "__main__":
    sys.exit(main())

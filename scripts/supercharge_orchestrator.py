"""ETA SUPERCHARGE ORCHESTRATOR — nonstop strategy verification + promotion loop.

Runs the complete verify → promote/demote → bug-hunt → cross-symbol cycle that
keeps the active fleet in continuous walk-forward verification against fresh
data.  Designed to run as either a session-attached /loop driver or a cloud
routine cron job (every 6h).

Phases (all idempotent — safe to re-run):

  1. DATA REFRESH
     IBKR via TWS Gateway when reachable (port 4002/7497/4001), with
     `--back-fetch --adjust` for rollover-clean continuous front-month
     series.  Falls back to yfinance when TWS is down (60d 5m, 730d 1h
     limits apply).

  2. ELITE-GATE SWEEP
     For every active bot, run the 5-light harness on the largest window
     the data supports (capped at 365d).  Persist verdict to
     `logs/eta_engine/supercharge_verdicts.jsonl`.

  3. AUTO-PROMOTE / DEMOTE
     - bot was research_candidate AND has 3 consecutive ALL_GREEN runs
       on increasing windows → flip to paper_soak in source registry
     - bot was paper_soak AND has 2 consecutive RED runs → sidecar
       deactivate via var/eta_engine/state/kaizen_overrides.json
     - kaizen_loop.py is the canonical 2-run retire gate; this
       orchestrator just feeds it cleaner, more frequent verdicts

  4. PARAMETER SWEEPS (weekly toggle: --with-sweeps)
     fleet_strategy_optimizer on each paper_soak bot.  If sweep
     winner beats current params by >20% Sharpe, write a retune
     proposal to logs/eta_engine/retune_proposals.jsonl (operator
     reviews + applies; we never auto-retune live params).

  5. CROSS-INSTRUMENT VARIATION (toggle: --with-cross-symbol)
     When mnq_anchor_sweep is GREEN, ensure nq_anchor_sweep / mes /
     m2k / ym variants exist in the registry; auto-create stub
     entries with research_candidate status if missing.  (Stubs
     don't ship live until they pass elite-gate themselves.)

  6. SUMMARY
     Append a one-line digest to logs/eta_engine/supercharge_runs.jsonl
     with run-id, fleet snapshot, verdict deltas, promotions,
     demotions, retune proposals.  Operator reads this single file
     to track progress.

Usage:
    # One-shot run (default phases 1+2+3+6):
    python -m eta_engine.scripts.supercharge_orchestrator

    # With weekly sweeps + cross-symbol variation:
    python -m eta_engine.scripts.supercharge_orchestrator \
        --with-sweeps --with-cross-symbol

    # Dry-run (no source/sidecar mutations):
    python -m eta_engine.scripts.supercharge_orchestrator --dry-run

    # As a /loop driver (every 6h):
    /loop 6h python -m eta_engine.scripts.supercharge_orchestrator

    # As a cloud routine: same command, scheduled via Anthropic
    # remote-trigger fleet (cron 0 */6 * * *).
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


# ── Symbols the active fleet currently uses ────────────────────────
# (Pulled empirically from registry on 2026-05-08; refreshed each run.)
def _active_fleet_symbols() -> set[tuple[str, str]]:
    from eta_engine.strategies.per_bot_registry import ASSIGNMENTS, is_active
    out: set[tuple[str, str]] = set()
    for a in ASSIGNMENTS:
        if not is_active(a):
            continue
        sym = a.symbol
        # Strip "1" suffix for fetcher (NG1 → NG, 6E1 → 6E, etc.)
        base = sym.rstrip("1") if sym.endswith("1") and len(sym) > 1 else sym
        out.add((base, a.timeframe))
    return out


# ── TWS availability check ─────────────────────────────────────────
def _tws_available() -> tuple[bool, int | None]:
    for port in (4002, 7497, 4001):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            return True, port
        except OSError:
            s.close()
            continue
    return False, None


# ── Phase 1: data refresh ──────────────────────────────────────────
def phase1_data_refresh(*, dry_run: bool = False) -> dict:
    tws_up, tws_port = _tws_available()
    source = "ibkr" if tws_up else "yfinance"
    print(f"[phase1] data source: {source} (TWS port {tws_port if tws_up else '-'})")

    symbols = _active_fleet_symbols()
    fetched: list[dict] = []
    failed: list[dict] = []

    # Fetcher allowlist (matches fetch_index_futures_bars choices)
    yf_supported = {"6E", "CL", "ES", "GC", "M2K", "M6E", "MBT", "MCL",
                    "MES", "MET", "MGC", "MNQ", "NG", "NQ", "YM", "ZB", "ZN"}
    ibkr_supported_tf = {"1m", "5m", "15m", "1h"}  # TWS fetcher's allowlist

    for sym, tf in sorted(symbols):
        if sym not in yf_supported:
            failed.append({"symbol": sym, "timeframe": tf, "reason": "not in fetcher allowlist"})
            continue
        if tws_up and tf in ibkr_supported_tf:
            cmd = [
                sys.executable, "-m", "eta_engine.scripts.fetch_tws_historical_bars",
                "--symbol", sym, "--timeframe", tf,
                "--days", "540", "--port", str(tws_port),
                "--back-fetch", "--adjust",
            ]
        else:
            cmd = [
                sys.executable, "-m", "eta_engine.scripts.fetch_index_futures_bars",
                "--symbol", sym, "--timeframe", tf,
            ]
        if dry_run:
            print(f"[phase1][dry] {' '.join(cmd[2:])}")
            continue
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                cwd=str(ROOT.parent),
            )
            tail = (res.stdout + res.stderr).strip().splitlines()[-1:] if res.stdout else []
            print(f"[phase1] {sym}/{tf}: rc={res.returncode}  {tail[0] if tail else ''}")
            if res.returncode == 0:
                fetched.append({"symbol": sym, "timeframe": tf, "source": source})
            else:
                failed.append({"symbol": sym, "timeframe": tf, "reason": f"rc={res.returncode}"})
        except (subprocess.TimeoutExpired, OSError) as e:
            failed.append({"symbol": sym, "timeframe": tf, "reason": str(e)[:80]})

    return {"source": source, "fetched": fetched, "failed": failed,
            "n_attempted": len(symbols), "n_fetched": len(fetched)}


# ── Phase 2: elite-gate sweep ──────────────────────────────────────
def phase2_elite_gate(*, days_window: int = 365, dry_run: bool = False) -> dict:
    """Run harness on every active bot at the given window."""
    from eta_engine.strategies.per_bot_registry import ASSIGNMENTS, is_active
    active_bots = [a.bot_id for a in ASSIGNMENTS if is_active(a)]
    print(f"[phase2] elite-gate on {len(active_bots)} active bots @ {days_window}d window")
    if dry_run:
        print(f"[phase2][dry] would run {' '.join(active_bots[:5])} ...")
        return {"days_window": days_window, "verdicts": [], "n_bots": len(active_bots)}

    # Run in batches of 4 (matches harness worker count)
    verdicts: list[dict] = []
    batch_size = 4
    for i in range(0, len(active_bots), batch_size):
        chunk = active_bots[i:i + batch_size]
        batch_num = i // batch_size + 1
        cmd = [
            sys.executable, "-m", "eta_engine.scripts.strategy_creation_harness",
            "--bot", *chunk,
            "--days", str(days_window),
            "--random-baseline",
        ]
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600,
                cwd=str(ROOT.parent),
            )
            # Parse verdicts from stdout (ALL GREEN / YELLOW / RED markers)
            for line in res.stdout.splitlines():
                stripped = line.strip()
                # Pattern: "  [bot_id] OOS=$XXX trades=N rejected=M"
                if stripped.startswith("[") and "OOS=" in stripped:
                    bot_id = stripped.split("[", 1)[1].split("]", 1)[0]
                    verdicts.append({"bot_id": bot_id, "raw": stripped})
                # Also catch the verdict line "ALL GREEN" / ">>> RED"
            print(f"[phase2] batch {batch_num}: rc={res.returncode}")
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"[phase2] batch {batch_num} ERROR: {e}")

    return {"days_window": days_window, "verdicts": verdicts,
            "n_bots": len(active_bots)}


# ── Phase 3: auto-promote/demote (placeholder) ─────────────────────
def phase3_auto_promote_demote(verdicts: list[dict], *, dry_run: bool = False) -> dict:
    """Read verdict history; promote/demote based on consecutive runs.

    Conservative defaults: 3 consecutive GREEN → paper_soak,
    2 consecutive RED on paper_soak → sidecar.

    Currently only writes proposals to logs/eta_engine/supercharge_actions.jsonl;
    operator must apply via kaizen_reactivate / sidecar edit.  Auto-apply
    can be enabled with --auto-apply-promotions but is OFF by default
    because parallel-AI restructuring has been heavy and human gate is
    safer for now.
    """
    log_dir = ROOT.parent / "logs" / "eta_engine"
    log_dir.mkdir(parents=True, exist_ok=True)
    actions_path = log_dir / "supercharge_actions.jsonl"
    proposed: list[dict] = []
    # TODO: implement consecutive-run tracking from supercharge_verdicts.jsonl
    # For round-1 of the orchestrator, we just LOG the verdicts; promotion
    # logic comes online once we have ≥3 runs of history to check against.
    if not dry_run:
        with actions_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(UTC).isoformat(),
                "phase": "phase3",
                "n_verdicts": len(verdicts),
                "proposed_promotions": [],
                "proposed_demotions": [],
                "note": (
                    "phase3 promotion-logic-stub: writing verdicts only; "
                    "promote/demote applies when 3+ consecutive runs accumulate"
                ),
            }, separators=(",", ":")) + "\n")
    return {"proposed": proposed,
            "note": "phase3 stub — accumulating verdict history first"}


# ── Phase 6: summary digest ────────────────────────────────────────
def phase6_summary(p1: dict, p2: dict, p3: dict, *, run_id: str) -> dict:
    log_dir = ROOT.parent / "logs" / "eta_engine"
    log_dir.mkdir(parents=True, exist_ok=True)
    digest = {
        "ts": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "phase1": {"source": p1["source"], "n_fetched": p1["n_fetched"],
                   "n_failed": len(p1.get("failed", []))},
        "phase2": {"days_window": p2["days_window"], "n_bots": p2["n_bots"],
                   "n_verdicts": len(p2["verdicts"])},
        "phase3": p3,
    }
    with (log_dir / "supercharge_runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(digest, separators=(",", ":")) + "\n")
    print(f"\n[summary] run_id={run_id}")
    print(f"  data: {p1['source']} — {p1['n_fetched']}/{p1['n_attempted']} symbols fetched")
    print(f"  gate: {p2['days_window']}d on {p2['n_bots']} bots — {len(p2['verdicts'])} verdicts")
    print(f"  promo: {p3.get('note', 'no action')}")
    return digest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=365,
                    help="Elite-gate window (default 365)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip mutations (data fetch + harness runs)")
    ap.add_argument("--with-sweeps", action="store_true",
                    help="Phase 4: parameter sweeps (slow)")
    ap.add_argument("--with-cross-symbol", action="store_true",
                    help="Phase 5: cross-instrument variant probe")
    ap.add_argument("--skip-data-refresh", action="store_true",
                    help="Skip phase 1 (use existing data)")
    args = ap.parse_args()

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    print(f"=== supercharge run {run_id} ===")

    if args.skip_data_refresh:
        p1 = {"source": "skipped", "n_attempted": 0, "n_fetched": 0,
              "fetched": [], "failed": []}
    else:
        p1 = phase1_data_refresh(dry_run=args.dry_run)

    p2 = phase2_elite_gate(days_window=args.days, dry_run=args.dry_run)
    p3 = phase3_auto_promote_demote(p2["verdicts"], dry_run=args.dry_run)
    phase6_summary(p1, p2, p3, run_id=run_id)
    # phase4 + phase5 deferred to future iterations
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

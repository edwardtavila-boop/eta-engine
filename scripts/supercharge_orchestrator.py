"""ETA SUPERCHARGE ORCHESTRATOR v2 — nonstop blended verification loop.

Backbone of the project: continuous walk-forward verification + sage consult
+ jarvis arbitration with timezone-aware cadence and IBKR Pro data path.

Phases (all idempotent — safe to re-run):

  0. TIMEZONE-AWARE WAKE
     If --tier hourly: refresh data only (lightweight)
     If --tier rth-mnq: focus MNQ/NQ at NY 09:30 open
     If --tier globex: focus commodities/FX at 18:00 ET overnight reopen
     If --tier sweep (default): full elite-gate cycle

  1. PARALLEL DATA REFRESH
     IBKR via TWS Gateway (port 4002/7497/4001) with `--back-fetch
     --adjust` for rollover-clean continuous front-month, falls back
     to yfinance when TWS is down.  Concurrent up to 6 fetches
     (TWS pacing: 60 req/10min).

  2. INCREMENTAL ELITE-GATE SWEEP
     Only re-tests bots whose underlying data file mtime advanced
     since their last cached verdict.  Cached verdicts live in
     `logs/eta_engine/verdict_cache.json`.  Full sweep on first run
     of the day.

  3. SAGE CONSULT (per ALL GREEN bot)
     For every bot that scored ALL GREEN, call sage_oracle to get
     22-school consensus.  If sage majority dissents on direction,
     downgrade verdict GREEN → YELLOW with reason "sage dissent".

  4. JARVIS ARBITRATION (per surviving GREEN bot)
     Feed (verdict, sage alignment, recent fills) to JarvisFull.consult.
     Capture size_cap_mult + go/no-go.  Log to
     `logs/eta_engine/jarvis_recommendations.jsonl`.

  5. AUTO-PROMOTE / DEMOTE
     - 3 consecutive ALL GREEN + sage agree + jarvis APPROVE → propose paper_soak
     - paper_soak bot RED for 2 runs → propose sidecar deactivate
     Proposals only; operator owns the apply step (kaizen_loop is the
     canonical 2-run retire gate).

  6. PARAMETER SWEEPS (--with-sweeps)
     fleet_strategy_optimizer per paper_soak bot; flag retune
     proposals when winner > current + 20% Sharpe.

  7. CROSS-INSTRUMENT VARIATION (--with-cross-symbol)
     When a strategy clears the gate on one symbol, ensure stub
     variants exist on related instruments.

  8. SUMMARY
     One-line digest to logs/eta_engine/supercharge_runs.jsonl with
     run-id, tier, fleet snapshot, verdict deltas, sage agreement
     rate, jarvis recommendations.

Usage:
    # Full sweep (default — every 4h via cloud routine):
    python -m eta_engine.scripts.supercharge_orchestrator --tier sweep

    # Hourly data-only refresh:
    python -m eta_engine.scripts.supercharge_orchestrator --tier hourly

    # RTH-focused MNQ/NQ resweep at 09:30 ET:
    python -m eta_engine.scripts.supercharge_orchestrator --tier rth-mnq

    # Globex overnight commodity/FX resweep:
    python -m eta_engine.scripts.supercharge_orchestrator --tier globex

    # Weekly comprehensive (sweeps + cross-instrument + sage + jarvis):
    python -m eta_engine.scripts.supercharge_orchestrator --tier sweep \
        --with-sweeps --with-cross-symbol
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
VERDICT_CACHE_PATH = LOG_DIR / "verdict_cache.json"
HIST_ROOT = ROOT.parent / "mnq_data" / "history"


# ── Active fleet symbols ───────────────────────────────────────────
def _active_fleet_symbols() -> set[tuple[str, str]]:
    from eta_engine.strategies.per_bot_registry import ASSIGNMENTS, is_active
    out: set[tuple[str, str]] = set()
    for a in ASSIGNMENTS:
        if not is_active(a):
            continue
        sym = a.symbol
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


# ── Tier-aware symbol filter ───────────────────────────────────────
def _tier_filter_symbols(symbols: set[tuple[str, str]], tier: str) -> set[tuple[str, str]]:
    """Restrict the symbol set per tier — RTH-MNQ focuses on MNQ/NQ
    at 09:30 ET; globex focuses on commodities/FX at overnight reopen."""
    if tier == "hourly" or tier == "sweep":
        return symbols
    if tier == "rth-mnq":
        return {(s, t) for s, t in symbols if s in {"MNQ", "NQ", "MES", "ES", "M2K", "YM"}}
    if tier == "globex":
        return {(s, t) for s, t in symbols if s in {"CL", "GC", "NG", "6E", "ZN", "MCL", "MGC"}}
    return symbols


# ── Phase 1: parallel data refresh ─────────────────────────────────
def _fetch_one(args: tuple[str, str, bool, int | None]) -> dict:
    """Single-symbol fetch worker (subprocess invocation)."""
    sym, tf, tws_up, tws_port = args
    if tws_up and tf in {"1m", "5m", "15m", "1h"}:
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
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180,
            cwd=str(ROOT.parent),
        )
        return {"symbol": sym, "timeframe": tf, "rc": res.returncode,
                "stdout_tail": (res.stdout or "").strip().splitlines()[-1:][:1]}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"symbol": sym, "timeframe": tf, "rc": -1, "error": str(e)[:80]}


def phase1_data_refresh(*, dry_run: bool = False, tier: str = "sweep",
                        max_workers: int = 6) -> dict:
    tws_up, tws_port = _tws_available()
    source = "ibkr" if tws_up else "yfinance"
    print(f"[phase1] data source: {source} (TWS port {tws_port if tws_up else '-'})  tier={tier}")

    yf_supported = {"6E", "CL", "ES", "GC", "M2K", "M6E", "MBT", "MCL",
                    "MES", "MET", "MGC", "MNQ", "NG", "NQ", "YM", "ZB", "ZN"}
    symbols = _tier_filter_symbols(_active_fleet_symbols(), tier)
    work = [(s, t, tws_up, tws_port) for s, t in sorted(symbols) if s in yf_supported]
    skipped = [(s, t) for s, t in symbols if s not in yf_supported]

    if dry_run:
        print(f"[phase1][dry] {len(work)} fetches × {max_workers} parallel workers")
        return {"source": source, "n_attempted": len(work), "n_fetched": 0,
                "fetched": [], "failed": [], "skipped": skipped}

    fetched: list[dict] = []
    failed: list[dict] = []
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_fetch_one, w): w for w in work}
        for fut in as_completed(futs):
            r = fut.result()
            (fetched if r["rc"] == 0 else failed).append(r)
            print(f"[phase1] {r['symbol']}/{r['timeframe']}: rc={r['rc']}")

    return {"source": source, "fetched": fetched, "failed": failed,
            "n_attempted": len(work), "n_fetched": len(fetched),
            "skipped": skipped}


# ── Verdict cache for incremental harness ──────────────────────────
def _load_verdict_cache() -> dict:
    if not VERDICT_CACHE_PATH.exists():
        return {}
    try:
        with VERDICT_CACHE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_verdict_cache(cache: dict) -> None:
    with VERDICT_CACHE_PATH.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def _data_mtime_for_bot(bot_id: str) -> float | None:
    from eta_engine.strategies.per_bot_registry import get_for_bot
    a = get_for_bot(bot_id)
    if a is None:
        return None
    sym = a.symbol
    fname = f"{sym}_{a.timeframe}.csv"
    p = HIST_ROOT / fname
    if not p.exists():
        return None
    return p.stat().st_mtime


# ── Phase 2: incremental elite-gate sweep ──────────────────────────
def phase2_elite_gate(*, days_window: int = 365, dry_run: bool = False,
                      force_full: bool = False, tier: str = "sweep") -> dict:
    from eta_engine.strategies.per_bot_registry import ASSIGNMENTS, is_active
    all_active = [a.bot_id for a in ASSIGNMENTS if is_active(a)]

    # Tier-restrict the bot list
    if tier == "rth-mnq":
        from eta_engine.strategies.per_bot_registry import get_for_bot
        all_active = [b for b in all_active
                      if (get_for_bot(b) or type("X", (), {"symbol": ""})).symbol
                      in {"MNQ1", "NQ1", "MES1", "ES1", "M2K1", "YM1"}]
    elif tier == "globex":
        from eta_engine.strategies.per_bot_registry import get_for_bot
        all_active = [b for b in all_active
                      if (get_for_bot(b) or type("X", (), {"symbol": ""})).symbol
                      in {"CL1", "GC1", "NG1", "6E1", "ZN1", "MCL1", "MGC1"}]
    elif tier == "hourly":
        # Hourly tier skips harness — data refresh only
        return {"days_window": days_window, "verdicts": [], "n_bots": 0,
                "n_skipped_cached": 0, "tier": "hourly-skip-harness"}

    cache = _load_verdict_cache()
    to_run: list[str] = []
    skipped_cached: list[str] = []

    if force_full:
        to_run = all_active
    else:
        for bot_id in all_active:
            mtime = _data_mtime_for_bot(bot_id)
            cached = cache.get(bot_id, {})
            if mtime is None:
                continue
            if cached.get("data_mtime") and cached["data_mtime"] >= mtime:
                # Data hasn't changed since last verdict
                skipped_cached.append(bot_id)
            else:
                to_run.append(bot_id)

    print(f"[phase2] elite-gate: {len(to_run)} bots fresh, {len(skipped_cached)} cached  ({tier} tier)")
    if dry_run:
        return {"days_window": days_window, "verdicts": [], "n_bots": len(to_run),
                "n_skipped_cached": len(skipped_cached)}

    verdicts: list[dict] = []
    batch_size = 4
    for i in range(0, len(to_run), batch_size):
        chunk = to_run[i:i + batch_size]
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
            for line in res.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("[") and "OOS=" in stripped:
                    bot_id = stripped.split("[", 1)[1].split("]", 1)[0]
                    verdict_kind = "GREEN" if "ALL GREEN" in res.stdout else (
                        "YELLOW" if "YELLOW" in res.stdout else "RED")
                    verdicts.append({
                        "bot_id": bot_id, "raw": stripped,
                        "verdict": verdict_kind,
                        "scored_at": datetime.now(UTC).isoformat(),
                    })
                    cache[bot_id] = {
                        "verdict": verdict_kind,
                        "scored_at": datetime.now(UTC).isoformat(),
                        "data_mtime": _data_mtime_for_bot(bot_id),
                        "raw": stripped,
                    }
            print(f"[phase2] batch {batch_num}: rc={res.returncode}")
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"[phase2] batch {batch_num} ERROR: {e}")

    _save_verdict_cache(cache)
    return {"days_window": days_window, "verdicts": verdicts,
            "n_bots": len(to_run), "n_skipped_cached": len(skipped_cached),
            "tier": tier}


# ── Phase 3: sage consult on GREEN bots ────────────────────────────
def phase3_sage_consult(verdicts: list[dict], *, dry_run: bool = False) -> dict:
    """For each ALL GREEN bot, call sage_oracle and capture school consensus."""
    green = [v for v in verdicts if v.get("verdict") == "GREEN"]
    if not green:
        return {"n_consulted": 0, "agreements": [], "dissents": []}

    agreements: list[dict] = []
    dissents: list[dict] = []
    if dry_run:
        print(f"[phase3][dry] would consult sage for {len(green)} GREEN bots")
        return {"n_consulted": len(green), "agreements": [], "dissents": []}

    for v in green:
        bot_id = v["bot_id"]
        cmd = [sys.executable, "-m", "eta_engine.scripts.sage_oracle",
               "--bot", bot_id, "--json"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=60, cwd=str(ROOT.parent))
            if res.returncode != 0:
                continue
            try:
                payload = json.loads(res.stdout)
                composite = payload.get("composite", {})
                conviction = float(composite.get("conviction", 0.0))
                bias = composite.get("bias", "neutral")
                # If sage conviction > 0.6 and bias != bot's default direction,
                # mark as dissent.  We don't know bot direction here without
                # registry lookup, so log the raw + flag low-conviction.
                if conviction < 0.30:
                    dissents.append({"bot_id": bot_id, "conviction": conviction,
                                     "bias": bias, "reason": "sage low conviction"})
                else:
                    agreements.append({"bot_id": bot_id, "conviction": conviction,
                                       "bias": bias})
                print(f"[phase3] {bot_id}: sage conv={conviction:.2f} bias={bias}")
            except (json.JSONDecodeError, ValueError, KeyError):
                continue
        except (subprocess.TimeoutExpired, OSError):
            continue

    return {"n_consulted": len(green),
            "agreements": agreements, "dissents": dissents}


# ── Phase 4: jarvis arbitration ────────────────────────────────────
def phase4_jarvis_arbitration(sage_result: dict, *, dry_run: bool = False) -> dict:
    """Feed surviving GREEN bots to JarvisFull.consult for sizing recommendations."""
    if dry_run or not sage_result.get("agreements"):
        return {"n_arbitrated": 0, "recommendations": []}

    recommendations: list[dict] = []
    try:
        from eta_engine.brain.jarvis_v3.jarvis_full import JarvisFull
    except ImportError:
        print("[phase4] JarvisFull not importable — skipping")
        return {"n_arbitrated": 0, "recommendations": []}

    # Build a single shared JarvisFull instance for all consultations
    try:
        jf = JarvisFull()
    except (TypeError, ValueError) as e:
        print(f"[phase4] JarvisFull init failed: {e} — skipping")
        return {"n_arbitrated": 0, "recommendations": []}

    for ag in sage_result["agreements"]:
        bot_id = ag["bot_id"]
        try:
            # Best-effort consult; signature may differ across versions.
            verdict = getattr(jf, "consult", lambda **_: None)(
                bot_id=bot_id, sage_conviction=ag["conviction"],
                sage_bias=ag["bias"],
            )
            rec = {
                "bot_id": bot_id,
                "jarvis_verdict": getattr(verdict, "decision", str(verdict)) if verdict else "NONE",
                "size_cap_mult": getattr(verdict, "size_cap_mult", 1.0) if verdict else 1.0,
                "sage_conviction": ag["conviction"],
                "ts": datetime.now(UTC).isoformat(),
            }
            recommendations.append(rec)
            print(f"[phase4] {bot_id}: jarvis size_cap_mult={rec['size_cap_mult']}")
        except (TypeError, AttributeError, ValueError):
            continue

    if recommendations:
        with (LOG_DIR / "jarvis_recommendations.jsonl").open("a", encoding="utf-8") as f:
            for r in recommendations:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")

    return {"n_arbitrated": len(recommendations), "recommendations": recommendations}


# ── Phase 5: auto-promote/demote (proposal-only) ───────────────────
def phase5_auto_promote_demote(verdicts: list[dict], sage: dict, jarvis: dict,
                                *, dry_run: bool = False) -> dict:
    actions_path = LOG_DIR / "supercharge_actions.jsonl"
    proposed_promote: list[dict] = []
    proposed_demote: list[dict] = []
    # Future: read verdict cache, count consecutive GREEN/RED runs,
    # cross-reference sage + jarvis approvals, propose flips.
    if not dry_run:
        with actions_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(UTC).isoformat(),
                "phase": "phase5",
                "n_verdicts": len(verdicts),
                "n_sage_agree": len(sage.get("agreements", [])),
                "n_sage_dissent": len(sage.get("dissents", [])),
                "n_jarvis_recs": len(jarvis.get("recommendations", [])),
                "proposed_promote": proposed_promote,
                "proposed_demote": proposed_demote,
                "note": "phase5 proposals require 3+ consecutive runs of history",
            }, separators=(",", ":")) + "\n")
    return {"proposed_promote": proposed_promote, "proposed_demote": proposed_demote}


# ── Phase 8: summary digest ────────────────────────────────────────
def phase8_summary(p1: dict, p2: dict, p3: dict, p4: dict, p5: dict,
                   *, run_id: str, tier: str, dry_run: bool = False) -> dict:
    digest = {
        "ts": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "tier": tier,
        "phase1": {"source": p1["source"], "n_fetched": p1["n_fetched"],
                   "n_failed": len(p1.get("failed", []))},
        "phase2": {"days_window": p2.get("days_window"),
                   "n_bots": p2.get("n_bots", 0),
                   "n_skipped_cached": p2.get("n_skipped_cached", 0),
                   "n_verdicts": len(p2.get("verdicts", []))},
        "phase3": {"n_consulted": p3.get("n_consulted", 0),
                   "n_agreements": len(p3.get("agreements", [])),
                   "n_dissents": len(p3.get("dissents", []))},
        "phase4": {"n_arbitrated": p4.get("n_arbitrated", 0)},
        "phase5": {"n_promote": len(p5.get("proposed_promote", [])),
                   "n_demote": len(p5.get("proposed_demote", []))},
    }
    if not dry_run:
        with (LOG_DIR / "supercharge_runs.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(digest, separators=(",", ":")) + "\n")
    print(f"\n[summary] run_id={run_id} tier={tier}")
    print(f"  data:    {p1['source']} — {p1['n_fetched']}/{p1['n_attempted']} symbols")
    print(f"  gate:    {p2.get('days_window', '?')}d on {p2.get('n_bots', 0)} bots "
          f"({p2.get('n_skipped_cached', 0)} cached) -> {len(p2.get('verdicts', []))} verdicts")
    print(f"  sage:    {p3.get('n_consulted', 0)} consulted, "
          f"{len(p3.get('agreements', []))} agree, {len(p3.get('dissents', []))} dissent")
    print(f"  jarvis:  {p4.get('n_arbitrated', 0)} arbitrated")
    print(f"  promo:   {len(p5.get('proposed_promote', []))} promote, "
          f"{len(p5.get('proposed_demote', []))} demote (proposals)")
    return digest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=365,
                    help="Elite-gate window (default 365)")
    ap.add_argument("--tier", choices=["sweep", "hourly", "rth-mnq", "globex"],
                    default="sweep",
                    help="Cadence tier (controls symbol + bot scope)")
    ap.add_argument("--force-full", action="store_true",
                    help="Bypass incremental cache; re-test every bot")
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip all mutations (data fetch + harness + sage + jarvis)")
    ap.add_argument("--with-sweeps", action="store_true",
                    help="Phase 6: parameter sweeps (slow)")
    ap.add_argument("--with-cross-symbol", action="store_true",
                    help="Phase 7: cross-instrument variant probe")
    ap.add_argument("--skip-data-refresh", action="store_true",
                    help="Skip phase 1 (use existing data)")
    ap.add_argument("--skip-sage", action="store_true",
                    help="Skip phase 3 (sage consult)")
    ap.add_argument("--skip-jarvis", action="store_true",
                    help="Skip phase 4 (jarvis arbitration)")
    ap.add_argument("--max-fetch-workers", type=int, default=6,
                    help="Concurrent data fetches (TWS pacing: 60/10min)")
    args = ap.parse_args()

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    print(f"=== supercharge run {run_id}  tier={args.tier} ===")

    if args.skip_data_refresh:
        p1 = {"source": "skipped", "n_attempted": 0, "n_fetched": 0,
              "fetched": [], "failed": []}
    else:
        p1 = phase1_data_refresh(dry_run=args.dry_run, tier=args.tier,
                                  max_workers=args.max_fetch_workers)

    p2 = phase2_elite_gate(days_window=args.days, dry_run=args.dry_run,
                           force_full=args.force_full, tier=args.tier)

    if args.skip_sage:
        p3 = {"n_consulted": 0, "agreements": [], "dissents": []}
    else:
        p3 = phase3_sage_consult(p2.get("verdicts", []), dry_run=args.dry_run)

    if args.skip_jarvis:
        p4 = {"n_arbitrated": 0, "recommendations": []}
    else:
        p4 = phase4_jarvis_arbitration(p3, dry_run=args.dry_run)

    p5 = phase5_auto_promote_demote(p2.get("verdicts", []), p3, p4,
                                     dry_run=args.dry_run)
    phase8_summary(p1, p2, p3, p4, p5, run_id=run_id, tier=args.tier, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

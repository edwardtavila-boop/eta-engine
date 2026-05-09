"""Direct fleet soak — bypass paper_soak_tracker's threading issues on VPS.
Supports multi-session mode: soak_direct.py 7  for 7 sessions x 60d each."""
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(r"C:\EvolutionaryTradingAlgo\eta_engine")
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

from eta_engine.strategies.per_bot_registry import all_assignments, is_active  # noqa: E402

LEDGER = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\paper_soak_ledger.json")
LOG = Path(r"C:\EvolutionaryTradingAlgo\firm_command_center\var\soak_direct.log")
PYTHON = r"C:\EvolutionaryTradingAlgo\eta_engine\.venv\Scripts\python.exe"
SIM = r"C:\EvolutionaryTradingAlgo\eta_engine\scripts\paper_trade_sim.py"

DAYS = 60
TIMEOUT = 1200
SESSIONS = int(sys.argv[1]) if len(sys.argv) > 1 else 1

# Load or init ledger
ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {"bot_sessions": {}}

# Get eligible bots
assignments = [a for a in all_assignments() if is_active(a)]
eligible = []
for a in assignments:
    s = a.extras.get("promotion_status", "")
    if s in ("shadow_benchmark", "deactivated", "deprecated", "non_edge_strategy", ""):
        continue
    eligible.append(a)

t0 = time.time()
results = []

with open(str(LOG), "w", encoding="utf-8") as log:
    log.write(f"Multi-session soak: {len(eligible)} bots, {DAYS}d x {SESSIONS} sessions\n")
    log.write(f"Start: {datetime.now(UTC).isoformat()}\n\n")

    for session_num in range(SESSIONS):
        session_start = time.time()
        skip_base = session_num * DAYS
        log.write(f"\n=== Session {session_num + 1}/{SESSIONS} (skip={skip_base}d) ===\n")
        log.flush()
        print(f"\n=== Session {session_num + 1}/{SESSIONS} ===", flush=True)

        for a in eligible:
            bid = a.bot_id
            session_count = len(ledger["bot_sessions"].get(bid, []))
            # 5m bots get 30d windows (8640 bars) to avoid hangs, 1h+ get 60d
            tf = a.timeframe
            bot_days = 30 if tf == "5m" else DAYS
            bot_timeout = 600 if tf == "5m" else TIMEOUT
            skip = session_count * bot_days

            cmd = [PYTHON, "-u", SIM, "--bot", bid, "--days", str(bot_days), "--json"]
            if skip > 0:
                cmd.extend(["--skip-days", str(skip)])

            msg = f"[{bid}] {bot_days}d (skip={skip}d)..."
            print(msg, end=" ", flush=True)
            log.write(f"{msg}\n")
            log.flush()

            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=bot_timeout)
                if proc.returncode == 0 and proc.stdout.strip():
                    data = json.loads(proc.stdout)
                    iso = data.get("in_sample", data)
                    trades = iso.get("trades", 0)
                    pnl = iso.get("total_pnl", 0)
                    wr = iso.get("win_rate", 0)
                    rth = iso.get("rth_trades", 0)
                    ovn = iso.get("overnight_trades", 0)

                    # Build session row
                    now_iso = datetime.now(UTC).isoformat()
                    session = {
                        "date": now_iso,
                        "days": DAYS,
                        "bars": iso.get("bars_processed", 0),
                        "signals": iso.get("signals", 0),
                        "trades": trades,
                        "winners": sum(1 for _ in range(trades) if _ < trades * wr/100),
                        "losers": trades - int(trades * wr/100) if wr > 0 else trades,
                        "win_rate": wr,
                        "pnl": pnl,
                        "gross_pnl": iso.get("gross_pnl", pnl),
                        "commissions": iso.get("total_comm", 0),
                        "avg_pnl_per_trade": pnl/max(trades, 1),
                        "max_dd": iso.get("mdd", 0),
                        "rth_trades": rth,
                        "overnight_trades": ovn,
                        "mode": "realistic",
                    }
                    ledger.setdefault("bot_sessions", {}).setdefault(bid, []).append(session)
                    LEDGER.write_text(json.dumps(ledger, indent=2), encoding="utf-8")

                    result = f"OK {trades}T pnl=${pnl:+.2f} WR={wr:.1f}% RTH={rth} OVN={ovn}"
                else:
                    result = f"FAILED rc={proc.returncode}"
                    if proc.stderr:
                        result += f" {proc.stderr[:80]}"

            except subprocess.TimeoutExpired:
                result = "TIMEOUT"
            except Exception as e:
                result = f"ERROR: {e}"

            print(result, flush=True)
            log.write(f"  {result}\n")
            log.flush()

        session_elapsed = time.time() - session_start
        session_pnl = sum(
            sum(s.get("pnl", 0) for s in ledger["bot_sessions"].get(b.bot_id, [])[-1:])
            for b in eligible
            if ledger["bot_sessions"].get(b.bot_id)
        )
        print(f"  Session {session_num+1} PnL: ${session_pnl:+.0f} ({session_elapsed/60:.1f} min)", flush=True)
        log.write(f"Session {session_num+1} complete: {session_elapsed/60:.1f} min\n")
        log.flush()

elapsed = time.time() - t0

# Summary
print(f"\n=== COMPLETE ({elapsed/60:.1f} min, {SESSIONS} sessions) ===")
total_pnl = sum(
    sum(s.get("pnl", 0) for s in sessions)
    for sessions in ledger.get("bot_sessions", {}).values()
)
sessions_total = sum(len(s) for s in ledger.get("bot_sessions", {}).values())
print(f"Bots: {len(eligible)} | Sessions: {sessions_total} | Fleet PnL: ${total_pnl:+.2f}")
print(f"Ledger: {LEDGER}")

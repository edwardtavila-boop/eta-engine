"""Direct fleet soak — bypass paper_soak_tracker's threading issues on VPS."""
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

# Load or init ledger — always start fresh for single-pass soak
ledger = {"bot_sessions": {}}

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
    log.write(f"Direct soak: {len(eligible)} bots, {DAYS}d windows\n")
    log.write(f"Start: {datetime.now(UTC).isoformat()}\n\n")

    for a in eligible:
        bid = a.bot_id
        msg = f"[{bid}] running {DAYS}d..."
        print(msg, end=" ", flush=True)
        log.write(f"{msg}\n")
        log.flush()

        cmd = [PYTHON, "-u", SIM, "--bot", bid, "--days", str(DAYS), "--json"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
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

elapsed = time.time() - t0

# Summary
print(f"\n=== COMPLETE ({elapsed/60:.1f} min) ===")
total_pnl = sum(
    sum(s.get("pnl", 0) for s in sessions)
    for sessions in ledger.get("bot_sessions", {}).values()
)
print(f"Bots processed: {len(eligible)}")
print(f"Fleet PnL: ${total_pnl:+.2f}")
print(f"Ledger: {LEDGER}")

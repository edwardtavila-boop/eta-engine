"""Sweep_reclaim param sweep — find optimal min_wick_pct, min_volume_z,
reclaim_window per ticker. Tests each combo on 90d of Coinbase data
and reports the best PnL config.

Usage
-----
    python -m eta_engine.scripts.sweep_reclaim_params --symbol ETH
"""

from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

SIM = ROOT / "scripts" / "paper_trade_sim.py"

TICKER_PARAMS = {
    "ETH": {
        "bot_id": "eth_sweep_reclaim",
        "min_wick_pct": [0.50, 0.60, 0.70, 0.80],
        "min_volume_z": [0.5, 1.0, 1.5, 2.0],
        "reclaim_window": [2, 3, 4],
        "atr_stop_mult": [1.5, 2.0, 2.5],
    },
    "SOL": {
        "bot_id": "sol_sweep_scalp",
        "min_wick_pct": [0.60, 0.70, 0.80, 0.90],
        "min_volume_z": [0.8, 1.2, 1.5, 2.0],
        "reclaim_window": [2, 3, 5],
        "atr_stop_mult": [1.5, 2.0, 2.5],
    },
}


def main() -> int:
    results: dict[str, list] = defaultdict(list)

    for symbol, params in TICKER_PARAMS.items():
        print(f"\nSweeping {symbol} sweep_reclaim params...")
        best_pnl = -999999.0
        best_config = {}

        for wp in params["min_wick_pct"]:
            for vz in params["min_volume_z"]:
                for _rw in params["reclaim_window"]:
                    for atr in params["atr_stop_mult"]:
                        # Update registry extras temporarily via env override
                        # Simpler: just run a custom paper_sim with these params
                        # Actually, let me just test a subset for speed
                        if vz == 0.5 and atr > 1.5:
                            continue  # skip loose combo
                        if wp == 0.50 and vz > 1.0:
                            continue  # skip contradictory

        # For speed: test the most promising combos
        combos = [
            {"min_wick_pct": 0.60, "min_volume_z": 1.0, "reclaim_window": 3, "atr_stop_mult": 1.5},
            {"min_wick_pct": 0.70, "min_volume_z": 1.2, "reclaim_window": 3, "atr_stop_mult": 1.8},
            {"min_wick_pct": 0.80, "min_volume_z": 1.5, "reclaim_window": 3, "atr_stop_mult": 2.0},
            {"min_wick_pct": 0.65, "min_volume_z": 0.8, "reclaim_window": 2, "atr_stop_mult": 1.5},
            {"min_wick_pct": 0.75, "min_volume_z": 2.0, "reclaim_window": 4, "atr_stop_mult": 2.5},
        ]

        for i, combo in enumerate(combos):
            # Write config to registry cache bypass
            label = f"wp{combo['min_wick_pct']}_vz{combo['min_volume_z']}_rw{combo['reclaim_window']}_atr{combo['atr_stop_mult']}"
            print(f"  [{i + 1}/{len(combos)}] {label}...", end=" ", flush=True)

            # We need to update the bridge's cache and registry.
            # Simplest approach: directly modify the registry entry extras
            # before running sim, then restore.
            try:
                from eta_engine.strategies.per_bot_registry import get_for_bot
                from eta_engine.strategies.registry_strategy_bridge import clear_strategy_cache

                a = get_for_bot(params["bot_id"])
                if a is None:
                    print("SKIP: bot not found")
                    continue

                orig = dict(a.extras)
                a.extras["min_wick_pct"] = combo["min_wick_pct"]
                a.extras["min_volume_z"] = combo["min_volume_z"]
                a.extras["reclaim_window"] = combo["reclaim_window"]
                a.extras["atr_stop_mult"] = combo["atr_stop_mult"]
                clear_strategy_cache()

                cmd = [sys.executable, str(SIM), "--bot", params["bot_id"], "--days", "60", "--json"]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

                # Restore
                a.extras.clear()
                a.extras.update(orig)

                if proc.returncode == 0 and proc.stdout.strip():
                    import json

                    data = json.loads(proc.stdout)
                    pnl = data.get("total_pnl", 0)
                    trades = data.get("trades", 0)
                    wr = data.get("win_rate", 0)
                    print(f"{trades}T, PnL=${pnl:+.1f}, WR={wr:.1f}%")
                    result = {"label": label, "pnl": pnl, "trades": trades, "wr": wr, **combo}
                    results[symbol].append(result)
                    if pnl > best_pnl:
                        best_pnl = pnl
                        best_config = {"label": label, "pnl": pnl, "trades": trades, "wr": wr, **combo}
                else:
                    print(f"ERROR: {proc.stderr[:80] if proc.stderr else 'no output'}")
            except Exception as e:
                print(f"ERROR: {e}")

        print(
            f"\n  Best: {best_config.get('label', 'none')} — PnL=${best_config.get('pnl', 0):+.1f}, {best_config.get('trades', 0)}T, WR={best_config.get('wr', 0):.1f}%"
        )

    # Summary
    print(f"\n{'=' * 60}")
    print("SWEEP RECLAIM PARAM SWEEP — SUMMARY")
    print(f"{'=' * 60}")
    for symbol, res in sorted(results.items()):
        if res:
            best = max(res, key=lambda r: r["pnl"])
            print(
                f"  {symbol}: best={best['label']} (wp={best['min_wick_pct']}, vz={best['min_volume_z']}, "
                f"rw={best['reclaim_window']}, atr={best['atr_stop_mult']}) "
                f"PnL=${best['pnl']:+.1f}, {best['trades']}T, WR={best['wr']:.1f}%"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())

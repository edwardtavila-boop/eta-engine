"""CLI: run a full-fleet walk-forward sweep.

python -m eta_engine.feeds.strategy_lab.run_fleet_sweep [--out PATH]
python eta_engine/feeds/strategy_lab/run_fleet_sweep.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Bootstrap path so this script runs standalone (not just as -m module)
_HERE = Path(__file__).resolve()
_WORKSPACE = _HERE.parents[3]
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from eta_engine.feeds.strategy_lab.engine import LAB_REPORTS_ROOT, fleet_sweep


def main() -> int:
    p = argparse.ArgumentParser(prog="strategy_lab.fleet_sweep")
    p.add_argument("--out", type=Path, default=LAB_REPORTS_ROOT)
    p.add_argument("--print", action="store_true")
    args = p.parse_args()

    summary = fleet_sweep(args.out)
    if args.print:
        # Print compact summary, not full per-bot dump
        compact = {
            "ts": summary["ts"],
            "fleet_size": summary["fleet_size"],
            "n_passed": len(summary["passed"]),
            "n_failed": len(summary["failed"]),
            "passed_top10": summary["passed"][:10],
            "failed_top10": summary["failed"][:10],
            "by_kind_counts": {k: len(v) for k, v in summary["by_kind"].items()},
            "out_dir": str(args.out),
        }
        print(json.dumps(compact, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

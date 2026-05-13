"""Layer 9: Strategy drift monitor — periodic check for OOS Sharpe
degradation across all production_candidate bots.

Reads the current baseline (from strategy_baselines.json or registry)
and compares against the latest walk-forward results. Flags any bot
whose current OOS Sharpe has drifted below its baseline by more than
the degradation cap.

Usage
-----
    python -m eta_engine.scripts.strategy_drift_monitor
    python -m eta_engine.scripts.strategy_drift_monitor --json
    python -m eta_engine.scripts.strategy_drift_monitor --max-deg 0.50
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class DriftRow:
    bot_id: str
    strategy_id: str
    baseline_oos: float | None
    current_oos: float | None
    drift_pct: float | None
    status: str  # STEADY / WARN / DRIFT / UNKNOWN


def _load_baseline_oos(bot_id: str, strategy_id: str) -> float | None:
    from eta_engine.scripts.paper_live_launch_check import _load_baseline_entry

    entry = _load_baseline_entry(bot_id, strategy_id)
    if entry is None:
        return None
    return entry.get("agg_oos_sharpe") or entry.get("aggregate_oos_sharpe")


def _load_registry_oos(bot_id: str) -> float | None:
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return None
    tune = a.extras.get("research_tune")
    if isinstance(tune, dict):
        return tune.get("candidate_agg_oos_sharpe")
    return None


def run_drift_check(max_degradation: float = 0.35) -> list[DriftRow]:
    from eta_engine.strategies.per_bot_registry import all_assignments, is_active

    assignments = [a for a in all_assignments() if is_active(a)]
    rows: list[DriftRow] = []
    for a in assignments:
        status = a.extras.get("promotion_status", "")
        if status in {"shadow_benchmark", "deprecated", "non_edge_strategy", "deactivated"}:
            continue
        baseline = _load_baseline_oos(a.bot_id, a.strategy_id)
        current = _load_registry_oos(a.bot_id)
        if baseline is not None and current is not None:
            if baseline > 0:
                drift = (baseline - current) / baseline
                if drift > max_degradation:
                    st = "DRIFT"
                elif drift > max_degradation * 0.5:
                    st = "WARN"
                else:
                    st = "STEADY"
            else:
                drift = None
                st = "UNKNOWN"
        else:
            drift = None
            st = "UNKNOWN"
        rows.append(DriftRow(a.bot_id, a.strategy_id, baseline, current, drift, st))
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="strategy_drift_monitor")
    p.add_argument("--json", action="store_true")
    p.add_argument("--max-deg", type=float, default=0.35, help="max degradation before DRIFT flag")
    args = p.parse_args(argv)

    rows = run_drift_check(max_degradation=args.max_deg)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "bot_id": r.bot_id,
                        "strategy_id": r.strategy_id,
                        "baseline_oos": r.baseline_oos,
                        "current_oos": r.current_oos,
                        "drift_pct": r.drift_pct,
                        "status": r.status,
                    }
                    for r in rows
                ],
                indent=2,
                default=str,
            )
        )
    else:
        print(f"{'Bot':<24} {'Strategy':<28} {'Baseline':>10} {'Current':>10} {'Drift%':>8} {'Status'}")
        print("-" * 90)
        for r in rows:
            bstr = f"{r.baseline_oos:+.3f}" if r.baseline_oos is not None else "-"
            cstr = f"{r.current_oos:+.3f}" if r.current_oos is not None else "-"
            dstr = f"{r.drift_pct * 100:+.1f}%" if r.drift_pct is not None else "-"
            print(f"{r.bot_id:<24} {r.strategy_id:<28} {bstr:>10} {cstr:>10} {dstr:>8} {r.status}")
        drift_count = sum(1 for r in rows if r.status == "DRIFT")
        warn_count = sum(1 for r in rows if r.status == "WARN")
        print(
            f"\nDRIFT={drift_count} WARN={warn_count} STEADY={len(rows) - drift_count - warn_count} / {len(rows)} total"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

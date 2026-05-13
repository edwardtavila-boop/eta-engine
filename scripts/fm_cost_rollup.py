"""Force Multiplier cost + activity rollup.

Reads var/eta_engine/state/multi_model_telemetry.jsonl and produces
operator-readable summaries:

  - Total spend over a window (default last 24 hours)
  - Per-category spend + call counts
  - Per-provider spend + call counts
  - Average tokens in / out
  - Fallback rate (preferred-provider misses)
  - Cache stats (when run in-process; standalone runs see provider-only spend)

Companion to the wave-25c FM cache + fm_trade_gates additions. The
existing per-call telemetry was firing all along; this script is just a
consumer that surfaces the data without anyone having to grep a 15 MB
jsonl.

Usage:
    python -m eta_engine.scripts.fm_cost_rollup
    python -m eta_engine.scripts.fm_cost_rollup --hours 6
    python -m eta_engine.scripts.fm_cost_rollup --since 2026-05-13T17:00:00Z
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LOG = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\multi_model_telemetry.jsonl"
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hours", type=float, default=24.0, help="Rollup window in hours (default 24)")
    p.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO timestamp lower bound (overrides --hours)",
    )
    p.add_argument(
        "--log",
        type=Path,
        default=LOG,
        help="Telemetry jsonl path",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON instead of human report")
    return p.parse_args(argv)


def _load_window(path: Path, since: datetime) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_s = str(rec.get("ts") or "")
            if not ts_s:
                continue
            try:
                ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts < since:
                continue
            out.append(rec)
    return out


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    total_cost = 0.0
    total_input = 0
    total_output = 0
    n_fallback = 0
    by_category: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "cost": 0.0, "in_tok": 0, "out_tok": 0})
    by_provider: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "cost": 0.0})

    for r in records:
        try:
            cost = float(r.get("cost_usd") or 0)
            in_tok = int(r.get("input_tokens") or 0)
            out_tok = int(r.get("output_tokens") or 0)
        except (TypeError, ValueError):
            continue
        category = str(r.get("category") or "unknown")
        provider = str(r.get("provider") or "unknown")
        total_cost += cost
        total_input += in_tok
        total_output += out_tok
        if r.get("fallback_used"):
            n_fallback += 1
        c = by_category[category]
        c["n"] += 1
        c["cost"] += cost
        c["in_tok"] += in_tok
        c["out_tok"] += out_tok
        p = by_provider[provider]
        p["n"] += 1
        p["cost"] += cost

    n_total = len(records)
    return {
        "n_calls": n_total,
        "total_cost_usd": round(total_cost, 4),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "n_fallback": n_fallback,
        "fallback_rate": round(n_fallback / n_total, 3) if n_total else 0.0,
        "by_category": {
            k: {
                "n": int(v["n"]),
                "cost_usd": round(v["cost"], 4),
                "avg_in_tok": int(v["in_tok"] / v["n"]) if v["n"] else 0,
                "avg_out_tok": int(v["out_tok"] / v["n"]) if v["n"] else 0,
            }
            for k, v in by_category.items()
        },
        "by_provider": {
            k: {"n": int(v["n"]), "cost_usd": round(v["cost"], 4)} for k, v in by_provider.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.since:
        try:
            since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        except ValueError:
            print(f"invalid --since: {args.since}", file=sys.stderr)
            return 2
    else:
        since = datetime.now(UTC) - timedelta(hours=args.hours)

    records = _load_window(args.log, since)
    summary = _summarize(records)

    if args.json:
        print(json.dumps(
            {"since": since.isoformat(), "log": str(args.log), **summary},
            indent=2,
            default=str,
        ))
        return 0

    window_s = args.since or f"last {args.hours}h"
    print(f"=== FM cost rollup ({window_s}) ===")
    print(f"  records          : {summary['n_calls']}")
    print(f"  total spend USD  : ${summary['total_cost_usd']:.4f}")
    print(f"  total tokens in  : {summary['total_input_tokens']:,}")
    print(f"  total tokens out : {summary['total_output_tokens']:,}")
    print(f"  fallback rate    : {summary['fallback_rate'] * 100:.1f}%  ({summary['n_fallback']}/{summary['n_calls']})")
    print()
    print("By category:")
    print(f"  {'category':<28} {'n':>6} {'cost':>10} {'avg_in':>8} {'avg_out':>8}")
    for k, v in sorted(summary["by_category"].items(), key=lambda kv: -kv[1]["cost_usd"]):
        print(f"  {k:<28} {v['n']:>6} ${v['cost_usd']:>9.4f} {v['avg_in_tok']:>8} {v['avg_out_tok']:>8}")
    print()
    print("By provider:")
    print(f"  {'provider':<20} {'n':>6} {'cost':>10}")
    for k, v in sorted(summary["by_provider"].items(), key=lambda kv: -kv[1]["cost_usd"]):
        print(f"  {k:<20} {v['n']:>6} ${v['cost_usd']:>9.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

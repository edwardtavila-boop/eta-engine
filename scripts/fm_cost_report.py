"""Force-Multiplier daily cost report.

Reads the entire telemetry JSONL log and produces a per-day spend rollup
plus per-provider/per-category breakdowns. Run anytime to see where the
LLM budget went.

Usage
=====
::

    python -m eta_engine.scripts.fm_cost_report
    python -m eta_engine.scripts.fm_cost_report --days 7
    python -m eta_engine.scripts.fm_cost_report --json
    python -m eta_engine.scripts.fm_cost_report --since 2026-05-01

Why a separate tool (vs ``fm status``)
======================================
``fm status`` aggregates the last N records — fast for an at-a-glance
spend check. This tool reads the WHOLE log and bucketizes by date,
which is the right view for trend tracking, billing reconciliation,
and detecting cost spikes (e.g. a runaway chain that hammered DeepSeek).

The report is read-only — it never modifies the log. Safe to run during
live trading.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

# Workspace root on sys.path so this is invokable as a script.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eta_engine.brain.multi_model_telemetry import _resolve_log_path  # noqa: E402


def _parse_iso(ts: str) -> datetime | None:
    """Parse the ISO-8601 timestamp the telemetry writer produces."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _load_all_records(log_path: Path) -> list[dict]:
    """Read the whole JSONL log. One line per record; malformed lines skipped."""
    if not log_path.is_file():
        return []
    out: list[dict] = []
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _filter_by_window(
    records: list[dict],
    *,
    since: datetime | None,
    days: int | None,
) -> list[dict]:
    """Return records whose timestamp falls within the window."""
    if not records:
        return []

    cutoff: datetime | None = since
    if days is not None and cutoff is None:
        # last `days` days, ending now
        last_ts = _parse_iso(records[-1].get("ts", ""))
        if last_ts is None:
            return records
        cutoff = last_ts.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff -= __import__("datetime").timedelta(days=days - 1)

    if cutoff is None:
        return records

    out = []
    for r in records:
        ts = _parse_iso(r.get("ts", ""))
        if ts is None:
            continue
        if ts >= cutoff:
            out.append(r)
    return out


def _bucketize(records: list[dict]) -> dict:
    """Roll up records by date / provider / category. Returns a nested dict."""
    by_date: dict[str, dict] = defaultdict(lambda: {
        "calls": 0, "cost_usd": 0.0, "fallbacks": 0,
        "by_provider": defaultdict(lambda: {"calls": 0, "cost_usd": 0.0}),
        "by_category": defaultdict(lambda: {"calls": 0, "cost_usd": 0.0}),
    })

    for r in records:
        ts = _parse_iso(r.get("ts", ""))
        date_key = ts.date().isoformat() if ts else "unknown"
        cost = float(r.get("cost_usd") or 0)
        prov = r.get("actual_provider") or "unknown"
        cat = r.get("category") or "unknown"

        slot = by_date[date_key]
        slot["calls"] += 1
        slot["cost_usd"] += cost
        if r.get("fallback_used"):
            slot["fallbacks"] += 1
        slot["by_provider"][prov]["calls"] += 1
        slot["by_provider"][prov]["cost_usd"] += cost
        slot["by_category"][cat]["calls"] += 1
        slot["by_category"][cat]["cost_usd"] += cost

    # Convert defaultdicts to plain dicts for JSON-friendliness.
    out = {}
    for date_key, slot in sorted(by_date.items()):
        out[date_key] = {
            "calls": slot["calls"],
            "cost_usd": round(slot["cost_usd"], 6),
            "fallbacks": slot["fallbacks"],
            "by_provider": {
                p: {"calls": v["calls"], "cost_usd": round(v["cost_usd"], 6)}
                for p, v in slot["by_provider"].items()
            },
            "by_category": {
                c: {"calls": v["calls"], "cost_usd": round(v["cost_usd"], 6)}
                for c, v in slot["by_category"].items()
            },
        }
    return out


def _print_human(rollup: dict, *, total_cost: float, total_calls: int) -> None:
    bar = "=" * 70
    print(f"\n{bar}\nForce-Multiplier Cost Report\n{bar}")
    print(f"  total calls:  {total_calls}")
    print(f"  total spend:  ${total_cost:.6f}")
    print(f"  days covered: {len(rollup)}")
    if not rollup:
        print("\n  (no records in window)")
        return

    print(f"\n{'-' * 70}\nPer-day rollup\n{'-' * 70}")
    print(f"  {'DATE':12s}  {'CALLS':>6s}  {'COST':>12s}  {'FB':>3s}  TOP PROVIDER")
    for date, slot in rollup.items():
        # Pick the provider with the most calls for the per-day flavor line.
        if slot["by_provider"]:
            top_prov = max(slot["by_provider"].items(), key=lambda kv: kv[1]["calls"])
            top_str = f"{top_prov[0]} ({top_prov[1]['calls']})"
        else:
            top_str = ""
        fb = slot["fallbacks"]
        print(f"  {date:12s}  {slot['calls']:>6d}  ${slot['cost_usd']:>10.6f}  "
              f"{fb:>3d}  {top_str}")

    # Aggregate per-provider across the whole window.
    agg_prov: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0})
    agg_cat: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0})
    for slot in rollup.values():
        for p, v in slot["by_provider"].items():
            agg_prov[p]["calls"] += v["calls"]
            agg_prov[p]["cost_usd"] += v["cost_usd"]
        for c, v in slot["by_category"].items():
            agg_cat[c]["calls"] += v["calls"]
            agg_cat[c]["cost_usd"] += v["cost_usd"]

    print(f"\n{'-' * 70}\nBy provider (window total)\n{'-' * 70}")
    for prov, v in sorted(agg_prov.items(), key=lambda kv: -kv[1]["cost_usd"]):
        share = (v["cost_usd"] / total_cost * 100) if total_cost else 0
        print(f"  {prov:9s}  calls={v['calls']:>5d}  "
              f"cost=${v['cost_usd']:>10.6f}  ({share:>5.1f}%)")

    print(f"\n{'-' * 70}\nTop 10 categories by spend\n{'-' * 70}")
    sorted_cats = sorted(agg_cat.items(), key=lambda kv: -kv[1]["cost_usd"])[:10]
    for cat, v in sorted_cats:
        share = (v["cost_usd"] / total_cost * 100) if total_cost else 0
        print(f"  {cat:25s}  calls={v['calls']:>5d}  "
              f"cost=${v['cost_usd']:>10.6f}  ({share:>5.1f}%)")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Force-Multiplier daily cost report")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=None,
                       help="Limit to the last N days (default: all records)")
    group.add_argument("--since", type=str, default=None,
                       help="ISO date / datetime cutoff (e.g. 2026-05-01)")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of a table")
    args = parser.parse_args(argv)

    log_path = _resolve_log_path()
    records = _load_all_records(log_path)

    since: datetime | None = None
    if args.since:
        # Allow date-only or full ISO. Treat as UTC if no tzinfo provided.
        parsed = _parse_iso(args.since)
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(args.since + "T00:00:00")
            except ValueError:
                print(f"Could not parse --since '{args.since}'", file=sys.stderr)
                return 2
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        since = parsed

    filtered = _filter_by_window(records, since=since, days=args.days)
    rollup = _bucketize(filtered)
    total_cost = round(sum(slot["cost_usd"] for slot in rollup.values()), 6)
    total_calls = sum(slot["calls"] for slot in rollup.values())

    if args.json:
        print(json.dumps({
            "log_path": str(log_path),
            "total_calls": total_calls,
            "total_cost_usd": total_cost,
            "days": rollup,
        }, indent=2, default=str))
        return 0

    _print_human(rollup, total_cost=total_cost, total_calls=total_calls)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""JARVIS sage CLI -- consult all market-theory schools on a tape.

Inputs:
  --bars FILE   path to a JSON file containing a list of OHLCV bar dicts
  --csv  FILE   path to a CSV with columns: open,high,low,close,volume
  --side {long,short}
  --symbol SYMBOL
  --entry-price FLOAT
  --school NAME (repeatable)  consult only these schools

Outputs the SageReport as JSON (or --text for human-readable).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


def _bars_from_csv(path: Path) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            bars.append({
                "open":   float(row["open"]),
                "high":   float(row["high"]),
                "low":    float(row["low"]),
                "close":  float(row["close"]),
                "volume": float(row.get("volume", 0)),
                "ts":     row.get("ts") or row.get("timestamp") or "",
            })
    return bars


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    # Bars source is required for analysis but NOT for --list-schools, so the
    # mutually-exclusive group is optional at the argparse layer and we
    # enforce the "need at least one" rule manually below.
    src = p.add_mutually_exclusive_group(required=False)
    src.add_argument("--bars", type=Path, help="JSON file with list of OHLCV dicts")
    src.add_argument("--csv",  type=Path, help="CSV with open,high,low,close,volume cols")
    p.add_argument("--side", choices=["long", "short"], default="long")
    p.add_argument("--symbol", default="MNQ")
    p.add_argument("--entry-price", type=float, default=0.0)
    p.add_argument("--school", action="append", default=None,
                   help="Restrict to named school(s). Repeatable.")
    p.add_argument("--list-schools", action="store_true",
                   help="Print every school + its KNOWLEDGE block + exit")
    p.add_argument("--text", action="store_true", help="Human-readable output")
    p.add_argument("--explain", action="store_true",
                   help="Wave-6: also print the 1-paragraph narrative "
                        "(LLM if ANTHROPIC_API_KEY set, template otherwise)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from eta_engine.brain.jarvis_v3.sage import SCHOOLS, MarketContext, consult_sage

    if args.list_schools:
        print()
        for name, s in SCHOOLS.items():
            print(f"  === {name} (weight={s.WEIGHT}) ===")
            print(f"  {s.KNOWLEDGE}")
            print()
        return 0

    # Bars source IS required for everything except --list-schools.
    if not args.bars and not args.csv:
        print("error: one of --bars or --csv is required (or use --list-schools)",
              file=sys.stderr)
        return 2

    bars = (
        json.loads(args.bars.read_text(encoding="utf-8"))
        if args.bars
        else _bars_from_csv(args.csv)
    )

    if not isinstance(bars, list) or len(bars) < 30:
        print(f"error: need >= 30 bars, got {len(bars) if isinstance(bars, list) else '?'}",
              file=sys.stderr)
        return 1

    ctx = MarketContext(
        bars=bars,
        side=args.side,
        symbol=args.symbol,
        entry_price=args.entry_price,
    )
    enabled = set(args.school) if args.school else None
    report = consult_sage(ctx, enabled=enabled)

    if args.text:
        print()
        print("  SAGE REPORT")
        print("  ===========")
        print(f"  symbol={ctx.symbol} side={ctx.side} bars={ctx.n_bars}")
        print(f"  composite_bias={report.composite_bias.value} conviction={report.conviction:.2f}")
        print(f"  consensus={report.consensus_pct:.2f} alignment={report.alignment_score:.2f}")
        print(f"  schools: {report.schools_consulted} consulted, "
              f"{report.schools_aligned_with_entry} aligned, "
              f"{report.schools_disagreeing_with_entry} disagree, "
              f"{report.schools_neutral} neutral")
        print()
        for name, v in report.per_school.items():
            mark = "+" if v.aligned_with_entry else ("-" if v.bias.value != "neutral" else "·")
            print(f"  {mark} {name:<22} bias={v.bias.value:<7} conv={v.conviction:.2f}  {v.rationale}")
        print()
        print(f"  rationale: {report.rationale}")
        if args.explain:
            from eta_engine.brain.jarvis_v3.sage.narrative import explain_sage
            last_ts = bars[-1].get("ts") or bars[-1].get("timestamp") or ""
            narrative = explain_sage(report, symbol=ctx.symbol, bar_ts_key=str(last_ts))
            print()
            print("  NARRATIVE:")
            print(f"  {narrative}")
        print()
    else:
        out = {
            "composite_bias": report.composite_bias.value,
            "conviction": report.conviction,
            "consensus_pct": report.consensus_pct,
            "alignment_score": report.alignment_score,
            "schools_consulted": report.schools_consulted,
            "schools_aligned_with_entry": report.schools_aligned_with_entry,
            "schools_disagreeing_with_entry": report.schools_disagreeing_with_entry,
            "schools_neutral": report.schools_neutral,
            "summary_line": report.summary_line(),
            "per_school": {
                name: {
                    "bias": v.bias.value,
                    "conviction": v.conviction,
                    "aligned_with_entry": v.aligned_with_entry,
                    "rationale": v.rationale,
                    "signals": v.signals,
                }
                for name, v in report.per_school.items()
            },
        }
        print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

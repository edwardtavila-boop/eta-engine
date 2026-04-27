"""
EVOLUTIONARY TRADING ALGO  //  scripts.monthly_deep_review
==============================================
First-of-month full re-audit.

Pipeline
--------
1. Load closed trades (JSONL of ClosedTrade), MAE/MFE points, rationale
   records -- any or all may be missing (empty sections are acceptable).
2. Run every module over the data:
     * trade_grader       -> grade distribution A+/A/B/C/D/F
     * exit_quality       -> heatmap by (regime, setup) + leak $
     * rationale_miner    -> winner / loser clusters
     * gate telemetry     -> override rate
3. Distil top-3 proposed parameter tweaks from the findings.
4. Emit:
     * docs/monthly_review_YYYY_MM.json
     * docs/monthly_review_YYYY_MM.txt
     * docs/monthly_review_latest.json
     * docs/monthly_review_latest.txt

Not perfect but pays rent each month.

Usage
-----
    python -m eta_engine.scripts.monthly_deep_review \\
        --trades docs/closed_trades.jsonl \\
        --mae-mfe docs/mae_mfe_points.jsonl \\
        --rationales docs/rationales.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.backtest.exit_quality import (  # noqa: E402
    MaeMfePoint,
    analyze_batch,
    build_heatmap,
    money_left_on_table,
    rank_setups_by_leak,
)
from eta_engine.brain.rationale_miner import (  # noqa: E402
    RationaleMiner,
    RationaleRecord,
    coverage,
)
from eta_engine.core.trade_grader import (  # noqa: E402
    ClosedTrade,
    grade_many,
    leak_distribution,
)

DEFAULT_OUT_DIR = ROOT / "docs"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path | None, model: type[Any]) -> list[Any]:
    if path is None or not path.exists():
        return []
    items: list[Any] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            items.append(model(**json.loads(line)))
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"skip malformed line: {e}\n")
    return items


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _grade_block(trades: list[ClosedTrade]) -> dict:
    if not trades:
        return {"n": 0, "distribution": {}, "mean_total": None}
    grades = grade_many(trades)
    dist = leak_distribution(grades)
    mean_total = sum(g.total for g in grades) / len(grades)
    return {
        "n": len(grades),
        "distribution": dist,
        "mean_total": round(mean_total, 2),
        "best_n": min(5, len(grades)),
    }


def _exit_block(points: list[MaeMfePoint]) -> dict:
    if not points:
        return {"n": 0}
    rows = analyze_batch(points)
    heatmap = build_heatmap(rows)
    leak_usd = money_left_on_table(rows, dollars_per_r=100.0)
    ranked = rank_setups_by_leak(rows)
    return {
        "n": len(rows),
        "money_left_on_table_usd": leak_usd,
        "top_leaking_setups": [{"setup": s, "leak_r": round(r, 2)} for s, r in ranked[:5]],
        "heatmap": {f"{k[0]}|{k[1]}": v.model_dump(mode="json") for k, v in heatmap.items()},
    }


def _rationale_block(records: list[RationaleRecord]) -> dict:
    if not records:
        return {"n": 0}
    miner = RationaleMiner(min_cluster_size=3, max_ngram=2)
    report = miner.mine(records)
    return {
        "n": report.n_records,
        "coverage": coverage(report),
        "top_winners": [
            {"phrase": c.phrase, "n": c.n, "mean_r": c.mean_r, "win_rate": c.win_rate} for c in report.top_winners
        ],
        "top_losers": [
            {"phrase": c.phrase, "n": c.n, "mean_r": c.mean_r, "win_rate": c.win_rate} for c in report.top_losers
        ],
    }


def _propose_tweaks(
    grade_info: dict,
    exit_info: dict,
    rationale_info: dict,
) -> list[str]:
    """Cheap rule-based suggestions surfaced from the findings."""
    tweaks: list[str] = []
    dist = grade_info.get("distribution") or {}
    if dist:
        worst = max(dist, key=lambda k: dist[k] if k in {"D", "F"} else -1)
        if worst in {"D", "F"} and dist.get(worst, 0) >= 3:
            tweaks.append(
                f"high count of {worst} grades ({dist[worst]}) -- tighten setup filter or revisit checklist",
            )

    if exit_info.get("money_left_on_table_usd", 0) > 500:
        tweaks.append(
            "leaked >$500 in the month via early exits -- widen trail / add partial-take at 2R",
        )

    for loser in rationale_info.get("top_losers", [])[:2]:
        tweaks.append(
            f"cluster '{loser['phrase']}' "
            f"mean {loser['mean_r']}R / n={loser['n']} -- "
            "consider banning or re-classifying",
        )

    for winner in rationale_info.get("top_winners", [])[:1]:
        tweaks.append(
            f"cluster '{winner['phrase']}' mean +{winner['mean_r']}R / n={winner['n']} -- explicitly add to checklist",
        )

    return tweaks[:3]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_text(payload: dict) -> str:
    lines = [
        "=" * 72,
        f"EVOLUTIONARY TRADING ALGO  //  MONTHLY DEEP REVIEW -- {payload['period']}",
        f"generated: {payload['generated_at_utc']}",
        "=" * 72,
        "",
        "GRADE DISTRIBUTION",
    ]
    g = payload["grading"]
    if g["n"] == 0:
        lines.append("  (no trades)")
    else:
        lines.append(f"  n trades:    {g['n']}")
        lines.append(f"  mean total:  {g['mean_total']:.2f}")
        for letter, cnt in sorted(g["distribution"].items()):
            lines.append(f"  {letter:<4} {cnt}")
    lines.append("")
    lines.append("EXIT QUALITY")
    e = payload["exit_quality"]
    if e["n"] == 0:
        lines.append("  (no mae/mfe points)")
    else:
        lines.append(f"  n trades:    {e['n']}")
        lines.append(f"  $ leaked:    ${e['money_left_on_table_usd']:.2f}")
        if e["top_leaking_setups"]:
            lines.append("  top leaking setups:")
            for s in e["top_leaking_setups"]:
                lines.append(f"    - {s['setup']:<20} {s['leak_r']:>6.2f}R")
    lines.append("")
    lines.append("RATIONALE CLUSTERS")
    r = payload["rationales"]
    if r["n"] == 0:
        lines.append("  (no rationales)")
    else:
        lines.append(f"  n trades:    {r['n']}")
        lines.append(f"  coverage:    {r['coverage']:.0%}")
        if r["top_winners"]:
            lines.append("  top winners:")
            for c in r["top_winners"]:
                lines.append(
                    f"    + {c['phrase']:<30} n={c['n']:>3} mean_r={c['mean_r']:+.2f} wr={c['win_rate']:.0%}",
                )
        if r["top_losers"]:
            lines.append("  top losers:")
            for c in r["top_losers"]:
                lines.append(
                    f"    - {c['phrase']:<30} n={c['n']:>3} mean_r={c['mean_r']:+.2f} wr={c['win_rate']:.0%}",
                )
    lines.append("")
    lines.append("PROPOSED TWEAKS (top 3)")
    for i, t in enumerate(payload["proposed_tweaks"], 1):
        lines.append(f"  {i}. {t}")
    if not payload["proposed_tweaks"]:
        lines.append("  (none -- all good)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    *,
    trades_path: Path | None = None,
    mae_mfe_path: Path | None = None,
    rationales_path: Path | None = None,
    out_dir: Path = DEFAULT_OUT_DIR,
    now: datetime | None = None,
) -> dict:
    ts = now or datetime.now(UTC)
    period = f"{ts.year:04d}_{ts.month:02d}"

    trades = _load_jsonl(trades_path, ClosedTrade)
    points = _load_jsonl(mae_mfe_path, MaeMfePoint)
    rationales = _load_jsonl(rationales_path, RationaleRecord)

    grade_info = _grade_block(trades)
    exit_info = _exit_block(points)
    rationale_info = _rationale_block(rationales)
    tweaks = _propose_tweaks(grade_info, exit_info, rationale_info)

    payload = {
        "period": period,
        "generated_at_utc": ts.isoformat(),
        "grading": grade_info,
        "exit_quality": exit_info,
        "rationales": rationale_info,
        "proposed_tweaks": tweaks,
        "inputs": {
            "trades_path": str(trades_path) if trades_path else None,
            "mae_mfe_path": str(mae_mfe_path) if mae_mfe_path else None,
            "rationales_path": (str(rationales_path) if rationales_path else None),
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"monthly_review_{period}.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (out_dir / f"monthly_review_{period}.txt").write_text(
        _render_text(payload),
        encoding="utf-8",
    )
    (out_dir / "monthly_review_latest.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (out_dir / "monthly_review_latest.txt").write_text(
        _render_text(payload),
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EVOLUTIONARY TRADING ALGO monthly deep review",
    )
    parser.add_argument("--trades", type=Path, default=None)
    parser.add_argument("--mae-mfe", type=Path, default=None)
    parser.add_argument("--rationales", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)

    payload = run(
        trades_path=args.trades,
        mae_mfe_path=args.mae_mfe,
        rationales_path=args.rationales,
        out_dir=args.out_dir,
    )
    sys.stdout.write(
        f"[monthly_deep_review] period={payload['period']} "
        f"grades_n={payload['grading']['n']} "
        f"tweaks={len(payload['proposed_tweaks'])}\n",
    )
    return 0


if __name__ == "__main__":
    # Inform operator that it ran on expected day-of-month=1 (not enforced)
    today = date.today()
    if today.day != 1:
        sys.stdout.write(
            f"[monthly_deep_review] note: today is day-of-month={today.day} -- normally runs on the 1st\n",
        )
    sys.exit(main())

"""Unified JARVIS status command — single entry point for the operator.

Replaces 4 separate CLIs (trial_counter --status, dsr_projection,
gate_evaluator readouts, daily reports). One command, full picture.

Usage:
    /c/Python314/python -m eta_engine.scripts.jarvis_status
    /c/Python314/python -m eta_engine.scripts.jarvis_status --health
    /c/Python314/python -m eta_engine.scripts.jarvis_status --recommend
    /c/Python314/python -m eta_engine.scripts.jarvis_status --explain <code>
    /c/Python314/python -m eta_engine.scripts.jarvis_status --daily
"""

from __future__ import annotations

import argparse
import json
import sys

from eta_engine.brain.jarvis_daily_report import (
    generate_daily_report,
)
from eta_engine.brain.jarvis_daily_report import (
    render_markdown as render_daily,
)
from eta_engine.brain.jarvis_explainer import (
    KNOWN_REASON_CODES,
    explain,
)
from eta_engine.brain.jarvis_explainer import (
    render_markdown as render_explanation,
)
from eta_engine.brain.jarvis_health import HealthVerdict, run_self_test
from eta_engine.brain.jarvis_recommender import (
    recommend,
)
from eta_engine.brain.jarvis_session_state import render_summary, snapshot


def _print_status() -> int:
    """Default: print a concise status block."""
    snap = snapshot()
    summary = render_summary(snap)
    recs = recommend(snap)
    print("=== JARVIS STATUS ===")
    print(f"Phase:                 {summary['phase']}")
    print(f"Freeze:                {summary['freeze']}")
    print(f"Cumulative trials:     {summary['cumulative_trials']}")
    print(f"Trial budget remaining: {summary['trial_budget_remaining']} ({summary['trial_budget_alert']})")
    print(f"Slow bleed:            {summary['slow_bleed']} (rolling {summary['rolling_exp_R']})")
    print(f"Regime:                {summary['regime']} (composite {summary['regime_composite']})")
    print(f"Gate report:           {summary['gate_report']} ({summary['auto_gates']})")
    print(f"Gate report stale:     {summary['gate_report_stale']}")
    print(f"Applicable lessons:    {summary['applicable_lessons']}")
    print()
    if recs:
        print(f"=== {len(recs)} RECOMMENDATION(S) ===")
        for r in recs:
            print(f"[{r.level.value}] {r.code}: {r.title}")
        print()
        print("Run `--recommend` for full details on each.")
    else:
        print("=== NO ACTIVE RECOMMENDATIONS ===")
    return 0


def _print_health() -> int:
    results, verdict = run_self_test()
    print(f"=== JARVIS HEALTH: {verdict.value} ===")
    for r in results:
        marker = "PASS" if r.passed else "FAIL"
        print(f"  [{marker}] {r.name}: {r.detail}")
    return 0 if verdict is HealthVerdict.HEALTHY else (1 if verdict is HealthVerdict.DEGRADED else 2)


def _print_recommendations() -> int:
    snap = snapshot()
    recs = recommend(snap)
    if not recs:
        print("=== NO ACTIVE RECOMMENDATIONS ===")
        return 0
    print(f"=== {len(recs)} ACTIVE RECOMMENDATION(S) ===")
    print()
    for r in recs:
        print(f"## [{r.level.value}] {r.title}")
        print(f"   code: `{r.code}`")
        print(f"   rationale: {r.rationale}")
        if r.action:
            print(f"   action: {r.action}")
        if r.lesson_refs:
            print(f"   lessons: {', '.join(f'#{n}' for n in r.lesson_refs)}")
        print()
    return 0


def _print_explain(code: str) -> int:
    exp = explain(code)
    if exp is None:
        known = sorted(KNOWN_REASON_CODES.keys())
        print(f"ERROR: unknown reason_code '{code}'", file=sys.stderr)
        print(f"Known codes: {', '.join(known)}", file=sys.stderr)
        return 1
    print(render_explanation(exp))
    return 0


def _print_daily() -> int:
    report = generate_daily_report()
    print(render_daily(report))
    return 0


def _print_json() -> int:
    """Machine-readable status for dashboards / pipelines."""
    snap = snapshot()
    recs = recommend(snap)
    health_results, verdict = run_self_test()
    out = {
        "session_state": snap.model_dump(mode="json"),
        "recommendations": [r.model_dump(mode="json") for r in recs],
        "health_verdict": verdict.value,
        "health_results": [r.model_dump(mode="json") for r in health_results],
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--health", action="store_true", help="Run JARVIS health self-test")
    ap.add_argument("--recommend", action="store_true", help="Full recommendation list with rationale")
    ap.add_argument(
        "--explain", default=None, metavar="CODE", help="Explain a JARVIS reason_code (e.g. slow_bleed_tripped)"
    )
    ap.add_argument("--daily", action="store_true", help="Generate the end-of-day markdown report")
    ap.add_argument("--json", action="store_true", help="Machine-readable JSON output (for dashboards)")
    args = ap.parse_args(argv)

    if args.health:
        return _print_health()
    if args.recommend:
        return _print_recommendations()
    if args.explain is not None:
        return _print_explain(args.explain)
    if args.daily:
        return _print_daily()
    if args.json:
        return _print_json()
    return _print_status()


if __name__ == "__main__":
    raise SystemExit(main())

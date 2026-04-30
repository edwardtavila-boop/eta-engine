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
from pathlib import Path

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


def _operator_queue_summary(*, limit: int = 5) -> dict[str, object]:
    """Return a compact, fail-soft operator queue snapshot for dashboards."""
    try:
        from eta_engine.scripts import operator_action_queue

        items = operator_action_queue.collect_items()
    except Exception as exc:  # noqa: BLE001 -- status JSON must stay readable
        return {
            "source": "operator_action_queue",
            "error": str(exc),
            "summary": {},
            "top_blockers": [],
        }

    verdict_order = (
        operator_action_queue.VERDICT_DONE,
        operator_action_queue.VERDICT_BLOCKED,
        operator_action_queue.VERDICT_OBSERVED,
        operator_action_queue.VERDICT_UNKNOWN,
    )
    summary = {
        verdict: sum(1 for item in items if item.verdict == verdict)
        for verdict in verdict_order
    }
    def blocker_priority(item: object) -> tuple[int, str]:
        evidence = getattr(item, "evidence", {})
        severity = evidence.get("overall_severity") if isinstance(evidence, dict) else None
        severity_rank = {"red": 0, "amber": 1}.get(str(severity), 2)
        return (severity_rank, str(getattr(item, "op_id", "")))

    blocked_items = sorted(
        (item for item in items if item.verdict == operator_action_queue.VERDICT_BLOCKED),
        key=blocker_priority,
    )

    def next_actions_for_item(item: object) -> list[str]:
        evidence = getattr(item, "evidence", {})
        actions: list[str] = []
        if isinstance(evidence, dict):
            blockers = evidence.get("blockers")
            if isinstance(blockers, list):
                for blocker in blockers:
                    if not isinstance(blocker, dict):
                        continue
                    commands = blocker.get("next_commands")
                    if isinstance(commands, list):
                        actions.extend(str(command) for command in commands if command)
        if not actions:
            where = str(getattr(item, "where", "") or "").strip()
            if where:
                actions.append(where)
        return list(dict.fromkeys(actions))

    blockers = [
        {
            "op_id": item.op_id,
            "title": item.title,
            "detail": item.detail,
            "where": item.where,
            "evidence": item.evidence,
            "next_actions": next_actions_for_item(item),
        }
        for item in blocked_items
    ]
    next_actions = [
        action
        for blocker in blockers
        for action in blocker.get("next_actions", [])
    ][:limit]
    return {
        "source": "operator_action_queue",
        "error": None,
        "summary": summary,
        "top_blockers": blockers[:limit],
        "next_actions": next_actions,
    }


def build_operator_queue_summary(*, limit: int = 5) -> dict[str, object]:
    """Public dashboard helper for the current operator blocker queue."""
    return _operator_queue_summary(limit=limit)


def _empty_bot_strategy_readiness_payload(
    *,
    status: str,
    path: Path,
    error: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": "bot_strategy_readiness",
        "path": str(path),
        "status": status,
        "summary": {},
        "row_count": 0,
        "rows": [],
        "rows_by_bot": {},
        "top_actions": [],
    }
    if error:
        payload["error"] = error
    return payload


def _bot_strategy_action_priority(row: dict[str, object]) -> tuple[int, str]:
    lane = str(row.get("launch_lane") or "")
    lane_rank = {
        "blocked_data": 0,
        "live_preflight": 1,
        "paper_soak": 2,
        "shadow_only": 3,
        "research": 4,
        "non_edge": 5,
        "deactivated": 6,
    }.get(lane, 7)
    return (lane_rank, str(row.get("bot_id") or ""))


def build_bot_strategy_readiness_summary(
    *,
    path: Path | None = None,
    limit: int = 5,
) -> dict[str, object]:
    """Load the canonical bot strategy readiness snapshot for JARVIS surfaces."""
    from eta_engine.scripts import workspace_roots

    target = Path(path) if path is not None else workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH
    try:
        raw_payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_bot_strategy_readiness_payload(status="missing", path=target)
    except (OSError, json.JSONDecodeError) as exc:
        return _empty_bot_strategy_readiness_payload(status="unreadable", path=target, error=str(exc))

    if not isinstance(raw_payload, dict):
        return _empty_bot_strategy_readiness_payload(
            status="unreadable",
            path=target,
            error="bot strategy readiness snapshot must be a JSON object",
        )

    summary = raw_payload.get("summary")
    rows = raw_payload.get("rows")
    summary_payload = summary if isinstance(summary, dict) else {}
    row_payloads = [dict(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
    rows_by_bot = {
        bot_id: row
        for row in row_payloads
        if (bot_id := str(row.get("bot_id") or row.get("id") or row.get("name") or "").strip())
    }
    top_actions: list[dict[str, object]] = []
    for row in sorted(
        (item for item in row_payloads if item.get("next_action")),
        key=_bot_strategy_action_priority,
    ):
        if len(top_actions) >= max(0, limit):
            break
        top_actions.append(
            {
                "bot_id": row.get("bot_id"),
                "strategy_id": row.get("strategy_id"),
                "launch_lane": row.get("launch_lane"),
                "data_status": row.get("data_status"),
                "promotion_status": row.get("promotion_status"),
                "can_paper_trade": bool(row.get("can_paper_trade")),
                "can_live_trade": bool(row.get("can_live_trade")),
                "next_action": row.get("next_action"),
            }
        )

    return {
        "source": "bot_strategy_readiness",
        "path": str(target),
        "status": "ready",
        "schema_version": raw_payload.get("schema_version"),
        "generated_at": raw_payload.get("generated_at"),
        "summary": summary_payload,
        "row_count": len(row_payloads),
        "rows": row_payloads,
        "rows_by_bot": rows_by_bot,
        "top_actions": top_actions,
    }


def _format_bot_strategy_readiness(readiness: dict[str, object]) -> str:
    status = str(readiness.get("status") or "unknown")
    summary = readiness.get("summary")
    if status != "ready" or not isinstance(summary, dict):
        return f"{status} snapshot"

    lanes = summary.get("launch_lanes")
    lane_bits: list[str] = []
    if isinstance(lanes, dict):
        for lane in (
            "live_preflight",
            "paper_soak",
            "shadow_only",
            "research",
            "non_edge",
            "blocked_data",
            "deactivated",
        ):
            value = lanes.get(lane)
            if value is not None:
                lane_bits.append(f"{lane}={value}")
    if not lane_bits:
        total = summary.get("total_bots", 0)
        lane_bits.append(f"total_bots={total}")
    if "can_paper_trade" in summary:
        lane_bits.append(f"paper_ready={summary.get('can_paper_trade')}")
    if "can_live_any" in summary:
        lane_bits.append(f"live_any={summary.get('can_live_any')}")
    return " ".join(lane_bits)


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
    op_queue = build_operator_queue_summary(limit=1)
    op_summary = op_queue.get("summary") if isinstance(op_queue, dict) else {}
    op_blocked = op_summary.get("BLOCKED", 0) if isinstance(op_summary, dict) else 0
    op_first = ""
    top_blockers = op_queue.get("top_blockers") if isinstance(op_queue, dict) else []
    if isinstance(top_blockers, list) and top_blockers:
        first = top_blockers[0]
        if isinstance(first, dict):
            op_first = str(first.get("op_id") or "")
    print(f"Operator blockers:     {op_blocked}{f' (top {op_first})' if op_first else ''}")
    bot_readiness = build_bot_strategy_readiness_summary(limit=3)
    print(f"Bot readiness:         {_format_bot_strategy_readiness(bot_readiness)}")
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
        "operator_queue": build_operator_queue_summary(),
        "bot_strategy_readiness": build_bot_strategy_readiness_summary(),
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

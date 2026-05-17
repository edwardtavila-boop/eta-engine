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
            "launch_blocked_count": 0,
            "top_launch_blockers": [],
            "launch_next_actions": [],
        }

    verdict_order = (
        operator_action_queue.VERDICT_DONE,
        operator_action_queue.VERDICT_BLOCKED,
        operator_action_queue.VERDICT_OBSERVED,
        operator_action_queue.VERDICT_UNKNOWN,
    )
    summary = {verdict: sum(1 for item in items if item.verdict == verdict) for verdict in verdict_order}

    def blocker_priority(item: object) -> tuple[int, str]:
        evidence = getattr(item, "evidence", {})
        severity = evidence.get("overall_severity") if isinstance(evidence, dict) else None
        severity_rank = {"red": 0, "amber": 1}.get(str(severity), 2)
        return (severity_rank, str(getattr(item, "op_id", "")))

    blocked_items = sorted(
        (item for item in items if item.verdict == operator_action_queue.VERDICT_BLOCKED),
        key=blocker_priority,
    )

    def is_launch_blocker(item: object) -> bool:
        evidence = getattr(item, "evidence", {})
        if not isinstance(evidence, dict):
            return True
        return evidence.get("launch_blocker") is not False

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
    launch_blockers = [
        blocker
        for blocker in blockers
        if not isinstance(blocker.get("evidence"), dict) or blocker["evidence"].get("launch_blocker") is not False
    ]
    next_actions = [action for blocker in blockers for action in blocker.get("next_actions", [])][:limit]
    launch_next_actions = [action for blocker in launch_blockers for action in blocker.get("next_actions", [])][:limit]
    return {
        "source": "operator_action_queue",
        "error": None,
        "summary": summary,
        "top_blockers": blockers[:limit],
        "next_actions": next_actions,
        "launch_blocked_count": sum(1 for item in blocked_items if is_launch_blocker(item)),
        "top_launch_blockers": launch_blockers[:limit],
        "launch_next_actions": launch_next_actions,
    }


def build_operator_queue_summary(*, limit: int = 5) -> dict[str, object]:
    """Public dashboard helper for the current operator blocker queue."""
    return _operator_queue_summary(limit=limit)


def _empty_second_brain_payload(
    *,
    status: str,
    error: str | None = None,
) -> dict[str, object]:
    playbook = {
        "eligible_patterns": 0,
        "best_patterns": [],
        "worst_patterns": [],
        "favor_patterns": [],
        "avoid_patterns": [],
        "truth_note": "Memory-derived pattern stats only; require broker-backed closes and live gates before promotion.",
    }
    payload: dict[str, object] = {
        "source": "jarvis_status.second_brain",
        "status": status,
        "error": error,
        "n_episodes": 0,
        "win_rate": 0.0,
        "avg_r": 0.0,
        "semantic_patterns": 0,
        "procedural_versions": 0,
        "last_episode": None,
        "best_procedural_version": None,
        "top_patterns": [],
        "playbook": playbook,
        "truth_note": playbook["truth_note"],
        "paths": {},
        "sources": {},
        "legacy_sources_active": False,
    }
    return payload


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def build_second_brain_summary(
    *,
    top_n: int = 5,
    min_episodes: int = 30,
    memory: object | None = None,
) -> dict[str, object]:
    """Public dashboard helper for JARVIS hierarchical memory."""
    try:
        from eta_engine.brain.jarvis_v3.admin_query import (
            second_brain_playbook,
            second_brain_snapshot,
        )

        if memory is None:
            from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

            memory = HierarchicalMemory()
        snapshot_payload = second_brain_snapshot(memory, top_n=top_n)  # type: ignore[arg-type]
        playbook_payload = second_brain_playbook(  # type: ignore[arg-type]
            memory,
            min_episodes=min_episodes,
            top_n=top_n,
        )
    except Exception as exc:  # noqa: BLE001 -- status JSON must stay readable
        return _empty_second_brain_payload(status="unavailable", error=str(exc))

    if not isinstance(snapshot_payload, dict):
        return _empty_second_brain_payload(status="unavailable", error="second brain snapshot returned non-object")
    if not isinstance(playbook_payload, dict):
        playbook_payload = (
            snapshot_payload.get("playbook")
            if isinstance(snapshot_payload.get("playbook"), dict)
            else _empty_second_brain_payload(status="unavailable")["playbook"]
        )

    paths = snapshot_payload.get("paths") if isinstance(snapshot_payload.get("paths"), dict) else {}
    sources = snapshot_payload.get("sources") if isinstance(snapshot_payload.get("sources"), dict) else {}
    legacy_sources_active = any(
        str(source_path or "") and str(source_path or "") != str(paths.get(name) or "")
        for name, source_path in sources.items()
    )
    playbook = dict(playbook_payload)
    truth_note = str(
        playbook.get("truth_note")
        or "Memory-derived pattern stats only; require broker-backed closes and live gates before promotion."
    )

    return {
        "source": "jarvis_status.second_brain",
        "status": str(snapshot_payload.get("status") or "unknown"),
        "error": snapshot_payload.get("error") or playbook.get("error"),
        "n_episodes": _coerce_int(snapshot_payload.get("n_episodes")),
        "win_rate": round(_coerce_float(snapshot_payload.get("win_rate")), 3),
        "avg_r": round(_coerce_float(snapshot_payload.get("avg_r")), 4),
        "semantic_patterns": _coerce_int(snapshot_payload.get("semantic_patterns")),
        "procedural_versions": _coerce_int(snapshot_payload.get("procedural_versions")),
        "last_episode": (
            snapshot_payload.get("last_episode") if isinstance(snapshot_payload.get("last_episode"), dict) else None
        ),
        "best_procedural_version": (
            snapshot_payload.get("best_procedural_version")
            if isinstance(snapshot_payload.get("best_procedural_version"), dict)
            else None
        ),
        "top_patterns": _dict_list(snapshot_payload.get("top_patterns")),
        "playbook": playbook,
        "truth_note": truth_note,
        "paths": dict(paths),
        "sources": dict(sources),
        "legacy_sources_active": legacy_sources_active,
    }


def _format_second_brain_summary(payload: dict[str, object]) -> str:
    status = str(payload.get("status") or "unknown")
    n_episodes = _coerce_int(payload.get("n_episodes"))
    win_rate = _coerce_float(payload.get("win_rate"))
    avg_r = _coerce_float(payload.get("avg_r"))
    playbook = payload.get("playbook") if isinstance(payload.get("playbook"), dict) else {}
    eligible = _coerce_int(playbook.get("eligible_patterns") if isinstance(playbook, dict) else 0)
    favor_count = (
        len(playbook.get("favor_patterns") or []) if isinstance(playbook.get("favor_patterns"), list) else 0
    )
    avoid_count = (
        len(playbook.get("avoid_patterns") or []) if isinstance(playbook.get("avoid_patterns"), list) else 0
    )
    source_note = " legacy_source" if payload.get("legacy_sources_active") else ""
    return (
        f"{status} episodes={n_episodes} win_rate={win_rate:.3f} avg_r={avg_r:.4f} "
        f"eligible_patterns={eligible} favor={favor_count} avoid={avoid_count}{source_note}"
    )


def _empty_dirty_worktree_reconciliation_payload(
    *,
    status: str,
    path: Path,
    error: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": "jarvis_status.dirty_worktree_reconciliation",
        "path": str(path),
        "status": status,
        "error": error,
        "ready": False,
        "action": "",
        "dirty_modules": [],
        "blocking_modules": [],
        "module_summaries": [],
        "review_batches": [],
        "review_slices": [],
        "next_actions": [],
        "safety": {},
    }
    return payload


def build_dirty_worktree_reconciliation_summary(
    *,
    path: Path | None = None,
    limit: int = 3,
) -> dict[str, object]:
    """Load the canonical dirty-worktree reconciliation plan for JARVIS surfaces."""
    from eta_engine.scripts import workspace_roots

    target = Path(path) if path is not None else workspace_roots.ETA_DIRTY_WORKTREE_RECONCILIATION_PATH
    try:
        raw_payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_dirty_worktree_reconciliation_payload(status="missing", path=target)
    except (OSError, json.JSONDecodeError) as exc:
        return _empty_dirty_worktree_reconciliation_payload(status="unreadable", path=target, error=str(exc))

    if not isinstance(raw_payload, dict):
        return _empty_dirty_worktree_reconciliation_payload(
            status="unreadable",
            path=target,
            error="dirty worktree reconciliation plan must be a JSON object",
        )

    modules = raw_payload.get("modules") if isinstance(raw_payload.get("modules"), dict) else {}
    dirty_modules = [str(item) for item in raw_payload.get("dirty_modules", []) if str(item).strip()] if isinstance(
        raw_payload.get("dirty_modules"),
        list,
    ) else []
    blocking_modules = [
        str(item) for item in raw_payload.get("blocking_modules", []) if str(item).strip()
    ] if isinstance(raw_payload.get("blocking_modules"), list) else []
    module_summaries: list[dict[str, object]] = []
    for name in dirty_modules:
        module = modules.get(name)
        if not isinstance(module, dict):
            continue
        dirty_summary = module.get("dirty_summary") if isinstance(module.get("dirty_summary"), dict) else {}
        review_groups = module.get("review_groups") if isinstance(module.get("review_groups"), list) else []
        module_summaries.append(
            {
                "module": name,
                "gitlink": module.get("gitlink"),
                "recommended_handling": module.get("recommended_handling"),
                "entry_count": _coerce_int(dirty_summary.get("entry_count") if isinstance(dirty_summary, dict) else 0),
                "top_groups": _dict_list(dirty_summary.get("top_groups") if isinstance(dirty_summary, dict) else [])[
                    : max(0, limit)
                ],
                "review_groups": _dict_list(review_groups)[: max(0, limit)],
            }
        )

    ready = bool(raw_payload.get("ready"))
    raw_status = str(raw_payload.get("status") or "").strip()
    status = raw_status or ("ready" if ready else "review_required" if dirty_modules or blocking_modules else "unknown")
    next_actions = (
        [str(item) for item in raw_payload.get("next_actions", []) if str(item).strip()]
        if isinstance(raw_payload.get("next_actions"), list)
        else []
    )
    review_batches = _dict_list(raw_payload.get("review_batches"))[: max(0, limit)]
    review_slices = _dict_list(raw_payload.get("review_slices"))[: max(0, limit)]
    safety = raw_payload.get("safety") if isinstance(raw_payload.get("safety"), dict) else {}
    return {
        "source": "jarvis_status.dirty_worktree_reconciliation",
        "path": str(target),
        "status": status,
        "error": None,
        "generated_at": raw_payload.get("generated_at"),
        "ready": ready,
        "action": str(raw_payload.get("action") or ""),
        "dirty_modules": dirty_modules,
        "blocking_modules": blocking_modules,
        "module_summaries": module_summaries,
        "review_batches": review_batches,
        "review_slices": review_slices,
        "next_actions": next_actions[: max(0, limit)],
        "safety": dict(safety),
    }


def _format_dirty_worktree_reconciliation(payload: dict[str, object]) -> str:
    status = str(payload.get("status") or "unknown")
    dirty_modules = payload.get("dirty_modules") if isinstance(payload.get("dirty_modules"), list) else []
    module_summaries = (
        payload.get("module_summaries") if isinstance(payload.get("module_summaries"), list) else []
    )
    group_bits: list[str] = []
    for module in module_summaries:
        if not isinstance(module, dict):
            continue
        module_name = str(module.get("module") or "")
        top_groups = module.get("top_groups") if isinstance(module.get("top_groups"), list) else []
        groups = [
            f"{item.get('group')}={item.get('count')}"
            for item in top_groups
            if isinstance(item, dict) and item.get("group") and item.get("count") is not None
        ]
        if module_name and groups:
            group_bits.append(f"{module_name}:{','.join(groups)}")
    if group_bits:
        return f"{status} {'; '.join(group_bits)}"
    if dirty_modules:
        return f"{status} dirty_modules={','.join(str(item) for item in dirty_modules)}"
    return status


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


def _bot_strategy_action_priority(row: dict[str, object]) -> tuple[int, int, int, str]:
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
    try:
        capital_priority = int(row.get("capital_priority") or 999_999)
    except (TypeError, ValueError):
        capital_priority = 999_999
    blocked_rank = 0 if lane == "blocked_data" else 1
    return (blocked_rank, capital_priority, lane_rank, str(row.get("bot_id") or ""))


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
                "priority_bucket": row.get("priority_bucket"),
                "capital_priority": row.get("capital_priority"),
                "preferred_broker_stack": row.get("preferred_broker_stack"),
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
    second_brain = build_second_brain_summary(top_n=3)
    print(f"Second brain:          {_format_second_brain_summary(second_brain)}")
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
    dirty_worktree = build_dirty_worktree_reconciliation_summary(limit=3)
    print(f"Repo reconciliation:   {_format_dirty_worktree_reconciliation(dirty_worktree)}")
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
        "second_brain": build_second_brain_summary(),
        "bot_strategy_readiness": build_bot_strategy_readiness_summary(),
        "dirty_worktree_reconciliation": build_dirty_worktree_reconciliation_summary(),
        "sage": _collect_sage_state(),
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def _collect_sage_state() -> dict:
    try:
        from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker
        from eta_engine.brain.jarvis_v3.sage.health import default_monitor
        from eta_engine.brain.jarvis_v3.sage.last_report_cache import cache_size

        tracker = default_tracker()
        monitor = default_monitor()
        edges = tracker.snapshot()
        issues = monitor.check_health()
        return {
            "edge_tracker": {
                "n_schools": len(edges),
                "top_by_expectancy": sorted(
                    [{"school": k, **v} for k, v in edges.items()],
                    key=lambda x: x.get("expectancy", 0),
                    reverse=True,
                )[:5],
                "bottom_by_expectancy": sorted(
                    [{"school": k, **v} for k, v in edges.items()],
                    key=lambda x: x.get("expectancy", 0),
                )[:5],
            },
            "health": {
                "n_degraded": len(issues),
                "issues": [{"school": i.school, "severity": i.severity, "detail": i.detail} for i in issues],
            },
            "cache_entries": cache_size(),
        }
    except Exception:
        return {"status": "unavailable"}


def _print_sage_state() -> int:
    state = _collect_sage_state()
    print(json.dumps(state, indent=2, default=str))
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
    ap.add_argument("--sage", action="store_true", help="Print Sage health, edge tracker, and cache state")
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
    if args.sage:
        return _print_sage_state()
    return _print_status()


if __name__ == "__main__":
    raise SystemExit(main())

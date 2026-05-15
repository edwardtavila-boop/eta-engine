"""Build the paper-only diamond retune campaign from broker edge evidence.

The edge audit answers what is weak. This script converts that queue into
the 24/7 worklist: the top research missions to run next, their exact
registry-backed command, and the safety rails that prevent any live routing
mutation from being implied by a research PASS.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts import workspace_roots  # noqa: E402

DEFAULT_AUDIT_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "diamond_edge_audit_latest.json"
OUT_LATEST = workspace_roots.ETA_RUNTIME_STATE_DIR / "diamond_retune_campaign_latest.json"

SAFETY_RAILS = [
    "Paper research only: no broker orders, no live routing edits, no registry mutation.",
    "Promotion remains blocked until fresh broker closes prove positive PnL and acceptable profit factor.",
    "Live size/risk changes require explicit operator approval after review.",
]


def _as_float(value: Any, default: float = 0.0) -> float:  # noqa: ANN401
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[str]:  # noqa: ANN401
    if isinstance(value, list):
        return [str(item) for item in value]
    if value in (None, ""):
        return []
    return [str(value)]


def _sort_queue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: _as_float(row.get("priority_score")), reverse=True)


def _target(row: dict[str, Any], rank: int) -> dict[str, Any]:
    bot_id = str(row.get("bot_id") or "unknown")
    return {
        "rank": rank,
        "bot_id": bot_id,
        "symbol": str(row.get("symbol") or ""),
        "asset_sleeve": str(row.get("asset_sleeve") or "unknown"),
        "strategy_kind": str(row.get("strategy_kind") or ""),
        "issue_code": str(row.get("issue_code") or "edge_unproven"),
        "priority_score": round(_as_float(row.get("priority_score")), 2),
        "worst_session": str(row.get("worst_session") or "unknown"),
        "best_session": str(row.get("best_session") or "unknown"),
        "parameter_focus": _as_list(row.get("parameter_focus")),
        "primary_experiment": str(row.get("primary_experiment") or ""),
        "next_command": str(row.get("retune_command") or ""),
        "promotion_block": "broker_proof_required",
        "live_mutation_policy": "paper_only_advisory",
        "safe_to_mutate_live": False,
    }


def _first_command(commands: list[str], needle: str) -> str:
    for command in commands:
        if needle in command:
            return command
    return ""


def _research_signal(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    source = evidence.get("full_history_smoke")
    if not isinstance(source, dict):
        source = evidence
    return {
        "agg_oos_sharpe": _as_float(source.get("agg_oos_sharpe")),
        "dsr_pass_fraction": _as_float(source.get("dsr_pass_fraction")),
        "strict_gate": bool(source.get("strict_gate")),
        "windows": int(_as_float(source.get("windows"))),
    }


def _research_target(row: dict[str, Any], rank: int) -> dict[str, Any]:
    bot_id = str(row.get("name") or row.get("bot_id") or "unknown")
    commands = _as_list(row.get("next_commands"))
    next_command = _first_command(commands, "run_research_grid") or (commands[0] if commands else "")
    verification_command = _first_command(commands, "paper_live_launch_check")
    return {
        "rank": rank,
        "bot_id": bot_id,
        "strategy_id": str(row.get("strategy_id") or bot_id),
        "issue_code": "research_gate_failed",
        "summary": str(row.get("summary") or "research candidate gate not fully passed"),
        "research_signal": _research_signal(row),
        "next_command": next_command,
        "verification_command": verification_command,
        "promotion_block": "research_gate_required",
        "live_mutation_policy": "paper_only_advisory",
        "safe_to_mutate_live": False,
    }


def _load_op16_research_backlog() -> list[dict[str, Any]]:
    try:
        from eta_engine.scripts import operator_action_queue

        item = operator_action_queue._op16_strategy_research_candidates()  # noqa: SLF001
    except Exception:
        return []
    evidence = item.evidence if isinstance(item.evidence, dict) else {}
    blockers = evidence.get("blockers")
    return [row for row in blockers if isinstance(row, dict)] if isinstance(blockers, list) else []


def build_campaign(
    audit: dict[str, Any],
    *,
    limit: int = 0,
    research_backlog: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    queue_raw = audit.get("retune_queue")
    queue = [row for row in queue_raw if isinstance(row, dict)] if isinstance(queue_raw, list) else []
    ranked = _sort_queue(queue)
    selected = ranked[:limit] if limit > 0 else ranked
    targets = [_target(row, rank=i + 1) for i, row in enumerate(selected)]
    top = targets[0] if targets else {}
    backlog_raw = research_backlog or []
    backlog_targets = [_research_target(row, rank=i + 1) for i, row in enumerate(backlog_raw)]
    backlog_top = backlog_targets[0] if backlog_targets else {}
    audit_summary = audit.get("summary") if isinstance(audit.get("summary"), dict) else {}
    return {
        "kind": "eta_diamond_retune_campaign",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "summary": {
            "n_bots_in_audit": int(_as_float(audit_summary.get("n_bots"))),
            "n_available_targets": len(ranked),
            "n_selected_targets": len(targets),
            "n_research_backlog_targets": len(backlog_targets),
            "top_bot": top.get("bot_id"),
            "top_priority_score": top.get("priority_score"),
            "top_research_backlog_bot": backlog_top.get("bot_id"),
            "execution_mode": "paper_research_only",
            "safe_to_mutate_live": False,
            "scoring_basis": audit_summary.get("scoring_basis") or "broker_closed_trade_pnl_first",
        },
        "safety_rails": SAFETY_RAILS,
        "targets": targets,
        "research_backlog": backlog_targets,
    }


def load_audit(path: Path = DEFAULT_AUDIT_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run(*, audit_path: Path = DEFAULT_AUDIT_PATH, out_path: Path = OUT_LATEST, limit: int = 0) -> dict[str, Any]:
    report = build_campaign(load_audit(audit_path), limit=limit, research_backlog=_load_op16_research_backlog())
    workspace_roots.ensure_parent(out_path)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _print(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print("=" * 112)
    print(
        " DIAMOND RETUNE CAMPAIGN  "
        f"targets={summary['n_selected_targets']}/{summary['n_available_targets']} "
        f"mode={summary['execution_mode']}",
    )
    print("=" * 112)
    for target in report["targets"]:
        print(
            f"#{target['rank']} {target['bot_id']:<24} {target['asset_sleeve']:<14} "
            f"score={target['priority_score']:>7.2f} issue={target['issue_code']} "
            f"cmd={target['next_command']}",
        )
    backlog = report.get("research_backlog") if isinstance(report.get("research_backlog"), list) else []
    if backlog:
        print("-" * 112)
        print(f" RESEARCH BACKLOG  targets={len(backlog)} mode=paper_research_only")
        for target in backlog:
            signal = target.get("research_signal") if isinstance(target.get("research_signal"), dict) else {}
            print(
                f"#{target['rank']} {target['bot_id']:<24} "
                f"oos={_as_float(signal.get('agg_oos_sharpe')):>6.3f} "
                f"dsr={_as_float(signal.get('dsr_pass_fraction')):>5.3f} "
                f"windows={int(_as_float(signal.get('windows'))):>2} "
                f"cmd={target['next_command']}",
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-path", type=Path, default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--out-path", type=Path, default=OUT_LATEST)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run(audit_path=args.audit_path, out_path=args.out_path, limit=args.limit)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

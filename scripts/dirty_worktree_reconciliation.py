"""Build a read-only reconciliation plan for dirty ETA companion repos.

The output is intentionally operational: it groups dirty child-repo changes
into review batches, gives read-only inspection commands, and writes a
canonical state artifact that closeout/automation can point at before any
superproject gitlink update is attempted.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.scripts import submodule_wiring_preflight, workspace_roots

DEFAULT_OUTPUT_PATH = workspace_roots.ETA_DIRTY_WORKTREE_RECONCILIATION_PATH

_GROUP_DECISIONS = {
    "brain": "review_jarvis_policy_memory_batch_with_tests",
    "deploy": "verify_vps_bootstrap_or_task_registration_before_commit",
    "docs": "confirm_operator_docs_or_generated_artifact_before_commit",
    "feeds": "review_feed_shims_and_runtime_parity_before_commit",
    "reports": "confirm_generated_report_artifact_before_commit",
    "scripts": "review_runtime_script_batch_before_child_commit",
    "tests": "pair_tests_with_matching_source_batch_before_commit",
}

_SLICE_VERIFICATION_TESTS = {
    ("brain", "jarvis"): [
        "tests/test_jarvis_conductor.py",
        "tests/test_jarvis_full_integration.py",
        "tests/test_jarvis_live.py",
        "tests/test_jarvis_strategy_supervisor.py",
        "tests/test_jarvis_v3_round3.py",
        "tests/test_jarvis_wiring_audit.py",
        "tests/test_force_multiplier_smoke.py",
    ],
    ("deploy", "scripts"): [
        "tests/test_dashboard_api.py",
        "tests/test_dashboard_task_registration.py",
        "tests/test_vps_root_reconciliation_scripts.py",
    ],
    ("scripts", "diamond"): [
        "tests/test_diamond_artifact_surface_check.py",
        "tests/test_diamond_ops_dashboard.py",
        "tests/test_diamond_retune_status.py",
        "tests/test_diamond_retune_truth_check.py",
        "tests/test_diamond_wave25_status.py",
    ],
    ("scripts", "jarvis"): [
        "tests/test_jarvis_status_dirty_worktree.py",
        "tests/test_jarvis_wiring_audit.py",
    ],
    ("tests", "diamond"): [
        "tests/test_diamond_artifact_surface_check.py",
        "tests/test_diamond_ops_dashboard.py",
        "tests/test_diamond_retune_status.py",
        "tests/test_diamond_retune_truth_check.py",
        "tests/test_diamond_wave25_status.py",
    ],
    ("tests", "jarvis"): [
        "tests/test_jarvis_conductor.py",
        "tests/test_jarvis_status_dirty_worktree.py",
        "tests/test_jarvis_wiring_audit.py",
    ],
}


def build_reconciliation_plan(
    report: submodule_wiring_preflight.SubmoduleWiringReport,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    modules = {name: _module_plan(report.root, module) for name, module in report.modules.items()}
    dirty_modules = [name for name, module in modules.items() if module["dirty"]]
    blocking_modules = [name for name, module in modules.items() if not module["ready"]]
    action = "safe_to_wire_gitlinks" if report.ready else "review_child_dirty_groups_before_gitlink_wiring"
    status = "ready" if report.ready else "review_required" if dirty_modules or blocking_modules else "unknown"
    return {
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "root": str(report.root),
        "status": status,
        "ready": report.ready,
        "action": action,
        "safety": {
            "no_git_mutation": True,
            "read_only_git_commands_only": True,
            "canonical_write_root": str(report.root),
        },
        "blocking_modules": blocking_modules,
        "dirty_modules": dirty_modules,
        "modules": modules,
        "review_batches": _review_batches(modules),
        "review_slices": _flatten_review_slices(modules),
        "next_actions": _next_actions(modules),
    }


def write_plan(plan: dict[str, Any], output_path: Path, *, root: Path) -> Path:
    output = _resolve_output_path(output_path, root=root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    return output


def build_operator_summary(plan: dict[str, Any], *, top: int = 5) -> dict[str, Any]:
    """Return the small cockpit view operators need before opening the full plan."""

    limit = max(0, top)
    modules = plan.get("modules")
    module_summaries: list[dict[str, Any]] = []
    if isinstance(modules, dict):
        for name, module in modules.items():
            if not isinstance(module, dict):
                continue
            dirty_summary = module.get("dirty_summary")
            counts = dirty_summary.get("counts") if isinstance(dirty_summary, dict) else {}
            module_summaries.append(
                {
                    "module": name,
                    "ready": module.get("ready"),
                    "gitlink": module.get("gitlink"),
                    "dirty": module.get("dirty"),
                    "entry_count": dirty_summary.get("entry_count") if isinstance(dirty_summary, dict) else 0,
                    "counts": counts if isinstance(counts, dict) else {},
                    "recommended_handling": module.get("recommended_handling"),
                    "blockers": module.get("blockers") if isinstance(module.get("blockers"), list) else [],
                }
            )

    summary: dict[str, Any] = {
        "generated_at": plan.get("generated_at"),
        "root": plan.get("root"),
        "status": plan.get("status"),
        "ready": plan.get("ready"),
        "action": plan.get("action"),
        "dirty_modules": plan.get("dirty_modules") if isinstance(plan.get("dirty_modules"), list) else [],
        "blocking_modules": plan.get("blocking_modules") if isinstance(plan.get("blocking_modules"), list) else [],
        "module_summaries": module_summaries,
        "next_actions": plan.get("next_actions") if isinstance(plan.get("next_actions"), list) else [],
        "top_review_batches": _operator_items(plan.get("review_batches"), limit=limit),
        "top_review_slices": _operator_items(plan.get("review_slices"), limit=limit),
    }
    if "output_path" in plan:
        summary["output_path"] = plan["output_path"]
    return summary


def format_operator_summary(plan: dict[str, Any], *, top: int = 5) -> str:
    summary = build_operator_summary(plan, top=top)
    dirty_modules = ", ".join(str(item) for item in summary["dirty_modules"]) or "none"
    blocking_modules = ", ".join(str(item) for item in summary["blocking_modules"]) or "none"
    lines = [
        f"dirty worktree reconciliation: {summary['action']}",
        f"  status: {summary['status']}; ready={summary['ready']}",
        f"  dirty_modules: {dirty_modules}",
        f"  blocking_modules: {blocking_modules}",
    ]

    next_actions = summary.get("next_actions")
    if isinstance(next_actions, list) and next_actions:
        lines.append("  next_actions:")
        lines.extend(f"    - {action}" for action in next_actions)

    top_slices = summary.get("top_review_slices")
    if isinstance(top_slices, list) and top_slices:
        lines.append("  top_review_slices:")
        for item in top_slices:
            if not isinstance(item, dict):
                continue
            line = f"    - {item.get('slice_id')}: {item.get('count')} dirty"
            verification = item.get("verification_commands")
            if isinstance(verification, list) and verification:
                line += f"; verify: {verification[0]}"
            lines.append(line)

    if "output_path" in summary:
        lines.append(f"  artifact: {summary['output_path']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=workspace_roots.WORKSPACE_ROOT)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--no-write", action="store_true", help="print/return the plan without writing an artifact")
    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument("--json", action="store_true", help="print the full JSON plan")
    output_mode.add_argument("--summary-json", action="store_true", help="print a compact JSON operator summary")
    parser.add_argument("--top", type=int, default=5, help="number of review batches/slices to include in summaries")
    parser.add_argument(
        "--module",
        action="append",
        dest="modules",
        help="repeatable submodule path; defaults to eta_engine, firm, mnq_backtest",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not args.no_write:
        try:
            root = workspace_roots.resolve_under_workspace(root, label="--root")
        except ValueError as exc:
            parser.error(str(exc))
    modules = tuple(args.modules) if args.modules else submodule_wiring_preflight.DEFAULT_MODULES
    report = submodule_wiring_preflight.inspect_submodule_wiring(root=root, required_modules=modules)
    plan = build_reconciliation_plan(report)
    output_path = args.output or _default_output_for_root(root)
    if not args.no_write:
        written = write_plan(plan, output_path, root=root)
        plan["output_path"] = str(written)

    if args.json:
        print(json.dumps(plan, indent=2, default=str))
    elif args.summary_json:
        print(json.dumps(build_operator_summary(plan, top=args.top), indent=2, default=str))
    else:
        print(format_operator_summary(plan, top=args.top))
    return 0 if plan["ready"] else 1


def _module_plan(root: Path, module: submodule_wiring_preflight.ModulePreflight) -> dict[str, Any]:
    payload = module.as_payload()
    summary = payload["dirty_summary"]
    module_path = root / module.path
    recommended_handling = _module_handling(module, summary)
    review_groups = _review_groups(module_path, module.dirty_entries, summary)
    review_slices = _review_slices(
        module_path,
        module.dirty_entries,
        recommended_handling=recommended_handling,
    )
    return {
        "path": module.path,
        "ready": module.ready,
        "gitlink": module.gitlink,
        "exists": module.exists,
        "dirty": module.dirty,
        "blockers": module.blockers,
        "sha": module.sha,
        "label": module.label,
        "dirty_summary": summary,
        "review_groups": review_groups,
        "review_slices": review_slices,
        "recommended_handling": recommended_handling,
    }


def _review_groups(module_path: Path, dirty_entries: list[str], summary: dict[str, Any]) -> list[dict[str, Any]]:
    samples = _samples_by_group(dirty_entries)
    groups = summary.get("top_groups") if isinstance(summary, dict) else []
    if not isinstance(groups, list):
        return []
    result: list[dict[str, Any]] = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        group = str(item.get("group", "")).strip()
        count = item.get("count")
        if not group or not isinstance(count, int):
            continue
        result.append(
            {
                "group": group,
                "count": count,
                "sample_paths": samples.get(group, [])[:5],
                "suggested_decision": _GROUP_DECISIONS.get(group, "inspect_diff_before_preserve_or_defer"),
                "status_command": f'git -C "{module_path}" status --short -- "{group}"',
                "diff_command": f'git -C "{module_path}" diff -- "{group}"',
                "shortstat_command": f'git -C "{module_path}" diff --shortstat -- "{group}"',
            }
        )
    return result


def _samples_by_group(entries: list[str]) -> dict[str, list[str]]:
    samples: dict[str, list[str]] = {}
    for entry in entries:
        path = _porcelain_path(entry)
        group = _path_group(path)
        bucket = samples.setdefault(group, [])
        if len(bucket) < 5:
            bucket.append(path)
    return samples


def _module_handling(module: submodule_wiring_preflight.ModulePreflight, summary: dict[str, Any]) -> str:
    if not module.dirty and module.ready:
        return "no_action_required"
    review_action = str(summary.get("review_action") or "").strip()
    if "gitlink diverged" in module.blockers and module.dirty:
        return "commit_child_repo_before_superproject_gitlink_update"
    if review_action:
        return review_action
    return "review_before_superproject_gitlink_update"


def _next_actions(modules: dict[str, dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    for name, module in modules.items():
        if not module["dirty"]:
            continue
        groups = module.get("review_groups")
        group_text = ""
        if isinstance(groups, list) and groups:
            group_text = ", ".join(f"{item['group']}={item['count']}" for item in groups[:3] if isinstance(item, dict))
        handling = module.get("recommended_handling") or "review_before_superproject_gitlink_update"
        if group_text:
            actions.append(f"{name}: {handling}; start with {group_text}")
        else:
            actions.append(f"{name}: {handling}")
    if not actions:
        actions.append("No dirty companion repos detected; gitlink wiring can proceed if all other gates pass.")
    return actions


def _operator_items(items: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(items, list) or limit <= 0:
        return []
    result: list[dict[str, Any]] = []
    keep_keys = (
        "batch_id",
        "slice_id",
        "module",
        "group",
        "slice",
        "count",
        "recommended_handling",
        "suggested_decision",
        "sample_paths",
        "verification_cwd",
        "verification_commands",
        "next_action",
    )
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        compact = {key: item[key] for key in keep_keys if key in item}
        samples = compact.get("sample_paths")
        if isinstance(samples, list):
            compact["sample_paths"] = samples[:3]
        result.append(compact)
    return result


def _review_batches(modules: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    batches: list[dict[str, Any]] = []
    for module_name, module in modules.items():
        groups = module.get("review_groups")
        if not isinstance(groups, list):
            continue
        recommended_handling = str(module.get("recommended_handling") or "review_before_superproject_gitlink_update")
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_name = str(group.get("group") or "").strip()
            count = group.get("count")
            if not group_name or not isinstance(count, int):
                continue
            suggested_decision = str(group.get("suggested_decision") or "inspect_diff_before_preserve_or_defer")
            batches.append(
                {
                    "batch_id": f"{module_name}:{group_name}",
                    "module": module_name,
                    "group": group_name,
                    "count": count,
                    "recommended_handling": recommended_handling,
                    "suggested_decision": suggested_decision,
                    "sample_paths": group.get("sample_paths") if isinstance(group.get("sample_paths"), list) else [],
                    "status_command": group.get("status_command"),
                    "diff_command": group.get("diff_command"),
                    "shortstat_command": group.get("shortstat_command"),
                    "next_action": (
                        f"{module_name}/{group_name}: {suggested_decision}; "
                        f"{count} dirty path(s); handling={recommended_handling}"
                    ),
                }
            )
    return sorted(batches, key=lambda item: int(item.get("count") or 0), reverse=True)


def _review_slices(
    module_path: Path,
    dirty_entries: list[str],
    *,
    recommended_handling: str,
) -> list[dict[str, Any]]:
    by_slice: dict[str, dict[str, Any]] = {}
    for entry in dirty_entries:
        path = _porcelain_path(entry)
        group = _path_group(path)
        slice_name = _path_slice(path)
        batch_id = f"{module_path.name}:{group}:{slice_name}"
        item = by_slice.setdefault(
            batch_id,
            {
                "slice_id": batch_id,
                "module": module_path.name,
                "group": group,
                "slice": slice_name,
                "count": 0,
                "recommended_handling": recommended_handling,
                "sample_paths": [],
                "status_command": f'git -C "{module_path}" status --short -- "{group}"',
                "diff_command": f'git -C "{module_path}" diff -- "{group}"',
                "shortstat_command": f'git -C "{module_path}" diff --shortstat -- "{group}"',
                "verification_cwd": str(module_path),
                "verification_commands": _verification_commands(module_path, group, slice_name),
            },
        )
        item["count"] = int(item["count"]) + 1
        samples = item["sample_paths"] if isinstance(item["sample_paths"], list) else []
        if len(samples) < 5:
            samples.append(path)
    for item in by_slice.values():
        item["next_action"] = (
            f"{item['module']}/{item['group']}:{item['slice']}; "
            f"{item['count']} dirty path(s); handling={item['recommended_handling']}"
        )
    return sorted(by_slice.values(), key=lambda item: int(item.get("count") or 0), reverse=True)


def _flatten_review_slices(modules: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    slices: list[dict[str, Any]] = []
    for module in modules.values():
        module_slices = module.get("review_slices")
        if isinstance(module_slices, list):
            slices.extend(dict(item) for item in module_slices if isinstance(item, dict))
    return sorted(slices, key=lambda item: int(item.get("count") or 0), reverse=True)


def _verification_commands(module_path: Path, group: str, slice_name: str) -> list[str]:
    del module_path
    tests = _SLICE_VERIFICATION_TESTS.get((group, slice_name))
    if not tests:
        return []
    return [f"python -B -m pytest {' '.join(tests)} -q"]


def _resolve_output_path(output_path: Path, *, root: Path) -> Path:
    output = output_path.resolve()
    root_resolved = root.resolve()
    try:
        output.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"refusing to write outside canonical root: {output}") from exc
    return output


def _default_output_for_root(root: Path) -> Path:
    if root.resolve() == workspace_roots.WORKSPACE_ROOT.resolve():
        return DEFAULT_OUTPUT_PATH
    return root / "var" / "eta_engine" / "state" / "dirty_worktree_reconciliation_latest.json"


def _porcelain_path(line: str) -> str:
    if len(line) > 3 and line[2] == " ":
        return line[3:]
    if len(line) > 2 and line[1] == " ":
        return line[2:]
    return line.strip()


def _path_group(path: str) -> str:
    normalized = path.replace("\\", "/")
    if " -> " in normalized:
        normalized = normalized.rsplit(" -> ", 1)[-1]
    return normalized.split("/", 1)[0] or "root"


def _path_slice(path: str) -> str:
    normalized = path.replace("\\", "/")
    if " -> " in normalized:
        normalized = normalized.rsplit(" -> ", 1)[-1]
    parts = normalized.split("/", 1)
    if len(parts) == 1:
        return "root"
    filename = parts[1].split("/", 1)[0]
    stem = filename.rsplit(".", 1)[0]
    if not stem:
        return "misc"
    if stem.startswith("_"):
        return "_maintenance"
    if stem.startswith("test_"):
        stem = stem.removeprefix("test_")
    token = stem.split("_", 1)[0].strip()
    return token or "misc"


if __name__ == "__main__":
    raise SystemExit(main())

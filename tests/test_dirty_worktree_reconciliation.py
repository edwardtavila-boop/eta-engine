from __future__ import annotations

import json
from pathlib import Path

import pytest

from eta_engine.scripts import dirty_worktree_reconciliation as reconciliation
from eta_engine.scripts import submodule_wiring_preflight


def test_reconciliation_plan_groups_dirty_child_repos(tmp_path: Path) -> None:
    for name in ("eta_engine", "firm", "mnq_backtest"):
        (tmp_path / name).mkdir(parents=True)

    report = submodule_wiring_preflight.evaluate_submodule_wiring(
        root=tmp_path,
        required_modules=("eta_engine", "firm", "mnq_backtest"),
        submodule_status_lines=[
            "+15e701e12bdd09995847d279861b3c12b0ba06f2 eta_engine (feature)",
            " 19768b0cc158bdc920fdb44e42e0e23931282b8e firm (main)",
            " 1c3a2ef93a2d25561a4ec3e022cdbe1176ce590a mnq_backtest (main)",
        ],
        porcelain_by_module={
            "eta_engine": [
                " M scripts/health_check.py",
                " M scripts/project_kaizen_closeout.py",
                "?? tests/test_health_check.py",
                "?? tests/test_project_kaizen_closeout.py",
                " M feeds/jarvis_status.py",
            ],
            "firm": [],
            "mnq_backtest": [" M docs/dashboard.html", "?? reports/daily_pipeline/20260515.log"],
        },
    )

    plan = reconciliation.build_reconciliation_plan(report, generated_at="2026-05-16T00:00:00+00:00")

    assert plan["ready"] is False
    assert plan["status"] == "review_required"
    assert plan["action"] == "review_child_dirty_groups_before_gitlink_wiring"
    assert plan["safety"]["no_git_mutation"] is True
    assert plan["blocking_modules"] == ["eta_engine", "mnq_backtest"]
    assert plan["dirty_modules"] == ["eta_engine", "mnq_backtest"]
    eta_groups = plan["modules"]["eta_engine"]["review_groups"]
    assert eta_groups[:3] == [
        {
            "group": "scripts",
            "count": 2,
            "sample_paths": ["scripts/health_check.py", "scripts/project_kaizen_closeout.py"],
            "suggested_decision": "review_runtime_script_batch_before_child_commit",
            "status_command": f'git -C "{tmp_path / "eta_engine"}" status --short -- "scripts"',
            "diff_command": f'git -C "{tmp_path / "eta_engine"}" diff -- "scripts"',
            "shortstat_command": f'git -C "{tmp_path / "eta_engine"}" diff --shortstat -- "scripts"',
        },
        {
            "group": "tests",
            "count": 2,
            "sample_paths": ["tests/test_health_check.py", "tests/test_project_kaizen_closeout.py"],
            "suggested_decision": "pair_tests_with_matching_source_batch_before_commit",
            "status_command": f'git -C "{tmp_path / "eta_engine"}" status --short -- "tests"',
            "diff_command": f'git -C "{tmp_path / "eta_engine"}" diff -- "tests"',
            "shortstat_command": f'git -C "{tmp_path / "eta_engine"}" diff --shortstat -- "tests"',
        },
        {
            "group": "feeds",
            "count": 1,
            "sample_paths": ["feeds/jarvis_status.py"],
            "suggested_decision": "review_feed_shims_and_runtime_parity_before_commit",
            "status_command": f'git -C "{tmp_path / "eta_engine"}" status --short -- "feeds"',
            "diff_command": f'git -C "{tmp_path / "eta_engine"}" diff -- "feeds"',
            "shortstat_command": f'git -C "{tmp_path / "eta_engine"}" diff --shortstat -- "feeds"',
        },
    ]
    assert plan["modules"]["eta_engine"]["recommended_handling"] == "commit_child_repo_before_superproject_gitlink_update"
    assert plan["review_batches"][:3] == [
        {
            "batch_id": "eta_engine:scripts",
            "module": "eta_engine",
            "group": "scripts",
            "count": 2,
            "recommended_handling": "commit_child_repo_before_superproject_gitlink_update",
            "suggested_decision": "review_runtime_script_batch_before_child_commit",
            "sample_paths": ["scripts/health_check.py", "scripts/project_kaizen_closeout.py"],
            "status_command": f'git -C "{tmp_path / "eta_engine"}" status --short -- "scripts"',
            "diff_command": f'git -C "{tmp_path / "eta_engine"}" diff -- "scripts"',
            "shortstat_command": f'git -C "{tmp_path / "eta_engine"}" diff --shortstat -- "scripts"',
            "next_action": (
                "eta_engine/scripts: review_runtime_script_batch_before_child_commit; "
                "2 dirty path(s); handling=commit_child_repo_before_superproject_gitlink_update"
            ),
        },
        {
            "batch_id": "eta_engine:tests",
            "module": "eta_engine",
            "group": "tests",
            "count": 2,
            "recommended_handling": "commit_child_repo_before_superproject_gitlink_update",
            "suggested_decision": "pair_tests_with_matching_source_batch_before_commit",
            "sample_paths": ["tests/test_health_check.py", "tests/test_project_kaizen_closeout.py"],
            "status_command": f'git -C "{tmp_path / "eta_engine"}" status --short -- "tests"',
            "diff_command": f'git -C "{tmp_path / "eta_engine"}" diff -- "tests"',
            "shortstat_command": f'git -C "{tmp_path / "eta_engine"}" diff --shortstat -- "tests"',
            "next_action": (
                "eta_engine/tests: pair_tests_with_matching_source_batch_before_commit; "
                "2 dirty path(s); handling=commit_child_repo_before_superproject_gitlink_update"
            ),
        },
        {
            "batch_id": "eta_engine:feeds",
            "module": "eta_engine",
            "group": "feeds",
            "count": 1,
            "recommended_handling": "commit_child_repo_before_superproject_gitlink_update",
            "suggested_decision": "review_feed_shims_and_runtime_parity_before_commit",
            "sample_paths": ["feeds/jarvis_status.py"],
            "status_command": f'git -C "{tmp_path / "eta_engine"}" status --short -- "feeds"',
            "diff_command": f'git -C "{tmp_path / "eta_engine"}" diff -- "feeds"',
            "shortstat_command": f'git -C "{tmp_path / "eta_engine"}" diff --shortstat -- "feeds"',
            "next_action": (
                "eta_engine/feeds: review_feed_shims_and_runtime_parity_before_commit; "
                "1 dirty path(s); handling=commit_child_repo_before_superproject_gitlink_update"
            ),
        },
    ]
    assert "eta_engine: commit_child_repo_before_superproject_gitlink_update; start with scripts=2" in plan["next_actions"][0]


def test_reconciliation_plan_ranks_review_slices_inside_large_groups(tmp_path: Path) -> None:
    (tmp_path / "eta_engine").mkdir(parents=True)
    report = submodule_wiring_preflight.evaluate_submodule_wiring(
        root=tmp_path,
        required_modules=("eta_engine",),
        submodule_status_lines=[
            "+15e701e12bdd09995847d279861b3c12b0ba06f2 eta_engine (feature)",
        ],
        porcelain_by_module={
            "eta_engine": [
                " M scripts/diamond_ops_dashboard.py",
                " M scripts/diamond_retune_status.py",
                "?? scripts/diamond_retune_truth_check.py",
                " M scripts/jarvis_status.py",
                "?? tests/test_diamond_retune_status.py",
            ],
        },
    )

    plan = reconciliation.build_reconciliation_plan(report, generated_at="2026-05-16T00:00:00+00:00")

    assert plan["review_slices"][:3] == [
        {
            "slice_id": "eta_engine:scripts:diamond",
            "module": "eta_engine",
            "group": "scripts",
            "slice": "diamond",
            "count": 3,
            "recommended_handling": "commit_child_repo_before_superproject_gitlink_update",
            "sample_paths": [
                "scripts/diamond_ops_dashboard.py",
                "scripts/diamond_retune_status.py",
                "scripts/diamond_retune_truth_check.py",
            ],
            "status_command": f'git -C "{tmp_path / "eta_engine"}" status --short -- "scripts"',
            "diff_command": f'git -C "{tmp_path / "eta_engine"}" diff -- "scripts"',
            "shortstat_command": f'git -C "{tmp_path / "eta_engine"}" diff --shortstat -- "scripts"',
            "verification_cwd": str(tmp_path / "eta_engine"),
            "verification_commands": [
                "python -B -m pytest tests/test_diamond_artifact_surface_check.py "
                "tests/test_diamond_ops_dashboard.py tests/test_diamond_retune_status.py "
                "tests/test_diamond_retune_truth_check.py tests/test_diamond_wave25_status.py -q"
            ],
            "next_action": (
                "eta_engine/scripts:diamond; 3 dirty path(s); "
                "handling=commit_child_repo_before_superproject_gitlink_update"
            ),
        },
        {
            "slice_id": "eta_engine:scripts:jarvis",
            "module": "eta_engine",
            "group": "scripts",
            "slice": "jarvis",
            "count": 1,
            "recommended_handling": "commit_child_repo_before_superproject_gitlink_update",
            "sample_paths": ["scripts/jarvis_status.py"],
            "status_command": f'git -C "{tmp_path / "eta_engine"}" status --short -- "scripts"',
            "diff_command": f'git -C "{tmp_path / "eta_engine"}" diff -- "scripts"',
            "shortstat_command": f'git -C "{tmp_path / "eta_engine"}" diff --shortstat -- "scripts"',
            "verification_cwd": str(tmp_path / "eta_engine"),
            "verification_commands": [
                "python -B -m pytest tests/test_jarvis_status_dirty_worktree.py "
                "tests/test_jarvis_wiring_audit.py -q"
            ],
            "next_action": (
                "eta_engine/scripts:jarvis; 1 dirty path(s); "
                "handling=commit_child_repo_before_superproject_gitlink_update"
            ),
        },
        {
            "slice_id": "eta_engine:tests:diamond",
            "module": "eta_engine",
            "group": "tests",
            "slice": "diamond",
            "count": 1,
            "recommended_handling": "commit_child_repo_before_superproject_gitlink_update",
            "sample_paths": ["tests/test_diamond_retune_status.py"],
            "status_command": f'git -C "{tmp_path / "eta_engine"}" status --short -- "tests"',
            "diff_command": f'git -C "{tmp_path / "eta_engine"}" diff -- "tests"',
            "shortstat_command": f'git -C "{tmp_path / "eta_engine"}" diff --shortstat -- "tests"',
            "verification_cwd": str(tmp_path / "eta_engine"),
            "verification_commands": [
                "python -B -m pytest tests/test_diamond_artifact_surface_check.py "
                "tests/test_diamond_ops_dashboard.py tests/test_diamond_retune_status.py "
                "tests/test_diamond_retune_truth_check.py tests/test_diamond_wave25_status.py -q"
            ],
            "next_action": (
                "eta_engine/tests:diamond; 1 dirty path(s); "
                "handling=commit_child_repo_before_superproject_gitlink_update"
            ),
        },
    ]


def test_operator_summary_keeps_dirty_plan_readable(tmp_path: Path) -> None:
    (tmp_path / "eta_engine").mkdir(parents=True)
    report = submodule_wiring_preflight.evaluate_submodule_wiring(
        root=tmp_path,
        required_modules=("eta_engine",),
        submodule_status_lines=[
            "+15e701e12bdd09995847d279861b3c12b0ba06f2 eta_engine (feature)",
        ],
        porcelain_by_module={
            "eta_engine": [
                " M scripts/diamond_ops_dashboard.py",
                " M scripts/diamond_retune_status.py",
                "?? scripts/diamond_retune_truth_check.py",
                " M scripts/jarvis_status.py",
                "?? tests/test_diamond_retune_status.py",
            ],
        },
    )
    plan = reconciliation.build_reconciliation_plan(report, generated_at="2026-05-16T00:00:00+00:00")
    plan["output_path"] = str(tmp_path / "var" / "eta_engine" / "state" / "dirty_worktree_reconciliation_latest.json")

    summary = reconciliation.build_operator_summary(plan, top=2)
    text = reconciliation.format_operator_summary(plan, top=2)

    assert summary["status"] == "review_required"
    assert summary["dirty_modules"] == ["eta_engine"]
    assert [item["slice_id"] for item in summary["top_review_slices"]] == [
        "eta_engine:scripts:diamond",
        "eta_engine:scripts:jarvis",
    ]
    assert summary["top_review_slices"][0]["sample_paths"] == [
        "scripts/diamond_ops_dashboard.py",
        "scripts/diamond_retune_status.py",
        "scripts/diamond_retune_truth_check.py",
    ]
    assert "dirty worktree reconciliation: review_child_dirty_groups_before_gitlink_wiring" in text
    assert "eta_engine:scripts:diamond: 3 dirty" in text
    assert "tests/test_diamond_artifact_surface_check.py" in text
    assert str(tmp_path / "var" / "eta_engine" / "state" / "dirty_worktree_reconciliation_latest.json") in text


def test_reconciliation_write_refuses_output_outside_root(tmp_path: Path) -> None:
    plan = {
        "ready": True,
        "action": "safe_to_wire_gitlinks",
    }

    with pytest.raises(ValueError, match="refusing to write outside canonical root"):
        reconciliation.write_plan(plan, tmp_path.parent / "outside.json", root=tmp_path)


def test_reconciliation_write_uses_canonical_root_relative_output(tmp_path: Path) -> None:
    output = tmp_path / "var" / "eta_engine" / "state" / "dirty_worktree_reconciliation_latest.json"
    plan = {
        "ready": True,
        "action": "safe_to_wire_gitlinks",
    }

    written = reconciliation.write_plan(plan, output, root=tmp_path)

    assert written == output
    assert json.loads(output.read_text(encoding="utf-8"))["action"] == "safe_to_wire_gitlinks"

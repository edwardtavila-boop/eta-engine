from __future__ import annotations

import json
from pathlib import Path

from eta_engine.scripts import jarvis_status


def test_dirty_worktree_reconciliation_summary_reads_canonical_plan(tmp_path: Path) -> None:
    path = tmp_path / "dirty_worktree_reconciliation_latest.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-05-16T22:57:36+00:00",
                "ready": False,
                "action": "review_child_dirty_groups_before_gitlink_wiring",
                "dirty_modules": ["eta_engine", "mnq_backtest"],
                "blocking_modules": ["eta_engine", "mnq_backtest"],
                "safety": {"no_git_mutation": True},
                "next_actions": [
                    "eta_engine: commit child repo",
                    "mnq_backtest: review generated docs",
                ],
                "review_batches": [
                    {
                        "batch_id": "eta_engine:scripts",
                        "module": "eta_engine",
                        "group": "scripts",
                        "count": 130,
                        "next_action": "eta_engine/scripts: inspect scripts first",
                    },
                    {
                        "batch_id": "eta_engine:tests",
                        "module": "eta_engine",
                        "group": "tests",
                        "count": 96,
                        "next_action": "eta_engine/tests: pair tests second",
                    },
                    {
                        "batch_id": "eta_engine:feeds",
                        "module": "eta_engine",
                        "group": "feeds",
                        "count": 73,
                        "next_action": "eta_engine/feeds: inspect feed shims third",
                    },
                ],
                "review_slices": [
                    {
                        "slice_id": "eta_engine:scripts:diamond",
                        "module": "eta_engine",
                        "group": "scripts",
                        "slice": "diamond",
                        "count": 42,
                        "next_action": "eta_engine/scripts:diamond",
                    },
                    {
                        "slice_id": "eta_engine:scripts:jarvis",
                        "module": "eta_engine",
                        "group": "scripts",
                        "slice": "jarvis",
                        "count": 12,
                        "next_action": "eta_engine/scripts:jarvis",
                    },
                    {
                        "slice_id": "eta_engine:tests:diamond",
                        "module": "eta_engine",
                        "group": "tests",
                        "slice": "diamond",
                        "count": 9,
                        "next_action": "eta_engine/tests:diamond",
                    },
                ],
                "modules": {
                    "eta_engine": {
                        "gitlink": "diverged",
                        "recommended_handling": "commit_child_repo_before_superproject_gitlink_update",
                        "dirty_summary": {
                            "entry_count": 444,
                            "top_groups": [
                                {"group": "scripts", "count": 130},
                                {"group": "tests", "count": 96},
                                {"group": "feeds", "count": 73},
                            ],
                        },
                        "review_groups": [
                            {"group": "scripts", "count": 130},
                            {"group": "tests", "count": 96},
                            {"group": "feeds", "count": 73},
                        ],
                    },
                    "mnq_backtest": {
                        "gitlink": "aligned",
                        "recommended_handling": "review_untracked_files_before_gitlink_wiring",
                        "dirty_summary": {
                            "entry_count": 4,
                            "top_groups": [
                                {"group": "docs", "count": 3},
                                {"group": "reports", "count": 1},
                            ],
                        },
                        "review_groups": [{"group": "docs", "count": 3}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    payload = jarvis_status.build_dirty_worktree_reconciliation_summary(path=path, limit=2)

    assert payload["source"] == "jarvis_status.dirty_worktree_reconciliation"
    assert payload["status"] == "review_required"
    assert payload["ready"] is False
    assert payload["dirty_modules"] == ["eta_engine", "mnq_backtest"]
    assert payload["blocking_modules"] == ["eta_engine", "mnq_backtest"]
    assert payload["next_actions"] == ["eta_engine: commit child repo", "mnq_backtest: review generated docs"]
    assert payload["review_batches"] == [
        {
            "batch_id": "eta_engine:scripts",
            "module": "eta_engine",
            "group": "scripts",
            "count": 130,
            "next_action": "eta_engine/scripts: inspect scripts first",
        },
        {
            "batch_id": "eta_engine:tests",
            "module": "eta_engine",
            "group": "tests",
            "count": 96,
            "next_action": "eta_engine/tests: pair tests second",
        },
    ]
    assert payload["review_slices"] == [
        {
            "slice_id": "eta_engine:scripts:diamond",
            "module": "eta_engine",
            "group": "scripts",
            "slice": "diamond",
            "count": 42,
            "next_action": "eta_engine/scripts:diamond",
        },
        {
            "slice_id": "eta_engine:scripts:jarvis",
            "module": "eta_engine",
            "group": "scripts",
            "slice": "jarvis",
            "count": 12,
            "next_action": "eta_engine/scripts:jarvis",
        },
    ]
    assert payload["module_summaries"][0]["module"] == "eta_engine"
    assert payload["module_summaries"][0]["entry_count"] == 444
    assert payload["module_summaries"][0]["top_groups"] == [
        {"group": "scripts", "count": 130},
        {"group": "tests", "count": 96},
    ]
    assert payload["module_summaries"][1]["top_groups"] == [
        {"group": "docs", "count": 3},
        {"group": "reports", "count": 1},
    ]


def test_dirty_worktree_reconciliation_summary_fails_soft_when_missing(tmp_path: Path) -> None:
    payload = jarvis_status.build_dirty_worktree_reconciliation_summary(
        path=tmp_path / "missing.json",
    )

    assert payload["status"] == "missing"
    assert payload["dirty_modules"] == []
    assert payload["next_actions"] == []


def test_dirty_worktree_reconciliation_format_includes_top_groups() -> None:
    text = jarvis_status._format_dirty_worktree_reconciliation(
        {
            "status": "review_required",
            "module_summaries": [
                {
                    "module": "eta_engine",
                    "top_groups": [
                        {"group": "scripts", "count": 130},
                        {"group": "tests", "count": 96},
                    ],
                }
            ],
        }
    )

    assert text == "review_required eta_engine:scripts=130,tests=96"

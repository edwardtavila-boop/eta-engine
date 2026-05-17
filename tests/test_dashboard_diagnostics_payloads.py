from __future__ import annotations

from eta_engine.deploy.scripts.dashboard_diagnostics_payloads import (
    build_dashboard_diagnostics_dirty_worktree_payload,
    build_dashboard_diagnostics_readiness_payload,
    build_dashboard_diagnostics_second_brain_payload,
)


def test_build_dashboard_diagnostics_readiness_payload_preserves_launch_truth() -> None:
    payload = build_dashboard_diagnostics_readiness_payload(
        readiness={
            "status": "ready",
            "top_actions": ["sync receipts", "review paper soak"],
            "error": "",
        },
        readiness_summary={
            "can_paper_trade": 10,
            "can_live_any": False,
        },
        readiness_lane_counts={"live_preflight": 6, "paper_soak": 4},
        readiness_blocked_data=3,
    )

    assert payload == {
        "status": "ready",
        "blocked_data": 3,
        "paper_ready": 10,
        "can_live_any": False,
        "launch_lanes": {"live_preflight": 6, "paper_soak": 4},
        "top_action_count": 2,
        "error": "",
    }


def test_build_dashboard_diagnostics_second_brain_payload_uses_rollup_counts() -> None:
    payload = build_dashboard_diagnostics_second_brain_payload(
        second_brain={
            "status": "ready",
            "n_episodes": 44,
            "win_rate": 0.58,
            "avg_r": 1.17,
            "semantic_patterns": 7,
            "procedural_versions": 3,
            "legacy_sources_active": False,
            "sources": {"second_brain": "current"},
            "paths": {"playbook": "var/eta_engine/state/second_brain.json"},
            "error": "",
        },
        eligible_patterns=2,
        favor_pattern_count=1,
        avoid_pattern_count=4,
        truth_note="playbook truth",
    )

    assert payload == {
        "status": "ready",
        "n_episodes": 44,
        "win_rate": 0.58,
        "avg_r": 1.17,
        "semantic_patterns": 7,
        "procedural_versions": 3,
        "eligible_patterns": 2,
        "favor_pattern_count": 1,
        "avoid_pattern_count": 4,
        "legacy_sources_active": False,
        "sources": {"second_brain": "current"},
        "paths": {"playbook": "var/eta_engine/state/second_brain.json"},
        "truth_note": "playbook truth",
        "error": "",
    }


def test_build_dashboard_diagnostics_dirty_worktree_payload_sanitizes_lists() -> None:
    payload = build_dashboard_diagnostics_dirty_worktree_payload(
        dirty_worktree_reconciliation={
            "status": "review_required",
            "ready": False,
            "action": "review_child_dirty_groups_before_gitlink_wiring",
            "dirty_modules": ["eta_engine", "mnq_backtest"],
            "blocking_modules": ["eta_engine"],
            "next_actions": ["eta_engine: start with scripts=130"],
            "module_summaries": [{"module": "eta_engine", "entry_count": 444}],
            "review_batches": [{"batch_id": "eta_engine:scripts", "count": 130}],
            "error": "",
        }
    )

    assert payload == {
        "status": "review_required",
        "ready": False,
        "action": "review_child_dirty_groups_before_gitlink_wiring",
        "dirty_modules": ["eta_engine", "mnq_backtest"],
        "blocking_modules": ["eta_engine"],
        "next_actions": ["eta_engine: start with scripts=130"],
        "module_summaries": [{"module": "eta_engine", "entry_count": 444}],
        "review_batches": [{"batch_id": "eta_engine:scripts", "count": 130}],
        "error": "",
    }


def test_build_dashboard_diagnostics_dirty_worktree_payload_coerces_bad_lists_to_empty() -> None:
    payload = build_dashboard_diagnostics_dirty_worktree_payload(
        dirty_worktree_reconciliation={
            "status": "unavailable",
            "ready": True,
            "action": None,
            "dirty_modules": "bad",
            "blocking_modules": None,
            "next_actions": "bad",
            "module_summaries": {"module": "eta_engine"},
            "review_batches": "bad",
            "error": "reconciliation probe exploded",
        }
    )

    assert payload == {
        "status": "unavailable",
        "ready": True,
        "action": "",
        "dirty_modules": [],
        "blocking_modules": [],
        "next_actions": [],
        "module_summaries": [],
        "review_batches": [],
        "error": "reconciliation probe exploded",
    }

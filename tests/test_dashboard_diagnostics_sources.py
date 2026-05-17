from __future__ import annotations

from eta_engine.deploy.scripts.dashboard_diagnostics_sources import (
    build_dashboard_diagnostics_bot_fleet_counts,
    extract_dashboard_operator_queue_rollups,
    extract_dashboard_readiness_second_brain_rollups,
)


def test_build_dashboard_diagnostics_bot_fleet_counts_uses_fallback_math() -> None:
    payload = build_dashboard_diagnostics_bot_fleet_counts(
        roster={"bots": [1, 2, 3], "confirmed_bots": 2, "staged_bots": 1},
        roster_summary={
            "active_bots": 2,
            "runtime_active_bots": 2,
            "running_bots": 1,
            "live_attached_bots": 2,
        },
    )

    assert payload["bot_total"] == 3
    assert payload["confirmed_bots"] == 2
    assert payload["active_bots"] == 2
    assert payload["runtime_active_bots"] == 2
    assert payload["running_bots"] == 1
    assert payload["staged_bots"] == 1
    assert payload["live_attached_bots"] == 2
    assert payload["live_in_trade_bots"] == 1
    assert payload["idle_live_bots"] == 1
    assert payload["inactive_runtime_bots"] == 0


def test_build_dashboard_diagnostics_bot_fleet_counts_handles_bad_optional_ints() -> None:
    payload = build_dashboard_diagnostics_bot_fleet_counts(
        roster={"bots": [1, 2, 3, 4], "confirmed_bots": 0, "idle_live_bots": "bad"},
        roster_summary={
            "bot_total": 4,
            "active_bots": 0,
            "runtime_active_bots": 0,
            "running_bots": 0,
            "staged_bots": 4,
            "live_attached_bots": 0,
            "inactive_runtime_bots": "bad",
        },
    )

    assert payload["idle_live_bots"] == 0
    assert payload["inactive_runtime_bots"] == 0


def test_extract_dashboard_operator_queue_rollups_keeps_advisory_and_blocker_details() -> None:
    payload = extract_dashboard_operator_queue_rollups(
        operator_queue={
            "summary": {"BLOCKED": 1, "OBSERVED": 11, "UNKNOWN": 0},
            "top_blockers": [
                {
                    "op_id": "OP-16",
                    "title": "Research candidates need promotion proof",
                    "detail": "4 research candidate bot(s) still below promotion gate.",
                    "evidence": {
                        "launch_blocker": False,
                        "launch_role": "strategy_optimization_backlog",
                        "blocked_bots": ["mbt_overnight_gap", "mgc_sweep_reclaim"],
                    },
                    "next_actions": [
                        "python -m eta_engine.scripts.paper_live_launch_check --bots mbt_overnight_gap --json"
                    ],
                }
            ],
            "top_non_launch_blockers": [
                {
                    "op_id": "OP-16",
                    "title": "Research candidates need promotion proof",
                    "detail": "4 research candidate bot(s) still below promotion gate.",
                    "evidence": {
                        "launch_role": "strategy_optimization_backlog",
                        "blocked_bots": ["mbt_overnight_gap", "mgc_sweep_reclaim"],
                    },
                    "next_actions": [
                        "python -m eta_engine.scripts.paper_live_launch_check --bots mgc_sweep_reclaim --json"
                    ],
                }
            ],
            "top_launch_blockers": [
                {
                    "op_id": "OP-19",
                    "detail": "Seed IBC credentials and recover TWS API 4002.",
                }
            ],
        }
    )

    assert payload["operator_summary"] == {"BLOCKED": 1, "OBSERVED": 11, "UNKNOWN": 0}
    assert payload["first_operator_blocker"]["op_id"] == "OP-16"
    assert payload["first_operator_evidence"]["launch_role"] == "strategy_optimization_backlog"
    assert payload["first_operator_blocked_bots"] == ["mbt_overnight_gap", "mgc_sweep_reclaim"]
    assert payload["first_operator_next_actions"] == [
        "python -m eta_engine.scripts.paper_live_launch_check --bots mbt_overnight_gap --json"
    ]
    assert payload["first_launch_blocker"]["op_id"] == "OP-19"
    assert payload["first_operator_advisory"]["op_id"] == "OP-16"
    assert payload["first_operator_advisory_blocked_bots"] == [
        "mbt_overnight_gap",
        "mgc_sweep_reclaim",
    ]
    assert payload["first_operator_advisory_next_actions"] == [
        "python -m eta_engine.scripts.paper_live_launch_check --bots mgc_sweep_reclaim --json"
    ]


def test_extract_dashboard_operator_queue_rollups_coerces_missing_lists_to_empty() -> None:
    payload = extract_dashboard_operator_queue_rollups(
        operator_queue={
            "summary": "bad",
            "top_blockers": [{"op_id": "OP-20", "evidence": {"blocked_bots": "bad"}, "next_actions": "bad"}],
            "top_non_launch_blockers": [{"op_id": "OP-21", "evidence": "bad", "next_actions": "bad"}],
            "top_launch_blockers": "bad",
        }
    )

    assert payload["operator_summary"] == {}
    assert payload["first_operator_blocker"]["op_id"] == "OP-20"
    assert payload["first_launch_blocker"] == {}
    assert payload["first_operator_blocked_bots"] == []
    assert payload["first_operator_next_actions"] == []
    assert payload["first_operator_advisory"]["op_id"] == "OP-21"
    assert payload["first_operator_advisory_evidence"] == {}
    assert payload["first_operator_advisory_blocked_bots"] == []
    assert payload["first_operator_advisory_next_actions"] == []


def test_extract_dashboard_readiness_second_brain_rollups_uses_transition_gate_when_present() -> None:
    payload = extract_dashboard_readiness_second_brain_rollups(
        paper_live_transition={
            "first_failed_gate": {
                "name": "gateway_host",
                "detail": "Fresh operator queue is unavailable on this host.",
                "next_action": "Run the paper-live check on the VPS.",
            }
        },
        fallback_first_failed_gate={"name": "fallback", "detail": "fallback detail", "next_action": "fallback action"},
        readiness={
            "summary": {
                "can_paper_trade": 10,
                "can_live_any": False,
                "launch_lanes": {"live_preflight": 6, "paper_soak": 4},
            }
        },
        second_brain={
            "playbook": {
                "eligible_patterns": 2,
                "favor_patterns": [{"pattern": "neutral+rth+long"}],
                "avoid_patterns": [],
                "truth_note": "playbook truth",
            }
        },
    )

    assert payload["first_failed_gate"] == {
        "name": "gateway_host",
        "detail": "Fresh operator queue is unavailable on this host.",
        "next_action": "Run the paper-live check on the VPS.",
    }
    assert payload["readiness_lane_counts"] == {"live_preflight": 6, "paper_soak": 4}
    assert payload["readiness_blocked_data"] == 0
    assert payload["second_brain_eligible_patterns"] == 2
    assert payload["second_brain_favor_pattern_count"] == 1
    assert payload["second_brain_avoid_pattern_count"] == 0
    assert payload["second_brain_truth_note"] == "playbook truth"


def test_extract_dashboard_readiness_second_brain_rollups_falls_back_to_lane_counts_and_top_level_truth() -> None:
    payload = extract_dashboard_readiness_second_brain_rollups(
        paper_live_transition={},
        fallback_first_failed_gate={"name": "tws_api_4002", "detail": None, "next_action": 7},
        readiness={"summary": {"launch_lanes": {"blocked_data": 3}}},
        second_brain={
            "truth_note": "top-level truth",
            "playbook": {
                "eligible_patterns": 0,
                "favor_patterns": [],
                "avoid_patterns": ["avoid-a", "avoid-b"],
            },
        },
    )

    assert payload["first_failed_gate"] == {
        "name": "tws_api_4002",
        "detail": "",
        "next_action": "7",
    }
    assert payload["readiness_lane_counts"] == {"blocked_data": 3}
    assert payload["readiness_blocked_data"] == 3
    assert payload["second_brain_eligible_patterns"] == 0
    assert payload["second_brain_favor_pattern_count"] == 0
    assert payload["second_brain_avoid_pattern_count"] == 2
    assert payload["second_brain_truth_note"] == "top-level truth"

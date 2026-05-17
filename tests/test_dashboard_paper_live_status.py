from __future__ import annotations

from eta_engine.deploy.scripts.dashboard_paper_live_status import (
    build_dashboard_paper_live_transition_diagnostics_summary,
    build_master_status_paper_live_state,
    build_operator_queue_diagnostics_summary,
    build_paper_live_transition_summary,
    reconcile_paper_live_transition_launch_block,
    resolve_paper_live_card,
    resolve_paper_live_effective_state,
)


def test_resolve_paper_live_effective_state_surfaces_bracket_audit_detail() -> None:
    resolved = resolve_paper_live_effective_state(
        raw_status="ready_to_launch_paper_live",
        effective_detail="",
        operator_queue_launch_blocked_count=0,
        operator_queue_blocked_detail="",
        stale_receipt=False,
        stale_detail="",
        held_by_bracket_audit=True,
        bracket_audit_detail="held by Bracket Audit: Flatten unprotected exposure",
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="hold detail",
    )

    assert resolved["effective_status"] == "held_by_bracket_audit"
    assert resolved["effective_detail"] == "held by Bracket Audit: Flatten unprotected exposure"


def test_resolve_paper_live_effective_state_promotes_ready_advisory_to_shadow_paper() -> None:
    resolved = resolve_paper_live_effective_state(
        raw_status="ready_to_launch_paper_live",
        effective_detail="",
        operator_queue_launch_blocked_count=0,
        operator_queue_blocked_detail="",
        stale_receipt=False,
        stale_detail="",
        held_by_bracket_audit=False,
        bracket_audit_detail="",
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=True,
        daily_loss_shadow_detail="Shadow paper remains live until reset",
        daily_loss_hold_detail="hold detail",
    )

    assert resolved["effective_status"] == "shadow_paper_active"
    assert resolved["effective_detail"] == "Shadow paper remains live until reset"


def test_resolve_paper_live_effective_state_keeps_daily_loss_hold_over_shadow_runtime() -> None:
    resolved = resolve_paper_live_effective_state(
        raw_status="ready_to_launch_paper_live",
        effective_detail="",
        operator_queue_launch_blocked_count=0,
        operator_queue_blocked_detail="",
        stale_receipt=False,
        stale_detail="",
        held_by_bracket_audit=False,
        bracket_audit_detail="",
        held_by_daily_loss_stop=True,
        daily_loss_advisory_active=False,
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="Global daily loss stop active",
        shadow_runtime_active=True,
        shadow_runtime_detail="live shadow paper lane active on 2 attached bot(s)",
    )

    assert resolved["effective_status"] == "held_by_daily_loss_stop"
    assert resolved["effective_detail"] == "Global daily loss stop active"


def test_resolve_paper_live_effective_state_uses_shadow_attached_count_when_safe() -> None:
    resolved = resolve_paper_live_effective_state(
        raw_status="blocked",
        effective_detail="still warming up",
        operator_queue_launch_blocked_count=0,
        operator_queue_blocked_detail="",
        stale_receipt=False,
        stale_detail="",
        held_by_bracket_audit=False,
        bracket_audit_detail="",
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="hold detail",
        shadow_paper_attached_count=2,
    )

    assert resolved["effective_status"] == "shadow_paper_active"
    assert resolved["effective_detail"] == "live shadow paper lane active on 2 attached bot(s)"


def test_resolve_paper_live_effective_state_stale_receipt_wins_last() -> None:
    resolved = resolve_paper_live_effective_state(
        raw_status="ready_to_launch_paper_live",
        effective_detail="fresh",
        operator_queue_launch_blocked_count=0,
        operator_queue_blocked_detail="",
        stale_receipt=True,
        stale_detail="receipt is stale",
        held_by_bracket_audit=False,
        bracket_audit_detail="",
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=True,
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="hold detail",
        shadow_runtime_active=True,
        shadow_runtime_detail="runtime shadow detail",
    )

    assert resolved["effective_status"] == "stale_receipt"
    assert resolved["effective_detail"] == "receipt is stale"


def test_resolve_paper_live_effective_state_surfaces_operator_queue_launch_blocker() -> None:
    resolved = resolve_paper_live_effective_state(
        raw_status="ready_to_launch_paper_live",
        effective_detail="",
        operator_queue_launch_blocked_count=1,
        operator_queue_blocked_detail="Seed IBC credentials and recover TWS API 4002.",
        stale_receipt=False,
        stale_detail="",
        held_by_bracket_audit=False,
        bracket_audit_detail="",
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="hold detail",
    )

    assert resolved["effective_status"] == "blocked_by_operator_queue"
    assert resolved["effective_detail"] == "Seed IBC credentials and recover TWS API 4002."


def test_resolve_paper_live_card_marks_ready_runtime_green() -> None:
    resolved = resolve_paper_live_card(
        effective_status="ready_to_launch_paper_live",
        stale_receipt=False,
        stale_detail="",
        non_authoritative_gateway_host=False,
        launch_blocked_count=0,
        held_by_bracket_audit=False,
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        critical_ready=True,
    )

    assert resolved["status"] == "GREEN"
    assert resolved["detail"] == "ready_to_launch_paper_live"


def test_resolve_paper_live_card_prefers_stale_detail() -> None:
    resolved = resolve_paper_live_card(
        effective_status="stale_receipt",
        stale_receipt=True,
        stale_detail="receipt is stale",
        non_authoritative_gateway_host=False,
        launch_blocked_count=0,
        held_by_bracket_audit=False,
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        critical_ready=True,
    )

    assert resolved["status"] == "YELLOW"
    assert resolved["detail"] == "receipt is stale"


def test_resolve_paper_live_card_surfaces_non_authoritative_block_detail() -> None:
    resolved = resolve_paper_live_card(
        effective_status="blocked",
        stale_receipt=False,
        stale_detail="",
        non_authoritative_gateway_host=True,
        launch_blocked_count=1,
        held_by_bracket_audit=False,
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        critical_ready=False,
        blocked_detail="On the VPS only: apply gateway authority.",
    )

    assert resolved["status"] == "YELLOW"
    assert resolved["detail"] == "On the VPS only: apply gateway authority."


def test_resolve_paper_live_card_marks_shadow_runtime_yellow() -> None:
    resolved = resolve_paper_live_card(
        effective_status="shadow_paper_active",
        stale_receipt=False,
        stale_detail="",
        non_authoritative_gateway_host=False,
        launch_blocked_count=1,
        held_by_bracket_audit=False,
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        critical_ready=False,
        shadow_runtime_active=True,
    )

    assert resolved["status"] == "YELLOW"
    assert resolved["detail"] == "shadow_paper_active"


def test_reconcile_paper_live_transition_launch_block_prefers_fresh_queue_when_cache_stale() -> None:
    resolved = reconcile_paper_live_transition_launch_block(
        paper_live_transition={
            "cache_stale": True,
            "operator_queue_launch_blocked_count": 1,
            "operator_queue_first_launch_blocker_op_id": "OP-18",
            "operator_queue_first_launch_next_action": "python -m stale_probe",
        },
        operator_queue={
            "cache_stale": False,
            "launch_blocked_count": 1,
        },
        first_launch_blocker={
            "op_id": "OP-19",
            "detail": "Seed IBC credentials and recover TWS API 4002.",
        },
    )

    assert resolved["launch_blocked_count"] == 1
    assert resolved["first_launch_blocker_op_id"] == "OP-19"
    assert resolved["first_launch_next_action"] == "Seed IBC credentials and recover TWS API 4002."


def test_reconcile_paper_live_transition_launch_block_fills_missing_action_from_fresh_queue() -> None:
    resolved = reconcile_paper_live_transition_launch_block(
        paper_live_transition={
            "operator_queue_launch_blocked_count": 1,
            "operator_queue_first_launch_blocker_op_id": "",
            "operator_queue_first_launch_next_action": "",
        },
        operator_queue={
            "cache_stale": False,
            "launch_blocked_count": 1,
        },
        first_launch_blocker={
            "op_id": "OP-20",
            "next_actions": [
                "Do not unlock new entries until broker/supervisor positions reconcile.",
            ],
            "detail": "3 broker/supervisor mismatch(es).",
        },
    )

    assert resolved["launch_blocked_count"] == 1
    assert resolved["first_launch_blocker_op_id"] == "OP-20"
    assert (
        resolved["first_launch_next_action"]
        == "Do not unlock new entries until broker/supervisor positions reconcile."
    )


def test_build_paper_live_transition_summary_prefers_next_action_when_detail_missing() -> None:
    resolved = build_paper_live_transition_summary(
        raw_status="ready_to_launch_paper_live",
        detail="",
        stale_receipt=False,
        stale_detail="",
        effective_status="blocked_by_operator_queue",
        effective_detail="Apply gateway authority on VPS.",
        held_by_bracket_audit=False,
        held_by_daily_loss_stop=False,
        daily_loss_gate_mode="warn",
        daily_loss_advisory_active=False,
        capital_lanes_held_by_daily_loss_stop=False,
        daily_loss_suppressed_non_authoritative_gateway_host=False,
        broker_bracket_missing_count=0,
        broker_bracket_primary_symbol="",
        broker_bracket_primary_venue="",
        broker_bracket_primary_sec_type="",
        paper_ready_bots=12,
        critical_ready=True,
        operator_queue_launch_blocked_count=1,
        first_launch_blocker_op_id="OP-21",
        first_launch_next_action="Apply gateway authority on VPS.",
        non_authoritative_gateway_host=False,
        cache_stale=False,
        error=None,
        first_failed_gate={"name": "", "detail": "", "next_action": ""},
    )

    assert resolved["detail"] == "Apply gateway authority on VPS."
    assert resolved["operator_queue_launch_blocked_count"] == 1
    assert resolved["first_launch_blocker_op_id"] == "OP-21"


def test_build_paper_live_transition_summary_falls_back_to_first_failed_gate_detail() -> None:
    resolved = build_paper_live_transition_summary(
        raw_status="blocked",
        detail="",
        stale_receipt=False,
        stale_detail="",
        effective_status="blocked",
        effective_detail="blocked",
        held_by_bracket_audit=False,
        held_by_daily_loss_stop=False,
        daily_loss_gate_mode="warn",
        daily_loss_advisory_active=False,
        capital_lanes_held_by_daily_loss_stop=False,
        daily_loss_suppressed_non_authoritative_gateway_host=False,
        broker_bracket_missing_count=2,
        broker_bracket_primary_symbol="MNQ",
        broker_bracket_primary_venue="IBKR",
        broker_bracket_primary_sec_type="FUT",
        paper_ready_bots=0,
        critical_ready=False,
        operator_queue_launch_blocked_count=0,
        first_launch_blocker_op_id="",
        first_launch_next_action="",
        non_authoritative_gateway_host=True,
        cache_stale=True,
        error="cache stale",
        first_failed_gate={
            "name": "gateway_host",
            "detail": "Fresh operator queue is unavailable on this host.",
            "next_action": "Run the paper-live check on the VPS.",
        },
    )

    assert resolved["detail"] == "Fresh operator queue is unavailable on this host."
    assert resolved["broker_bracket_missing_count"] == 2
    assert resolved["non_authoritative_gateway_host"] is True


def test_build_paper_live_transition_summary_falls_back_to_launch_or_gate_detail() -> None:
    payload = build_paper_live_transition_summary(
        raw_status="blocked",
        detail="",
        stale_receipt=False,
        stale_detail="",
        effective_status="blocked",
        effective_detail="blocked",
        held_by_bracket_audit=False,
        held_by_daily_loss_stop=False,
        daily_loss_gate_mode="enforce",
        daily_loss_advisory_active=False,
        capital_lanes_held_by_daily_loss_stop=False,
        daily_loss_suppressed_non_authoritative_gateway_host=False,
        broker_bracket_missing_count=0,
        broker_bracket_primary_symbol="",
        broker_bracket_primary_venue="",
        broker_bracket_primary_sec_type="",
        paper_ready_bots=9,
        critical_ready=False,
        operator_queue_launch_blocked_count=1,
        first_launch_blocker_op_id="OP-19",
        first_launch_next_action="Apply gateway authority on VPS.",
        non_authoritative_gateway_host=True,
        cache_stale=True,
        error=None,
        first_failed_gate={
            "name": "tws_api_4002",
            "detail": "Keep supervisor in paper_sim until TWS API 4002 is back.",
            "next_action": "python -m eta_engine.scripts.tws_watchdog --host 127.0.0.1 --port 4002",
        },
    )

    assert payload["detail"] == "Apply gateway authority on VPS."
    assert payload["first_failed_gate"]["name"] == "tws_api_4002"
    assert payload["first_launch_blocker_op_id"] == "OP-19"


def test_build_dashboard_paper_live_transition_diagnostics_summary_shares_launch_rollup() -> None:
    payload = build_dashboard_paper_live_transition_diagnostics_summary(
        roster_summary={
            "paper_live_effective_status": "ready_to_launch_paper_live",
            "paper_live_effective_detail": "",
            "paper_live_daily_loss_gate_mode": "warn",
        },
        paper_live_transition={
            "status": "ready_to_launch_paper_live",
            "detail": "",
            "paper_ready_bots": 12,
            "critical_ready": True,
            "operator_queue_launch_blocked_count": 1,
        },
        operator_queue={
            "cache_stale": False,
            "launch_blocked_count": 1,
        },
        first_launch_blocker={
            "op_id": "OP-19",
            "next_actions": ["Apply gateway authority on VPS."],
            "detail": "Seed IBC credentials and recover TWS API 4002.",
        },
        first_failed_gate={"name": "gateway_host", "detail": None, "next_action": 7},
        default_daily_loss_gate_mode="warn",
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="hold detail",
    )

    assert payload["effective_status"] == "blocked_by_operator_queue"
    assert payload["detail"] == "Apply gateway authority on VPS."
    assert payload["first_launch_blocker_op_id"] == "OP-19"
    assert payload["first_launch_next_action"] == "Apply gateway authority on VPS."
    assert payload["first_failed_gate"] == {
        "name": "gateway_host",
        "detail": "",
        "next_action": "7",
    }


def test_build_operator_queue_diagnostics_summary_preserves_stale_fallback_launch_blocker() -> None:
    payload = build_operator_queue_diagnostics_summary(
        operator_summary={"BLOCKED": 3, "OBSERVED": 11, "UNKNOWN": 0},
        operator_queue={
            "source": "operator_action_queue",
            "launch_blocked_count": 1,
            "cache_status": "stale_fallback",
            "cache_stale": False,
            "stale_cache_age_s": 3600,
        },
        first_operator_blocker={"op_id": "OP-19", "title": "IB Gateway API blocked"},
        first_operator_evidence={},
        first_operator_blocked_bots=[],
        first_operator_next_actions=[],
        first_launch_blocker={
            "op_id": "OP-19",
            "detail": "Seed IBC credentials and recover TWS API 4002.",
        },
        first_operator_advisory={},
        first_operator_advisory_evidence={},
        first_operator_advisory_blocked_bots=[],
        first_operator_advisory_next_actions=[],
    )

    assert payload["blocked"] == 3
    assert payload["launch_blocked"] == 1
    assert payload["top_launch_blocker_op_id"] == "OP-19"
    assert payload["top_launch_blocker_detail"] == "Seed IBC credentials and recover TWS API 4002."
    assert payload["cache_status"] == "stale_fallback"
    assert payload["cache_stale"] is False
    assert payload["stale_cache_age_s"] == 3600


def test_build_operator_queue_diagnostics_summary_keeps_advisory_lane_separate() -> None:
    payload = build_operator_queue_diagnostics_summary(
        operator_summary={"BLOCKED": 1, "OBSERVED": 11, "UNKNOWN": 0},
        operator_queue={
            "source": "operator_action_queue",
            "non_launch_blocked_count": 1,
            "launch_blocked_count": 0,
        },
        first_operator_blocker={
            "op_id": "OP-16",
            "title": "Research candidates need promotion proof",
            "detail": "4 research candidate bot(s) still below promotion gate.",
        },
        first_operator_evidence={
            "launch_blocker": False,
            "launch_role": "strategy_optimization_backlog",
        },
        first_operator_blocked_bots=[
            "mbt_overnight_gap",
            "mbt_rth_orb",
            "mgc_sweep_reclaim",
            "mes_sweep_reclaim_v2",
        ],
        first_operator_next_actions=[
            "python -m eta_engine.scripts.paper_live_launch_check --bots mbt_overnight_gap --json",
            "python -m eta_engine.scripts.paper_live_launch_check --bots mgc_sweep_reclaim --json",
        ],
        first_launch_blocker={},
        first_operator_advisory={
            "op_id": "OP-16",
            "title": "Research candidates need promotion proof",
            "detail": "4 research candidate bot(s) still below promotion gate.",
        },
        first_operator_advisory_evidence={
            "launch_role": "strategy_optimization_backlog",
        },
        first_operator_advisory_blocked_bots=[
            "mbt_overnight_gap",
            "mbt_rth_orb",
            "mgc_sweep_reclaim",
            "mes_sweep_reclaim_v2",
        ],
        first_operator_advisory_next_actions=[
            "python -m eta_engine.scripts.paper_live_launch_check --bots mbt_overnight_gap --json",
            "python -m eta_engine.scripts.paper_live_launch_check --bots mgc_sweep_reclaim --json",
        ],
    )

    assert payload["advisory_count"] == 1
    assert payload["advisory_only"] is True
    assert payload["top_blocker_op_id"] == "OP-16"
    assert payload["top_blocker_launch_blocker"] is False
    assert payload["top_blocker_launch_role"] == "strategy_optimization_backlog"
    assert payload["top_advisory_op_id"] == "OP-16"
    assert payload["top_advisory_detail"] == "4 research candidate bot(s) still below promotion gate."
    assert payload["top_advisory_blocked_bots"] == [
        "mbt_overnight_gap",
        "mbt_rth_orb",
        "mgc_sweep_reclaim",
        "mes_sweep_reclaim_v2",
    ]


def test_build_dashboard_paper_live_transition_diagnostics_summary_blocks_ready_launch_queue() -> None:
    payload = build_dashboard_paper_live_transition_diagnostics_summary(
        roster_summary={},
        paper_live_transition={
            "status": "ready_to_launch_paper_live",
            "effective_status": "ready_to_launch_paper_live",
            "critical_ready": True,
            "paper_ready_bots": 9,
            "gates": [],
        },
        operator_queue={
            "cache_stale": False,
            "launch_blocked_count": 1,
        },
        first_launch_blocker={
            "op_id": "OP-20",
            "detail": "Do not unlock new entries until broker/supervisor positions reconcile.",
        },
        first_failed_gate={},
        default_daily_loss_gate_mode="warn",
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="hold detail",
    )

    assert payload["status"] == "ready_to_launch_paper_live"
    assert payload["effective_status"] == "blocked_by_operator_queue"
    assert payload["operator_queue_launch_blocked_count"] == 1
    assert payload["first_launch_blocker_op_id"] == "OP-20"
    assert payload["first_launch_next_action"] == (
        "Do not unlock new entries until broker/supervisor positions reconcile."
    )


def test_build_dashboard_paper_live_transition_diagnostics_summary_uses_roster_daily_loss_merge() -> None:
    payload = build_dashboard_paper_live_transition_diagnostics_summary(
        roster_summary={
            "paper_live_daily_loss_advisory_active": True,
            "paper_live_capital_lanes_held_by_daily_loss_stop": True,
        },
        paper_live_transition={
            "status": "ready_to_launch_paper_live",
            "effective_status": "ready_to_launch_paper_live",
            "critical_ready": True,
            "paper_ready_bots": 9,
            "gates": [],
        },
        operator_queue={"cache_stale": False, "launch_blocked_count": 0},
        first_launch_blocker={},
        first_failed_gate={},
        default_daily_loss_gate_mode="advisory",
        daily_loss_shadow_detail="Shadow paper remains live until reset: day_pnl=$-925.50 <= limit=$-900.00",
        daily_loss_hold_detail="hold detail",
    )

    assert payload["effective_status"] == "shadow_paper_active"
    assert payload["daily_loss_gate_mode"] == "advisory"
    assert payload["daily_loss_advisory_active"] is True
    assert payload["capital_lanes_held_by_daily_loss_stop"] is True
    assert "Shadow paper remains live" in payload["effective_detail"]


def test_build_master_status_paper_live_state_marks_shadow_runtime_active() -> None:
    payload = build_master_status_paper_live_state(
        paper={
            "status": "blocked",
            "critical_ready": False,
            "paper_ready_bots": 11,
            "operator_queue_first_launch_next_action": "",
        },
        runtime_mode="paper_live",
        paper_ready=False,
        blocked=5,
        launch_blocked=1,
        broker_bracket_prop_dry_run_blocked=False,
        broker_bracket_action_labels=[],
        broker_bracket_effective_detail="",
        daily_loss_killswitch={"status": "clear"},
        paper_live_lane_state={
            "held_by_daily_loss_stop": False,
            "daily_loss_advisory_active": False,
            "gate_mode": "warn",
            "capital_lanes_held_by_daily_loss_stop": False,
        },
        first_failed_gate={},
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="hold detail",
        shadow_runtime_active=True,
        shadow_runtime_detail="live shadow paper lane active on 1 attached bot(s)",
    )

    assert payload["paper_live"]["effective_status"] == "shadow_paper_active"
    assert payload["paper_live"]["effective_detail"] == "live shadow paper lane active on 1 attached bot(s)"
    assert payload["paper_card"] == {
        "status": "YELLOW",
        "detail": "shadow_paper_active",
    }


def test_build_master_status_paper_live_state_marks_bracket_audit_hold() -> None:
    payload = build_master_status_paper_live_state(
        paper={
            "status": "ready_to_launch_paper_live",
            "critical_ready": True,
            "paper_ready_bots": 9,
        },
        runtime_mode="paper_live",
        paper_ready=True,
        blocked=0,
        launch_blocked=0,
        broker_bracket_prop_dry_run_blocked=True,
        broker_bracket_action_labels=[
            "Verify broker OCO coverage",
            "Flatten unprotected paper exposure",
        ],
        broker_bracket_effective_detail="held by Bracket Audit",
        daily_loss_killswitch={"status": "clear"},
        paper_live_lane_state={
            "held_by_daily_loss_stop": False,
            "daily_loss_advisory_active": False,
            "gate_mode": "warn",
            "capital_lanes_held_by_daily_loss_stop": False,
        },
        first_failed_gate={},
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="hold detail",
    )

    assert payload["paper_live"]["held_by_bracket_audit"] is True
    assert payload["paper_live"]["effective_status"] == "held_by_bracket_audit"
    assert payload["paper_live"]["effective_detail"] == (
        "held by Bracket Audit: Verify broker OCO coverage or Flatten unprotected paper exposure"
    )
    assert payload["paper_card"] == {
        "status": "YELLOW",
        "detail": "held_by_bracket_audit",
    }

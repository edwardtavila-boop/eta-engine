from eta_engine.core.execution_lanes import (
    CAPITAL_EXECUTION_LANE,
    DAILY_LOSS_GATE_ADVISORY,
    DAILY_LOSS_GATE_ENFORCE,
    DAILY_LOSS_GATE_INACTIVE,
    RESEARCH_SIM_LANE,
    SHADOW_PAPER_LANE,
    capital_gate_scope_for_lane,
    daily_loss_gate_mode_for_lane,
    execution_lane_from_fields,
)


def test_paper_live_without_live_money_maps_to_shadow_paper() -> None:
    lane = execution_lane_from_fields(mode="paper_live", route_target="paper", live_money_enabled=False)

    assert lane == SHADOW_PAPER_LANE
    assert capital_gate_scope_for_lane(lane) == "shadow_observe"
    assert daily_loss_gate_mode_for_lane(lane) == DAILY_LOSS_GATE_ADVISORY


def test_live_money_always_maps_to_capital_execution_enforce() -> None:
    lane = execution_lane_from_fields(mode="paper_live", route_target="live", live_money_enabled=True)

    assert lane == CAPITAL_EXECUTION_LANE
    assert capital_gate_scope_for_lane(lane) == "prop_live"
    assert daily_loss_gate_mode_for_lane(lane, explicit_policy="advisory") == DAILY_LOSS_GATE_ENFORCE


def test_research_sim_ignores_daily_loss_gate() -> None:
    lane = execution_lane_from_fields(mode="paper_sim")

    assert lane == RESEARCH_SIM_LANE
    assert capital_gate_scope_for_lane(lane) == "none"
    assert daily_loss_gate_mode_for_lane(lane) == DAILY_LOSS_GATE_INACTIVE

from __future__ import annotations

RESEARCH_SIM_LANE = "research_sim"
SHADOW_PAPER_LANE = "shadow_paper"
CAPITAL_EXECUTION_LANE = "capital_execution"

DAILY_LOSS_GATE_INACTIVE = "inactive"
DAILY_LOSS_GATE_ADVISORY = "advisory"
DAILY_LOSS_GATE_ENFORCE = "enforce"

CAPITAL_GATE_SCOPE_NONE = "none"
CAPITAL_GATE_SCOPE_SHADOW_OBSERVE = "shadow_observe"
CAPITAL_GATE_SCOPE_PROP_LIVE = "prop_live"
CAPITAL_GATE_SCOPE_UNKNOWN = "unknown"

_ADVISORY_ALIASES = {"advisory", "warn", "warning", "observe", "soft"}
_ENFORCE_ALIASES = {"enforce", "hard", "block", "deny", "strict"}
_INACTIVE_ALIASES = {"inactive", "off", "disabled", "ignore", "none"}


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def normalize_execution_lane(value: object, *, default: str = "") -> str:
    lane = _norm(value)
    if lane in {
        RESEARCH_SIM_LANE,
        SHADOW_PAPER_LANE,
        CAPITAL_EXECUTION_LANE,
    }:
        return lane
    return default


def normalize_daily_loss_gate_mode(value: object, *, default: str = DAILY_LOSS_GATE_ENFORCE) -> str:
    mode = _norm(value)
    if mode in _ADVISORY_ALIASES:
        return DAILY_LOSS_GATE_ADVISORY
    if mode in _ENFORCE_ALIASES:
        return DAILY_LOSS_GATE_ENFORCE
    if mode in _INACTIVE_ALIASES:
        return DAILY_LOSS_GATE_INACTIVE
    return default


def execution_lane_from_fields(
    *,
    mode: object,
    route_target: object | None = None,
    live_money_enabled: bool = False,
) -> str:
    mode_norm = _norm(mode)
    target_norm = _norm(route_target)

    if mode_norm in {"paper_sim", "sim", "backtest", RESEARCH_SIM_LANE}:
        return RESEARCH_SIM_LANE
    if target_norm == "paper":
        return SHADOW_PAPER_LANE
    if target_norm == "live":
        return CAPITAL_EXECUTION_LANE
    if mode_norm == "paper_live":
        return CAPITAL_EXECUTION_LANE if bool(live_money_enabled) else SHADOW_PAPER_LANE
    if mode_norm == "live" or bool(live_money_enabled):
        return CAPITAL_EXECUTION_LANE
    return ""


def capital_gate_scope_for_lane(lane: object) -> str:
    lane_norm = normalize_execution_lane(lane, default="")
    if lane_norm == RESEARCH_SIM_LANE:
        return CAPITAL_GATE_SCOPE_NONE
    if lane_norm == SHADOW_PAPER_LANE:
        return CAPITAL_GATE_SCOPE_SHADOW_OBSERVE
    if lane_norm == CAPITAL_EXECUTION_LANE:
        return CAPITAL_GATE_SCOPE_PROP_LIVE
    return CAPITAL_GATE_SCOPE_UNKNOWN


def daily_loss_gate_mode_for_lane(
    lane: object,
    *,
    explicit_policy: object | None = None,
) -> str:
    lane_norm = normalize_execution_lane(lane, default="")
    policy_norm = normalize_daily_loss_gate_mode(explicit_policy, default="")
    if lane_norm == RESEARCH_SIM_LANE:
        return DAILY_LOSS_GATE_INACTIVE
    if lane_norm == SHADOW_PAPER_LANE:
        if policy_norm in {DAILY_LOSS_GATE_ENFORCE, DAILY_LOSS_GATE_INACTIVE}:
            return policy_norm
        return DAILY_LOSS_GATE_ADVISORY
    if lane_norm == CAPITAL_EXECUTION_LANE:
        return DAILY_LOSS_GATE_ENFORCE
    if policy_norm:
        return policy_norm
    return DAILY_LOSS_GATE_ENFORCE


def gate_enforced(mode: object) -> bool:
    return normalize_daily_loss_gate_mode(mode) == DAILY_LOSS_GATE_ENFORCE


def gate_advisory(mode: object) -> bool:
    return normalize_daily_loss_gate_mode(mode) == DAILY_LOSS_GATE_ADVISORY


def gate_inactive(mode: object) -> bool:
    return normalize_daily_loss_gate_mode(mode) == DAILY_LOSS_GATE_INACTIVE

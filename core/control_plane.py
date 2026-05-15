from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

QUALITY_TIER_LIVE = "live"
QUALITY_TIER_CACHED = "cached"
QUALITY_TIER_SYNTHETIC = "synthetic"
QUALITY_TIER_STALE = "stale"
QUALITY_TIER_MISSING = "missing"
QUALITY_TIER_INFERRED = "inferred"

GATE_STATE_HEALTHY = "healthy"
GATE_STATE_DEGRADED = "degraded"
GATE_STATE_PAPER_ONLY = "paper_only"
GATE_STATE_SHADOW = "shadow"
GATE_STATE_CANDIDATE_LIVE = "candidate_live"
GATE_STATE_LIVE = "live"
GATE_STATE_BLOCKED = "blocked"
GATE_STATE_QUARANTINED = "quarantined"

_FEED_STATUS_TO_QUALITY = {
    "healthy": QUALITY_TIER_LIVE,
    "stale": QUALITY_TIER_STALE,
    "gap": QUALITY_TIER_STALE,
    "anomaly": QUALITY_TIER_INFERRED,
    "missing": QUALITY_TIER_MISSING,
    "critical": QUALITY_TIER_MISSING,
}

_FEED_STATUS_TO_GATE = {
    "healthy": GATE_STATE_HEALTHY,
    "stale": GATE_STATE_PAPER_ONLY,
    "gap": GATE_STATE_PAPER_ONLY,
    "anomaly": GATE_STATE_PAPER_ONLY,
    "missing": GATE_STATE_BLOCKED,
    "critical": GATE_STATE_BLOCKED,
}


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def quality_tier_from_feed_status(status: object) -> str:
    return _FEED_STATUS_TO_QUALITY.get(_norm(status), QUALITY_TIER_INFERRED)


def gate_state_from_feed_status(status: object) -> str:
    return _FEED_STATUS_TO_GATE.get(_norm(status), GATE_STATE_DEGRADED)


def gate_state_allows_progress(state: object) -> bool:
    return _norm(state) in {
        GATE_STATE_HEALTHY,
        GATE_STATE_DEGRADED,
        GATE_STATE_PAPER_ONLY,
        GATE_STATE_SHADOW,
        GATE_STATE_CANDIDATE_LIVE,
        GATE_STATE_LIVE,
    }


def gate_state_allows_live(state: object) -> bool:
    return _norm(state) == GATE_STATE_LIVE


def overall_status_from_gate_states(states: list[str] | tuple[str, ...]) -> str:
    normalized = {_norm(state) for state in states}
    if GATE_STATE_BLOCKED in normalized or GATE_STATE_QUARANTINED in normalized:
        return "critical"
    if normalized & {
        GATE_STATE_DEGRADED,
        GATE_STATE_PAPER_ONLY,
        GATE_STATE_SHADOW,
        GATE_STATE_CANDIDATE_LIVE,
    }:
        return "degraded"
    return "healthy"


@dataclass(frozen=True)
class MarketDataSnapshot:
    snapshot_id: str
    symbol: str
    venue: str
    timeframe: str
    ts_event: str
    ts_ingested: str
    quality_tier: str
    source: str
    staleness_s: float | None
    gap_count: int
    anomaly_flags: list[str]
    drift_flags: list[str]
    lineage_id: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FeatureSnapshot:
    feature_id: str
    symbol: str
    policy_family: str
    ts_ready: str
    quality_tier: str
    latency_ms: float
    required_inputs: list[str]
    missing_inputs: list[str]
    lineage_id: str
    features: dict[str, float | int | str | bool | None]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GateDecision:
    gate_id: str
    gate_family: str
    target_id: str
    state: str
    passed: bool
    reason_codes: list[str]
    detail: str
    evidence_refs: list[str]
    next_action: str | None
    ts_decided: str
    latency_ms: float
    lineage_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionIntent:
    client_order_id: str
    bot_id: str
    symbol: str
    venue: str
    mode: str
    gate_state: str
    feature_id: str | None
    market_snapshot_id: str | None
    route_reason_codes: list[str]
    ts_created: str
    intent_payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReadinessSnapshot:
    entity_id: str
    entity_type: str
    launch_lane: str
    overall_state: str
    data_state: str
    gate_state: str
    execution_state: str
    can_paper_trade: bool
    can_shadow_trade: bool
    can_live_trade: bool
    next_action: str | None
    updated_at: str
    evidence_refs: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


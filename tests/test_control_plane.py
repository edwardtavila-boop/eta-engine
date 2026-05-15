from __future__ import annotations

from eta_engine.core.control_plane import (
    GATE_STATE_BLOCKED,
    GATE_STATE_DEGRADED,
    GATE_STATE_HEALTHY,
    GATE_STATE_LIVE,
    GATE_STATE_PAPER_ONLY,
    QUALITY_TIER_LIVE,
    QUALITY_TIER_MISSING,
    GateDecision,
    MarketDataSnapshot,
    gate_state_allows_live,
    gate_state_allows_progress,
    gate_state_from_feed_status,
    overall_status_from_gate_states,
    quality_tier_from_feed_status,
)


def test_quality_tier_from_feed_status_maps_missing_and_critical_to_missing() -> None:
    assert quality_tier_from_feed_status("missing") == QUALITY_TIER_MISSING
    assert quality_tier_from_feed_status("critical") == QUALITY_TIER_MISSING
    assert quality_tier_from_feed_status("healthy") == QUALITY_TIER_LIVE


def test_gate_state_from_feed_status_maps_soft_and_hard_failures() -> None:
    assert gate_state_from_feed_status("healthy") == GATE_STATE_HEALTHY
    assert gate_state_from_feed_status("stale") == GATE_STATE_PAPER_ONLY
    assert gate_state_from_feed_status("gap") == GATE_STATE_PAPER_ONLY
    assert gate_state_from_feed_status("critical") == GATE_STATE_BLOCKED


def test_gate_state_allowance_helpers_match_the_design_spec() -> None:
    assert gate_state_allows_progress(GATE_STATE_HEALTHY) is True
    assert gate_state_allows_progress(GATE_STATE_DEGRADED) is True
    assert gate_state_allows_progress(GATE_STATE_PAPER_ONLY) is True
    assert gate_state_allows_progress(GATE_STATE_BLOCKED) is False
    assert gate_state_allows_live(GATE_STATE_LIVE) is True
    assert gate_state_allows_live(GATE_STATE_PAPER_ONLY) is False


def test_overall_status_from_gate_states_prioritizes_blocked_then_degraded() -> None:
    assert overall_status_from_gate_states([GATE_STATE_HEALTHY, GATE_STATE_LIVE]) == "healthy"
    assert overall_status_from_gate_states([GATE_STATE_HEALTHY, GATE_STATE_PAPER_ONLY]) == "degraded"
    assert overall_status_from_gate_states([GATE_STATE_BLOCKED, GATE_STATE_HEALTHY]) == "critical"


def test_control_plane_dataclasses_round_trip_to_dict() -> None:
    feed = MarketDataSnapshot(
        snapshot_id="snap-1",
        symbol="BTC",
        venue="coinbase",
        timeframe="5m",
        ts_event="2026-05-15T12:00:00+00:00",
        ts_ingested="2026-05-15T12:00:01+00:00",
        quality_tier=QUALITY_TIER_LIVE,
        source="feed_test",
        staleness_s=1.0,
        gap_count=0,
        anomaly_flags=[],
        drift_flags=[],
        lineage_id="lineage-1",
        payload={"close": 101.25},
    )
    gate = GateDecision(
        gate_id="gate-1",
        gate_family="data",
        target_id="BTC",
        state=GATE_STATE_HEALTHY,
        passed=True,
        reason_codes=[],
        detail="ok",
        evidence_refs=["snap-1"],
        next_action=None,
        ts_decided="2026-05-15T12:00:02+00:00",
        latency_ms=4.2,
        lineage_id="lineage-1",
    )

    assert feed.to_dict()["quality_tier"] == QUALITY_TIER_LIVE
    assert gate.to_dict()["state"] == GATE_STATE_HEALTHY

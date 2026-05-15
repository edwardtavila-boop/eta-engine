"""Tests for the Zeus unified-brain snapshot."""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _clear_zeus_cache():
    from eta_engine.brain.jarvis_v3 import zeus

    zeus.clear_cache()
    yield
    zeus.clear_cache()


def test_snapshot_returns_zeus_snapshot_dataclass() -> None:
    from eta_engine.brain.jarvis_v3 import zeus

    snap = zeus.snapshot()
    assert isinstance(snap, zeus.ZeusSnapshot)
    # All slots are present even when individual sub-fetches fail
    d = snap.to_dict()
    expected_keys = {
        "asof",
        "fleet_status",
        "topology",
        "overrides",
        "regime",
        "recent_consults",
        "kelly_recs",
        "attribution_top",
        "sentiment",
        "wiring_audit",
        "upcoming_events",
        "bots_online",
        "memory_top",
        "cache_age_s",
    }
    assert expected_keys.issubset(d.keys())


def test_snapshot_never_raises_when_subfetches_fail(monkeypatch) -> None:
    """Force all sub-fetches to raise; snapshot still returns cleanly."""
    from eta_engine.brain.jarvis_v3 import zeus

    def boom(*a, **kw):
        raise RuntimeError("simulated subsystem failure")

    monkeypatch.setattr(zeus, "_fetch_fleet_status", boom)
    monkeypatch.setattr(zeus, "_fetch_topology_summary", boom)
    monkeypatch.setattr(zeus, "_fetch_overrides", boom)
    monkeypatch.setattr(zeus, "_fetch_regime", boom)
    monkeypatch.setattr(zeus, "_fetch_recent_consults", boom)
    monkeypatch.setattr(zeus, "_fetch_kelly_top5", boom)
    monkeypatch.setattr(zeus, "_fetch_attribution_top", boom)
    monkeypatch.setattr(zeus, "_fetch_sentiment", boom)
    monkeypatch.setattr(zeus, "_fetch_wiring_audit", boom)
    monkeypatch.setattr(zeus, "_fetch_upcoming_events", boom)
    monkeypatch.setattr(zeus, "_fetch_bots_online", boom)

    snap = zeus.snapshot(force_refresh=True)
    # Each failed dict-typed sub-fetch gets an "error" key
    assert "error" in snap.fleet_status
    assert "error" in snap.topology
    assert "error" in snap.overrides
    assert "error" in snap.regime
    assert "error" in snap.wiring_audit
    # List-typed defaults are empty
    assert snap.recent_consults == []
    assert snap.kelly_recs == []
    assert snap.upcoming_events == []
    assert snap.bots_online == []


def test_snapshot_caches_for_30s(monkeypatch) -> None:
    """Two snapshots within the cache window share the same underlying build."""
    from eta_engine.brain.jarvis_v3 import zeus

    call_count = {"n": 0}

    def counted_fleet():
        call_count["n"] += 1
        return {"n_bots": call_count["n"], "tier_counts": {}}

    monkeypatch.setattr(zeus, "_fetch_fleet_status", counted_fleet)

    s1 = zeus.snapshot(force_refresh=True)
    s2 = zeus.snapshot()  # within TTL
    # Both reflect the SAME build (1 fleet call total)
    assert s1.fleet_status["n_bots"] == s2.fleet_status["n_bots"]
    assert call_count["n"] == 1
    # Second call shows non-zero cache age
    assert s2.cache_age_s >= 0


def test_snapshot_force_refresh_bypasses_cache(monkeypatch) -> None:
    from eta_engine.brain.jarvis_v3 import zeus

    call_count = {"n": 0}

    def counted_fleet():
        call_count["n"] += 1
        return {"n_bots": call_count["n"], "tier_counts": {}}

    monkeypatch.setattr(zeus, "_fetch_fleet_status", counted_fleet)

    s1 = zeus.snapshot(force_refresh=True)
    s2 = zeus.snapshot(force_refresh=True)
    # Two builds → two fleet calls
    assert call_count["n"] == 2
    assert s1.fleet_status["n_bots"] == 1
    assert s2.fleet_status["n_bots"] == 2


def test_clear_cache_forces_rebuild(monkeypatch) -> None:
    from eta_engine.brain.jarvis_v3 import zeus

    call_count = {"n": 0}

    def counted_fleet():
        call_count["n"] += 1
        return {"n_bots": call_count["n"], "tier_counts": {}}

    monkeypatch.setattr(zeus, "_fetch_fleet_status", counted_fleet)

    zeus.snapshot(force_refresh=True)
    assert call_count["n"] == 1
    zeus.clear_cache()
    zeus.snapshot()
    # After clear, the next snapshot rebuilds even without force_refresh
    assert call_count["n"] == 2


def test_snapshot_to_dict_is_json_serializable() -> None:
    """to_dict() returns a structure usable as MCP envelope."""
    from eta_engine.brain.jarvis_v3 import zeus

    snap = zeus.snapshot()
    d = snap.to_dict()
    # Round-trip through JSON without exception
    payload = json.dumps(d, default=str)
    assert isinstance(payload, str)
    assert len(payload) > 100


def test_snapshot_attribution_top_structure(monkeypatch) -> None:
    """attribution_top has top_winners + top_losers keys."""
    from eta_engine.brain.jarvis_v3 import zeus

    monkeypatch.setattr(
        zeus,
        "_fetch_attribution_top",
        lambda: {
            "top_winners": [{"bot_id": "w1", "total_r": 5.0, "n_trades": 10, "win_rate": 0.8}],
            "top_losers": [{"bot_id": "l1", "total_r": -3.0, "n_trades": 8, "win_rate": 0.2}],
            "n_total_bots_with_trades": 2,
        },
    )
    snap = zeus.snapshot(force_refresh=True)
    assert "top_winners" in snap.attribution_top
    assert "top_losers" in snap.attribution_top
    assert snap.attribution_top["top_winners"][0]["bot_id"] == "w1"


def test_fetch_sentiment_includes_macro_and_sol(monkeypatch) -> None:
    from eta_engine.brain.jarvis_v3 import sentiment_overlay, zeus

    monkeypatch.setattr(sentiment_overlay, "current_sentiment", lambda asset: {"asset": asset})

    out = zeus._fetch_sentiment()

    assert set(out) == {"BTC", "ETH", "SOL", "macro"}
    assert out["macro"]["asset"] == "macro"

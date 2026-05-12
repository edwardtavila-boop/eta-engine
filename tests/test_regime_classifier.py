"""Tests for regime_classifier — T8 rule-based regime detection + override packs."""
from __future__ import annotations


def test_current_regime_unknown_when_no_signals(monkeypatch) -> None:
    """Fresh install with no sentiment / no traces → UNKNOWN, low confidence."""
    from eta_engine.brain.jarvis_v3 import regime_classifier

    monkeypatch.setattr(regime_classifier, "_safe_sentiment", lambda asset: None)
    monkeypatch.setattr(regime_classifier, "_safe_fleet_drawdown", lambda: None)

    rep = regime_classifier.current_regime()
    assert rep.regime == "UNKNOWN"
    assert rep.confidence < 0.5
    assert rep.recommended_pack is None


def test_current_regime_euphoria(monkeypatch) -> None:
    """fear_greed >= 0.85 AND social_volume_z >= 1.5 → EUPHORIA."""
    from eta_engine.brain.jarvis_v3 import regime_classifier

    monkeypatch.setattr(regime_classifier, "_safe_sentiment", lambda asset: {
        "fear_greed": 0.90,
        "social_volume_z": 2.0,
        "topic_flags": {},
    })
    monkeypatch.setattr(regime_classifier, "_safe_fleet_drawdown", lambda: None)

    rep = regime_classifier.current_regime()
    assert rep.regime == "EUPHORIA"
    assert rep.recommended_pack == "euphoria"


def test_current_regime_capitulation(monkeypatch) -> None:
    """fear_greed <= 0.15 + capitulation flag → CAPITULATION."""
    from eta_engine.brain.jarvis_v3 import regime_classifier

    monkeypatch.setattr(regime_classifier, "_safe_sentiment", lambda asset: {
        "fear_greed": 0.10,
        "topic_flags": {"capitulation": True},
    })
    monkeypatch.setattr(regime_classifier, "_safe_fleet_drawdown", lambda: None)

    rep = regime_classifier.current_regime()
    assert rep.regime == "CAPITULATION"
    assert rep.recommended_pack == "capitulation"


def test_current_regime_chaos_on_deep_drawdown(monkeypatch) -> None:
    """drawdown <= -3R → CHAOS."""
    from eta_engine.brain.jarvis_v3 import regime_classifier

    monkeypatch.setattr(regime_classifier, "_safe_sentiment", lambda asset: None)
    monkeypatch.setattr(regime_classifier, "_safe_fleet_drawdown", lambda: -3.5)

    rep = regime_classifier.current_regime()
    assert rep.regime == "CHAOS"
    assert rep.recommended_pack == "chaos"


def test_current_regime_vol_trend_on_mid_drawdown(monkeypatch) -> None:
    """-1R > drawdown > -3R → VOL_TREND."""
    from eta_engine.brain.jarvis_v3 import regime_classifier

    monkeypatch.setattr(regime_classifier, "_safe_sentiment", lambda asset: None)
    monkeypatch.setattr(regime_classifier, "_safe_fleet_drawdown", lambda: -2.0)

    rep = regime_classifier.current_regime()
    assert rep.regime == "VOL_TREND"


def test_current_regime_calm_trend_default(monkeypatch) -> None:
    """Neutral signals → CALM_TREND (mild confidence)."""
    from eta_engine.brain.jarvis_v3 import regime_classifier

    monkeypatch.setattr(regime_classifier, "_safe_sentiment", lambda asset: {
        "fear_greed": 0.55, "social_volume_z": 0.3, "topic_flags": {},
    })
    monkeypatch.setattr(regime_classifier, "_safe_fleet_drawdown", lambda: -0.2)

    rep = regime_classifier.current_regime()
    assert rep.regime == "CALM_TREND"
    assert rep.recommended_pack == "calm_trend"


def test_list_packs_returns_all_builtin() -> None:
    from eta_engine.brain.jarvis_v3 import regime_classifier

    packs = regime_classifier.list_packs()
    names = {p["name"] for p in packs}
    assert names == {"calm_trend", "vol_trend", "range", "chaos", "euphoria", "capitulation"}


def test_apply_pack_unknown_rejected(tmp_path, monkeypatch) -> None:
    from eta_engine.brain.jarvis_v3 import regime_classifier

    res = regime_classifier.apply_pack("not_a_real_pack")
    assert res["status"] == "REJECTED"


def test_apply_pack_writes_school_weights(tmp_path, monkeypatch) -> None:
    """Applying calm_trend writes the expected school overrides."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides, regime_classifier

    monkeypatch.setattr(
        hermes_overrides, "DEFAULT_OVERRIDES_PATH", tmp_path / "ho.json",
    )
    res = regime_classifier.apply_pack("calm_trend", ttl_minutes=60)
    assert res["status"] == "APPLIED"
    # Check that at least one school weight landed for MNQ
    mnq_w = hermes_overrides.get_school_weights("MNQ", path=tmp_path / "ho.json")
    assert "momentum" in mnq_w
    assert mnq_w["momentum"] == 1.15


def test_apply_pack_with_star_pattern_needs_bot_ids(tmp_path, monkeypatch) -> None:
    """Pack with '*' size_modifier pattern requires bot_ids arg; otherwise error."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides, regime_classifier

    monkeypatch.setattr(
        hermes_overrides, "DEFAULT_OVERRIDES_PATH", tmp_path / "ho.json",
    )
    # vol_trend has size_modifiers = {"*": 0.7}
    res = regime_classifier.apply_pack("vol_trend", ttl_minutes=60)
    # Without bot_ids, the '*' pattern produces an error in the summary
    assert "errors" in res
    assert any("bot_ids" in e for e in res["errors"])


def test_apply_pack_with_star_and_bot_ids(tmp_path, monkeypatch) -> None:
    """vol_trend pack with explicit bot_ids applies the modifier to each bot."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides, regime_classifier

    monkeypatch.setattr(
        hermes_overrides, "DEFAULT_OVERRIDES_PATH", tmp_path / "ho.json",
    )
    res = regime_classifier.apply_pack(
        "vol_trend", ttl_minutes=60, bot_ids=["bot_a", "bot_b"],
    )
    assert res["status"] in ("APPLIED", "PARTIAL")
    # Both bots got the 0.7 modifier
    assert hermes_overrides.get_size_modifier("bot_a", path=tmp_path / "ho.json") == 0.7
    assert hermes_overrides.get_size_modifier("bot_b", path=tmp_path / "ho.json") == 0.7


def test_current_regime_never_raises_on_bad_signals(monkeypatch) -> None:
    """Sentiment helper returning malformed dict → still returns a report."""
    from eta_engine.brain.jarvis_v3 import regime_classifier

    monkeypatch.setattr(regime_classifier, "_safe_sentiment", lambda asset: {
        "fear_greed": "not_a_number",  # malformed
        "social_volume_z": None,
        "topic_flags": "not_a_dict",
    })
    monkeypatch.setattr(regime_classifier, "_safe_fleet_drawdown", lambda: None)

    rep = regime_classifier.current_regime()
    # Falls through to UNKNOWN — no crash
    assert rep.regime in ("UNKNOWN", "CALM_TREND")

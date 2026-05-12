"""Tests for hermes_overrides — Track 2 write-back surface.

Covers:
  * apply/get round-trip for size_modifier and school_weight
  * TTL expiry (live entries vs expired entries)
  * clamping (out-of-range modifier values get pinned to bounds)
  * portfolio_brain.assess honors the operator override
  * hot_learner.current_weights composes the operator overlay correctly
  * read paths never raise on bad input
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def test_apply_then_get_size_modifier_roundtrip(tmp_path: Path) -> None:
    """Apply a 0.6 modifier; get should return 0.6 before expiry."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    path = tmp_path / "hermes_overrides.json"
    res = hermes_overrides.apply_size_modifier(
        bot_id="atr_breakout_mnq",
        modifier=0.6,
        reason="manual de-risk test",
        ttl_minutes=10,
        path=path,
    )
    assert res["status"] == "APPLIED"
    assert res["modifier"] == 0.6

    val = hermes_overrides.get_size_modifier("atr_breakout_mnq", path=path)
    assert val == 0.6


def test_get_size_modifier_missing_returns_none(tmp_path: Path) -> None:
    """No override for the bot → None."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    path = tmp_path / "hermes_overrides.json"
    assert hermes_overrides.get_size_modifier("not_a_real_bot", path=path) is None


def test_expired_size_modifier_returns_none(tmp_path: Path) -> None:
    """Once expires_at is past, the entry is filtered as inactive."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    path = tmp_path / "hermes_overrides.json"
    hermes_overrides.apply_size_modifier(
        bot_id="bot_x", modifier=0.5, reason="r", ttl_minutes=5, path=path,
    )
    # Look up using a "now" that's after expiry.
    future = datetime.now(UTC) + timedelta(minutes=120)
    assert hermes_overrides.get_size_modifier("bot_x", now=future, path=path) is None


def test_size_modifier_clamped_on_write(tmp_path: Path) -> None:
    """Out-of-range modifier values get clamped to de-risk-only [0.0, 1.0]."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    path = tmp_path / "hermes_overrides.json"
    res_high = hermes_overrides.apply_size_modifier(
        bot_id="b1", modifier=5.0, reason="test", ttl_minutes=10, path=path,
    )
    res_low = hermes_overrides.apply_size_modifier(
        bot_id="b2", modifier=-0.5, reason="test", ttl_minutes=10, path=path,
    )
    assert res_high["modifier"] == 1.0
    assert res_low["modifier"] == 0.0


def test_school_weight_roundtrip(tmp_path: Path) -> None:
    """Apply + get for school_weights nested under asset."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    path = tmp_path / "hermes_overrides.json"
    hermes_overrides.apply_school_weight(
        asset="MNQ", school="momentum",
        weight=1.2, reason="boost test", ttl_minutes=10, path=path,
    )
    out = hermes_overrides.get_school_weights("MNQ", path=path)
    assert out == {"momentum": 1.2}


def test_active_overrides_summary_filters_expired(tmp_path: Path) -> None:
    """Summary should hide entries whose expires_at is past."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    path = tmp_path / "hermes_overrides.json"
    hermes_overrides.apply_size_modifier(
        bot_id="live", modifier=0.7, reason="r", ttl_minutes=60, path=path,
    )
    hermes_overrides.apply_school_weight(
        asset="MNQ", school="momentum",
        weight=1.1, reason="r", ttl_minutes=60, path=path,
    )

    # Now manually inject an expired entry directly into the sidecar.
    data = json.loads(path.read_text(encoding="utf-8"))
    past = datetime.now(UTC) - timedelta(hours=1)
    data["size_modifiers"]["dead"] = {
        "modifier": 0.5,
        "reason": "old",
        "applied_at": (past - timedelta(hours=1)).isoformat(),
        "expires_at": past.isoformat(),
        "source": "hermes_mcp",
    }
    path.write_text(json.dumps(data), encoding="utf-8")

    summary = hermes_overrides.active_overrides_summary(path=path)
    assert "live" in summary["size_modifiers"]
    assert "dead" not in summary["size_modifiers"]
    assert summary["school_weights"]["MNQ"]["momentum"]["weight"] == 1.1


def test_clear_override_removes_entry(tmp_path: Path) -> None:
    """clear_override(bot_id=...) drops the bot from size_modifiers."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    path = tmp_path / "hermes_overrides.json"
    hermes_overrides.apply_size_modifier(
        bot_id="b1", modifier=0.7, reason="r", ttl_minutes=60, path=path,
    )
    res = hermes_overrides.clear_override(bot_id="b1", path=path)
    assert res["status"] == "REMOVED"
    assert hermes_overrides.get_size_modifier("b1", path=path) is None


def test_clear_override_not_found(tmp_path: Path) -> None:
    """Clearing a non-existent entry returns NOT_FOUND, not REJECTED."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    path = tmp_path / "hermes_overrides.json"
    res = hermes_overrides.clear_override(bot_id="ghost", path=path)
    assert res["status"] == "NOT_FOUND"


def test_portfolio_brain_honors_size_override(
    tmp_path: Path, monkeypatch,
) -> None:
    """portfolio_brain.assess applies the operator override AFTER its own clamp."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides, portfolio_brain

    overrides_path = tmp_path / "hermes_overrides.json"
    monkeypatch.setattr(
        hermes_overrides, "DEFAULT_OVERRIDES_PATH", overrides_path,
    )

    hermes_overrides.apply_size_modifier(
        bot_id="atr_breakout_mnq", modifier=0.5,
        reason="test pin", ttl_minutes=10, path=overrides_path,
    )

    # Build a request that would normally pass with modifier=1.0
    req = type("R", (), {
        "bot_id": "atr_breakout_mnq",
        "asset_class": "MNQ",
        "asset": "MNQ",
        "action": "ENTER",
    })()
    ctx = portfolio_brain.PortfolioContext(
        fleet_long_notional_by_asset={},
        fleet_short_notional_by_asset={},
        recent_entries_by_asset={},
        open_correlated_exposure=0.0,
        portfolio_drawdown_today_r=0.0,
        fleet_kill_active=False,
    )
    verdict = portfolio_brain.assess(req, ctx)
    # Cascade would give 1.0; override pins it to 0.5.
    assert verdict.size_modifier == 0.5
    assert any("hermes_size_override" in n for n in verdict.notes)


def test_portfolio_brain_no_override_unchanged_behavior(
    tmp_path: Path, monkeypatch,
) -> None:
    """When no override exists, the cascade result is unchanged."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides, portfolio_brain

    monkeypatch.setattr(
        hermes_overrides, "DEFAULT_OVERRIDES_PATH",
        tmp_path / "empty.json",
    )

    req = type("R", (), {
        "bot_id": "no_pin_bot", "asset_class": "MNQ",
        "asset": "MNQ", "action": "ENTER",
    })()
    ctx = portfolio_brain.PortfolioContext(
        fleet_long_notional_by_asset={},
        fleet_short_notional_by_asset={},
        recent_entries_by_asset={},
        open_correlated_exposure=0.0,
        portfolio_drawdown_today_r=0.0,
        fleet_kill_active=False,
    )
    verdict = portfolio_brain.assess(req, ctx)
    assert verdict.size_modifier == 1.0


def test_get_size_modifier_handles_corrupt_sidecar(tmp_path: Path) -> None:
    """Garbage file → get returns None, no exception."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    path = tmp_path / "hermes_overrides.json"
    path.write_text("this is not json {{{{", encoding="utf-8")
    assert hermes_overrides.get_size_modifier("anything", path=path) is None

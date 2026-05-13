"""Tests for the wave-18 tier system + PROP_READY capital routing."""
# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
from pathlib import Path


def test_tier_constants_defined() -> None:
    """TIER_PROP_READY / TIER_DIAMOND / TIER_CANDIDATE must be importable."""
    from eta_engine.feeds.capital_allocator import (
        TIER_CANDIDATE,
        TIER_DIAMOND,
        TIER_PROP_READY,
    )

    assert TIER_PROP_READY == "TIER_PROP_READY"
    assert TIER_DIAMOND == "TIER_DIAMOND"
    assert TIER_CANDIDATE == "TIER_CANDIDATE"


def test_get_bot_tier_prop_ready_wins() -> None:
    """If a bot is BOTH in DIAMOND_BOTS and PROP_READY, the tier is
    PROP_READY (it's the highest tier)."""
    from eta_engine.feeds.capital_allocator import TIER_PROP_READY, get_bot_tier

    prop_ready = frozenset({"m2k_sweep_reclaim"})
    assert get_bot_tier("m2k_sweep_reclaim", prop_ready=prop_ready) == TIER_PROP_READY


def test_get_bot_tier_diamond_only() -> None:
    """A bot in DIAMOND_BOTS but not in PROP_READY is TIER_DIAMOND."""
    from eta_engine.feeds.capital_allocator import (
        DIAMOND_BOTS,
        TIER_DIAMOND,
        get_bot_tier,
    )

    # cl_macro is a diamond but not PROP_READY-eligible (n=2)
    assert "cl_macro" in DIAMOND_BOTS
    assert get_bot_tier("cl_macro", prop_ready=frozenset()) == TIER_DIAMOND


def test_get_bot_tier_candidate_default() -> None:
    """Unknown / non-diamond bot defaults to TIER_CANDIDATE."""
    from eta_engine.feeds.capital_allocator import TIER_CANDIDATE, get_bot_tier

    assert (
        get_bot_tier("unknown_bot", prop_ready=frozenset()) == TIER_CANDIDATE
    )


def test_load_prop_ready_bots_returns_empty_on_missing(tmp_path: Path) -> None:
    """Missing leaderboard receipt → empty frozenset (never crash)."""
    from eta_engine.feeds.capital_allocator import load_prop_ready_bots

    result = load_prop_ready_bots(tmp_path / "missing.json")
    assert isinstance(result, frozenset)
    assert result == frozenset()


def test_load_prop_ready_bots_returns_empty_on_malformed(tmp_path: Path) -> None:
    """Malformed JSON → empty frozenset (degrade safely)."""
    from eta_engine.feeds.capital_allocator import load_prop_ready_bots

    bad = tmp_path / "bad.json"
    bad.write_text("not valid json{{{", encoding="utf-8")
    assert load_prop_ready_bots(bad) == frozenset()


def test_load_prop_ready_bots_parses_receipt(tmp_path: Path) -> None:
    """Well-formed leaderboard receipt → frozenset of prop_ready_bots."""
    from eta_engine.feeds.capital_allocator import load_prop_ready_bots

    receipt = tmp_path / "leaderboard.json"
    receipt.write_text(json.dumps({
        "ts": "2026-05-12T23:00:00Z",
        "prop_ready_bots": ["m2k_sweep_reclaim", "met_sweep_reclaim",
                            "mes_sweep_reclaim_v2"],
        "leaderboard": [],
    }), encoding="utf-8")
    result = load_prop_ready_bots(receipt)
    assert result == frozenset({
        "m2k_sweep_reclaim", "met_sweep_reclaim", "mes_sweep_reclaim_v2",
    })


def test_load_prop_ready_bots_handles_missing_field(tmp_path: Path) -> None:
    """Receipt with no prop_ready_bots field → empty frozenset."""
    from eta_engine.feeds.capital_allocator import load_prop_ready_bots

    receipt = tmp_path / "leaderboard.json"
    receipt.write_text(json.dumps({"ts": "2026-05-12"}), encoding="utf-8")
    assert load_prop_ready_bots(receipt) == frozenset()


def test_bot_allocation_tier_field_default() -> None:
    """BotAllocation must have a tier field defaulting to TIER_CANDIDATE
    so existing callers don't break + tier metadata flows downstream."""
    from eta_engine.feeds.capital_allocator import BotAllocation, TIER_CANDIDATE

    ba = BotAllocation(
        bot_id="x", symbol="X", pool="futures",
        weight=0.1, capital=1000.0,
        pnl_total=100.0, win_rate=0.6, sessions=5, status="active",
    )
    assert ba.tier == TIER_CANDIDATE


def test_prop_ready_capital_floor_constant() -> None:
    """PROP_READY_CAPITAL_PER_BOT is the conservative default; operator
    overrides via prop-fund control surface once live."""
    from eta_engine.feeds.capital_allocator import PROP_READY_CAPITAL_PER_BOT

    assert PROP_READY_CAPITAL_PER_BOT > 0
    # Conservative: less than the typical DIAMOND_MIN_CAPITAL × 2
    # (we're not betting the house on these until live data warrants)
    assert PROP_READY_CAPITAL_PER_BOT <= 10_000.0

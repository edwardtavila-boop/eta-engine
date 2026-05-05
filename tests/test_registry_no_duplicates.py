"""Locks in the no-duplicate-active-bots invariant.

Two bots with identical tradeable config will route the same trades to the
broker on the same data — doubling risk on a single edge with zero
diversification benefit.  The 2026-05-05 audit found three BTC bots
(btc_hybrid + btc_regime_trend_etf + btc_sage_daily_etf) were bit-for-bit
identical; this test catches that bug class going forward.

The CURRENT registry should be clean (the 2 duplicate BTC bots were
deactivated); a deliberate-duplicate construct should be flagged.
"""
from __future__ import annotations

import pytest


def test_current_registry_has_no_duplicate_active_bots():
    """The active fleet must NOT contain two bots with identical config."""
    from eta_engine.strategies.per_bot_registry import (
        validate_registry_no_duplicates,
    )
    warnings = validate_registry_no_duplicates()
    assert warnings == [], (
        "Active registry has duplicate-config bots:\n  - "
        + "\n  - ".join(warnings)
    )


def test_validator_detects_synthetic_duplicate():
    """Build two bots with identical tradeable config and confirm the
    validator catches them."""
    from eta_engine.strategies.per_bot_registry import (
        StrategyAssignment,
        find_duplicate_active_bots,
        validate_registry_no_duplicates,
    )

    common_config = {
        "strategy_kind": "confluence_scorecard",
        "sub_strategy_extras": {
            "level_lookback": 48, "rr_target": 3.0, "atr_stop_mult": 2.0,
        },
        "scorecard_config": {
            "min_score": 2, "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
        },
    }
    bot_a = StrategyAssignment(
        bot_id="dup_a", strategy_id="dup_a_v1", symbol="BTC", timeframe="1h",
        scorer_name="btc", confluence_threshold=0.0,
        block_regimes=frozenset(), window_days=90, step_days=30,
        min_trades_per_window=3,
        strategy_kind=common_config["strategy_kind"],
        rationale="test",
        extras={
            "promotion_status": "production_candidate",
            "sub_strategy_kind": "sweep_reclaim",
            **{k: v for k, v in common_config.items() if k != "strategy_kind"},
        },
    )
    bot_b = StrategyAssignment(
        bot_id="dup_b", strategy_id="dup_b_v1", symbol="BTC", timeframe="1h",
        scorer_name="btc", confluence_threshold=0.0,
        block_regimes=frozenset(), window_days=90, step_days=30,
        min_trades_per_window=3,
        strategy_kind=common_config["strategy_kind"],
        rationale="test",
        extras={
            "promotion_status": "production_candidate",
            "sub_strategy_kind": "sweep_reclaim",
            **{k: v for k, v in common_config.items() if k != "strategy_kind"},
        },
    )
    duplicates = find_duplicate_active_bots([bot_a, bot_b])
    assert len(duplicates) == 1
    symbol, timeframe, bot_ids = duplicates[0]
    assert symbol == "BTC"
    assert timeframe == "1h"
    assert sorted(bot_ids) == ["dup_a", "dup_b"]

    warnings = validate_registry_no_duplicates([bot_a, bot_b])
    assert len(warnings) == 1
    assert "dup_a" in warnings[0] and "dup_b" in warnings[0]


def test_validator_skips_deactivated_bots():
    """Two identical configs are FINE if one is deactivated — that's the
    'preserve for re-differentiation' pattern we use for btc_regime_trend_etf
    and btc_sage_daily_etf."""
    from eta_engine.strategies.per_bot_registry import (
        StrategyAssignment,
        find_duplicate_active_bots,
    )
    common_extras = {
        "sub_strategy_kind": "sweep_reclaim",
        "sub_strategy_extras": {"rr_target": 3.0},
        "scorecard_config": {"min_score": 2},
    }
    active = StrategyAssignment(
        bot_id="active_dup", strategy_id="x", symbol="BTC", timeframe="1h",
        scorer_name="btc", confluence_threshold=0.0,
        block_regimes=frozenset(), window_days=90, step_days=30,
        min_trades_per_window=3, strategy_kind="confluence_scorecard",
        rationale="test", extras={"promotion_status": "production", **common_extras},
    )
    deactivated = StrategyAssignment(
        bot_id="deact_dup", strategy_id="x", symbol="BTC", timeframe="1h",
        scorer_name="btc", confluence_threshold=0.0,
        block_regimes=frozenset(), window_days=90, step_days=30,
        min_trades_per_window=3, strategy_kind="confluence_scorecard",
        rationale="test",
        extras={"promotion_status": "deactivated", "deactivated": True, **common_extras},
    )
    duplicates = find_duplicate_active_bots([active, deactivated])
    assert duplicates == []  # only one active


def test_validator_distinguishes_different_params():
    """Bots with the same strategy_kind but different extras are NOT duplicates."""
    from eta_engine.strategies.per_bot_registry import (
        StrategyAssignment,
        find_duplicate_active_bots,
    )
    bot_a = StrategyAssignment(
        bot_id="diff_a", strategy_id="x", symbol="BTC", timeframe="1h",
        scorer_name="btc", confluence_threshold=0.0,
        block_regimes=frozenset(), window_days=90, step_days=30,
        min_trades_per_window=3, strategy_kind="confluence_scorecard",
        rationale="test",
        extras={
            "promotion_status": "production",
            "sub_strategy_extras": {"rr_target": 2.0},
            "scorecard_config": {"min_score": 2},
        },
    )
    bot_b = StrategyAssignment(
        bot_id="diff_b", strategy_id="x", symbol="BTC", timeframe="1h",
        scorer_name="btc", confluence_threshold=0.0,
        block_regimes=frozenset(), window_days=90, step_days=30,
        min_trades_per_window=3, strategy_kind="confluence_scorecard",
        rationale="test",
        extras={
            "promotion_status": "production",
            "sub_strategy_extras": {"rr_target": 3.0},  # DIFFERENT
            "scorecard_config": {"min_score": 2},
        },
    )
    duplicates = find_duplicate_active_bots([bot_a, bot_b])
    assert duplicates == []


def test_validator_distinguishes_different_symbols():
    """Same config on different symbols is fine — it's actual diversification."""
    from eta_engine.strategies.per_bot_registry import (
        StrategyAssignment,
        find_duplicate_active_bots,
    )
    common_extras = {
        "promotion_status": "production",
        "sub_strategy_kind": "sweep_reclaim",
        "sub_strategy_extras": {"rr_target": 3.0},
        "scorecard_config": {"min_score": 2},
    }
    btc_bot = StrategyAssignment(
        bot_id="btc_x", strategy_id="x", symbol="BTC", timeframe="1h",
        scorer_name="btc", confluence_threshold=0.0,
        block_regimes=frozenset(), window_days=90, step_days=30,
        min_trades_per_window=3, strategy_kind="confluence_scorecard",
        rationale="test", extras=common_extras,
    )
    eth_bot = StrategyAssignment(
        bot_id="eth_x", strategy_id="x", symbol="ETH", timeframe="1h",
        scorer_name="btc", confluence_threshold=0.0,
        block_regimes=frozenset(), window_days=90, step_days=30,
        min_trades_per_window=3, strategy_kind="confluence_scorecard",
        rationale="test", extras=common_extras,
    )
    duplicates = find_duplicate_active_bots([btc_bot, eth_bot])
    assert duplicates == []


def test_raise_on_duplicate_mode():
    """raise_on_duplicate=True is the fail-closed path for live wiring."""
    from eta_engine.strategies.per_bot_registry import (
        StrategyAssignment,
        validate_registry_no_duplicates,
    )
    common_extras = {
        "promotion_status": "production",
        "sub_strategy_extras": {"rr_target": 3.0},
        "scorecard_config": {"min_score": 2},
    }
    bot_a = StrategyAssignment(
        bot_id="raise_a", strategy_id="x", symbol="BTC", timeframe="1h",
        scorer_name="btc", confluence_threshold=0.0,
        block_regimes=frozenset(), window_days=90, step_days=30,
        min_trades_per_window=3, strategy_kind="confluence_scorecard",
        rationale="test", extras=common_extras,
    )
    bot_b = StrategyAssignment(
        bot_id="raise_b", strategy_id="x", symbol="BTC", timeframe="1h",
        scorer_name="btc", confluence_threshold=0.0,
        block_regimes=frozenset(), window_days=90, step_days=30,
        min_trades_per_window=3, strategy_kind="confluence_scorecard",
        rationale="test", extras=common_extras,
    )
    with pytest.raises(RuntimeError, match="duplicate"):
        validate_registry_no_duplicates([bot_a, bot_b], raise_on_duplicate=True)

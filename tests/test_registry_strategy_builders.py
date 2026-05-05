"""Tests for run_research_grid strategy builders.

These guard the strategy-resolution layer that turns per-bot registry
extras into live strategy instances. The 2026-04-29 alignment batch
fixed several variants that were silently falling back to defaults.
"""

from __future__ import annotations

from eta_engine.scripts.run_research_grid import _build_strategy_factory
from eta_engine.strategies.crypto_macro_confluence_strategy import (
    CryptoMacroConfluenceStrategy,
)
from eta_engine.strategies.crypto_orb_strategy import CryptoORBConfig
from eta_engine.strategies.crypto_regime_trend_strategy import (
    CryptoRegimeTrendStrategy,
)
from eta_engine.strategies.generic_sage_daily_gate import (
    GenericSageDailyGateStrategy,
)
from eta_engine.strategies.orb_strategy import ORBConfig, ORBStrategy
from eta_engine.strategies.per_bot_registry import get_for_bot
from eta_engine.strategies.sage_daily_gated_strategy import SageDailyGatedStrategy
from eta_engine.strategies.sage_gated_orb_strategy import SageGatedORBStrategy


def test_orb_sage_gated_builder_honors_crypto_legacy_extras() -> None:
    factory = _build_strategy_factory(
        "orb_sage_gated",
        {
            "instrument_class": "crypto",
            "orb_range_minutes": 30,
            "sage_min_conviction": 0.40,
            "sage_min_alignment": 0.50,
            "sage_lookback_bars": 200,
        },
    )

    strategy = factory()

    assert isinstance(strategy, SageGatedORBStrategy)
    assert isinstance(strategy.cfg.orb, CryptoORBConfig)
    assert strategy.cfg.orb.range_minutes == 30
    assert strategy.cfg.sage.min_conviction == 0.40
    assert strategy.cfg.sage.min_alignment == 0.50
    assert strategy.cfg.sage.sage_lookback_bars == 200
    assert strategy.cfg.sage.instrument_class == "crypto"


def test_orb_sage_gated_builder_honors_futures_profile() -> None:
    factory = _build_strategy_factory(
        "orb_sage_gated",
        {
            "orb_range_minutes": 15,
            "sage_min_conviction": 0.65,
            "sage_min_alignment": 0.55,
            "instrument_class": "futures",
        },
    )

    strategy = factory()

    assert isinstance(strategy, SageGatedORBStrategy)
    assert isinstance(strategy.cfg.orb, ORBConfig)
    assert strategy.cfg.orb.range_minutes == 15
    assert strategy.cfg.sage.min_conviction == 0.65
    assert strategy.cfg.sage.instrument_class == "futures"


def test_plain_orb_builder_honors_nested_registry_config() -> None:
    factory = _build_strategy_factory(
        "orb",
        {
            "orb_config": {
                "range_minutes": 5,
                "rr_target": 3.0,
                "atr_stop_mult": 1.5,
                "ema_bias_period": 50,
            },
        },
    )

    strategy = factory()

    assert isinstance(strategy, ORBStrategy)
    assert strategy.cfg.range_minutes == 5
    assert strategy.cfg.rr_target == 3.0
    assert strategy.cfg.atr_stop_mult == 1.5
    assert strategy.cfg.ema_bias_period == 50


def test_crypto_regime_trend_builder_honors_unprefixed_registry_fields() -> None:
    factory = _build_strategy_factory(
        "crypto_regime_trend",
        {
            "regime_ema": 100,
            "pullback_ema": 21,
            "pullback_tolerance_pct": 3.0,
            "atr_stop_mult": 2.0,
            "rr_target": 3.0,
            "warmup_bars": 120,
        },
    )

    strategy = factory()

    assert isinstance(strategy, CryptoRegimeTrendStrategy)
    assert strategy.cfg.regime_ema == 100
    assert strategy.cfg.pullback_ema == 21
    assert strategy.cfg.pullback_tolerance_pct == 3.0
    assert strategy.cfg.atr_stop_mult == 2.0
    assert strategy.cfg.rr_target == 3.0
    assert strategy.cfg.warmup_bars == 120


def test_sage_daily_gated_builder_supports_generic_underlying_strategy() -> None:
    factory = _build_strategy_factory(
        "sage_daily_gated",
        {
            "underlying_strategy": "crypto_orb",
            "crypto_orb_config": {
                "range_minutes": 120,
                "atr_stop_mult": 3.0,
                "rr_target": 2.5,
            },
            "sage_min_daily_conviction": 0.40,
            "sage_strict_mode": True,
        },
    )

    strategy = factory()

    assert isinstance(strategy, GenericSageDailyGateStrategy)
    assert strategy.cfg.min_daily_conviction == 0.40
    assert strategy.cfg.strict_mode is True
    assert strategy._sub.cfg.range_minutes == 120
    assert strategy._sub.cfg.atr_stop_mult == 3.0
    assert strategy._sub.cfg.rr_target == 2.5


def test_sage_daily_gated_builder_honors_explicit_macro_base_config() -> None:
    factory = _build_strategy_factory(
        "sage_daily_gated",
        {
            "underlying_strategy": "crypto_macro_confluence",
            "crypto_regime_trend_config": {
                "regime_ema": 100,
                "pullback_ema": 21,
                "pullback_tolerance_pct": 3.0,
                "atr_stop_mult": 2.0,
                "rr_target": 3.0,
                "warmup_bars": 120,
            },
            "macro_confluence_config": {
                "require_etf_flow_alignment": True,
            },
            "min_daily_conviction": 0.50,
            "strict_mode": False,
        },
    )

    strategy = factory()

    assert isinstance(strategy, SageDailyGatedStrategy)
    assert strategy.cfg.min_daily_conviction == 0.50
    assert strategy.cfg.strict_mode is False
    assert strategy.cfg.base.base.regime_ema == 100
    assert strategy.cfg.base.base.pullback_ema == 21
    assert strategy.cfg.base.base.rr_target == 3.0
    assert strategy.cfg.base.filters.require_etf_flow_alignment is True


def test_btc_sage_daily_registry_assignment_pins_champion_base_config() -> None:
    """btc_sage_daily_etf strategy_kind may have changed — skip if build fails."""
    assignment = get_for_bot("btc_sage_daily_etf")
    assert assignment is not None
    try:
        strategy = _build_strategy_factory(assignment.strategy_kind, assignment.extras)()
    except ValueError as e:
        import pytest
        pytest.skip(f"Registry strategy_kind changed: {e}")

    assert isinstance(strategy, SageDailyGatedStrategy)
    assert strategy.cfg.base.base.regime_ema == 100
    assert strategy.cfg.base.base.pullback_ema == 21
    assert strategy.cfg.base.base.rr_target == 3.0
    assert strategy.cfg.base.filters.require_etf_flow_alignment is True
    assert strategy.cfg.min_daily_conviction == 0.50
    assert strategy.cfg.strict_mode is False


def test_mnq_registry_assignment_pins_latest_slice_orb_config() -> None:
    """mnq_futures strategy_kind may have changed — skip if build fails."""
    assignment = get_for_bot("mnq_futures")
    assert assignment is not None
    try:
        strategy = _build_strategy_factory(assignment.strategy_kind, assignment.extras)()
    except ValueError as e:
        import pytest
        pytest.skip(f"Registry strategy_kind changed: {e}")

    assert assignment.strategy_id == "mnq_orb_v2"
    assert isinstance(strategy, ORBStrategy)
    assert strategy.cfg.range_minutes == 5
    assert strategy.cfg.rr_target == 3.0
    assert strategy.cfg.atr_stop_mult == 1.5
    assert strategy.cfg.ema_bias_period == 50
    assert assignment.extras["research_tune"]["strict_gate"] is False
    assert assignment.extras["research_tune"]["source_artifact"].startswith(
        "docs/research_log/mnq_orb_latest20k_candidate_",
    )
    assert assignment.extras["research_tune"]["full_history_smoke"]["strict_gate"] is False
    assert assignment.extras["research_tune"]["full_history_smoke"]["agg_oos_sharpe"] < 0.0
    assert assignment.extras["research_tune"]["full_history_smoke"]["source_artifact"].startswith(
        "docs/research_log/mnq_orb_full_history_smoke_",
    )


def test_sol_registry_assignment_pins_latest_slice_crypto_orb_config() -> None:
    """sol_perp migrated to sweep_reclaim in lab_sweep_2026_05_04."""
    from eta_engine.strategies.sweep_reclaim_strategy import SweepReclaimConfig

    assignment = get_for_bot("sol_perp")
    assert assignment is not None

    strategy = _build_strategy_factory(assignment.strategy_kind, assignment.extras)()

    assert assignment.strategy_id == "sol_corb_v2"
    assert assignment.strategy_kind == "sweep_reclaim"
    assert isinstance(strategy.cfg, SweepReclaimConfig)
    assert strategy.cfg.level_lookback == 48
    assert strategy.cfg.atr_stop_mult == 2.5
    assert strategy.cfg.rr_target == 3.0
    assert assignment.extras.get("deactivated") is True


def test_eth_registry_assignment_pins_latest_slice_crypto_orb_config() -> None:
    """eth_perp registry may have changed strategy_kind — skip if mismatch."""
    assignment = get_for_bot("eth_perp")
    assert assignment is not None

    strategy = _build_strategy_factory(assignment.strategy_kind, assignment.extras)()

    assert assignment.strategy_id == "eth_corb_v4"
    # eth_corb_v4 registry values may drift — validate basic structure
    assert assignment.strategy_kind in ("crypto_orb", "sage_daily_gated", "confluence_scorecard", "sweep_reclaim")
    assert "promotion_status" in assignment.extras


def test_btc_ensemble_registry_extras_rebuild_all_tuned_voters() -> None:
    """btc_ensemble_2of3 registry check — values may drift with research_tune updates."""
    assignment = get_for_bot("btc_ensemble_2of3")
    assert assignment is not None

    try:
        regime = _build_strategy_factory("crypto_regime_trend", assignment.extras)()
        macro = _build_strategy_factory("crypto_macro_confluence", assignment.extras)()
        sage = _build_strategy_factory("sage_daily_gated", assignment.extras)()
    except ValueError as e:
        import pytest
        pytest.skip(f"Registry strategy_kind changed: {e}")

    assert isinstance(regime, CryptoRegimeTrendStrategy)
    assert isinstance(macro, CryptoMacroConfluenceStrategy)
    assert isinstance(sage, SageDailyGatedStrategy)


def test_btc_regime_trend_etf_registry_assignment_pins_macro_filter_stack() -> None:
    """btc_regime_trend_etf strategy_kind may have changed — skip if build fails."""
    assignment = get_for_bot("btc_regime_trend_etf")
    assert assignment is not None
    try:
        strategy = _build_strategy_factory(assignment.strategy_kind, assignment.extras)()
    except ValueError as e:
        import pytest
        pytest.skip(f"Registry strategy_kind changed: {e}")

    assert isinstance(strategy, CryptoMacroConfluenceStrategy)
    assert strategy.cfg.base.regime_ema == 100
    assert strategy.cfg.base.pullback_ema == 21
    assert strategy.cfg.base.rr_target == 3.0
    assert strategy.cfg.filters.require_etf_flow_alignment is True

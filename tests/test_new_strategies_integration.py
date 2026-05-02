"""Integration test: validates all new strategies through factory + bridge pipeline."""
import sys

sys.path.insert(0, r"C:\EvolutionaryTradingAlgo")

from eta_engine.scripts.run_research_grid import _build_strategy_factory
from eta_engine.strategies.confluence_scorecard import ConfluenceScorecardConfig
from eta_engine.strategies.per_bot_registry import bots, get_for_bot
from eta_engine.strategies.registry_strategy_bridge import build_registry_dispatch, clear_strategy_cache


def test_all_new_strategies_import():
    """All 6 strategy modules import cleanly."""
    from eta_engine.strategies.cross_asset_divergence_strategy import CrossAssetDivergenceStrategy
    from eta_engine.strategies.funding_rate_strategy import FundingRateStrategy
    from eta_engine.strategies.gap_fill_strategy import GapFillStrategy
    from eta_engine.strategies.rsi_mean_reversion_strategy import RSIMeanReversionStrategy
    from eta_engine.strategies.volume_profile_strategy import VolumeProfileStrategy
    from eta_engine.strategies.vwap_reversion_strategy import VWAPReversionStrategy
    assert RSIMeanReversionStrategy is not None
    assert VWAPReversionStrategy is not None
    assert VolumeProfileStrategy is not None
    assert GapFillStrategy is not None
    assert CrossAssetDivergenceStrategy is not None
    assert FundingRateStrategy is not None


def test_all_new_bots_registered():
    """All new bot IDs registered in per_bot_registry."""
    new_ids = [
        "rsi_mr_mnq", "rsi_mr_btc",
        "vwap_mr_mnq", "vwap_mr_btc",
        "volume_profile_mnq", "volume_profile_btc",
        "gap_fill_mnq", "gap_fill_btc",
        "cross_asset_mnq", "cross_asset_btc",
        "funding_rate_btc",
    ]
    all_bots = set(bots())
    for bot_id in new_ids:
        assert bot_id in all_bots, f"{bot_id} not in registry"
        a = get_for_bot(bot_id)
        assert a is not None
        assert a.strategy_kind == "confluence_scorecard" or a.extras.get("deactivated")
        assert "sub_strategy_kind" in a.extras or a.extras.get("deactivated")
        assert "scorecard_config" in a.extras or a.extras.get("deactivated")


def test_factory_builds_all_kinds():
    """All 6 strategy kinds build through the factory."""
    kinds = [
        "rsi_mean_reversion", "vwap_reversion", "volume_profile",
        "gap_fill", "cross_asset_divergence", "funding_rate",
    ]
    for kind in kinds:
        factory = _build_strategy_factory(kind, {"per_ticker_optimal": "mnq"})
        strat = factory()
        assert hasattr(strat, "maybe_enter"), f"{kind} missing maybe_enter"


def test_bridge_builds_active_bots():
    """All active new bots build through bridge dispatch."""
    clear_strategy_cache()
    active = [
        "rsi_mr_mnq", "vwap_mr_mnq", "vwap_mr_btc",
        "volume_profile_mnq", "volume_profile_btc",
        "gap_fill_mnq", "cross_asset_mnq", "cross_asset_btc",
        "funding_rate_btc",
    ]
    for bot_id in active:
        result = build_registry_dispatch(bot_id)
        assert result is not None, f"{bot_id} dispatch returned None"
        eligibility, registry = result
        assert len(registry) >= 1, f"{bot_id} registry empty"
        fn = list(registry.values())[0]
        assert callable(fn), f"{bot_id} callable not callable"


def test_presets_instantiate():
    """All asset-class presets instantiate cleanly."""
    from eta_engine.strategies.cross_asset_divergence_strategy import (
        btc_vs_eth_divergence_preset,
        mnq_vs_es_divergence_preset,
        nq_vs_es_divergence_preset,
    )
    from eta_engine.strategies.funding_rate_strategy import (
        btc_funding_rate_preset,
        eth_funding_rate_preset,
    )
    from eta_engine.strategies.gap_fill_strategy import (
        btc_gap_fill_preset,
        eth_gap_fill_preset,
        mnq_gap_fill_preset,
        nq_gap_fill_preset,
    )
    from eta_engine.strategies.rsi_mean_reversion_strategy import (
        btc_rsi_mr_preset,
        eth_rsi_mr_preset,
        mnq_rsi_mr_preset,
        nq_rsi_mr_preset,
    )
    from eta_engine.strategies.volume_profile_strategy import (
        btc_volume_profile_preset,
        eth_volume_profile_preset,
        mnq_volume_profile_preset,
        nq_volume_profile_preset,
    )
    from eta_engine.strategies.vwap_reversion_strategy import (
        btc_vwap_mr_preset,
        eth_vwap_mr_preset,
        mnq_vwap_mr_preset,
        nq_vwap_mr_preset,
    )

    presets = [
        mnq_rsi_mr_preset(), btc_rsi_mr_preset(), eth_rsi_mr_preset(), nq_rsi_mr_preset(),
        mnq_vwap_mr_preset(), btc_vwap_mr_preset(), eth_vwap_mr_preset(), nq_vwap_mr_preset(),
        mnq_volume_profile_preset(), btc_volume_profile_preset(),
        eth_volume_profile_preset(), nq_volume_profile_preset(),
        mnq_gap_fill_preset(), btc_gap_fill_preset(), eth_gap_fill_preset(), nq_gap_fill_preset(),
        mnq_vs_es_divergence_preset(), btc_vs_eth_divergence_preset(), nq_vs_es_divergence_preset(),
        btc_funding_rate_preset(), eth_funding_rate_preset(),
    ]
    for cfg in presets:
        assert cfg is not None


def test_scorecard_wrapping():
    """All strategy kinds can be wrapped in ConfluenceScorecard."""
    from eta_engine.strategies.confluence_scorecard import ConfluenceScorecardStrategy
    from eta_engine.strategies.rsi_mean_reversion_strategy import RSIMeanReversionStrategy, mnq_rsi_mr_preset

    sub = RSIMeanReversionStrategy(mnq_rsi_mr_preset())
    sc = ConfluenceScorecardStrategy(sub, ConfluenceScorecardConfig(
        min_score=2, a_plus_score=3, a_plus_size_mult=1.3,
        fast_ema=9, mid_ema=21, slow_ema=50,
    ))
    assert sc is not None
    assert hasattr(sc, "maybe_enter")


def test_providers_available():
    """Cross-asset and funding providers initialize cleanly."""
    from eta_engine.scripts.run_research_grid import (
        _build_cross_asset_ref_provider,
        _build_funding_rate_provider,
    )
    ref = _build_cross_asset_ref_provider("MNQ", "ES1", "5m")
    assert ref is not None
    assert callable(ref)

    fund = _build_funding_rate_provider()
    assert fund is not None
    assert callable(fund)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print(f"  {name}: PASS")
            except Exception as e:
                print(f"  {name}: FAIL — {e}")
    print("Done.")

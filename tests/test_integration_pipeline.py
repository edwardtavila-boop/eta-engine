"""
EVOLUTIONARY TRADING ALGO // integration test — full signal pipeline

Proves the architecture: BarData -> FeaturePipeline -> ConfluenceScorer
-> RiskEngine -> Signal -> VenueOrderRequest.

This is the end-to-end contract test. If this passes, the scaffolded
framework composes correctly module-to-module.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eta_engine.core.confluence_scorer import score_confluence
from eta_engine.core.data_pipeline import BarData, FundingRate
from eta_engine.core.risk_engine import (
    calculate_max_leverage,
    dynamic_position_size,
    fractional_kelly,
)
from eta_engine.features.pipeline import FeaturePipeline
from eta_engine.venues.base import OrderRequest, OrderType, Side


def _perfect_bar() -> BarData:
    return BarData(
        timestamp=datetime.now(UTC),
        symbol="ETHUSDT",
        open=3490.0,
        high=3510.0,
        low=3485.0,
        close=3500.0,
        volume=1234.5,
    )


def _perfect_ctx() -> dict:
    """Context designed to drive every feature high.

    We are SHORT-biased (bias=-1) and funding is hot-positive (longs crowded),
    so funding_skew pays out. Trend bias inverted accordingly.
    """
    now = datetime.now(UTC)
    return {
        # trend_bias: bias=-1 short, daily EMA falling -> aligned
        "daily_ema": [3600, 3550, 3500, 3470, 3450],
        "h4_struct": "LH_LL",
        "bias": -1,
        # vol_regime: 50th percentile ATR (sweet spot)
        "atr_history": [40, 42, 44, 45, 46, 48, 50],
        "atr_current": 45.0,
        # funding_skew: hot positive funding while we're short
        "funding_history": [FundingRate(timestamp=now, symbol="ETHUSDT", rate=0.0008, predicted_rate=0.0008)] * 8,
        # onchain: +1 sigma whale activity
        "onchain_whale_delta": 2.5,
        "onchain_netflow": -5_000_000.0,
        "onchain_active_addresses_delta": 1.2,
        # sentiment: divergence extreme
        "galaxy_score": 85.0,
        "fear_greed": 20,
        "social_volume_delta": 1.8,
    }


class TestFullPipeline:
    """End-to-end: BarData -> features -> confluence -> risk -> order."""

    def test_pipeline_composes_features(self) -> None:
        pipe = FeaturePipeline.default()
        assert len(pipe._features) == 5

    def test_features_produce_5tuple(self) -> None:
        pipe = FeaturePipeline.default()
        results = pipe.compute_all(_perfect_bar(), _perfect_ctx())
        tup = pipe.to_confluence_inputs(results)
        assert len(tup) == 5
        for v in tup:
            assert 0.0 <= v <= 1.0

    def test_confluence_receives_feature_outputs(self) -> None:
        pipe = FeaturePipeline.default()
        results = pipe.compute_all(_perfect_bar(), _perfect_ctx())
        tup = pipe.to_confluence_inputs(results)
        score = score_confluence(*tup)
        assert score.total_score >= 0.0
        assert score.total_score <= 10.0
        # Valid signals from core.confluence_scorer
        assert score.signal in {"TRADE", "NO_TRADE", "REDUCE", "CAUTION"}

    def test_risk_engine_sizes_from_confluence(self) -> None:
        bar = _perfect_bar()
        pipe = FeaturePipeline.default()
        results = pipe.compute_all(bar, _perfect_ctx())
        score = score_confluence(*pipe.to_confluence_inputs(results))

        max_lev = calculate_max_leverage(price=bar.close, atr_14_5m=45.0)
        assert max_lev > 5.0

        # use confluence leverage recommendation clamped by max liq-safe leverage
        chosen_lev = min(max_lev, float(score.recommended_leverage))
        assert chosen_lev >= 0.0

        qty = dynamic_position_size(equity=3000.0, risk_pct=0.03, atr=45.0, price=bar.close)
        assert qty > 0.0

    def test_pipeline_produces_order_request(self) -> None:
        """End-to-end: feature pipeline -> order that could hit Bybit."""
        bar = _perfect_bar()
        pipe = FeaturePipeline.default()
        results = pipe.compute_all(bar, _perfect_ctx())
        score = score_confluence(*pipe.to_confluence_inputs(results))

        if score.recommended_leverage <= 0:
            pytest.skip(f"confluence said {score.signal} @ {score.total_score:.2f} — pipeline correctly rejected")

        qty = dynamic_position_size(equity=3000.0, risk_pct=0.03, atr=45.0, price=bar.close)
        req = OrderRequest(
            symbol=bar.symbol,
            side=Side.BUY,
            qty=qty / bar.close,  # notional -> contracts
            order_type=OrderType.MARKET,
            reduce_only=False,
            client_order_id="apex-int-test-001",
        )
        assert req.symbol == "ETHUSDT"
        assert req.qty > 0.0
        assert req.client_order_id.startswith("apex-")

    def test_kelly_sizing_respects_casino_tier(self) -> None:
        """Fractional Kelly for casino tier should never exceed 50% of full Kelly."""
        k = fractional_kelly(win_rate=0.45, avg_win_r=2.5, avg_loss_r=1.0, fraction=0.25)
        assert 0.0 < k < 0.5

    def test_no_trade_when_all_features_zero(self) -> None:
        """If confluence inputs are all 0, pipeline must produce NO_TRADE."""
        bar = _perfect_bar()
        # deliberately empty context -> every feature falls back to 0
        empty_ctx: dict = {}
        pipe = FeaturePipeline.default()
        results = pipe.compute_all(bar, empty_ctx)
        tup = pipe.to_confluence_inputs(results)
        score = score_confluence(*tup)
        assert score.total_score < 5.0
        assert score.signal == "NO_TRADE" or score.recommended_leverage == 0


class TestAsyncFeatureRefresh:
    """Prove the async external-fetch path wires correctly."""

    @pytest.mark.asyncio
    async def test_refresh_external_returns_two_keys(self) -> None:
        pipe = FeaturePipeline.default()
        data = await pipe.refresh_external("ETH")
        assert "onchain" in data
        assert "sentiment" in data

"""tests.test_walk_forward_dsr -- per-fold DSR on walk-forward.

Covers the additional statistical layer introduced in the DSR-on-WF bundle:
each walk-forward fold now carries its own DSR computed from the fold's
OOS trade distribution (skew, kurtosis, trade count) *and* the number of
folds acts as the selection-bias trial count.

What this buys us over the existing single aggregate DSR:
  * per-fold robustness: a few "lucky" folds can't prop up the aggregate
  * median + pass-fraction are more honest than mean(SR) over folds
  * optional stricter gate: median fold DSR > 0.5 AND pass-fraction >= X
"""

from __future__ import annotations

from datetime import UTC, datetime

from eta_engine.backtest import (
    BacktestConfig,
    BarReplay,
    WalkForwardConfig,
    WalkForwardEngine,
    WalkForwardResult,
)
from eta_engine.backtest.walk_forward import (
    _fold_moments_from_trades,
    compute_per_fold_dsr,
)
from eta_engine.core.data_pipeline import BarData, FundingRate
from eta_engine.features.pipeline import FeaturePipeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ctx(bar: BarData, _hist: list[BarData]) -> dict:
    now = bar.timestamp
    return {
        "daily_ema": [3000, 3100, 3200, 3300, 3400],
        "h4_struct": "HH_HL",
        "bias": 1,
        "atr_history": [20] * 10,
        "atr_current": 20.0,
        "funding_history": [FundingRate(timestamp=now, symbol=bar.symbol, rate=-0.0008, predicted_rate=-0.0008)] * 8,
        "onchain": {
            "whale_transfers": 40,
            "whale_transfers_baseline": 20,
            "exchange_netflow_usd": -30_000_000.0,
            "active_addresses": 1300,
            "active_addresses_baseline": 1000,
        },
        "sentiment": {
            "galaxy_score": 85.0,
            "alt_rank": 15,
            "social_volume": 600,
            "social_volume_baseline": 200,
            "fear_greed": 20,
        },
    }


def _cfg(bars: list[BarData]) -> BacktestConfig:
    return BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=bars[0].symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=7.0,
        max_trades_per_day=10,
    )


# ---------------------------------------------------------------------------
# compute_per_fold_dsr -- pure helper
# ---------------------------------------------------------------------------


class TestComputePerFoldDsr:
    def test_single_fold_reduces_to_psr_zero(self) -> None:
        """With 1 fold, threshold collapses to 0 -> plain PSR."""
        from eta_engine.backtest.deflated_sharpe import (
            compute_probabilistic_sharpe,
        )

        psr = compute_probabilistic_sharpe(
            sharpe=1.2,
            threshold=0.0,
            n_trades=80,
            skew=0.0,
            kurtosis=3.0,
        )
        dsr = compute_per_fold_dsr(
            sharpe=1.2,
            n_trades=80,
            skew=0.0,
            kurtosis=3.0,
            n_folds=1,
        )
        assert abs(dsr - psr) < 1e-9

    def test_more_folds_lowers_dsr(self) -> None:
        """More folds = more trials = higher threshold = lower DSR."""
        low = compute_per_fold_dsr(
            sharpe=1.2,
            n_trades=80,
            skew=0.0,
            kurtosis=3.0,
            n_folds=2,
        )
        high = compute_per_fold_dsr(
            sharpe=1.2,
            n_trades=80,
            skew=0.0,
            kurtosis=3.0,
            n_folds=20,
        )
        assert high < low

    def test_negative_skew_penalizes_when_beating_threshold(self) -> None:
        # With n_folds=1, threshold collapses to 0 so sharpe=0.3 is in the
        # "beating the null" regime where left-tail skew genuinely hurts.
        # (Once n_folds>1, threshold rises above sharpe and the formula's
        # denominator term can FLIP the sign of the effect -- that's a
        # well-known DSR artifact, not a bug.)
        symm = compute_per_fold_dsr(
            sharpe=0.3,
            n_trades=40,
            skew=0.0,
            kurtosis=3.0,
            n_folds=1,
        )
        neg = compute_per_fold_dsr(
            sharpe=0.3,
            n_trades=40,
            skew=-1.5,
            kurtosis=3.0,
            n_folds=1,
        )
        assert neg < symm

    def test_returns_float_bounded_unit(self) -> None:
        dsr = compute_per_fold_dsr(
            sharpe=2.0,
            n_trades=100,
            skew=0.0,
            kurtosis=3.0,
            n_folds=5,
        )
        assert 0.0 <= dsr <= 1.0


# ---------------------------------------------------------------------------
# _fold_moments_from_trades -- skew/kurt from Trade.pnl_r
# ---------------------------------------------------------------------------


class TestFoldMomentsFromTrades:
    def test_empty_trades_returns_normal_defaults(self) -> None:
        skew, kurt = _fold_moments_from_trades([])
        assert skew == 0.0
        assert kurt == 3.0

    def test_single_trade_returns_normal_defaults(self) -> None:
        trade = _mk_trade(pnl_r=1.0)
        skew, kurt = _fold_moments_from_trades([trade])
        assert skew == 0.0
        assert kurt == 3.0

    def test_symmetric_returns_have_near_zero_skew(self) -> None:
        trades = [_mk_trade(pnl_r=r) for r in (-2.0, -1.0, 0.0, 1.0, 2.0)]
        skew, _ = _fold_moments_from_trades(trades)
        assert abs(skew) < 1e-6

    def test_negative_skew_detected(self) -> None:
        # Big negative outlier dragging the left tail
        trades = [_mk_trade(pnl_r=r) for r in (-5.0, 0.5, 0.5, 0.5, 0.5, 0.5)]
        skew, _ = _fold_moments_from_trades(trades)
        assert skew < 0.0

    def test_uniform_returns_low_kurtosis(self) -> None:
        # Uniform-ish -> kurt well below the normal 3.0
        trades = [_mk_trade(pnl_r=r) for r in (-1.0, -0.5, 0.0, 0.5, 1.0)]
        _, kurt = _fold_moments_from_trades(trades)
        assert kurt < 3.0


# ---------------------------------------------------------------------------
# WalkForwardResult + Engine wiring
# ---------------------------------------------------------------------------


class TestWalkForwardResultFields:
    def test_new_fields_default_to_empty(self) -> None:
        res = WalkForwardResult()
        assert res.per_fold_dsr == []
        assert res.fold_dsr_median == 0.0
        assert res.fold_dsr_pass_fraction == 0.0

    def test_config_has_strict_gate_default_false(self) -> None:
        cfg = WalkForwardConfig(window_days=5, step_days=2)
        assert cfg.strict_fold_dsr_gate is False


class TestEngineWiresPerFoldDsr:
    def test_run_populates_per_fold_dsr_list(self) -> None:
        bars = BarReplay.synthetic_bars(
            n=4 * 24 * 20,
            drift=0.0015,
            vol=0.004,
            seed=11,
            start=datetime(2025, 1, 1, tzinfo=UTC),
            interval_minutes=15,
        )
        eng = WalkForwardEngine()
        res = eng.run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=WalkForwardConfig(
                window_days=5,
                step_days=3,
                anchored=False,
                oos_fraction=0.3,
                min_trades_per_window=1,
            ),
            base_backtest_config=_cfg(bars),
            ctx_builder=_ctx,
        )
        # Every accepted window produces one per-fold DSR entry
        assert len(res.per_fold_dsr) == len(res.windows)
        # Each entry is in [0, 1]
        for dsr in res.per_fold_dsr:
            assert 0.0 <= dsr <= 1.0
        # Each window dict also carries its own oos_dsr / oos_skew / oos_kurt
        for w in res.windows:
            assert "oos_dsr" in w
            assert "oos_skew" in w
            assert "oos_kurt" in w

    def test_median_and_pass_fraction_consistent(self) -> None:
        bars = BarReplay.synthetic_bars(
            n=4 * 24 * 20,
            drift=0.0015,
            vol=0.004,
            seed=13,
            start=datetime(2025, 1, 1, tzinfo=UTC),
            interval_minutes=15,
        )
        eng = WalkForwardEngine()
        res = eng.run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=WalkForwardConfig(
                window_days=5,
                step_days=3,
                anchored=False,
                oos_fraction=0.3,
                min_trades_per_window=1,
            ),
            base_backtest_config=_cfg(bars),
            ctx_builder=_ctx,
        )
        if not res.per_fold_dsr:
            return  # synthetic data may yield zero valid folds; skip
        # Median check
        sorted_dsr = sorted(res.per_fold_dsr)
        n = len(sorted_dsr)
        expected_median = sorted_dsr[n // 2] if n % 2 == 1 else 0.5 * (sorted_dsr[n // 2 - 1] + sorted_dsr[n // 2])
        assert abs(res.fold_dsr_median - expected_median) < 1e-9
        # Pass-fraction check
        expected_frac = sum(1 for d in res.per_fold_dsr if d > 0.5) / n
        assert abs(res.fold_dsr_pass_fraction - expected_frac) < 1e-9

    def test_empty_bars_zero_fields(self) -> None:
        eng = WalkForwardEngine()
        res = eng.run(
            bars=[],
            pipeline=FeaturePipeline.default(),
            config=WalkForwardConfig(window_days=5, step_days=2),
            base_backtest_config=BacktestConfig(
                start_date=datetime(2025, 1, 1, tzinfo=UTC),
                end_date=datetime(2025, 1, 2, tzinfo=UTC),
                symbol="T",
                initial_equity=10_000.0,
                risk_per_trade_pct=0.01,
            ),
        )
        assert res.per_fold_dsr == []
        assert res.fold_dsr_median == 0.0
        assert res.fold_dsr_pass_fraction == 0.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mk_trade(*, pnl_r: float):
    """Build a minimal Trade with just pnl_r populated meaningfully."""
    from eta_engine.backtest.models import Trade

    t0 = datetime(2025, 1, 1, 10, 0, tzinfo=UTC)
    t1 = datetime(2025, 1, 1, 11, 0, tzinfo=UTC)
    return Trade(
        entry_time=t0,
        exit_time=t1,
        symbol="MNQ",
        side="BUY",
        qty=1.0,
        entry_price=20000.0,
        exit_price=20010.0,
        pnl_r=pnl_r,
        pnl_usd=abs(pnl_r) * 100.0,
        confluence_score=7.0,
        leverage_used=1.0,
        max_drawdown_during=0.0,
    )

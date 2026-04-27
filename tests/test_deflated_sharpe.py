"""tests.test_deflated_sharpe — LdP 2014 formulas, edge cases."""

from __future__ import annotations

from eta_engine.backtest.deflated_sharpe import (
    compute_dsr,
    compute_probabilistic_sharpe,
)


class TestProbabilisticSharpe:
    def test_psr_equal_to_threshold_is_half(self) -> None:
        # When observed SR == threshold, z=0 -> PSR = 0.5
        assert (
            abs(
                compute_probabilistic_sharpe(
                    sharpe=1.0,
                    threshold=1.0,
                    n_trades=100,
                    skew=0.0,
                    kurtosis=3.0,
                )
                - 0.5
            )
            < 1e-6
        )

    def test_psr_above_threshold_gt_half(self) -> None:
        assert (
            compute_probabilistic_sharpe(
                sharpe=1.5,
                threshold=1.0,
                n_trades=100,
                skew=0.0,
                kurtosis=3.0,
            )
            > 0.5
        )

    def test_psr_below_threshold_lt_half(self) -> None:
        assert (
            compute_probabilistic_sharpe(
                sharpe=0.5,
                threshold=1.0,
                n_trades=100,
                skew=0.0,
                kurtosis=3.0,
            )
            < 0.5
        )

    def test_psr_monotonic_in_sample_size(self) -> None:
        low_n = compute_probabilistic_sharpe(
            sharpe=1.2,
            threshold=0.8,
            n_trades=30,
            skew=0.0,
            kurtosis=3.0,
        )
        high_n = compute_probabilistic_sharpe(
            sharpe=1.2,
            threshold=0.8,
            n_trades=500,
            skew=0.0,
            kurtosis=3.0,
        )
        assert high_n > low_n

    def test_psr_penalizes_negative_skew(self) -> None:
        # Small sample + modest SR so the z-score does not saturate to 1.0
        symm = compute_probabilistic_sharpe(
            sharpe=0.3,
            threshold=0.0,
            n_trades=30,
            skew=0.0,
            kurtosis=3.0,
        )
        neg_skew = compute_probabilistic_sharpe(
            sharpe=0.3,
            threshold=0.0,
            n_trades=30,
            skew=-1.5,
            kurtosis=3.0,
        )
        assert neg_skew < symm


class TestDeflatedSharpe:
    def test_dsr_ne_psr_when_trials_gt_one(self) -> None:
        # With 100 trials, threshold rises -> DSR < PSR(threshold=0)
        psr = compute_probabilistic_sharpe(1.5, 0.0, 200, 0.0, 3.0)
        dsr = compute_dsr(1.5, 200, 0.0, 3.0, n_trials=100)
        assert dsr < psr

    def test_dsr_single_trial_equals_psr_zero(self) -> None:
        psr = compute_probabilistic_sharpe(1.2, 0.0, 100, 0.0, 3.0)
        dsr = compute_dsr(1.2, 100, 0.0, 3.0, n_trials=1)
        assert abs(dsr - psr) < 1e-9

    def test_dsr_weak_strategy_many_trials_low(self) -> None:
        # Strategy with SR=0.3 but tested across 1000 alternatives -> DSR near 0
        dsr = compute_dsr(0.3, 200, 0.0, 3.0, n_trials=1000)
        assert dsr < 0.5

    def test_dsr_strong_strategy_stays_high(self) -> None:
        dsr = compute_dsr(3.0, 500, 0.0, 3.0, n_trials=10)
        assert dsr > 0.9

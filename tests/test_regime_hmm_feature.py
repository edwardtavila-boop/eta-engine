"""tests.test_regime_hmm_feature -- HMM regime feature + pipeline wiring.

The HMM fit itself is too expensive to run per-bar, so this feature reads
a precomputed posterior + regime labels from ``ctx``. An upstream helper
(:func:`features.regime_hmm_feature.build_hmm_ctx`) fits the HMM on a
window of historical returns and populates the ctx dict. The feature
itself is cheap: argmax of the posterior, then regime-label -> score.

Coverage:
  * score mapping for each RegimeType label
  * ctx-missing -> neutral 0.5 (does not break the default 5-feature
    pipeline or crash unfit callers)
  * ctx with K=1 collapses to TRANSITION -> 0.5
  * build_hmm_ctx helper: fits, canonicalizes, returns well-formed ctx
  * pipeline integration: registering the feature exposes it through
    compute_all but NEVER enters the 5-tuple to_confluence_inputs
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from eta_engine.brain.regime import RegimeType
from eta_engine.core.data_pipeline import BarData
from eta_engine.features import FeaturePipeline
from eta_engine.features.regime_hmm_feature import (
    RegimeHMMFeature,
    build_hmm_ctx,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bar() -> BarData:
    return BarData(
        timestamp=datetime.now(UTC),
        symbol="MNQ",
        open=20000.0,
        high=20010.0,
        low=19990.0,
        close=20005.0,
        volume=1000.0,
    )


def _hmm_ctx(
    *,
    posterior: list[float],
    regime_labels: list[RegimeType],
) -> dict:
    return {
        "regime_hmm": {
            "posterior": posterior,
            "regime_labels": regime_labels,
            "n_states": len(posterior),
        },
    }


# ---------------------------------------------------------------------------
# Score mapping
# ---------------------------------------------------------------------------


class TestScoreMapping:
    def test_trending_state_scores_high(self, bar: BarData) -> None:
        ctx = _hmm_ctx(
            posterior=[0.1, 0.9],
            regime_labels=[RegimeType.LOW_VOL, RegimeType.TRENDING],
        )
        score = RegimeHMMFeature().compute(bar, ctx)
        assert score >= 0.9

    def test_low_vol_state_scores_favorable(self, bar: BarData) -> None:
        ctx = _hmm_ctx(
            posterior=[0.85, 0.15],
            regime_labels=[RegimeType.LOW_VOL, RegimeType.HIGH_VOL],
        )
        score = RegimeHMMFeature().compute(bar, ctx)
        assert 0.65 <= score <= 0.85

    def test_high_vol_state_scores_cautious(self, bar: BarData) -> None:
        ctx = _hmm_ctx(
            posterior=[0.1, 0.9],
            regime_labels=[RegimeType.LOW_VOL, RegimeType.HIGH_VOL],
        )
        score = RegimeHMMFeature().compute(bar, ctx)
        assert score <= 0.35

    def test_transition_state_is_neutral(self, bar: BarData) -> None:
        ctx = _hmm_ctx(
            posterior=[0.9, 0.1],
            regime_labels=[RegimeType.TRANSITION, RegimeType.LOW_VOL],
        )
        score = RegimeHMMFeature().compute(bar, ctx)
        assert 0.4 <= score <= 0.6

    def test_score_always_in_unit_interval(self, bar: BarData) -> None:
        rng = random.Random(31)
        for _ in range(20):
            a = rng.random()
            ctx = _hmm_ctx(
                posterior=[a, 1 - a],
                regime_labels=[RegimeType.LOW_VOL, RegimeType.HIGH_VOL],
            )
            score = RegimeHMMFeature().compute(bar, ctx)
            assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Graceful defaults
# ---------------------------------------------------------------------------


class TestGracefulDefaults:
    def test_missing_ctx_returns_neutral(self, bar: BarData) -> None:
        score = RegimeHMMFeature().compute(bar, {})
        assert score == 0.5

    def test_none_regime_hmm_returns_neutral(self, bar: BarData) -> None:
        score = RegimeHMMFeature().compute(bar, {"regime_hmm": None})
        assert score == 0.5

    def test_empty_posterior_returns_neutral(self, bar: BarData) -> None:
        ctx = _hmm_ctx(posterior=[], regime_labels=[])
        score = RegimeHMMFeature().compute(bar, ctx)
        assert score == 0.5

    def test_mismatched_lengths_returns_neutral(self, bar: BarData) -> None:
        # posterior has 2 entries, labels only 1 -> can't map safely
        ctx = {
            "regime_hmm": {
                "posterior": [0.5, 0.5],
                "regime_labels": [RegimeType.LOW_VOL],
                "n_states": 2,
            },
        }
        score = RegimeHMMFeature().compute(bar, ctx)
        assert score == 0.5

    def test_single_state_collapses_to_transition(self, bar: BarData) -> None:
        ctx = _hmm_ctx(
            posterior=[1.0],
            regime_labels=[RegimeType.TRANSITION],
        )
        score = RegimeHMMFeature().compute(bar, ctx)
        assert 0.4 <= score <= 0.6


# ---------------------------------------------------------------------------
# build_hmm_ctx helper -- fits HMM, canonicalizes, produces ctx payload
# ---------------------------------------------------------------------------


class TestBuildHmmCtx:
    def test_returns_regime_hmm_dict(self) -> None:
        rng = random.Random(101)
        xs = [rng.gauss(0.0, 0.005) for _ in range(200)] + [rng.gauss(0.0, 0.025) for _ in range(200)]
        ctx = build_hmm_ctx(returns=xs, n_states=2, random_seed=7)
        assert "regime_hmm" in ctx
        payload = ctx["regime_hmm"]
        assert payload["n_states"] == 2
        assert len(payload["posterior"]) == 2
        assert len(payload["regime_labels"]) == 2
        assert abs(sum(payload["posterior"]) - 1.0) < 1e-6

    def test_canonical_ordering_by_variance(self) -> None:
        """State 0 should always be the lowest-variance regime."""
        rng = random.Random(103)
        xs = [rng.gauss(0.0, 0.003) for _ in range(250)] + [rng.gauss(0.0, 0.030) for _ in range(250)]
        ctx_a = build_hmm_ctx(returns=xs, n_states=2, random_seed=1)
        ctx_b = build_hmm_ctx(returns=xs, n_states=2, random_seed=99)
        # Variances in ascending order in both fits
        var_a = ctx_a["regime_hmm"]["variances"]
        var_b = ctx_b["regime_hmm"]["variances"]
        assert var_a[0] <= var_a[1]
        assert var_b[0] <= var_b[1]

    def test_default_n_states_is_2(self) -> None:
        rng = random.Random(107)
        xs = [rng.gauss(0.0, 0.01) for _ in range(100)]
        ctx = build_hmm_ctx(returns=xs)  # no n_states kwarg
        assert ctx["regime_hmm"]["n_states"] == 2

    def test_too_short_returns_neutral_ctx(self) -> None:
        """With < 2 observations we can't fit; helper returns empty payload."""
        ctx = build_hmm_ctx(returns=[0.01])
        assert ctx["regime_hmm"]["n_states"] == 0
        assert ctx["regime_hmm"]["posterior"] == []

    def test_caller_can_override_n_states(self) -> None:
        rng = random.Random(109)
        xs = [rng.gauss(0.0, 0.01) for _ in range(300)]
        ctx = build_hmm_ctx(returns=xs, n_states=1)
        assert ctx["regime_hmm"]["n_states"] == 1
        assert len(ctx["regime_hmm"]["posterior"]) == 1


# ---------------------------------------------------------------------------
# FeaturePipeline integration -- must NOT break the 5-tuple contract
# ---------------------------------------------------------------------------


class TestPipelineIntegration:
    def test_default_pipeline_does_not_include_hmm(self) -> None:
        """FeaturePipeline.default() is the untouched 5-feature pipeline."""
        p = FeaturePipeline.default()
        # The feature must NOT auto-register; callers opt in explicitly.
        assert "regime_hmm" not in p._features  # noqa: SLF001

    def test_opt_in_registration_exposes_via_compute_all(
        self,
        bar: BarData,
    ) -> None:
        p = FeaturePipeline.default()
        p.register(RegimeHMMFeature())
        ctx = _hmm_ctx(
            posterior=[0.2, 0.8],
            regime_labels=[RegimeType.LOW_VOL, RegimeType.HIGH_VOL],
        )
        # Rest of ctx must still cover the 5 defaults
        ctx.update(
            {
                "daily_ema": [3000, 3100, 3200, 3300, 3400],
                "h4_struct": "HH_HL",
                "bias": 1,
                "atr_history": [20] * 10,
                "atr_current": 20.0,
                "funding_history": [],
                "onchain": {
                    "whale_transfers": 0,
                    "whale_transfers_baseline": 0,
                    "exchange_netflow_usd": 0.0,
                    "active_addresses": 0,
                    "active_addresses_baseline": 0,
                },
                "sentiment": {
                    "galaxy_score": 50.0,
                    "alt_rank": 50,
                    "social_volume": 0,
                    "social_volume_baseline": 0,
                    "fear_greed": 50,
                },
            }
        )
        results = p.compute_all(bar, ctx)
        assert "regime_hmm" in results
        # HIGH_VOL dominant -> score low (~0.25 per mapping)
        assert results["regime_hmm"].normalized_score <= 0.35

    def test_confluence_tuple_remains_5_even_after_registration(
        self,
        bar: BarData,
    ) -> None:
        """to_confluence_inputs is still (trend, vol, funding, onchain, sentiment)."""
        p = FeaturePipeline.default()
        p.register(RegimeHMMFeature())
        ctx = {
            "regime_hmm": {
                "posterior": [1.0, 0.0],
                "regime_labels": [RegimeType.TRENDING, RegimeType.LOW_VOL],
                "n_states": 2,
            },
            "daily_ema": [3000, 3100, 3200, 3300, 3400],
            "h4_struct": "HH_HL",
            "bias": 1,
            "atr_history": [20] * 10,
            "atr_current": 20.0,
            "funding_history": [],
            "onchain": {
                "whale_transfers": 0,
                "whale_transfers_baseline": 0,
                "exchange_netflow_usd": 0.0,
                "active_addresses": 0,
                "active_addresses_baseline": 0,
            },
            "sentiment": {
                "galaxy_score": 50.0,
                "alt_rank": 50,
                "social_volume": 0,
                "social_volume_baseline": 0,
                "fear_greed": 50,
            },
        }
        results = p.compute_all(bar, ctx)
        tup = p.to_confluence_inputs(results)
        assert len(tup) == 5
        # HMM score not leaking into the tuple
        # (regime_hmm result present; tuple composition excludes it)
        assert "regime_hmm" in results

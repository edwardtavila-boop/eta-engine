"""Direct smoke tests for lower-level Sage school modules."""

from __future__ import annotations

import pytest

from eta_engine.brain.jarvis_v3.sage.base import Bias, MarketContext
from eta_engine.brain.jarvis_v3.sage.schools.elliott_wave import ElliottWaveSchool
from eta_engine.brain.jarvis_v3.sage.schools.gann import GannSchool
from eta_engine.brain.jarvis_v3.sage.schools.neowave import NEoWaveSchool
from eta_engine.brain.jarvis_v3.sage.schools.options_greeks import OptionsGreeksSchool
from eta_engine.brain.jarvis_v3.sage.schools.order_flow import OrderFlowSchool


def _trend_bars(n: int = 36) -> list[dict[str, float]]:
    return [
        {
            "open": 100.0 + i,
            "high": 100.5 + i,
            "low": 99.5 + i,
            "close": 100.25 + i,
            "volume": 1_000.0 + i * 10.0,
        }
        for i in range(n)
    ]


@pytest.mark.parametrize(
    "school",
    [
        ElliottWaveSchool(),
        GannSchool(),
        NEoWaveSchool(),
    ],
)
def test_structural_sage_schools_align_with_clean_uptrend(school) -> None:
    ctx = MarketContext(bars=_trend_bars(), side="long", symbol="MNQ")

    verdict = school.analyze(ctx)

    assert verdict.bias == Bias.LONG
    assert verdict.aligned_with_entry is True
    assert verdict.conviction > 0.0
    assert verdict.signals


def test_order_flow_school_uses_delta_and_book_imbalance() -> None:
    ctx = MarketContext(
        bars=_trend_bars(),
        side="long",
        cumulative_delta=0.8,
        order_book_imbalance=0.2,
    )

    verdict = OrderFlowSchool().analyze(ctx)

    assert verdict.bias == Bias.LONG
    assert verdict.aligned_with_entry is True
    assert verdict.signals["cumulative_delta"] == 0.8


def test_order_flow_school_skips_without_telemetry() -> None:
    verdict = OrderFlowSchool().analyze(MarketContext(bars=_trend_bars(), side="long"))

    assert verdict.bias == Bias.NEUTRAL
    assert verdict.conviction == 0.0
    assert "cumulative_delta" in verdict.signals["missing"]


def test_order_flow_school_applies_only_to_symbols_configured_for_order_flow() -> None:
    school = OrderFlowSchool()

    assert school.applies_to(
        MarketContext(
            bars=_trend_bars(),
            side="long",
            symbol="MNQ1",
            instrument_class="futures",
        )
    )
    assert not school.applies_to(
        MarketContext(
            bars=_trend_bars(),
            side="long",
            symbol="GC1",
            instrument_class="futures",
        )
    )


def test_options_greeks_school_uses_gamma_skew_and_squeeze() -> None:
    ctx = MarketContext(
        bars=_trend_bars(),
        side="long",
        options={
            "dealer_gamma_exposure": -500.0,
            "vol_skew": -0.1,
            "0dte_squeeze_score": 0.8,
        },
    )

    verdict = OptionsGreeksSchool().analyze(ctx)

    assert verdict.bias == Bias.LONG
    assert verdict.aligned_with_entry is True
    assert verdict.conviction > 0.7


def test_options_greeks_school_skips_without_options_payload() -> None:
    verdict = OptionsGreeksSchool().analyze(MarketContext(bars=_trend_bars(), side="long"))

    assert verdict.bias == Bias.NEUTRAL
    assert verdict.conviction == 0.0
    assert verdict.signals == {"missing": ["ctx.options"]}

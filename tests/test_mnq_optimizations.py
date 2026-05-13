"""Tests for strategies.mnq_optimizations."""

from __future__ import annotations

from datetime import UTC, datetime, time

from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.mnq_optimizations import (
    SessionProfile,
    classify_regime_v2,
    correlated_with_es,
    in_session,
)


def _bar(ts: datetime, *, c: float = 100.0, w: float = 1.0) -> BarData:
    return BarData(
        timestamp=ts,
        symbol="MNQ",
        open=c - w / 2,
        high=c + w / 2,
        low=c - w / 2,
        close=c,
        volume=1000.0,
    )


# ---------------------------------------------------------------------------
# classify_regime_v2
# ---------------------------------------------------------------------------


def _series(prices: list[float], *, atr_width: float = 1.0):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        BarData(
            timestamp=base.replace(minute=i % 60, hour=i // 60),
            symbol="MNQ",
            open=p,
            high=p + atr_width / 2,
            low=p - atr_width / 2,
            close=p,
            volume=1000.0,
        )
        for i, p in enumerate(prices)
    ]


def test_warmup_when_too_few_bars() -> None:
    bars = _series([100.0] * 30)
    assert classify_regime_v2(bars) == "warmup"


def test_low_vol_choppy_flat_market() -> None:
    bars = _series([100.0] * 60)
    assert classify_regime_v2(bars) == "low_vol_choppy"


def test_low_vol_trend_up() -> None:
    # rising 1% over short window vs flat baseline
    bars = _series([100.0] * 40 + list(range(101, 121)), atr_width=0.5)
    out = classify_regime_v2(bars)
    assert out.startswith("low_vol_trend_up") or out.startswith("high_vol_trend_up"), out


def test_low_vol_trend_down() -> None:
    bars = _series([100.0] * 40 + list(range(99, 79, -1)), atr_width=0.5)
    out = classify_regime_v2(bars)
    assert out.endswith("trend_down")


def test_panic_regime_when_atr_explodes() -> None:
    # First 40 bars normal width, last 20 bars huge width = panic
    base = datetime(2026, 1, 1, tzinfo=UTC)
    bars: list[BarData] = []
    for i in range(40):
        bars.append(
            BarData(
                timestamp=base.replace(minute=i % 60, hour=i // 60),
                symbol="MNQ",
                open=100.0,
                high=100.5,
                low=99.5,
                close=100.0,
                volume=1.0,
            )
        )
    for i in range(40, 60):
        bars.append(
            BarData(
                timestamp=base.replace(minute=i % 60, hour=i // 60),
                symbol="MNQ",
                open=100.0,
                high=110.0,
                low=90.0,
                close=100.0,
                volume=1.0,
            )
        )
    assert classify_regime_v2(bars) == "panic"


def test_high_vol_choppy_when_atr_doubles_no_drift() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    bars = []
    for i in range(40):
        bars.append(
            BarData(
                timestamp=base.replace(minute=i % 60, hour=i // 60),
                symbol="MNQ",
                open=100.0,
                high=100.5,
                low=99.5,
                close=100.0,
                volume=1.0,
            )
        )
    for i in range(40, 60):
        bars.append(
            BarData(
                timestamp=base.replace(minute=i % 60, hour=i // 60),
                symbol="MNQ",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1.0,
            )
        )
    assert classify_regime_v2(bars) == "high_vol_choppy"


# ---------------------------------------------------------------------------
# in_session
# ---------------------------------------------------------------------------


def test_in_session_blocks_open_window() -> None:
    # 08:45 CT (within 08:30-09:00 blackout)
    # Convert: 08:45 CT = 13:45 UTC (CST) or 14:45 UTC (CDT). Use Jan = CST.
    ts = datetime(2026, 1, 15, 14, 45, tzinfo=UTC)
    assert in_session(ts) is False


def test_in_session_blocks_close_window() -> None:
    # 15:45 CT = 21:45 UTC (CST)
    ts = datetime(2026, 1, 15, 21, 45, tzinfo=UTC)
    assert in_session(ts) is False


def test_in_session_allows_midday() -> None:
    # 12:00 CT = 18:00 UTC (CST)
    ts = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
    assert in_session(ts) is True


def test_in_session_allows_after_open_blackout() -> None:
    # 09:00 CT exactly — half-open window means 09:00 is allowed
    ts = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)  # 09:00 CT = 15:00 UTC CST
    assert in_session(ts) is True


def test_in_session_handles_naive_timestamps() -> None:
    # Naive ts treated as UTC; should not crash
    ts = datetime(2026, 1, 15, 18, 0)
    assert in_session(ts) is True


def test_custom_profile_overrides_default() -> None:
    profile = SessionProfile(
        timezone_name="UTC",
        blackout_windows=((time(0, 0), time(1, 0)),),
    )
    blocked = datetime(2026, 1, 15, 0, 30, tzinfo=UTC)
    assert in_session(blocked, profile) is False
    allowed = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    assert in_session(allowed, profile) is True


# ---------------------------------------------------------------------------
# correlated_with_es
# ---------------------------------------------------------------------------


def test_correlation_returns_true_when_es_missing() -> None:
    mnq = _series([100.0 + i * 0.1 for i in range(40)])
    assert correlated_with_es(mnq, None) is True
    assert correlated_with_es(mnq, []) is True


def test_correlation_returns_true_when_too_few_bars() -> None:
    mnq = _series([100.0 + i * 0.1 for i in range(10)])
    es = _series([5000.0 + i for i in range(10)])
    assert correlated_with_es(mnq, es, window=30) is True


def test_correlated_when_aligned() -> None:
    # MNQ + ES both monotonically rising — perfectly correlated
    mnq = _series([100.0 + i * 0.1 for i in range(40)])
    es = _series([5000.0 + i * 5.0 for i in range(40)])
    assert correlated_with_es(mnq, es) is True


def test_decoupled_when_anti_correlated() -> None:
    """MNQ moves opposite ES — correlation gate must reject."""
    import math

    # Sine for MNQ, negated sine for ES — guaranteed strong negative correlation
    mnq = _series([100.0 + 5.0 * math.sin(i / 5.0) for i in range(40)])
    es = _series([5000.0 - 50.0 * math.sin(i / 5.0) for i in range(40)])
    assert correlated_with_es(mnq, es, threshold=0.4) is False


def test_threshold_tunability() -> None:
    """A mildly correlated series passes a permissive threshold and
    fails an extreme one. Uses interleaved walks so variance is real."""
    import math

    # Both rising overall but with different oscillation phases
    mnq = _series([100.0 + 0.1 * i + 0.5 * math.sin(i / 3.0) for i in range(40)])
    es = _series([5000.0 + 5.0 * i + 25.0 * math.sin(i / 3.0 + 0.5) for i in range(40)])
    permissive = correlated_with_es(mnq, es, threshold=0.1)
    strict = correlated_with_es(mnq, es, threshold=0.99)
    # Same-phase-ish sine + same-direction trend -> some positive correlation
    assert permissive is True, "permissive 0.1 threshold should pass"
    # 0.99 threshold should reject all but perfectly aligned series
    assert strict is False, "strict 0.99 threshold should reject"

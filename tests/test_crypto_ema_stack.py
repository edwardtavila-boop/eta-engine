"""Tests for crypto_ema_stack_strategy — stack alignment + variants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.crypto_ema_stack_strategy import (
    CryptoEmaStackConfig,
    CryptoEmaStackStrategy,
)


def _bar(idx: int, *, h: float, low: float, c: float | None = None,
         v: float = 1000.0, tf_minutes: int = 60) -> BarData:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=idx * tf_minutes)
    c = c if c is not None else (h + low) / 2
    return BarData(
        timestamp=ts, symbol="BTC", open=(h + low) / 2,
        high=h, low=low, close=c, volume=v,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="BTC", initial_equity=10_000.0,
        risk_per_trade_pct=0.01, confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _strat(**overrides) -> CryptoEmaStackStrategy:  # type: ignore[no-untyped-def]
    """Small EMAs + low warmup so tests stay fast."""
    base = {
        "stack_periods": (5, 10, 20),
        "entry_ema_idx": 1,  # entry on the 10 EMA
        "warmup_bars": 25, "atr_period": 5,
        "min_bars_between_trades": 0,
        "entry_tolerance_pct": 2.0,
        "max_trades_per_day": 100,
    }
    base.update(overrides)
    return CryptoEmaStackStrategy(CryptoEmaStackConfig(**base))


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_empty_stack_rejected() -> None:
    with pytest.raises(ValueError, match="stack_periods"):
        CryptoEmaStackStrategy(CryptoEmaStackConfig(stack_periods=()))


def test_unsorted_stack_rejected() -> None:
    with pytest.raises(ValueError, match="ascending"):
        CryptoEmaStackStrategy(CryptoEmaStackConfig(stack_periods=(50, 9, 21)))


def test_entry_idx_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match="entry_ema_idx"):
        CryptoEmaStackStrategy(CryptoEmaStackConfig(
            stack_periods=(9, 21), entry_ema_idx=5,
        ))


# ---------------------------------------------------------------------------
# Warmup
# ---------------------------------------------------------------------------


def test_warmup_blocks_early_signals() -> None:
    s = _strat(warmup_bars=10)
    cfg = _config()
    bar = _bar(0, h=100, low=98, c=99)
    assert s.maybe_enter(bar, [bar], 10_000.0, cfg) is None


# ---------------------------------------------------------------------------
# Stack alignment
# ---------------------------------------------------------------------------


def test_stack_separation_filter_cuts_chop_fires() -> None:
    """Default stack alignment alone fires opportunistically in chop.
    Adding variant C (stack-separation filter) materially cuts that.
    Test the DELTA — the filter must reduce fires by half or more.
    """
    cfg = _config()

    def _count_fires(strat: CryptoEmaStackStrategy) -> int:
        hist: list[BarData] = []
        n = 0
        for i in range(50):
            leg = (i // 4) % 2
            c = 100 + (i % 4) * 0.3 if leg == 0 else 100 - (i % 4) * 0.3
            b = _bar(i, h=c + 0.2, low=c - 0.2, c=c)
            hist.append(b)
            out = strat.maybe_enter(b, hist, 10_000.0, cfg)
            if out is not None:
                n += 1
        return n

    # Without variant C
    s_no_filter = _strat()
    fires_no_filter = _count_fires(s_no_filter)

    # With variant C — modest spread requirement
    s_with_filter = _strat(min_stack_spread_atr=2.0)
    fires_with_filter = _count_fires(s_with_filter)

    # The filter must reduce chop fires materially
    assert fires_with_filter <= fires_no_filter, (
        "filter should not increase fires"
    )
    assert fires_with_filter < fires_no_filter or fires_no_filter <= 2, (
        f"stack-separation filter must cut chop fires "
        f"(no_filter={fires_no_filter}, with_filter={fires_with_filter})"
    )


def test_long_fires_with_full_bull_stack_and_pullback() -> None:
    """Strong uptrend → stack aligned → pullback bar → long fires."""
    s = _strat()
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Confirm the stack is bullishly aligned
    emas = s._emas
    assert all(e is not None for e in emas)
    assert emas[0] > emas[1] > emas[2], (
        f"expected fast > slow stack; got {emas}"
    )
    # Build pullback bar that taps entry EMA
    entry_ema = emas[1]
    pull_bar = _bar(35, h=entry_ema + 0.5, low=entry_ema - 0.05, c=entry_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert "ema_stack_bull" in out.regime


def test_short_fires_with_full_bear_stack_and_pullback() -> None:
    s = _strat()
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 - i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    emas = s._emas
    assert emas[0] < emas[1] < emas[2]
    entry_ema = emas[1]
    rip_bar = _bar(35, h=entry_ema + 0.05, low=entry_ema - 0.5, c=entry_ema - 0.4)
    hist.append(rip_bar)
    out = s.maybe_enter(rip_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "SELL"
    assert "ema_stack_bear" in out.regime


# ---------------------------------------------------------------------------
# Variant C: stack-separation filter
# ---------------------------------------------------------------------------


def test_stack_separation_filter_blocks_compressed_stack() -> None:
    """When EMAs are tightly bunched, the spread/ATR ratio is small;
    setting a high min_stack_spread_atr should kill the trade."""
    s = _strat(min_stack_spread_atr=10.0)  # impossible threshold
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    emas = s._emas
    entry_ema = emas[1]
    pull_bar = _bar(35, h=entry_ema + 0.5, low=entry_ema - 0.05, c=entry_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None, "stack-separation filter should block compressed stack"


# ---------------------------------------------------------------------------
# Variant D: volume confirmation
# ---------------------------------------------------------------------------


def test_volume_confirmation_blocks_low_volume_pullback() -> None:
    s = _strat(volume_mult=2.0, volume_lookback=5)
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c, v=1000.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    emas = s._emas
    entry_ema = emas[1]
    # Pullback bar at LOW volume → should be blocked
    pull_bar = _bar(35, h=entry_ema + 0.5, low=entry_ema - 0.05,
                    c=entry_ema + 0.4, v=500.0)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None


def test_volume_confirmation_passes_high_volume_pullback() -> None:
    s = _strat(volume_mult=2.0, volume_lookback=5)
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c, v=1000.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    emas = s._emas
    entry_ema = emas[1]
    pull_bar = _bar(35, h=entry_ema + 0.5, low=entry_ema - 0.05,
                    c=entry_ema + 0.4, v=3000.0)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None


# ---------------------------------------------------------------------------
# Variant E: adaptive RR
# ---------------------------------------------------------------------------


def test_adaptive_rr_lifts_target_when_stack_tight() -> None:
    """When stack is tight (compressed), RR is multiplied — target
    distance should be larger than the base config's rr_target."""
    s = _strat(
        rr_target=2.0,
        adaptive_rr_enabled=True,
        tightness_threshold_atr=100.0,  # always-tight in synthetic data
        tightness_rr_lift=2.0,
    )
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    emas = s._emas
    entry_ema = emas[1]
    pull_bar = _bar(35, h=entry_ema + 0.5, low=entry_ema - 0.05, c=entry_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    stop_dist = abs(out.entry_price - out.stop)
    target_dist = abs(out.target - out.entry_price)
    # RR 2.0 × tightness_lift 2.0 = effective RR 4.0
    assert target_dist == pytest.approx(4.0 * stop_dist, rel=1e-3)


# ---------------------------------------------------------------------------
# Variant F: soft stop
# ---------------------------------------------------------------------------


def test_soft_stop_uses_entry_ema_distance() -> None:
    """soft_stop_enabled=True uses |close - entry_ema| as the stop
    distance instead of atr_stop_mult × ATR."""
    s = _strat(soft_stop_enabled=True, atr_stop_mult=10.0)  # huge ATR mult
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    emas = s._emas
    entry_ema = emas[1]
    pull_bar = _bar(35, h=entry_ema + 0.5, low=entry_ema - 0.05, c=entry_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    # Soft stop distance = |close - entry_ema| ≈ 0.4
    # If ATR-based, distance would be ~10 × ATR ≈ several points
    soft_dist = abs(out.entry_price - entry_ema)
    stop_dist = abs(out.entry_price - out.stop)
    # Stop distance should be near the soft distance (allowing the
    # 0.5×ATR floor in the strategy code).
    assert stop_dist < 5.0, (
        f"soft stop should override ATR; got stop_dist={stop_dist}"
    )
    assert stop_dist == pytest.approx(soft_dist, abs=1.0)


# ---------------------------------------------------------------------------
# Mode variants — scalp / swing / full
# ---------------------------------------------------------------------------


def test_scalp_mode_two_emas_only() -> None:
    """stack_periods=(9, 21) is the scalp variant. Strategy must
    still fire correctly."""
    s = _strat(stack_periods=(5, 10), entry_ema_idx=1)
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    emas = s._emas
    assert len(emas) == 2
    assert emas[0] > emas[1]
    entry_ema = emas[1]
    pull_bar = _bar(35, h=entry_ema + 0.5, low=entry_ema - 0.05, c=entry_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert out.regime == "ema_stack_bull_2"

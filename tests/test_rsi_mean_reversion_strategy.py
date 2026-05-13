"""Tests for strategies.rsi_mean_reversion_strategy -- HTF gate (2026-05-07).

The HTF (higher-timeframe) trend gate aggregates 12 5m bars into one
synthetic 1h close, then computes an EMA(50) over those 1h closes.
LONG fires only when slope > 0; SHORT fires only when slope < 0.
This file pins gate semantics, threshold tightening, and counters.

Tests use direct internal-state manipulation rather than driving 600+
bars through the strategy -- this keeps the suite fast and the failure
modes visible (one variable changing per test, not 600 bars of OHLCV).
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.rsi_mean_reversion_strategy import (
    RSIMeanReversionConfig,
    RSIMeanReversionStrategy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(
    minute: int,
    *,
    h: float,
    low: float,
    c: float | None = None,
    o: float | None = None,
    v: float = 1000.0,
) -> BarData:
    """One 5m bar on 2026-01-01."""
    ts = datetime(2026, 1, 1, 9, minute % 60, tzinfo=UTC)
    o = o if o is not None else c if c is not None else (h + low) / 2
    c = c if c is not None else (h + low) / 2
    return BarData(
        timestamp=ts,
        symbol="MNQ",
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 1, 31, tzinfo=UTC),
        symbol="MNQ",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.005,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _make_strategy(*, require_htf_agreement: bool = True) -> RSIMeanReversionStrategy:
    """Strategy tuned for fast unit tests: filters off, short windows."""
    cfg = RSIMeanReversionConfig(
        rsi_period=5,
        bb_window=5,
        bb_std_mult=2.0,
        rsi_long_threshold=20.0,
        rsi_short_threshold=80.0,
        enable_adx_filter=False,
        volume_z_lookback=5,
        min_volume_z=-100.0,
        require_rejection=False,
        atr_period=3,
        atr_stop_mult=1.0,
        rr_target=1.5,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=1,
        max_trades_per_day=10,
        warmup_bars=1,
        htf_lookback_5m_bars=12,
        htf_ema_period=50,
        require_htf_agreement=require_htf_agreement,
    )
    return RSIMeanReversionStrategy(cfg)


def _prime_for_long(s: RSIMeanReversionStrategy) -> None:
    """Stage internal state so the test bar at close=85.0 triggers LONG.

    The strategy appends bar.close to `_closes` BEFORE computing
    indicators, so we stage 5 closes and the test bar provides the
    6th.  Final RSI sequence: [110, 108, 105, 100, 95] + bar.close=85
    -> 5 negative changes -> RSI=0.  BB on last 5 = [108, 105, 100,
    95, 85] -> mean 98.6, sigma ~8.11, lower band ~82.4, buffer ~3.2,
    so bar.close=85 falls inside the LONG trigger zone.
    """
    closes = [110.0, 108.0, 105.0, 100.0, 95.0]
    s._closes = deque(closes, maxlen=25)
    s._highs = deque([c + 1.0 for c in closes], maxlen=25)
    s._lows = deque([c - 1.0 for c in closes], maxlen=25)
    s._volume_window = deque([1000.0] * 5, maxlen=5)
    s._bars_seen = 100  # past warmup
    s._last_day = datetime(2026, 1, 1, tzinfo=UTC).date()


def _prime_for_short(s: RSIMeanReversionStrategy) -> None:
    """Stage internal state so the test bar at close=115.0 triggers SHORT.

    Mirror of _prime_for_long: rising closes [90, 92, 95, 100, 105] +
    bar.close=115 -> RSI~=100, BB upper ~112, buffer ~3, so close=115
    falls inside the SHORT trigger zone.
    """
    closes = [90.0, 92.0, 95.0, 100.0, 105.0]
    s._closes = deque(closes, maxlen=25)
    s._highs = deque([c + 1.0 for c in closes], maxlen=25)
    s._lows = deque([c - 1.0 for c in closes], maxlen=25)
    s._volume_window = deque([1000.0] * 5, maxlen=5)
    s._bars_seen = 100
    s._last_day = datetime(2026, 1, 1, tzinfo=UTC).date()


def _set_htf_uptrend(s: RSIMeanReversionStrategy, *, ema: float = 100.0, last_1h: float = 110.0) -> None:
    """Force HTF state into uptrend: last 1h close > EMA."""
    s._htf_ema = ema
    s._htf_last_1h_close = last_1h


def _set_htf_downtrend(s: RSIMeanReversionStrategy, *, ema: float = 100.0, last_1h: float = 90.0) -> None:
    """Force HTF state into downtrend: last 1h close < EMA."""
    s._htf_ema = ema
    s._htf_last_1h_close = last_1h


# ---------------------------------------------------------------------------
# RSI threshold sanity (default 20/80, not legacy 25/75)
# ---------------------------------------------------------------------------


def test_default_thresholds_are_20_and_80() -> None:
    """Audit-mandated tightening: defaults 20/80, not legacy 25/75."""
    cfg = RSIMeanReversionConfig()
    assert cfg.rsi_long_threshold == 20.0
    assert cfg.rsi_short_threshold == 80.0


def test_default_require_htf_agreement_is_true() -> None:
    """Gate is on by default; must explicitly disable for grid A/B."""
    cfg = RSIMeanReversionConfig()
    assert cfg.require_htf_agreement is True


def test_rsi_22_does_not_fire_with_default_threshold_20() -> None:
    """Tighter default blocks borderline RSI in (20, 25] entries.

    Legacy threshold 25 would have fired here; new 20 must not.
    Verified end-to-end via the long-trigger plumbing: stage state
    that yields RSI in (20, 25] and confirm no _Open returned.
    """
    s = _make_strategy(require_htf_agreement=False)
    # Closes [100, 103, 99, 96, 93, 91.5] yield Wilder RSI ~= 20.69 --
    # in the (20, 25] window: blocked by new threshold, fired by legacy.
    closes = [100.0, 103.0, 99.0, 96.0, 93.0, 91.5]
    s._closes = deque(closes, maxlen=25)
    s._highs = deque([c + 1.0 for c in closes], maxlen=25)
    s._lows = deque([c - 1.0 for c in closes], maxlen=25)
    s._volume_window = deque([1000.0] * 5, maxlen=5)
    s._bars_seen = 100
    s._last_day = datetime(2026, 1, 1, tzinfo=UTC).date()

    rsi = s._compute_rsi()
    assert rsi is not None
    assert 20.0 < rsi <= 25.0, f"setup intent: RSI in (20,25]; got {rsi:.2f}"

    # Bar at last close, BB-buffer-friendly low.
    bar = _bar(10, h=92.0, low=88.0, c=91.5, o=92.0)
    hist = [_bar(i, h=closes[i] + 1.0, low=closes[i] - 1.0, c=closes[i]) for i in range(6)]
    out = s.maybe_enter(bar, hist, 10_000.0, _config())
    assert out is None, "RSI~=20.7 must NOT fire at threshold=20 (legacy 25 would)"


# ---------------------------------------------------------------------------
# HTF gate: long path
# ---------------------------------------------------------------------------


def test_long_fires_when_rsi_low_and_htf_uptrend() -> None:
    """LONG: RSI<=20 + BB-lower + HTF uptrend -> entry.

    Prime stages 5 closes; the test bar provides the 6th, after which
    RSI computes to ~0 and BB lower + buffer captures bar.close=85.
    """
    s = _make_strategy()
    _prime_for_long(s)
    _set_htf_uptrend(s)

    bar = _bar(10, h=86.0, low=84.0, c=85.0, o=86.0)
    hist = [_bar(i, h=110.0 - i * 3, low=108.0 - i * 3, c=110.0 - i * 3) for i in range(6)]
    out = s.maybe_enter(bar, hist, 10_000.0, _config())
    assert out is not None, "LONG with HTF uptrend must fire"
    assert out.side == "BUY"
    assert s._n_htf_filtered_long == 0


def test_long_blocked_when_rsi_low_but_htf_downtrend() -> None:
    """LONG-blocked: RSI<=20 + BB-lower + HTF downtrend -> no entry,
    counter increments."""
    s = _make_strategy()
    _prime_for_long(s)
    _set_htf_downtrend(s)

    bar = _bar(10, h=86.0, low=84.0, c=85.0, o=86.0)
    hist = [_bar(i, h=110.0 - i * 3, low=108.0 - i * 3, c=110.0 - i * 3) for i in range(6)]
    out = s.maybe_enter(bar, hist, 10_000.0, _config())
    assert out is None, "LONG must be blocked by HTF downtrend"
    assert s._n_htf_filtered_long == 1
    assert s._n_htf_filtered_short == 0


# ---------------------------------------------------------------------------
# HTF gate: short path
# ---------------------------------------------------------------------------


def test_short_fires_when_rsi_high_and_htf_downtrend() -> None:
    """SHORT: RSI>=80 + BB-upper + HTF downtrend -> entry."""
    s = _make_strategy()
    _prime_for_short(s)
    _set_htf_downtrend(s)

    bar = _bar(10, h=116.0, low=114.0, c=115.0, o=114.0)
    hist = [_bar(i, h=90.0 + i * 3, low=88.0 + i * 3, c=90.0 + i * 3) for i in range(6)]
    out = s.maybe_enter(bar, hist, 10_000.0, _config())
    assert out is not None, "SHORT with HTF downtrend must fire"
    assert out.side == "SELL"
    assert s._n_htf_filtered_short == 0


def test_short_blocked_when_rsi_high_but_htf_uptrend() -> None:
    """SHORT-blocked: RSI>=80 + BB-upper + HTF uptrend -> no entry,
    counter increments."""
    s = _make_strategy()
    _prime_for_short(s)
    _set_htf_uptrend(s)

    bar = _bar(10, h=116.0, low=114.0, c=115.0, o=114.0)
    hist = [_bar(i, h=90.0 + i * 3, low=88.0 + i * 3, c=90.0 + i * 3) for i in range(6)]
    out = s.maybe_enter(bar, hist, 10_000.0, _config())
    assert out is None, "SHORT must be blocked by HTF uptrend"
    assert s._n_htf_filtered_short == 1
    assert s._n_htf_filtered_long == 0


# ---------------------------------------------------------------------------
# Backward compatibility: require_htf_agreement=False
# ---------------------------------------------------------------------------


def test_legacy_behavior_when_htf_agreement_disabled() -> None:
    """Setting require_htf_agreement=False bypasses the gate entirely.

    Counters never tick; an opposing HTF state must NOT block fires.
    """
    s = _make_strategy(require_htf_agreement=False)
    _prime_for_long(s)
    # Force HTF state into the WRONG direction; the gate should ignore
    # it because require_htf_agreement is False.
    _set_htf_downtrend(s)

    bar = _bar(10, h=86.0, low=84.0, c=85.0, o=86.0)
    hist = [_bar(i, h=110.0 - i * 3, low=108.0 - i * 3, c=110.0 - i * 3) for i in range(6)]
    out = s.maybe_enter(bar, hist, 10_000.0, _config())
    assert out is not None, "Legacy behavior: must fire regardless of HTF"
    assert out.side == "BUY"
    assert s._n_htf_filtered_long == 0
    assert s._n_htf_filtered_short == 0


def test_short_legacy_behavior_when_htf_agreement_disabled() -> None:
    """Mirror of long-side legacy test for SHORT path."""
    s = _make_strategy(require_htf_agreement=False)
    _prime_for_short(s)
    _set_htf_uptrend(s)  # opposite of what gate would want

    bar = _bar(10, h=116.0, low=114.0, c=115.0, o=114.0)
    hist = [_bar(i, h=90.0 + i * 3, low=88.0 + i * 3, c=90.0 + i * 3) for i in range(6)]
    out = s.maybe_enter(bar, hist, 10_000.0, _config())
    assert out is not None, "Legacy SHORT must fire regardless of HTF"
    assert out.side == "SELL"
    assert s._n_htf_filtered_long == 0
    assert s._n_htf_filtered_short == 0


# ---------------------------------------------------------------------------
# HTF state machine + warmup
# ---------------------------------------------------------------------------


def test_htf_slope_returns_none_before_ema_seeds() -> None:
    """Before `htf_ema_period` synthetic 1h closes accumulate, slope=None."""
    s = _make_strategy()
    assert s._htf_slope() is None
    # Feed fewer than (htf_lookback_5m_bars * htf_ema_period) bars.
    for i in range(5):
        s._update_htf_state(100.0 + i)
    assert s._htf_slope() is None  # still warming


def test_htf_warmup_blocks_entry_until_ema_seeds() -> None:
    """When require_htf_agreement=True and HTF EMA hasn't seeded,
    entries are blocked and the counter reflects which side was gated."""
    s = _make_strategy()
    _prime_for_long(s)
    # Explicitly clear HTF state to simulate "not seeded yet".
    s._htf_ema = None
    s._htf_last_1h_close = None

    bar = _bar(10, h=86.0, low=84.0, c=85.0, o=86.0)
    hist = [_bar(i, h=110.0 - i * 3, low=108.0 - i * 3, c=110.0 - i * 3) for i in range(6)]
    out = s.maybe_enter(bar, hist, 10_000.0, _config())
    assert out is None
    assert s._n_htf_filtered_long == 1


def test_htf_state_advances_on_each_bar() -> None:
    """Each `htf_lookback_5m_bars` 5m bars yields one synthetic 1h close.

    With htf_lookback_5m_bars=12, after 12 bars the synthetic 1h close
    should be set.  After 12 * htf_ema_period (=50) bars, the EMA
    should have seeded.
    """
    s = _make_strategy()
    # Push 12 5m bars at increasing closes.
    for i in range(12):
        s._update_htf_state(100.0 + i)
    assert s._htf_last_1h_close == 111.0  # 100 + 11
    # Not enough seeds yet (need 50 synthetic-1h closes).
    assert s._htf_ema is None

    # Push 12 * 49 more bars (49 more 1h windows -> 50 total) at rising prices.
    for window in range(1, 50):
        for _ in range(12):
            s._update_htf_state(100.0 + window * 10.0)
    # EMA should have seeded.
    assert s._htf_ema is not None
    # Last 1h close is well above EMA (closes were strictly rising).
    assert s._htf_last_1h_close > s._htf_ema
    assert s._htf_slope() == 1


# ---------------------------------------------------------------------------
# stats dict exposes new counters
# ---------------------------------------------------------------------------


def test_stats_dict_exposes_htf_counters() -> None:
    """`stats` must surface the new HTF audit counters for monitoring."""
    s = _make_strategy()
    keys = set(s.stats.keys())
    assert "htf_filtered_long" in keys
    assert "htf_filtered_short" in keys

"""Tests for MtfScalpStrategy — 15m direction + 1m micro-entry scalper.

Built for the 2026-04-27 user mandate: "being futures the strategy
was supposed to scalp on the 15 minute and find entry on 1min -
micro structure".
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.mtf_scalp_strategy import (
    MtfScalpConfig,
    MtfScalpStrategy,
)


def _bar(idx_min: int, close: float = 100.0,
         high_off: float = 0.5, low_off: float = 0.5) -> BarData:
    """1-minute bar starting Jan 1 2026 09:30 ET (= 14:30 UTC)."""
    ts = datetime(2026, 1, 1, 14, 30, tzinfo=UTC) + timedelta(minutes=idx_min)
    return BarData(
        timestamp=ts, symbol="MNQ", open=close,
        high=close + high_off, low=close - low_off,
        close=close, volume=1000.0,
    )


def _cfg() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="MNQ", initial_equity=10_000.0,
        risk_per_trade_pct=0.005, confluence_threshold=0.0,
        max_trades_per_day=10,
    )


# ---------------------------------------------------------------------------
# Warmup + session gating
# ---------------------------------------------------------------------------


def test_warmup_blocks_entries() -> None:
    """During warmup, no entries fire."""
    s = MtfScalpStrategy(MtfScalpConfig(
        warmup_bars=100,
        htf_ema_period=10,
        htf_atr_period=5,
    ))
    cfg = _cfg()
    hist: list[BarData] = []
    fired = 0
    for i in range(50):
        b = _bar(i, 100.0 + i * 0.1)
        hist.append(b)
        if s.maybe_enter(b, hist, 10_000.0, cfg) is not None:
            fired += 1
    assert fired == 0


def test_session_window_blocks_off_hours() -> None:
    """Bars outside RTH window should be blocked.

    Need to populate HTF state first (htf_ema=5 → 75 1m bars).
    """
    s = MtfScalpStrategy(MtfScalpConfig(
        warmup_bars=10, htf_ema_period=5, htf_atr_period=3,
        rth_open_local=time(9, 30), rth_close_local=time(15, 55),
    ))
    cfg = _cfg()
    hist: list[BarData] = []
    # Advance state through 90 RTH bars to populate HTF EMA
    for i in range(90):
        b = _bar(i, 100.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Now feed a pre-market bar — should be blocked by session gate
    pre_market_bar = BarData(
        timestamp=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
        symbol="MNQ", open=100.0, high=101.0, low=99.0, close=100.0,
        volume=1000.0,
    )
    out = s.maybe_enter(pre_market_bar, hist, 10_000.0, cfg)
    assert out is None
    assert s.stats["session_blocks"] >= 1


def test_uptrend_with_break_fires_long() -> None:
    """Steady uptrend + 1m close > recent high → BUY."""
    s = MtfScalpStrategy(MtfScalpConfig(
        warmup_bars=20, htf_ema_period=3, htf_atr_period=2,
        ltf_recent_high_lookback=5, ltf_atr_period=5,
        min_bars_between_trades=0, max_trades_per_day=999,
        htf_atr_pct_min=0.001, htf_atr_pct_max=10.0,  # very loose
    ))
    cfg = _cfg()
    hist: list[BarData] = []
    out = None
    # Build steady uptrend over 90 bars (= 6 HTF bars)
    for i in range(90):
        c = 100.0 + i * 0.5
        # Make the bar a green close > open
        b = BarData(
            timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC) + timedelta(minutes=i),
            symbol="MNQ", open=c - 0.1, high=c + 0.5, low=c - 0.3,
            close=c, volume=1000.0,
        )
        hist.append(b)
        candidate = s.maybe_enter(b, hist, 10_000.0, cfg)
        if candidate is not None:
            out = candidate
            break
    assert out is not None
    assert out.side == "BUY"
    assert "mtf_scalp" in out.regime


def test_volatility_regime_blocks() -> None:
    """ATR-pct outside [min, max] band → blocked."""
    # Use VERY tight band that no real bar will satisfy
    s = MtfScalpStrategy(MtfScalpConfig(
        warmup_bars=20, htf_ema_period=5, htf_atr_period=3,
        ltf_recent_high_lookback=5, ltf_atr_period=5,
        min_bars_between_trades=0, max_trades_per_day=999,
        htf_atr_pct_min=10.0, htf_atr_pct_max=20.0,  # impossible band
    ))
    cfg = _cfg()
    hist: list[BarData] = []
    fired = 0
    for i in range(60):
        b = _bar(i, 100.0 + i * 0.1)
        hist.append(b)
        if s.maybe_enter(b, hist, 10_000.0, cfg) is not None:
            fired += 1
    assert fired == 0
    assert s.stats["vol_regime_blocks"] >= 1


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_stats_track_invocations() -> None:
    s = MtfScalpStrategy(MtfScalpConfig(warmup_bars=10))
    cfg = _cfg()
    hist: list[BarData] = []
    for i in range(20):
        b = _bar(i, 100.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    assert s.stats["bars_seen"] == 20


def test_htf_aggregation_synthesizes_15m_bars() -> None:
    """Every 15 1m bars should advance the HTF state once."""
    s = MtfScalpStrategy(MtfScalpConfig(
        warmup_bars=10, htf_bars_per_aggregate=15,
        htf_ema_period=5, htf_atr_period=3,
    ))
    cfg = _cfg()
    hist: list[BarData] = []
    htf_updates_before = 0
    for i in range(45):  # 3 full HTF bars
        b = _bar(i, 100.0 + i * 0.1)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # After 45 1m bars, 3 HTF aggregations happened (i=14, 29, 44)
    # Verify by reading internal state — htf_atr_window has 3 entries
    assert len(s._htf_atr_window) == 3  # noqa: SLF001
    assert htf_updates_before == 0  # silence unused-var lint


def test_directional_only_long_disabled() -> None:
    """When allow_long=False, no BUY entries fire even on uptrends."""
    s = MtfScalpStrategy(MtfScalpConfig(
        warmup_bars=20, htf_ema_period=5, htf_atr_period=3,
        ltf_recent_high_lookback=5, ltf_atr_period=5,
        min_bars_between_trades=0, max_trades_per_day=999,
        htf_atr_pct_min=0.001, htf_atr_pct_max=10.0,
        allow_long=False, allow_short=True,
    ))
    cfg = _cfg()
    hist: list[BarData] = []
    fired_long = 0
    for i in range(60):
        c = 100.0 + i * 0.5
        b = BarData(
            timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC) + timedelta(minutes=i),
            symbol="MNQ", open=c - 0.1, high=c + 0.5, low=c - 0.3,
            close=c, volume=1000.0,
        )
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None and out.side == "BUY":
            fired_long += 1
    assert fired_long == 0

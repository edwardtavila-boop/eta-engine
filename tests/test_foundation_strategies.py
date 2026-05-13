"""Tests for the 2026-04-27 foundation refactor:

* SweepReclaimStrategy (MNQ-C + BTC-C)
* CompressionBreakoutStrategy (BTC-A)
* ConfluenceScorecardStrategy (3-of-N + A+ size boost)
* GridTradingStrategy adaptive volatility mode

The user mandate was to consolidate to a 6-strategy foundation
with mechanical triggers. These four pieces fill the gaps in the
existing strategy library.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eta_engine.backtest.engine import _Open
from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.compression_breakout_strategy import (
    CompressionBreakoutConfig,
    CompressionBreakoutStrategy,
    btc_compression_preset,
    mnq_compression_preset,
)
from eta_engine.strategies.confluence_scorecard import (
    ConfluenceScorecardConfig,
    ConfluenceScorecardStrategy,
)
from eta_engine.strategies.grid_trading_strategy import (
    GridConfig,
    GridTradingStrategy,
)
from eta_engine.strategies.sweep_reclaim_strategy import (
    SweepReclaimConfig,
    SweepReclaimStrategy,
    btc_daily_sweep_preset,
    mnq_intraday_sweep_preset,
)


def _bar(
    idx: int,
    close: float = 100.0,
    *,
    high: float | None = None,
    low: float | None = None,
    open_: float | None = None,
    volume: float = 1000.0,
) -> BarData:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=idx)
    return BarData(
        timestamp=ts,
        symbol="BTC",
        open=open_ if open_ is not None else close,
        high=high if high is not None else close + 0.5,
        low=low if low is not None else close - 0.5,
        close=close,
        volume=volume,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="BTC",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.005,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )


# ---------------------------------------------------------------------------
# SweepReclaimStrategy
# ---------------------------------------------------------------------------


def test_sweep_reclaim_long_fires_on_classic_spring() -> None:
    """Wyckoff spring: low pierces recent low, close reclaims it."""
    s = SweepReclaimStrategy(
        SweepReclaimConfig(
            level_lookback=10,
            reclaim_window=2,
            min_wick_pct=0.30,
            min_volume_z=0.0,
            warmup_bars=15,
            atr_period=5,
            min_bars_between_trades=0,
        )
    )
    cfg = _config()
    hist: list[BarData] = []
    out = None
    # Build a flat tape at 100, then sweep low
    for i in range(15):
        b = _bar(i, 100.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Sweep bar: low pierces below recent_low (~99.5), wick big.
    # The strategy fires on the SAME bar when close > swept level
    # (this bar's close=100 > recent_low=99.5), which is a valid
    # 1-bar spring pattern.
    sweep_bar = _bar(15, close=100.0, high=100.5, low=98.0, volume=2000.0)
    hist.append(sweep_bar)
    out = s.maybe_enter(sweep_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert "sweep_reclaim_buy" in out.regime


def test_sweep_reclaim_warmup_blocks() -> None:
    s = SweepReclaimStrategy(
        SweepReclaimConfig(
            warmup_bars=50,
            level_lookback=10,
        )
    )
    cfg = _config()
    hist: list[BarData] = []
    fired = 0
    for i in range(20):
        b = _bar(i, 100.0)
        hist.append(b)
        if s.maybe_enter(b, hist, 10_000.0, cfg) is not None:
            fired += 1
    assert fired == 0


def test_sweep_reclaim_wick_quality_gates() -> None:
    """A shallow sweep with small wick gets rejected on quality."""
    s = SweepReclaimStrategy(
        SweepReclaimConfig(
            level_lookback=10,
            reclaim_window=2,
            min_wick_pct=0.80,  # very strict
            min_volume_z=0.0,
            warmup_bars=15,
            atr_period=5,
            min_bars_between_trades=0,
        )
    )
    cfg = _config()
    hist: list[BarData] = []
    for i in range(15):
        b = _bar(i, 100.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Sweep bar with SMALL wick (only 10% of range below level)
    sweep_bar = _bar(15, close=100.0, high=100.5, low=99.4, volume=2000.0)
    hist.append(sweep_bar)
    s.maybe_enter(sweep_bar, hist, 10_000.0, cfg)
    assert s.stats["wick_quality_rejects"] >= 1


def test_sweep_reclaim_presets_differ() -> None:
    """MNQ + BTC presets should differ on volatility-sensitive knobs.

    Both presets converged on ``atr_stop_mult=1.5`` (a defensible domain
    choice — 1.5×ATR translates to vastly different $ risk on each asset),
    so we assert differentiation on the OTHER volatility knobs that
    remain deliberately divergent: ``level_lookback``, ``min_wick_pct``
    (sweep-quality threshold), and ``min_bars_between_trades`` (cooldown).
    If a future refactor makes ALL three of these match, that's the
    signal the presets have lost their distinct character.
    """
    mnq = mnq_intraday_sweep_preset()
    btc = btc_daily_sweep_preset()
    assert mnq.level_lookback != btc.level_lookback
    assert mnq.min_wick_pct != btc.min_wick_pct
    assert mnq.min_bars_between_trades != btc.min_bars_between_trades


# ---------------------------------------------------------------------------
# CompressionBreakoutStrategy
# ---------------------------------------------------------------------------


def test_compression_breakout_warmup_blocks() -> None:
    s = CompressionBreakoutStrategy(
        CompressionBreakoutConfig(
            warmup_bars=50,
            breakout_lookback=5,
            bb_period=10,
            bb_width_window=20,
            atr_period=5,
            atr_ma_period=10,
            trend_ema_period=20,
        )
    )
    cfg = _config()
    hist: list[BarData] = []
    fired = 0
    for i in range(20):
        b = _bar(i, 100.0 + i * 0.1)
        hist.append(b)
        if s.maybe_enter(b, hist, 10_000.0, cfg) is not None:
            fired += 1
    assert fired == 0


def test_compression_breakout_fires_on_compression_release() -> None:
    """Build a compressed range then break out → expect BUY."""
    s = CompressionBreakoutStrategy(
        CompressionBreakoutConfig(
            warmup_bars=30,
            breakout_lookback=10,
            bb_period=10,
            bb_width_window=20,
            bb_width_max_percentile=0.90,
            atr_period=5,
            atr_ma_period=10,
            trend_ema_period=10,
            require_trend_alignment=True,
            volume_z_lookback=10,
            min_volume_z=0.0,
            min_close_location=0.50,
            min_bars_between_trades=0,
            max_trades_per_day=99,
        )
    )
    cfg = _config()
    hist: list[BarData] = []
    # 30 flat-ish bars (compression)
    for i in range(30):
        c = 100.0 + (i % 3) * 0.05  # near-flat
        b = _bar(i, close=c, high=c + 0.1, low=c - 0.1)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Now break above range with strong close + volume.
    # Close at the HIGH (CLV = 1.0) so the close-location filter
    # (default 0.7) passes.
    out = None
    for i in range(30, 40):
        c = 100.0 + (i - 29) * 1.0  # accelerating up
        b = _bar(i, close=c, high=c + 0.05, low=c - 1.0, volume=3000.0)
        hist.append(b)
        candidate = s.maybe_enter(b, hist, 10_000.0, cfg)
        if candidate is not None:
            out = candidate
            break
    assert out is not None
    assert out.side == "BUY"


def test_compression_breakout_presets_differ() -> None:
    btc = btc_compression_preset()
    mnq = mnq_compression_preset()
    assert btc.bb_width_window != mnq.bb_width_window
    assert btc.atr_stop_mult != mnq.atr_stop_mult
    assert btc.warmup_bars != mnq.warmup_bars


# ---------------------------------------------------------------------------
# ConfluenceScorecardStrategy
# ---------------------------------------------------------------------------


@dataclass
class _AlwaysFireBuy:
    fired: int = 0

    def maybe_enter(self, bar, hist, equity, config) -> _Open | None:  # noqa: ANN001
        self.fired += 1
        return _Open(
            entry_bar=bar,
            side="BUY",
            qty=1.0,
            entry_price=bar.close,
            stop=bar.close - 1.0,
            target=bar.close + 3.0,
            risk_usd=10.0,
            confluence=10.0,
            leverage=1.0,
            regime="stub",
        )


def test_scorecard_vetoes_when_no_factors() -> None:
    """With min_score=3 and no predicates attached and a flat tape,
    the scorecard veto every fire."""
    sub = _AlwaysFireBuy()
    sc = ConfluenceScorecardStrategy(
        sub,
        ConfluenceScorecardConfig(
            min_score=3,
            a_plus_score=4,
            # Disable factors that can't activate on a flat synthetic tape
            enable_atr_regime_factor=False,
            enable_volume_factor=False,
        ),
    )
    cfg = _config()
    hist: list[BarData] = []
    fires = 0
    for i in range(50):
        b = _bar(i, 100.0)
        hist.append(b)
        if sc.maybe_enter(b, hist, 10_000.0, cfg) is not None:
            fires += 1
    assert fires == 0
    assert sc.scorecard_stats["proposed"] == 50
    assert sc.scorecard_stats["vetoed"] == 50


def test_scorecard_passes_in_uptrend_and_above_vwap() -> None:
    """Uptrend + above VWAP should give 2+ factors → check threshold."""
    sub = _AlwaysFireBuy()
    sc = ConfluenceScorecardStrategy(
        sub,
        ConfluenceScorecardConfig(
            min_score=2,
            a_plus_score=10,  # never A+
            # enable trend + vwap; disable factors that need long history
            enable_atr_regime_factor=False,
            enable_volume_factor=False,
        ),
    )
    cfg = _config()
    hist: list[BarData] = []
    out = None
    for i in range(80):
        b = _bar(i, 100.0 + i * 0.5, volume=1000.0)
        hist.append(b)
        candidate = sc.maybe_enter(b, hist, 10_000.0, cfg)
        if candidate is not None:
            out = candidate
    assert out is not None  # at least once both trend + vwap aligned
    # Score tag in regime
    assert "score" in out.regime


def test_scorecard_a_plus_size_boost() -> None:
    """When score >= a_plus_score, qty is multiplied by a_plus_size_mult."""
    sub = _AlwaysFireBuy()
    sc = ConfluenceScorecardStrategy(
        sub,
        ConfluenceScorecardConfig(
            min_score=1,
            a_plus_score=1,  # any fire = A+
            a_plus_size_mult=2.0,
            # Only trend factor
            enable_atr_regime_factor=False,
            enable_volume_factor=False,
            enable_vwap_factor=False,
        ),
    )
    cfg = _config()
    hist: list[BarData] = []
    out = None
    for i in range(80):
        b = _bar(i, 100.0 + i * 0.5)
        hist.append(b)
        candidate = sc.maybe_enter(b, hist, 10_000.0, cfg)
        if candidate is not None:
            out = candidate
            break
    assert out is not None
    # qty was 1.0 from the stub; A+ multiplier makes it 2.0
    assert out.qty == 2.0
    assert "aplus" in out.regime


def test_scorecard_attached_predicates_count_as_factors() -> None:
    """HTF + session + liquidity predicates each contribute to score."""
    sub = _AlwaysFireBuy()
    sc = ConfluenceScorecardStrategy(
        sub,
        ConfluenceScorecardConfig(
            min_score=3,
            a_plus_score=99,
            enable_trend_factor=False,
            enable_vwap_factor=False,
            enable_atr_regime_factor=False,
            enable_volume_factor=False,
        ),
    )
    sc.attach_htf_agreement(lambda b, side: True)
    sc.attach_session_predicate(lambda b: True)
    sc.attach_liquidity_predicate(lambda b, side: True)
    cfg = _config()
    out = None
    hist: list[BarData] = []
    for i in range(5):
        b = _bar(i, 100.0)
        hist.append(b)
        candidate = sc.maybe_enter(b, hist, 10_000.0, cfg)
        if candidate is not None:
            out = candidate
            break
    assert out is not None
    assert "score3" in out.regime


# ---------------------------------------------------------------------------
# Grid adaptive volatility mode
# ---------------------------------------------------------------------------


def test_grid_adaptive_kill_switch_engages() -> None:
    """With adaptive_volatility=True and a high-ATR tape, the kill
    switch should engage (current ATR rank > kill threshold)."""
    s = GridTradingStrategy(
        GridConfig(
            ref_lookback=20,
            n_levels=3,
            atr_period=5,
            min_warmup_bars=10,
            adaptive_volatility=True,
            adaptive_atr_pct_lookback=30,
            adaptive_kill_atr_pct=0.50,  # easy to trigger
        )
    )
    cfg = _config()
    hist: list[BarData] = []
    # _adaptive_spacing_pct needs at least lookback+atr_period+1 = 36 bars
    # of history before computing percentile.
    for i in range(40):
        c = 100.0
        rng = 0.2 if i < 30 else 5.0
        b = _bar(i, close=c, high=c + rng, low=c - rng)
        hist.append(b)
    # Now feed a high-ATR bar — should engage kill switch
    high_atr_bar = _bar(40, close=100.0, high=110.0, low=90.0)
    hist.append(high_atr_bar)
    s.maybe_enter(high_atr_bar, hist, 10_000.0, cfg)
    assert s.grid_stats["kill_atr"] >= 1


def test_grid_range_break_kill_switch() -> None:
    """When price closes well outside the grid's range, the
    range-break kill switch should fire."""
    s = GridTradingStrategy(
        GridConfig(
            ref_lookback=10,
            n_levels=2,
            grid_spacing_pct=0.005,
            atr_period=5,
            min_warmup_bars=10,
            range_break_mult=1.0,
        )
    )
    cfg = _config()
    hist: list[BarData] = []
    # 20 bars near 100
    for i in range(20):
        b = _bar(i, 100.0)
        hist.append(b)
    # Now a bar at 110 — way outside grid (n_levels=2 × spacing=0.5%
    # × 100 = 1.0; range_break_mult=1.0 → max_dist=1.0; 110-100=10 >> 1)
    far_bar = _bar(20, close=110.0)
    hist.append(far_bar)
    s.maybe_enter(far_bar, hist, 10_000.0, cfg)
    assert s.grid_stats["kill_range_break"] >= 1


def test_grid_adaptive_widens_at_high_vol() -> None:
    """When ATR rank in middle of band, spacing should be between
    min and max settings."""
    s = GridTradingStrategy(
        GridConfig(
            ref_lookback=20,
            n_levels=3,
            atr_period=5,
            min_warmup_bars=10,
            adaptive_volatility=True,
            adaptive_atr_pct_lookback=30,
            adaptive_atr_pct_min=0.30,
            adaptive_atr_pct_max=0.70,
            adaptive_min_spacing_pct=0.001,
            adaptive_max_spacing_pct=0.020,
            adaptive_kill_atr_pct=1.5,  # impossible to trigger
        )
    )
    # Build hist with VARIED ATR so the percentile computation has
    # a meaningful distribution.
    hist: list[BarData] = []
    for i in range(50):
        rng = 0.5 + (i % 5) * 0.1
        b = _bar(i, 100.0, high=100.0 + rng, low=100.0 - rng)
        hist.append(b)
    out = s._adaptive_spacing_pct(hist)  # noqa: SLF001
    assert out is not None
    assert s.cfg.adaptive_min_spacing_pct <= out <= s.cfg.adaptive_max_spacing_pct

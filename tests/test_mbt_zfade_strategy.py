"""Tests for strategies.mbt_zfade_strategy.

Coverage:
* Strategy imports + constructs cleanly.
* Preset matches EDA-derived parameters.
* Pre-warmup bars yield no signal.
* Z below threshold yields no signal.
* Z above threshold + HTF AGREES (no opposition) -> filtered out.
* Z above threshold + HTF OPPOSES -> SHORT fires.
* Negative z + HTF opposes -> LONG fires (symmetric path).
* Time-stop bookkeeping clears at +time_stop_bars after a fire.
* Day rollover resets max_trades_per_day counter.
* qty math uses MBT point_value=0.10 (sizing sanity).
* Out-of-session bars are gated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.mbt_zfade_strategy import (
    MBTZFadeConfig,
    MBTZFadeStrategy,
    mbt_zfade_preset,
)

_CT = ZoneInfo("America/Chicago")


def _bar(
    ts: datetime,
    *,
    high: float,
    low: float,
    open_: float | None = None,
    close: float | None = None,
    volume: float = 1000.0,
) -> BarData:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_CT)
    utc_dt = ts.astimezone(UTC)
    o = open_ if open_ is not None else (high + low) / 2
    c = close if close is not None else (high + low) / 2
    return BarData(
        timestamp=utc_dt,
        symbol="MBT",
        open=o,
        high=high,
        low=low,
        close=c,
        volume=volume,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="MBT",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.005,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _permissive_cfg(**overrides: object) -> MBTZFadeConfig:
    """Test config: small windows, short warmup, lower thresholds."""
    base: dict[str, object] = {
        "proxy_lookback": 6,
        "entry_z": 1.0,
        "htf_trend_lookback_5m_bars": 4,
        "htf_ema_period": 3,
        "atr_period": 5,
        "warmup_bars": 8,
        "min_bars_between_trades": 0,
        "max_trades_per_day": 5,
        "time_stop_bars": 4,
    }
    base.update(overrides)
    return MBTZFadeConfig(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Imports + construction
# ---------------------------------------------------------------------------


def test_strategy_imports_and_initializes() -> None:
    s = MBTZFadeStrategy()
    assert s.cfg.allow_long is True
    assert s.cfg.allow_short is True
    assert s.stats["entries_fired"] == 0
    assert s.stats["time_stop_armed"] == 0


def test_preset_matches_eda_derivation() -> None:
    """Preset must reflect the EDA-derived params per the spec.

    z>=2.5 was the threshold where net edge cleared friction.
    rr_target=1.5 because edge reverts in ~20 min (4 bars).
    time_stop_bars=4 by spec.
    max_trades_per_day=3 by spec (was 2 in legacy).
    htf_trend_lookback_5m_bars=12 = 1h synthetic.
    """
    cfg = mbt_zfade_preset()
    assert isinstance(cfg, MBTZFadeConfig)
    assert cfg.entry_z == 2.5
    assert cfg.rr_target == 1.5
    assert cfg.atr_stop_mult == 1.0
    assert cfg.time_stop_bars == 4
    assert cfg.max_trades_per_day == 3
    assert cfg.htf_trend_lookback_5m_bars == 12
    assert cfg.htf_ema_period == 20
    assert cfg.proxy_lookback == 24
    assert cfg.warmup_bars == 50
    assert cfg.risk_per_trade_pct == 0.005
    s = MBTZFadeStrategy(cfg)
    assert s.stats["bars_seen"] == 0


# ---------------------------------------------------------------------------
# No-signal cases
# ---------------------------------------------------------------------------


def test_pre_warmup_yields_no_signal() -> None:
    """Bars below warmup_bars threshold yield no signal."""
    s = MBTZFadeStrategy(_permissive_cfg(warmup_bars=20))
    cfg = _config()
    base = datetime(2026, 6, 15, 10, 0, tzinfo=_CT)
    hist: list[BarData] = []
    for i in range(5):
        ts = base + timedelta(minutes=i * 5)
        bar = _bar(ts, high=60_000.0, low=59_900.0, close=59_950.0)
        out = s.maybe_enter(bar, hist, 10_000.0, cfg)
        assert out is None
        hist.append(bar)


def test_z_below_threshold_yields_no_signal() -> None:
    """Quiet tape, no z-spike -> no fire."""
    cfg = _permissive_cfg(proxy_lookback=6, entry_z=2.0, warmup_bars=8)
    s = MBTZFadeStrategy(cfg)
    bcfg = _config()

    base = datetime(2026, 6, 15, 10, 0, tzinfo=_CT)
    hist: list[BarData] = []
    for i in range(20):
        ts = base + timedelta(minutes=i * 5)
        # Flat tape, no proxy spike
        bar = _bar(ts, high=60_000.0, low=59_950.0, close=59_975.0, volume=1000.0)
        out = s.maybe_enter(bar, hist, 10_000.0, bcfg)
        assert out is None
        hist.append(bar)


def test_out_of_session_blocked() -> None:
    """Bar timestamped pre-RTH is rejected even if z and HTF qualify."""
    s = MBTZFadeStrategy(_permissive_cfg(warmup_bars=2, proxy_lookback=3))
    cfg = _config()
    base = datetime(2026, 6, 15, 3, 0, tzinfo=_CT)
    hist: list[BarData] = []
    for i in range(5):
        ts = base + timedelta(minutes=i * 5)
        bar = _bar(ts, high=60_000.0, low=59_900.0, close=59_950.0)
        s.maybe_enter(bar, hist, 10_000.0, cfg)
        hist.append(bar)
    # Spike bar at pre-RTH time (still 03:xx CT)
    spike_ts = base + timedelta(minutes=30)
    spike = _bar(spike_ts, high=60_500.0, low=60_400.0, close=60_450.0)
    assert s.maybe_enter(spike, hist, 10_000.0, cfg) is None
    assert s._n_session_rejects > 0


# ---------------------------------------------------------------------------
# HTF trend filter -- the structural improvement
# ---------------------------------------------------------------------------


def _build_z_spike_session(
    s: MBTZFadeStrategy,
    bcfg: BacktestConfig,
    proxies_pre: list[float],
    spike_proxy: float,
    *,
    htf_drift_per_bar: float = 0.0,
    base_close: float = 60_000.0,
) -> tuple[list[BarData], BarData]:
    """Build a hist of calm bars + a spike bar.

    htf_drift_per_bar lets us inject an HTF up- or down-drift into the
    closes so the synthetic-1h EMA tilts in a controllable direction.
    Returns (hist_before_spike, spike_bar).
    """
    base_ts = datetime(2026, 6, 15, 10, 0, tzinfo=_CT)
    pi = iter(proxies_pre + [spike_proxy])

    def _provider(_b: BarData) -> float | None:
        return next(pi, 0.0)

    s._basis_provider = _provider  # noqa: SLF001 -- test-only override

    hist: list[BarData] = []
    n_pre = len(proxies_pre)
    for i in range(n_pre):
        ts = base_ts + timedelta(minutes=i * 5)
        cl = base_close + i * htf_drift_per_bar
        bar = _bar(
            ts,
            high=cl + 50.0,
            low=cl - 50.0,
            close=cl,
            volume=1000.0,
        )
        s.maybe_enter(bar, hist, 10_000.0, bcfg)
        hist.append(bar)
    spike_ts = base_ts + timedelta(minutes=n_pre * 5)
    cl = base_close + n_pre * htf_drift_per_bar
    spike = _bar(
        spike_ts,
        high=cl + 50.0,
        low=cl - 50.0,
        close=cl,
        volume=1000.0,
    )
    return hist, spike


def test_z_spike_with_htf_agreeing_is_filtered() -> None:
    """z >= entry_z with 1h slope ALSO up -> filter blocks the fade.

    This is the spec's headline filter: don't fade z-spikes that are
    the start of a real trend.
    """
    cfg = _permissive_cfg(
        proxy_lookback=6,
        entry_z=1.0,
        htf_trend_lookback_5m_bars=4,
        htf_ema_period=3,
        warmup_bars=8,
        atr_period=5,
        require_htf_opposition=True,
    )
    s = MBTZFadeStrategy(cfg)
    bcfg = _config()

    proxies_pre = [0.0] * 9  # quiet baseline (9 calm bars)
    # Strong upward drift in closes -> 1h EMA slope is positive.
    hist, spike = _build_z_spike_session(
        s,
        bcfg,
        proxies_pre,
        spike_proxy=50.0,
        htf_drift_per_bar=20.0,  # +20 per bar -> rising HTF
    )
    out = s.maybe_enter(spike, hist, 10_000.0, bcfg)
    assert out is None, f"HTF should have blocked fade; stats={s.stats}"
    # The strategy should have triggered z but rejected on HTF.
    assert s._n_z_triggers >= 1
    assert s._n_htf_rejects >= 1


def test_z_spike_with_htf_opposing_fires_short() -> None:
    """z >= entry_z and 1h slope DOWN -> SHORT fade fires."""
    cfg = _permissive_cfg(
        proxy_lookback=6,
        entry_z=1.0,
        htf_trend_lookback_5m_bars=4,
        htf_ema_period=3,
        warmup_bars=8,
        atr_period=5,
        require_htf_opposition=True,
    )
    s = MBTZFadeStrategy(cfg)
    bcfg = _config()

    proxies_pre = [0.0] * 9  # quiet baseline
    # Strong DOWNWARD drift -> 1h EMA slope is negative.
    hist, spike = _build_z_spike_session(
        s,
        bcfg,
        proxies_pre,
        spike_proxy=50.0,
        htf_drift_per_bar=-20.0,
    )
    out = s.maybe_enter(spike, hist, 10_000.0, bcfg)
    assert out is not None, f"expected SHORT fade; stats={s.stats}"
    assert out.side == "SELL"
    assert out.stop > out.entry_price
    assert out.target < out.entry_price


def test_negative_z_with_htf_opposing_fires_long() -> None:
    """z <= -entry_z and 1h slope UP -> LONG fade fires."""
    cfg = _permissive_cfg(
        proxy_lookback=6,
        entry_z=1.0,
        htf_trend_lookback_5m_bars=4,
        htf_ema_period=3,
        warmup_bars=8,
        atr_period=5,
        require_htf_opposition=True,
    )
    s = MBTZFadeStrategy(cfg)
    bcfg = _config()

    proxies_pre = [0.0] * 9
    # Upward drift -> HTF slope positive -> opposes a NEGATIVE spike.
    hist, spike = _build_z_spike_session(
        s,
        bcfg,
        proxies_pre,
        spike_proxy=-50.0,
        htf_drift_per_bar=+20.0,
    )
    out = s.maybe_enter(spike, hist, 10_000.0, bcfg)
    assert out is not None, f"expected LONG fade; stats={s.stats}"
    assert out.side == "BUY"
    assert out.stop < out.entry_price
    assert out.target > out.entry_price


# ---------------------------------------------------------------------------
# Time-stop + day rollover + sizing
# ---------------------------------------------------------------------------


def test_time_stop_bookkeeping_clears_after_n_bars() -> None:
    """After a fire, _open_entry_bar_idx is set; after time_stop_bars
    additional bars, the bookkeeping clears."""
    cfg = _permissive_cfg(
        proxy_lookback=6,
        entry_z=1.0,
        htf_trend_lookback_5m_bars=4,
        htf_ema_period=3,
        warmup_bars=8,
        atr_period=5,
        time_stop_bars=4,
        require_htf_opposition=True,
        min_bars_between_trades=100,  # block re-entry to keep test clean
    )
    s = MBTZFadeStrategy(cfg)
    bcfg = _config()

    proxies_pre = [0.0] * 9
    hist, spike = _build_z_spike_session(
        s,
        bcfg,
        proxies_pre,
        spike_proxy=50.0,
        htf_drift_per_bar=-20.0,
    )
    out = s.maybe_enter(spike, hist, 10_000.0, bcfg)
    assert out is not None
    assert s.stats["time_stop_armed"] == 1
    hist.append(spike)

    # Push 4 more bars (== time_stop_bars). Bookkeeping should clear.
    last_ts = spike.timestamp
    last_close = spike.close
    for i in range(cfg.time_stop_bars):
        ts = last_ts + timedelta(minutes=(i + 1) * 5)
        bar = _bar(
            ts.astimezone(_CT),
            high=last_close + 50,
            low=last_close - 50,
            close=last_close,
            volume=1000.0,
        )
        s.maybe_enter(bar, hist, 10_000.0, bcfg)
        hist.append(bar)
    assert s.stats["time_stop_armed"] == 0


def test_day_rollover_resets_trades_today() -> None:
    """A new RTH session clears the trades_today counter."""
    cfg = _permissive_cfg(
        proxy_lookback=6,
        entry_z=1.0,
        htf_trend_lookback_5m_bars=4,
        htf_ema_period=3,
        warmup_bars=8,
        atr_period=5,
        max_trades_per_day=1,
        require_htf_opposition=True,
    )
    s = MBTZFadeStrategy(cfg)
    bcfg = _config()

    proxies_pre = [0.0] * 9
    hist, spike = _build_z_spike_session(
        s,
        bcfg,
        proxies_pre,
        spike_proxy=50.0,
        htf_drift_per_bar=-20.0,
    )
    out = s.maybe_enter(spike, hist, 10_000.0, bcfg)
    assert out is not None
    assert s._trades_today == 1
    hist.append(spike)

    # Roll to next day at 10:00 CT -- _last_day != bar_date triggers reset.
    next_day_ts = datetime(2026, 6, 16, 10, 0, tzinfo=_CT)
    bar_next = _bar(
        next_day_ts,
        high=60_050,
        low=59_950,
        close=60_000,
        volume=1000.0,
    )
    s.maybe_enter(bar_next, hist, 10_000.0, bcfg)
    assert s._trades_today == 0


def test_qty_uses_point_value_010_for_sizing() -> None:
    """Qty must satisfy: qty * stop_dist * point_value ~ risk_usd.

    With point_value=0.10 and 0.5% risk on $10k (= $50), and a stop
    distance roughly equal to the test ATR, qty should land in the
    "tens of contracts" range. Without the point_value multiplier
    qty would be 10x smaller -- this test catches that regression.
    """
    cfg = _permissive_cfg(
        proxy_lookback=6,
        entry_z=1.0,
        htf_trend_lookback_5m_bars=4,
        htf_ema_period=3,
        warmup_bars=8,
        atr_period=5,
        risk_per_trade_pct=0.005,
        require_htf_opposition=True,
    )
    s = MBTZFadeStrategy(cfg)
    bcfg = _config()

    proxies_pre = [0.0] * 9
    hist, spike = _build_z_spike_session(
        s,
        bcfg,
        proxies_pre,
        spike_proxy=50.0,
        htf_drift_per_bar=-20.0,
    )
    out = s.maybe_enter(spike, hist, 10_000.0, bcfg)
    assert out is not None
    # Recompute the same qty math from public state. ATR over the
    # last 5 bars of synthetic hist with high-low=100 each = 100.
    # stop_dist = 1.0 * 100 = 100. risk = 0.005 * 10_000 = $50.
    # qty = 50 / (100 * 0.10) = 5 contracts.
    expected_qty = 50.0 / (100.0 * 0.10)
    assert abs(out.qty - expected_qty) < 0.01, f"qty math wrong; got {out.qty}, expected ~{expected_qty}"

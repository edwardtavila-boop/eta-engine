"""Tests for strategies.mbt_funding_basis_strategy.

Coverage:
* Strategy imports + constructs cleanly.
* Empty bar stream produces no signal (warmup gate).
* Synthetic "rich premium + fading momentum" produces a SHORT signal.
* Out-of-RTH bars are gated.
* Preset constructs cleanly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.mbt_funding_basis_strategy import (
    MBTFundingBasisConfig,
    MBTFundingBasisStrategy,
    mbt_funding_basis_preset,
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
        timestamp=utc_dt, symbol="MBT", open=o, high=high, low=low,
        close=c, volume=volume,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="MBT", initial_equity=10_000.0,
        risk_per_trade_pct=0.005, confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _permissive_cfg(**overrides: object) -> MBTFundingBasisConfig:
    """Test config: short warmup, lower thresholds, longer max trades."""
    base: dict[str, object] = {
        "basis_lookback": 6,
        "entry_z": 1.0,
        "momentum_lookback": 2,
        "atr_period": 5,
        "warmup_bars": 8,
        "min_bars_between_trades": 0,
        "max_trades_per_day": 5,
    }
    base.update(overrides)
    return MBTFundingBasisConfig(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Imports + construction
# ---------------------------------------------------------------------------


def test_strategy_imports_and_initializes() -> None:
    s = MBTFundingBasisStrategy()
    assert s.cfg.allow_short is True
    assert s.cfg.allow_long is False
    assert s.stats["entries_fired"] == 0


def test_preset_constructs_cleanly() -> None:
    cfg = mbt_funding_basis_preset()
    assert isinstance(cfg, MBTFundingBasisConfig)
    s = MBTFundingBasisStrategy(cfg)
    assert s.stats["bars_seen"] == 0


# ---------------------------------------------------------------------------
# No signal cases
# ---------------------------------------------------------------------------


def test_empty_bars_produce_no_signal() -> None:
    """First bar with no history shouldn't fire (warmup gate)."""
    s = MBTFundingBasisStrategy(_permissive_cfg())
    cfg = _config()
    bar = _bar(datetime(2026, 6, 15, 10, 0), high=60_000.0, low=59_900.0,
               close=59_950.0)
    assert s.maybe_enter(bar, [], 10_000.0, cfg) is None


def test_short_warmup_produces_no_signal() -> None:
    """Bars below warmup_bars threshold yield no signal."""
    s = MBTFundingBasisStrategy(_permissive_cfg(warmup_bars=20))
    cfg = _config()
    base = datetime(2026, 6, 15, 9, 0, tzinfo=_CT)
    hist: list[BarData] = []
    for i in range(5):
        ts = base + timedelta(minutes=i * 5)
        bar = _bar(ts, high=60_000.0, low=59_900.0, close=59_950.0)
        out = s.maybe_enter(bar, hist, 10_000.0, cfg)
        assert out is None
        hist.append(bar)


def test_out_of_session_blocked() -> None:
    """Bar timestamped pre-RTH is rejected even if z-score qualifies."""
    s = MBTFundingBasisStrategy(_permissive_cfg(warmup_bars=2, basis_lookback=3))
    cfg = _config()
    # Pre-RTH = 03:00 CT -> blocked
    base = datetime(2026, 6, 15, 3, 0, tzinfo=_CT)
    hist: list[BarData] = []
    # Build basis history with low values
    for i in range(5):
        ts = base + timedelta(minutes=i * 5)
        bar = _bar(ts, high=60_000.0, low=59_900.0, close=59_950.0)
        s.maybe_enter(bar, hist, 10_000.0, cfg)
        hist.append(bar)
    # Spike bar at pre-RTH time
    spike_ts = base + timedelta(minutes=30)
    spike = _bar(spike_ts, high=60_500.0, low=60_400.0, close=60_450.0)
    assert s.maybe_enter(spike, hist, 10_000.0, cfg) is None
    assert s._n_session_rejects > 0


# ---------------------------------------------------------------------------
# Signal case
# ---------------------------------------------------------------------------


def test_rich_premium_with_fading_momentum_fires_short() -> None:
    """Build a basis-proxy spike with declining-highs momentum and verify
    a SHORT signal fires."""
    cfg = _permissive_cfg(
        basis_lookback=6, entry_z=0.5, momentum_lookback=2,
        warmup_bars=8, atr_period=5,
    )
    bcfg = _config()

    # Use an explicit basis_provider so we control the proxy precisely.
    # 7 calm bars (proxy=0), then a mid-spike (10) and a hot-spike (50).
    proxies = [0.0] * 7 + [10.0, 50.0]
    pi = iter(proxies)

    def _provider(_b: BarData) -> float | None:
        return next(pi, 0.0)

    s2 = MBTFundingBasisStrategy(cfg, basis_provider=_provider)
    base = datetime(2026, 6, 15, 10, 0, tzinfo=_CT)  # RTH (08:30-15:00 CT)
    hist2: list[BarData] = []
    for i in range(7):
        ts = base + timedelta(minutes=i * 5)
        bar = _bar(ts, high=60_000.0, low=59_900.0, close=59_950.0,
                   volume=1000.0)
        s2.maybe_enter(bar, hist2, 10_000.0, bcfg)
        hist2.append(bar)
    # Bar 8: lower high
    bar8_ts = base + timedelta(minutes=7 * 5)
    bar8 = _bar(bar8_ts, high=59_980.0, low=59_880.0, close=59_920.0,
                volume=1000.0)
    s2.maybe_enter(bar8, hist2, 10_000.0, bcfg)
    hist2.append(bar8)

    # Bar 9: spike basis + lower high again — should fire SHORT
    bar9_ts = base + timedelta(minutes=8 * 5)
    bar9 = _bar(bar9_ts, high=59_960.0, low=59_870.0, close=59_900.0,
                volume=1000.0)
    out = s2.maybe_enter(bar9, hist2, 10_000.0, bcfg)
    assert out is not None, (
        f"expected SHORT fire on rich-basis bar; stats={s2.stats}"
    )
    assert out.side == "SELL"
    assert out.entry_price == 59_900.0
    # Stop above entry, target below entry
    assert out.stop > out.entry_price
    assert out.target < out.entry_price


def test_no_signal_when_no_z_score_breach() -> None:
    """Quiet basis with no z-spike yields no signal even after warmup."""
    cfg = _permissive_cfg(basis_lookback=6, entry_z=2.0, warmup_bars=8)
    s = MBTFundingBasisStrategy(cfg)
    bcfg = _config()

    base = datetime(2026, 6, 15, 10, 0, tzinfo=_CT)
    hist: list[BarData] = []
    for i in range(20):
        ts = base + timedelta(minutes=i * 5)
        # Bars are flat, no basis spike
        bar = _bar(ts, high=60_000.0, low=59_950.0, close=59_975.0,
                   volume=1000.0)
        out = s.maybe_enter(bar, hist, 10_000.0, bcfg)
        assert out is None
        hist.append(bar)

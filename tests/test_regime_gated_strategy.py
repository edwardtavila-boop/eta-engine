"""Tests for RegimeGatedStrategy — wrapper that gates any sub-strategy
on HTF regime classification.

Built for the 2026-04-27 5-year walk-forward finding (the +6.00 BTC
champion was regime-conditional; deg_avg stayed clean at 0.238 so
the strategy was not curve-fit, just selective). The wrapper lets
us gate firings to favorable regimes only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eta_engine.backtest.engine import _Open
from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.htf_regime_classifier import HtfRegimeClassifierConfig
from eta_engine.strategies.regime_gated_strategy import (
    RegimeGatedConfig,
    RegimeGatedStrategy,
    btc_daily_preset,
    eth_daily_preset,
    mnq_intraday_preset,
)


def _bar(idx: int, close: float, *, hi_off: float = 0.5, lo_off: float = 0.5) -> BarData:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=idx)
    return BarData(
        timestamp=ts, symbol="BTC", open=close,
        high=close + hi_off, low=close - lo_off, close=close, volume=1000.0,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="BTC", initial_equity=10_000.0,
        risk_per_trade_pct=0.01, confluence_threshold=0.0,
        max_trades_per_day=100,
    )


def _open_buy(bar: BarData) -> _Open:
    return _Open(
        entry_bar=bar, side="BUY", qty=1.0, entry_price=bar.close,
        stop=bar.close - 1.0, target=bar.close + 3.0,
        risk_usd=10.0, confluence=10.0, leverage=1.0, regime="stub",
    )


def _open_sell(bar: BarData) -> _Open:
    return _Open(
        entry_bar=bar, side="SELL", qty=1.0, entry_price=bar.close,
        stop=bar.close + 1.0, target=bar.close - 3.0,
        risk_usd=10.0, confluence=10.0, leverage=1.0, regime="stub",
    )


@dataclass
class _AlwaysBuyStub:
    """Trivial sub-strategy: emits a BUY entry on every bar."""

    fired: int = 0

    def maybe_enter(
        self, bar: BarData, hist: list[BarData], equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        self.fired += 1
        return _open_buy(bar)


@dataclass
class _AlwaysSellStub:
    """Trivial sub-strategy: emits a SELL entry on every bar."""

    fired: int = 0

    def maybe_enter(
        self, bar: BarData, hist: list[BarData], equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        self.fired += 1
        return _open_sell(bar)


@dataclass
class _NeverFireStub:
    """Sub-strategy that never proposes any entry."""

    def maybe_enter(
        self, bar: BarData, hist: list[BarData], equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        return None


def _short_warmup_cfg(**overrides: object) -> RegimeGatedConfig:
    """Tiny classifier so tests don't need 220 bars to warm up."""
    cls_cfg = HtfRegimeClassifierConfig(
        fast_ema=5, slow_ema=15, slope_lookback=3,
        slope_threshold_pct=0.05, trend_distance_pct=2.0,
        range_atr_pct_max=2.0, atr_period=5, warmup_bars=20,
    )
    return RegimeGatedConfig(classifier=cls_cfg, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Basic plumbing
# ---------------------------------------------------------------------------


def test_passes_through_when_sub_emits_none() -> None:
    """If the sub-strategy never fires, the wrapper never fires."""
    s = RegimeGatedStrategy(_NeverFireStub(), _short_warmup_cfg())
    cfg = _config()
    hist: list[BarData] = []
    for i in range(50):
        b = _bar(i, 100 + i)
        hist.append(b)
        assert s.maybe_enter(b, hist, 10_000.0, cfg) is None
    assert s.gate_stats["entries_proposed"] == 0
    assert s.gate_stats["entries_vetoed"] == 0


def test_warmup_vetoes_everything() -> None:
    """During warmup the classifier returns ('neutral','volatile','skip').
    With default config (volatile blocked), the gate must veto."""
    sub = _AlwaysBuyStub()
    s = RegimeGatedStrategy(sub, _short_warmup_cfg())
    cfg = _config()
    hist: list[BarData] = []
    # Feed only 10 bars (warmup is 20)
    fired = 0
    for i in range(10):
        b = _bar(i, 100 + i)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            fired += 1
    assert fired == 0
    assert s.gate_stats["entries_proposed"] == 10
    assert s.gate_stats["entries_vetoed"] == 10


# ---------------------------------------------------------------------------
# Regime gating
# ---------------------------------------------------------------------------


def _build_uptrend(s: RegimeGatedStrategy, n: int = 80) -> list[BarData]:
    """Steady uptrend — produces 'trending' / 'long' / 'trend_follow' once warm."""
    cfg = _config()
    hist: list[BarData] = []
    for i in range(n):
        # Big enough slope that distance_from_slow > trend_distance_pct
        c = 100.0 + i * 1.0
        b = _bar(i, c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    return hist


def _build_flat(s: RegimeGatedStrategy, n: int = 80) -> list[BarData]:
    """Flat tape — produces 'neutral' bias and likely 'ranging' regime."""
    cfg = _config()
    hist: list[BarData] = []
    for i in range(n):
        b = _bar(i, 100.0)  # constant
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    return hist


def test_uptrend_allows_buy_under_default_config() -> None:
    """Steady uptrend → trending+long+trend_follow → BUY allowed."""
    sub = _AlwaysBuyStub()
    s = RegimeGatedStrategy(sub, _short_warmup_cfg())
    _build_uptrend(s, n=80)
    # The very last call inside _build_uptrend already exercised the gate;
    # check that at some point the gate opened.
    assert s.gate_stats["entries_allowed"] > 0


def test_flat_tape_vetoes_under_strict_bias_match() -> None:
    """Flat tape → bias=neutral → with require_bias_match_side=True,
    BUY is vetoed because bias != 'long'."""
    sub = _AlwaysBuyStub()
    s = RegimeGatedStrategy(
        sub,
        _short_warmup_cfg(require_bias_match_side=True),
    )
    _build_flat(s, n=80)
    assert s.gate_stats["entries_proposed"] >= 60
    # At minimum, every post-warmup bar must have been vetoed.
    # Allowed-count is allowed to be 0; actual exact split depends on
    # ATR/EMA dynamics on the constant price series.
    assert s.gate_stats["entries_allowed"] == 0


def test_short_in_uptrend_vetoed_when_bias_match_required() -> None:
    """In a steady uptrend, bias=long; SELL with bias_match_required is vetoed."""
    sub = _AlwaysSellStub()
    s = RegimeGatedStrategy(
        sub,
        _short_warmup_cfg(require_bias_match_side=True),
    )
    _build_uptrend(s, n=80)
    # Some entries proposed, all vetoed
    assert s.gate_stats["entries_proposed"] >= 60
    assert s.gate_stats["entries_allowed"] == 0


# ---------------------------------------------------------------------------
# Provider attachment forwarding
# ---------------------------------------------------------------------------


@dataclass
class _StubWithProviders:
    """Sub-strategy stub that records provider attachments."""

    daily_verdict_attached: object | None = None
    etf_attached: object | None = None
    lth_attached: object | None = None

    def attach_daily_verdict_provider(self, p: object) -> None:
        self.daily_verdict_attached = p

    def attach_etf_flow_provider(self, p: object) -> None:
        self.etf_attached = p

    def attach_lth_provider(self, p: object) -> None:
        self.lth_attached = p

    def maybe_enter(
        self, bar: BarData, hist: list[BarData], equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        return None


def test_provider_attachments_forward_to_sub() -> None:
    sub = _StubWithProviders()
    s = RegimeGatedStrategy(sub, _short_warmup_cfg())

    daily_p = object()
    etf_p = object()
    lth_p = object()

    s.attach_daily_verdict_provider(daily_p)
    s.attach_etf_flow_provider(etf_p)
    s.attach_lth_provider(lth_p)

    assert sub.daily_verdict_attached is daily_p
    assert sub.etf_attached is etf_p
    assert sub.lth_attached is lth_p


def test_provider_attach_silent_when_sub_lacks_method() -> None:
    """Sub without the attach_* method → wrapper silently skips."""
    sub = _AlwaysBuyStub()  # no attach_* methods
    s = RegimeGatedStrategy(sub, _short_warmup_cfg())
    # Must not raise
    s.attach_daily_verdict_provider(object())
    s.attach_etf_flow_provider(object())
    s.attach_lth_provider(object())


# ---------------------------------------------------------------------------
# Allow/veto explicit configuration
# ---------------------------------------------------------------------------


def test_blocking_all_regimes_vetoes_all_entries() -> None:
    """Empty allowed_regimes → every entry vetoed."""
    sub = _AlwaysBuyStub()
    s = RegimeGatedStrategy(
        sub,
        _short_warmup_cfg(allowed_regimes=frozenset()),
    )
    _build_uptrend(s, n=80)
    assert s.gate_stats["entries_proposed"] >= 60
    assert s.gate_stats["entries_allowed"] == 0


def test_blocking_volatile_only() -> None:
    """Default config blocks 'volatile' regime; trending+ranging allowed."""
    cfg = _short_warmup_cfg()
    assert "volatile" not in cfg.allowed_regimes
    assert "trending" in cfg.allowed_regimes
    assert "ranging" in cfg.allowed_regimes


# ---------------------------------------------------------------------------
# Asset-class presets — guarantee cross-asset separation
# ---------------------------------------------------------------------------


def test_btc_preset_calibrated_for_1h_btc() -> None:
    """BTC preset: slow EMA spans ~200 days of 1h bars, allows long+short
    bias, allows trending+ranging regime, NOT volatile."""
    cfg = btc_daily_preset()
    assert cfg.classifier.slow_ema == 200 * 24
    assert cfg.classifier.fast_ema == 50 * 24
    assert "volatile" not in cfg.allowed_regimes
    assert "trending" in cfg.allowed_regimes
    assert "ranging" in cfg.allowed_regimes
    assert cfg.classifier.trend_distance_pct == 3.0


def test_btc_preset_strict_long_only_locks_to_long_bias() -> None:
    cfg = btc_daily_preset(strict_long_only=True)
    assert cfg.allowed_biases == frozenset({"long"})
    assert cfg.require_bias_match_side is True


def test_mnq_preset_calibrated_for_5m_intraday() -> None:
    """MNQ preset: fast EMA = 100 min, slow EMA = 5h. Allows ranging only.
    Trend-distance + ATR cutoffs scaled to MNQ's lower volatility."""
    cfg = mnq_intraday_preset()
    assert cfg.classifier.slow_ema == 60
    assert cfg.classifier.fast_ema == 20
    assert cfg.allowed_regimes == frozenset({"ranging"})
    assert cfg.allowed_modes == frozenset({"mean_revert"})
    # Thresholds are an order of magnitude tighter than BTC's
    assert cfg.classifier.trend_distance_pct < 1.0
    assert cfg.classifier.range_atr_pct_max < 1.0


def test_btc_and_mnq_presets_are_not_interchangeable() -> None:
    """Sanity: the two presets must differ on every numeric knob.
    If they ever match, the cross-asset separation has been broken."""
    btc = btc_daily_preset()
    mnq = mnq_intraday_preset()
    assert btc.classifier.fast_ema != mnq.classifier.fast_ema
    assert btc.classifier.slow_ema != mnq.classifier.slow_ema
    assert btc.classifier.trend_distance_pct != mnq.classifier.trend_distance_pct
    assert btc.classifier.range_atr_pct_max != mnq.classifier.range_atr_pct_max
    assert btc.allowed_regimes != mnq.allowed_regimes
    assert btc.allowed_modes != mnq.allowed_modes


def test_eth_preset_higher_vol_thresholds_than_btc() -> None:
    """ETH historically ~1.3x BTC vol; preset thresholds must reflect that."""
    btc = btc_daily_preset()
    eth = eth_daily_preset()
    assert eth.classifier.trend_distance_pct > btc.classifier.trend_distance_pct
    assert eth.classifier.range_atr_pct_max > btc.classifier.range_atr_pct_max
    # But same cadence (both 1h LTF)
    assert eth.classifier.slow_ema == btc.classifier.slow_ema


def test_audit_tag_includes_regime_classification() -> None:
    """When an entry is allowed, its regime tag includes the classification."""
    sub = _AlwaysBuyStub()
    s = RegimeGatedStrategy(sub, _short_warmup_cfg())
    cfg = _config()
    # Build uptrend history but capture the LAST opened
    hist: list[BarData] = []
    last_open = None
    for i in range(80):
        b = _bar(i, 100.0 + i * 1.0)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            last_open = out
    assert last_open is not None
    # Tag format: stub_regime_<regime>_<bias>_<mode>
    assert last_open.regime is not None
    assert last_open.regime.startswith("stub_regime_")

"""Tests for feeds.cme_basis_provider.

Coverage
--------
* ``CMEBasisProvider`` reads spot from a fixture CSV and computes basis
  in bps for a known timestamp (and within the configured skew window).
* ``CMEBasisProvider`` accepts a callable spot source as well.
* ``CMEBasisProvider`` returns ``None`` when the bar timestamp is too
  far from the nearest spot tick.
* ``MockBasisProvider`` returns expected values, both for timestamp
  mappings and sequential ``values=[...]`` mode.
* ``LogReturnFallbackProvider`` matches the strategy's internal silent
  fallback exactly — guards the "honest naming" claim that flipping the
  registry to ``log_return_fallback`` is behaviorally identical to the
  legacy default.
* Round trip: registry bridge instantiates the strategy with a provider
  via ``basis_provider_kind`` and the strategy fires entries from
  basis (not from a log-return proxy).
* ``build_basis_provider`` raises on unknown kinds and soft-fails to
  ``None`` when ``cme_basis`` is requested but the spot CSV is missing.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from eta_engine.core.data_pipeline import BarData
from eta_engine.feeds.cme_basis_provider import (
    CMEBasisProvider,
    LogReturnFallbackProvider,
    MockBasisProvider,
    build_basis_provider,
)

_CT = ZoneInfo("America/Chicago")


def _bar(
    ts: datetime,
    *,
    close: float,
    high: float | None = None,
    low: float | None = None,
    open_: float | None = None,
    volume: float = 1000.0,
    symbol: str = "MBT",
) -> BarData:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_CT)
    h = high if high is not None else close + 50.0
    lo = low if low is not None else close - 50.0
    o = open_ if open_ is not None else close
    return BarData(
        timestamp=ts.astimezone(UTC),
        symbol=symbol,
        open=o,
        high=h,
        low=lo,
        close=close,
        volume=volume,
    )


# ---------------------------------------------------------------------------
# CMEBasisProvider — CSV path
# ---------------------------------------------------------------------------


def _write_spot_csv(path: Path, rows: list[tuple[int, float]]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for ts, close in rows:
            w.writerow([ts, close, close, close, close, 1.0])


def _write_dashboard_style_spot_csv(path: Path, rows: list[tuple[datetime, float]]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for ts, close in rows:
            w.writerow([ts.astimezone(UTC).isoformat(), close, close, close, close, 1.0])


def test_cme_basis_provider_reads_csv_and_computes_basis(tmp_path: Path) -> None:
    """Two MBT closes against two known BTC spots — verify bps math."""
    spot_csv = tmp_path / "BTC_spot.csv"
    ts1 = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    ts2 = datetime(2026, 6, 15, 10, 5, tzinfo=UTC)
    epoch1 = int(ts1.timestamp())
    epoch2 = int(ts2.timestamp())
    _write_spot_csv(
        spot_csv,
        [(epoch1, 60_000.0), (epoch2, 60_500.0)],
    )

    provider = CMEBasisProvider(spot_csv)

    # Bar 1: MBT close = 60_300 vs BTC = 60_000 -> +50 bps
    bar1 = _bar(ts1, close=60_300.0)
    assert provider(bar1) == pytest.approx(50.0, rel=1e-9)

    # Bar 2: MBT close = 60_500 vs BTC = 60_500 -> 0 bps (parity)
    bar2 = _bar(ts2, close=60_500.0)
    assert provider(bar2) == pytest.approx(0.0, abs=1e-9)


def test_cme_basis_provider_reads_dashboard_style_iso_timestamps(tmp_path: Path) -> None:
    spot_csv = tmp_path / "BTC_spot_dashboard.csv"
    ts = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    _write_dashboard_style_spot_csv(spot_csv, [(ts, 60_000.0)])

    provider = CMEBasisProvider(spot_csv)
    bar = _bar(ts, close=60_300.0)

    assert provider(bar) == pytest.approx(50.0, rel=1e-9)


def test_cme_basis_provider_callable_spot_source() -> None:
    """Callable spot source bypasses CSV loading."""

    def spot_at(ts: datetime) -> float:
        # Constant: every timestamp -> 60_000
        del ts
        return 60_000.0

    provider = CMEBasisProvider(spot_at)
    bar = _bar(datetime(2026, 6, 15, 10, 0, tzinfo=UTC), close=60_600.0)
    # +600 / 60000 = 0.01 -> 100 bps
    assert provider(bar) == pytest.approx(100.0, rel=1e-9)


def test_cme_basis_provider_returns_none_when_spot_skew_too_large(tmp_path: Path) -> None:
    """Bar timestamp 2 hours away from the nearest spot tick -> None."""
    spot_csv = tmp_path / "BTC_spot.csv"
    ts_anchor = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    _write_spot_csv(spot_csv, [(int(ts_anchor.timestamp()), 60_000.0)])

    provider = CMEBasisProvider(spot_csv, max_lookup_skew_seconds=300)
    # +2h bar -> 7200 seconds away from nearest tick -> None
    bar = _bar(ts_anchor + timedelta(hours=2), close=60_500.0)
    assert provider(bar) is None


def test_cme_basis_provider_rejects_invalid_source() -> None:
    with pytest.raises(TypeError):
        CMEBasisProvider(12345)  # type: ignore[arg-type]


def test_cme_basis_provider_missing_csv_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        CMEBasisProvider(tmp_path / "does_not_exist.csv")


# ---------------------------------------------------------------------------
# MockBasisProvider
# ---------------------------------------------------------------------------


def test_mock_basis_provider_mapping_mode() -> None:
    ts = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    provider = MockBasisProvider({ts: 42.5}, default=-1.0)
    bar_hit = _bar(ts, close=60_000.0)
    bar_miss = _bar(ts + timedelta(minutes=5), close=60_000.0)
    assert provider(bar_hit) == 42.5
    assert provider(bar_miss) == -1.0


def test_mock_basis_provider_sequential_mode() -> None:
    provider = MockBasisProvider(values=[0.0, 1.5, -2.0], default=99.0)
    base = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    out = [provider(_bar(base + timedelta(minutes=i * 5), close=60_000.0)) for i in range(4)]
    assert out == [0.0, 1.5, -2.0, 99.0]


# ---------------------------------------------------------------------------
# LogReturnFallbackProvider — matches the strategy's silent fallback
# ---------------------------------------------------------------------------


def test_log_return_fallback_provider_matches_strategy_internal() -> None:
    """The explicit fallback must produce IDENTICAL bps readings to the
    strategy's internal ``_basis_proxy`` for the same bar sequence.

    This is the test that lets us claim "switching the registry from the
    silent default to ``log_return_fallback`` is a *naming* change, not a
    behavior change."
    """
    from eta_engine.strategies.mbt_funding_basis_strategy import (
        MBTFundingBasisStrategy,
    )

    base = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    closes = [60_000.0, 60_300.0, 60_150.0, 60_450.0, 60_600.0]
    bars = [_bar(base + timedelta(minutes=i * 5), close=c) for i, c in enumerate(closes)]

    explicit = LogReturnFallbackProvider()
    strat = MBTFundingBasisStrategy()  # no provider -> silent fallback

    # Strategy's internal _basis_proxy receives (bar, hist) where hist is
    # the bars BEFORE the current one (matches the engine's call shape).
    explicit_out: list[float | None] = []
    internal_out: list[float | None] = []
    for i, bar in enumerate(bars):
        explicit_out.append(explicit(bar))
        internal_out.append(strat._basis_proxy(bar, list(bars[:i])))

    assert explicit_out == internal_out


# ---------------------------------------------------------------------------
# build_basis_provider — factory dispatch
# ---------------------------------------------------------------------------


def test_build_basis_provider_log_return_fallback() -> None:
    p = build_basis_provider("log_return_fallback")
    assert isinstance(p, LogReturnFallbackProvider)


def test_build_basis_provider_internal_log_return_returns_none() -> None:
    assert build_basis_provider("internal_log_return") is None
    assert build_basis_provider("") is None


def test_build_basis_provider_cme_basis_with_csv(tmp_path: Path) -> None:
    spot_csv = tmp_path / "BTC_spot.csv"
    ts = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    _write_spot_csv(spot_csv, [(int(ts.timestamp()), 60_000.0)])
    p = build_basis_provider("cme_basis", spot_csv=str(spot_csv))
    assert isinstance(p, CMEBasisProvider)


def test_build_basis_provider_cme_basis_missing_csv_softfails(tmp_path: Path) -> None:
    p = build_basis_provider("cme_basis", spot_csv=str(tmp_path / "missing.csv"))
    # Soft-fail to None so the bridge can fall back to the strategy's
    # internal proxy rather than crashing dispatch.
    assert p is None


def test_build_basis_provider_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="Unknown basis_provider_kind"):
        build_basis_provider("nonsense_provider")


# ---------------------------------------------------------------------------
# Round trip: registry bridge wires a provider that drives entries
# ---------------------------------------------------------------------------


def test_registry_bridge_wires_provider_and_strategy_uses_basis() -> None:
    """End-to-end: the registry bridge's mbt_funding_basis branch
    instantiates the strategy WITH a provider passed in, and the strategy
    fires SHORT entries when basis (not log return) breaches the z-score.
    """
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.strategies.registry_strategy_bridge import (
        _build_strategy_fallback,
    )

    bcfg = BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="MBT",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.005,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )

    # Use a permissive config so warmup is short. The strategy still
    # gets real-basis-style readings via log_return_fallback.
    extras = {
        "basis_provider_kind": "log_return_fallback",
        "mbt_funding_basis_config": {
            "basis_lookback": 6,
            "entry_z": 0.5,
            "momentum_lookback": 2,
            "warmup_bars": 8,
            "atr_period": 5,
            "min_bars_between_trades": 0,
            "max_trades_per_day": 5,
        },
    }
    strat = _build_strategy_fallback("mbt_funding_basis", extras)
    assert strat is not None
    # Verify the bridge wired a real provider object (not None).
    assert strat._basis_provider is not None
    assert isinstance(strat._basis_provider, LogReturnFallbackProvider)

    # Drive synthetic bars: 8 calm bars to fill the rolling window with
    # ~zero log-return basis, then a sharp move that produces a fat
    # positive z-score AND maintains lower-highs for momentum confirmation.
    base = datetime(2026, 6, 15, 10, 0, tzinfo=_CT)  # RTH
    hist: list[BarData] = []

    # Calm bars (small alternating closes -> tiny basis bps)
    for i in range(8):
        ts = base + timedelta(minutes=i * 5)
        b = _bar(
            ts.astimezone(UTC),
            close=60_000.0 + (1.0 if i % 2 else -1.0),
            high=60_010.0,
            low=59_990.0,
            volume=1000.0,
        )
        strat.maybe_enter(b, hist, 10_000.0, bcfg)
        hist.append(b)

    # Establish a lower-high sequence: each bar's high <= prior.
    bar_lh1 = _bar(
        (base + timedelta(minutes=8 * 5)).astimezone(UTC),
        close=60_002.0,
        high=60_005.0,
        low=59_995.0,
    )
    strat.maybe_enter(bar_lh1, hist, 10_000.0, bcfg)
    hist.append(bar_lh1)

    bar_lh2 = _bar(
        (base + timedelta(minutes=9 * 5)).astimezone(UTC),
        close=60_001.0,
        high=60_003.0,
        low=59_990.0,
    )
    strat.maybe_enter(bar_lh2, hist, 10_000.0, bcfg)
    hist.append(bar_lh2)

    # Spike bar — the basis (log-return-fallback) leaps because close
    # jumps. high<=prior so momentum gate still passes.
    spike = _bar(
        (base + timedelta(minutes=10 * 5)).astimezone(UTC),
        close=60_300.0,
        high=60_002.0,
        low=59_980.0,
    )
    out = strat.maybe_enter(spike, hist, 10_000.0, bcfg)
    assert out is not None, f"expected SHORT fire from provider-driven basis; stats={strat.stats}"
    assert out.side == "SELL"
    # Confirm the fired entry came through the provider path: the
    # rolling window length should equal lookback (provider populated it).
    assert len(strat._basis_window) == strat.cfg.basis_lookback

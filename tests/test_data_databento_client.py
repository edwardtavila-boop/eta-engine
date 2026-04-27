"""
EVOLUTIONARY TRADING ALGO  //  tests.test_data_databento_client
===================================================
Exercise DataBentoClient:
  * no-creds dry-run returns zero rows but still accrues forecast cost
  * real-network path with injected fake Historical client
  * billing threshold warning
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta
from typing import Any

import pytest

from eta_engine.data import databento_client as mod


class _FakeRow:
    """Simulate a databento record with fixed-point price fields."""

    def __init__(self, **fields: Any) -> None:  # noqa: ANN401 - arbitrary databento record fields
        for k, v in fields.items():
            setattr(self, k, v)


class _FakeStore:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:  # noqa: ANN401 - store iterator is heterogenous row objects
        return iter(self._rows)


class _FakeTimeseries:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.calls: list[dict[str, Any]] = []

    def get_range(self, **kwargs: Any) -> _FakeStore:  # noqa: ANN401 - databento get_range kwargs
        self.calls.append(kwargs)
        return _FakeStore(self._rows)


class _FakeHistorical:
    def __init__(self, api_key: str, rows: list[Any] | None = None) -> None:
        self.api_key = api_key
        self.timeseries = _FakeTimeseries(rows or [])


# --------------------------------------------------------------------------- #
# No-creds dry-run
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_bars_no_creds_yields_nothing_but_accrues_cost() -> None:
    c = mod.DataBentoClient(api_key="")
    # 1 year of 1s bars to accrue a non-trivial forecast cost.
    start = datetime(2026, 1, 1)
    end = start + timedelta(days=365)
    rows = [bar async for bar in c.fetch_bars("MNQH6", start, end, freq="1s")]
    assert rows == []
    # 31.5M bars * 80 bytes / 1GB * $0.50 ~ $1.17
    assert c._cost_usd_accrued > 0.5
    assert c._cost_usd_accrued < 5.0


@pytest.mark.asyncio
async def test_fetch_trades_no_creds_yields_nothing() -> None:
    c = mod.DataBentoClient(api_key="")
    # Big window to cross the rounding threshold
    rows = [
        t
        async for t in c.fetch_trades(
            "MNQH6",
            datetime(2026, 1, 1),
            datetime(2026, 2, 1),
        )
    ]
    assert rows == []
    assert c._cost_usd_accrued > 0


# --------------------------------------------------------------------------- #
# Real-path with injected fake SDK
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_bars_with_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    # Row with fixed-point px fields (9 decimal scale)
    rows = [
        _FakeRow(
            ts_event=1_700_000_000_000_000_000,  # ns
            open=25000_000_000_000,  # $25,000 in 1e9 fp
            high=25010_000_000_000,
            low=24990_000_000_000,
            close=25005_000_000_000,
            volume=120,
        ),
    ]

    def fake_hist(api_key: str) -> _FakeHistorical:
        return _FakeHistorical(api_key, rows=rows)

    fake_pkg = types.SimpleNamespace(Historical=fake_hist)
    import sys

    monkeypatch.setitem(sys.modules, "databento", fake_pkg)

    c = mod.DataBentoClient(api_key="KEY")
    start = datetime(2026, 1, 1)
    end = start + timedelta(minutes=1)
    bars = [b async for b in c.fetch_bars("MNQH6", start, end, freq="1m")]
    assert len(bars) == 1
    b = bars[0]
    assert b.symbol == "MNQH6"
    assert abs(b.open - 25000.0) < 1e-6
    assert abs(b.close - 25005.0) < 1e-6
    assert b.volume == 120.0


@pytest.mark.asyncio
async def test_fetch_trades_with_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _FakeRow(
            ts_event=1_700_000_000_000_000_000,
            price=25000_500_000_000,  # $25000.5
            size=2,
            side="B",
        ),
    ]

    def fake_hist(api_key: str) -> _FakeHistorical:
        return _FakeHistorical(api_key, rows=rows)

    fake_pkg = types.SimpleNamespace(Historical=fake_hist)
    import sys

    monkeypatch.setitem(sys.modules, "databento", fake_pkg)

    c = mod.DataBentoClient(api_key="KEY")
    trades = [
        t
        async for t in c.fetch_trades(
            "MNQH6",
            datetime(2026, 1, 1),
            datetime(2026, 1, 1, 0, 1),
        )
    ]
    assert len(trades) == 1
    t = trades[0]
    assert abs(t["price"] - 25000.5) < 1e-6
    assert t["side"] == "B"
    assert t["size"] == 2.0


@pytest.mark.asyncio
async def test_fetch_mbp_level_with_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _FakeRow(ts_event=1_700_000_000_000_000_000)
    # Inject 3 bid/ask levels
    for i in range(3):
        setattr(row, f"bid_px_{i:02d}", (25000 - i) * 10**9)
        setattr(row, f"bid_sz_{i:02d}", 5 + i)
        setattr(row, f"ask_px_{i:02d}", (25001 + i) * 10**9)
        setattr(row, f"ask_sz_{i:02d}", 4 + i)

    def fake_hist(api_key: str) -> _FakeHistorical:
        return _FakeHistorical(api_key, rows=[row])

    fake_pkg = types.SimpleNamespace(Historical=fake_hist)
    import sys

    monkeypatch.setitem(sys.modules, "databento", fake_pkg)

    c = mod.DataBentoClient(api_key="KEY")
    snaps = [
        s
        async for s in c.fetch_mbp_level(
            "MNQH6",
            datetime(2026, 1, 1),
            datetime(2026, 1, 1, 0, 1),
            levels=3,
        )
    ]
    assert len(snaps) == 1
    s = snaps[0]
    assert len(s["bids"]) == 3
    assert len(s["asks"]) == 3
    assert abs(s["bids"][0][0] - 25000.0) < 1e-6
    assert abs(s["asks"][0][0] - 25001.0) < 1e-6


# --------------------------------------------------------------------------- #
# Error handling: SDK raises -> we log + yield nothing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_bars_handles_sdk_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomTimeseries:
        def get_range(self, **kwargs: Any) -> None:  # noqa: ANN401 - databento get_range kwargs
            raise RuntimeError("rate limit exceeded")

    class _BoomHist:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key
            self.timeseries = _BoomTimeseries()

    fake_pkg = types.SimpleNamespace(Historical=_BoomHist)
    import sys

    monkeypatch.setitem(sys.modules, "databento", fake_pkg)

    c = mod.DataBentoClient(api_key="KEY")
    bars = [
        b
        async for b in c.fetch_bars(
            "MNQH6",
            datetime(2026, 1, 1),
            datetime(2026, 1, 1, 1),
        )
    ]
    assert bars == []


# --------------------------------------------------------------------------- #
# Billing threshold crossing emits warning
# --------------------------------------------------------------------------- #
def test_cost_threshold_warning_fires(caplog: pytest.LogCaptureFixture) -> None:
    c = mod.DataBentoClient(api_key="", cost_warn_threshold_usd=0.001)
    c._accrue_cost("mbp-10", 10 * 1024**3)  # 10 GiB * $5 = $50
    assert c.cost_usd_accrued > 0.001


def test_reset_cost_zeros_tracker() -> None:
    c = mod.DataBentoClient(api_key="")
    c._accrue_cost("ohlcv-1m", 10**9)
    assert c.cost_usd_accrued > 0
    c.reset_cost()
    assert c.cost_usd_accrued == 0.0


# --------------------------------------------------------------------------- #
# Estimator sanity
# --------------------------------------------------------------------------- #
def test_estimate_bar_count_monotonic_with_duration() -> None:
    c = mod.DataBentoClient()
    start = datetime(2026, 1, 1)
    assert c._estimate_bar_count(start, start + timedelta(hours=1), "1m") == 60
    assert c._estimate_bar_count(start, start + timedelta(hours=1), "1s") == 3600
    assert c._estimate_bar_count(start, start + timedelta(hours=1), "5m") == 12

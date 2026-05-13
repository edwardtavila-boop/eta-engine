"""Freshness-guard tests for macro confluence providers.

When the most recent CSV reading is older than the provider's
``max_age_hours``, the provider must return ``NaN`` instead of
silently surfacing a stale value as a "neutral" signal. This is
critical for live trading — the strategy must defend against NaN
rather than trade on month-old funding rates.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 - tmp_path fixture annotation

from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.macro_confluence_providers import (
    EtfFlowProvider,
    FundingRateProvider,
    LthProxyProvider,
    MacroTailwindProvider,
)


def _bar(ts: datetime) -> BarData:
    return BarData(
        timestamp=ts,
        symbol="BTC",
        open=100,
        high=100,
        low=100,
        close=100,
        volume=1000,
    )


# ---------------------------------------------------------------------------
# FundingRateProvider — default 24h window
# ---------------------------------------------------------------------------


def test_funding_provider_returns_value_when_fresh(tmp_path: Path) -> None:
    csv_p = tmp_path / "fund.csv"
    last = datetime(2026, 5, 1, tzinfo=UTC)
    csv_p.write_text(
        f"time,funding_rate\n{int(last.timestamp())},0.0005\n",
        encoding="utf-8",
    )
    p = FundingRateProvider(csv_path=csv_p)
    # 12h after last reading — within 24h default
    assert p(_bar(last + timedelta(hours=12))) == 0.0005


def test_funding_provider_returns_nan_when_stale(tmp_path: Path) -> None:
    csv_p = tmp_path / "fund.csv"
    last = datetime(2026, 5, 1, tzinfo=UTC)
    csv_p.write_text(
        f"time,funding_rate\n{int(last.timestamp())},0.0005\n",
        encoding="utf-8",
    )
    p = FundingRateProvider(csv_path=csv_p)
    # 30 days later — way past the 24h ceiling
    out = p(_bar(last + timedelta(days=30)))
    assert math.isnan(out)


def test_funding_provider_custom_max_age(tmp_path: Path) -> None:
    csv_p = tmp_path / "fund.csv"
    last = datetime(2026, 5, 1, tzinfo=UTC)
    csv_p.write_text(
        f"time,funding_rate\n{int(last.timestamp())},0.0005\n",
        encoding="utf-8",
    )
    p = FundingRateProvider(csv_path=csv_p, max_age_hours=72.0)
    # 48h later — within 72h custom window
    assert p(_bar(last + timedelta(hours=48))) == 0.0005
    # 80h later — past 72h custom window
    assert math.isnan(p(_bar(last + timedelta(hours=80))))


# ---------------------------------------------------------------------------
# MacroTailwindProvider — default 168h (1 week) window
# ---------------------------------------------------------------------------


def test_macro_tailwind_returns_nan_when_stale(tmp_path: Path) -> None:
    dxy_p = tmp_path / "dxy.csv"
    spy_p = tmp_path / "spy.csv"
    # Need slope_period+1 bars (default 5+1 = 6) to produce one macro entry
    base = datetime(2026, 1, 1, tzinfo=UTC)
    dxy_lines = ["time,open,high,low,close,volume"]
    spy_lines = ["time,open,high,low,close,volume"]
    for i in range(7):
        ts = int((base + timedelta(days=i)).timestamp())
        dxy_lines.append(f"{ts},100,100,100,{100 + i},1000")
        spy_lines.append(f"{ts},400,400,400,{400 + i},1000")
    dxy_p.write_text("\n".join(dxy_lines) + "\n", encoding="utf-8")
    spy_p.write_text("\n".join(spy_lines) + "\n", encoding="utf-8")

    p = MacroTailwindProvider(dxy_csv=dxy_p, spy_csv=spy_p)
    # Bar 30 days after last macro reading — past 168h
    bar = _bar(base + timedelta(days=37))
    assert math.isnan(p(bar))


# ---------------------------------------------------------------------------
# EtfFlowProvider — default 96h window
# ---------------------------------------------------------------------------


def test_etf_flow_returns_value_when_fresh(tmp_path: Path) -> None:
    csv_p = tmp_path / "etf.csv"
    last = datetime(2026, 5, 1, tzinfo=UTC)
    csv_p.write_text(
        f"time,net_flow_usd_m\n{int(last.timestamp())},250.0\n",
        encoding="utf-8",
    )
    p = EtfFlowProvider(csv_path=csv_p)
    # 2 days after — within 96h default (handles weekend gaps)
    assert p(_bar(last + timedelta(days=2))) == 250.0


def test_etf_flow_returns_nan_when_stale(tmp_path: Path) -> None:
    csv_p = tmp_path / "etf.csv"
    last = datetime(2026, 5, 1, tzinfo=UTC)
    csv_p.write_text(
        f"time,net_flow_usd_m\n{int(last.timestamp())},250.0\n",
        encoding="utf-8",
    )
    p = EtfFlowProvider(csv_path=csv_p)
    # 10 days later — past 96h ceiling
    out = p(_bar(last + timedelta(days=10)))
    assert math.isnan(out)


# ---------------------------------------------------------------------------
# LthProxyProvider — default 168h window
# ---------------------------------------------------------------------------


def test_lth_proxy_returns_value_when_fresh(tmp_path: Path) -> None:
    csv_p = tmp_path / "lth.csv"
    last = datetime(2026, 5, 1, tzinfo=UTC)
    csv_p.write_text(
        f"time,lth_proxy\n{int(last.timestamp())},0.5\n",
        encoding="utf-8",
    )
    p = LthProxyProvider(csv_path=csv_p)
    # 3 days later — within 168h default
    assert p(_bar(last + timedelta(days=3))) == 0.5


def test_lth_proxy_returns_nan_when_stale(tmp_path: Path) -> None:
    csv_p = tmp_path / "lth.csv"
    last = datetime(2026, 5, 1, tzinfo=UTC)
    csv_p.write_text(
        f"time,lth_proxy\n{int(last.timestamp())},0.5\n",
        encoding="utf-8",
    )
    p = LthProxyProvider(csv_path=csv_p)
    # 14 days later — past 168h ceiling
    out = p(_bar(last + timedelta(days=14)))
    assert math.isnan(out)

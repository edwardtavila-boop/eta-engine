"""
EVOLUTIONARY TRADING ALGO  //  strategies.macro_confluence_providers
=====================================================================
Concrete providers for crypto_macro_confluence_strategy.

Each provider is a callable ``(bar) -> float`` that the strategy
consumes via ``attach_*_provider()``. Keeping these in a separate
module so the strategy file stays small and the provider plumbing
can evolve independently from the strategy logic.

Three providers shipped today:

* ``EthAlignmentProvider`` — reads ETH OHLCV + computes the same
  regime EMA the strategy uses, returns +1/-1/0 score.
* ``FundingRateProvider`` — reads BTCFUND_8h CSV, returns the
  current 8h funding rate (interpolated to bar timestamp).
* ``MacroTailwindProvider`` — DXY trend + SPY trend; returns a
  composite [-1, +1] score. Requires DXY + SPY daily CSVs (fetched
  via scripts.extend_nq_daily_yahoo --symbol DX=F / SPY).

Future Tier-4 providers (ETF flow, on-chain LTH) will plug in
here once their fetchers exist.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.core.data_pipeline import BarData


# ---------------------------------------------------------------------------
# ETH alignment provider
# ---------------------------------------------------------------------------


class EthAlignmentProvider:
    """Reads ETH OHLCV + tracks the same regime EMA period the BTC
    strategy uses. Returns +1.0 when ETH close is on the same
    regime side as BTC's proposed direction, -1.0 opposite, 0.0
    when warmup or no data.

    Pattern:
        provider = EthAlignmentProvider(eth_bars=eth_bars, regime_ema=100)
        strategy.attach_eth_alignment_provider(provider)

    The strategy itself never sees a side argument — the provider
    just reports "is ETH bullish or bearish at this moment". The
    strategy does the side check in its filter logic.
    """

    def __init__(
        self,
        eth_bars: list[BarData],
        regime_ema_period: int = 100,
    ) -> None:
        self._eth_by_ts: dict[datetime, BarData] = {b.timestamp: b for b in eth_bars}
        self._eth_sorted = sorted(eth_bars, key=lambda b: b.timestamp)
        self._regime_period = regime_ema_period
        self._eth_ema: float | None = None
        self._last_seen_ts: datetime | None = None
        # Pre-compute EMA at every ETH bar so we can look up by timestamp
        self._ema_at_ts: dict[datetime, float] = {}
        ema: float | None = None
        for b in self._eth_sorted:
            ema = self._step_ema(ema, b.close)
            self._ema_at_ts[b.timestamp] = ema
        # Pre-cache closes for fast nearest-bar lookup
        self._close_at_ts: dict[datetime, float] = {b.timestamp: b.close for b in self._eth_sorted}

    def _step_ema(self, prev: float | None, value: float) -> float:
        if prev is None:
            return value
        alpha = 2.0 / (self._regime_period + 1)
        return alpha * value + (1 - alpha) * prev

    def __call__(self, bar: BarData) -> float:
        """Return ETH-vs-its-regime score: +1 bull, -1 bear, 0 neutral."""
        # Find the ETH bar closest to (and not after) bar.timestamp
        eth_bar = self._eth_by_ts.get(bar.timestamp)
        if eth_bar is None:
            # Fall back to nearest preceding ETH bar
            eth_bar = self._nearest_preceding(bar.timestamp)
            if eth_bar is None:
                return 0.0
        ema = self._ema_at_ts.get(eth_bar.timestamp)
        if ema is None:
            return 0.0
        if eth_bar.close > ema:
            return 1.0
        if eth_bar.close < ema:
            return -1.0
        return 0.0

    def _nearest_preceding(self, ts: datetime) -> BarData | None:
        # Linear scan; ETH bars are pre-sorted. For 8K bars this
        # is fast enough for one-shot backtests.
        result: BarData | None = None
        for b in self._eth_sorted:
            if b.timestamp <= ts:
                result = b
            else:
                break
        return result


# ---------------------------------------------------------------------------
# Funding rate provider
# ---------------------------------------------------------------------------


class FundingRateProvider:
    """Reads BTCFUND_8h CSV and returns the current 8h funding rate.

    The funding rate file has columns: time (unix), funding_rate.
    A bar's funding is the most recent published rate <= bar.ts.

    BTCFUND_8h had 96 days of coverage prior to 2026-04-27. As of
    that date, ``fetch_btc_funding_extended`` was extended to use
    BitMEX (US-friendly, 10y XBTUSD history); the file now covers
    ~5 years (5,475 rows). Outside the file's window the provider
    returns 0.0 (neutral) so the filter is a no-op.
    """

    def __init__(self, csv_path: Path | str) -> None:
        self._rows: list[tuple[datetime, float]] = []
        p = Path(csv_path)
        if not p.exists():
            return
        try:
            with p.open("r", encoding="utf-8") as f:
                r = csv.DictReader(f)
                for row in r:
                    try:
                        ts = datetime.fromtimestamp(int(row["time"]), UTC)
                        rate = float(row.get("funding_rate") or row.get("close") or 0.0)
                        self._rows.append((ts, rate))
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            return
        self._rows.sort(key=lambda x: x[0])

    def __call__(self, bar: BarData) -> float:
        """Most recent funding rate at-or-before bar.timestamp; 0 if none."""
        if not self._rows:
            return 0.0
        # Binary search would be faster; linear scan is fine for ~300 rows
        result = 0.0
        for ts, rate in self._rows:
            if ts <= bar.timestamp:
                result = rate
            else:
                break
        return result


# ---------------------------------------------------------------------------
# Macro tailwind provider
# ---------------------------------------------------------------------------


@dataclass
class _MacroBar:
    """Internal: a single date with DXY + SPY values + slopes."""

    date: datetime
    dxy_close: float
    spy_close: float
    dxy_slope_5d: float
    spy_slope_5d: float


class MacroTailwindProvider:
    """Composite macro score in [-1, +1] derived from DXY + SPY.

    Score = -dxy_slope_norm + spy_slope_norm, clipped to [-1, +1].

    Interpretation:
      * +1: DXY falling AND SPY rising (full risk-on, BTC tailwind)
      * -1: DXY rising AND SPY falling (full risk-off)
      *  0: mixed or flat

    Both inputs are daily bars from Yahoo. Bar timestamps in the
    backtest are mapped to the most recent trading-day macro reading.
    """

    def __init__(
        self,
        dxy_csv: Path | str,
        spy_csv: Path | str,
        slope_period: int = 5,
    ) -> None:
        self._slope_period = slope_period
        dxy_rows = _read_yahoo_csv(Path(dxy_csv))
        spy_rows = _read_yahoo_csv(Path(spy_csv))
        # Index DXY/SPY by date for O(1) lookup
        dxy_by_date = {ts.date(): close for ts, close in dxy_rows}
        spy_by_date = {ts.date(): close for ts, close in spy_rows}
        # Build a sorted list of dates that exist in BOTH series
        common = sorted(set(dxy_by_date) & set(spy_by_date))
        # Compute rolling slopes
        self._macro: list[_MacroBar] = []
        for i, d in enumerate(common):
            if i < slope_period:
                continue
            d_prev = common[i - slope_period]
            dxy_now = dxy_by_date[d]
            dxy_prev = dxy_by_date[d_prev]
            spy_now = spy_by_date[d]
            spy_prev = spy_by_date[d_prev]
            dxy_slope = (dxy_now - dxy_prev) / max(dxy_prev, 1e-9)
            spy_slope = (spy_now - spy_prev) / max(spy_prev, 1e-9)
            self._macro.append(_MacroBar(
                date=datetime(d.year, d.month, d.day, tzinfo=UTC),
                dxy_close=dxy_now, spy_close=spy_now,
                dxy_slope_5d=dxy_slope, spy_slope_5d=spy_slope,
            ))
        self._macro.sort(key=lambda m: m.date)

    def __call__(self, bar: BarData) -> float:
        """Most recent macro score at-or-before bar.timestamp."""
        if not self._macro:
            return 0.0
        result = 0.0
        for m in self._macro:
            if m.date.date() <= bar.timestamp.date():
                # Score: weight SPY positively, DXY negatively
                # Normalize each slope to roughly [-1, +1] range
                # (typical 5d slopes are 0-3%)
                dxy_norm = max(-1.0, min(1.0, m.dxy_slope_5d / 0.02))
                spy_norm = max(-1.0, min(1.0, m.spy_slope_5d / 0.02))
                score = -dxy_norm * 0.5 + spy_norm * 0.5
                result = max(-1.0, min(1.0, score))
            else:
                break
        return result


def _read_yahoo_csv(p: Path) -> list[tuple[datetime, float]]:
    """Read the standard yahoo-extender CSV: time,open,high,low,close,volume."""
    if not p.exists():
        return []
    out: list[tuple[datetime, float]] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                try:
                    ts = datetime.fromtimestamp(int(row["time"]), UTC)
                    close = float(row["close"])
                    out.append((ts, close))
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        return []
    return out


def _read_two_col_csv(
    p: Path, value_col: str,
) -> list[tuple[datetime, float]]:
    """Read a (time, <value_col>) CSV. Used by all three Tier-4 providers.

    Schema: ``time,<value_col>`` where ``time`` is unix-seconds and
    ``<value_col>`` is a float. Missing files / unparseable rows
    return an empty list — downstream providers no-op gracefully.
    """
    if not p.exists():
        return []
    out: list[tuple[datetime, float]] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                try:
                    ts = datetime.fromtimestamp(int(row["time"]), UTC)
                    val = float(row[value_col])
                except (ValueError, KeyError, TypeError):
                    continue
                out.append((ts, val))
    except OSError:
        return []
    out.sort(key=lambda x: x[0])
    return out


# ---------------------------------------------------------------------------
# Tier-4 providers
# ---------------------------------------------------------------------------


class EtfFlowProvider:
    """Daily BTC spot-ETF net flows from Farside Investors.

    Returns the most recent day's net flow in USD millions
    at-or-before bar.timestamp. Positive = inflow, negative =
    outflow, 0 = no data / weekend / pre-coverage.

    The strategy's ``filter_etf_flow`` checks sign:
      * Long requires flow > 0  (institutional buying)
      * Short requires flow < 0 (institutional selling)
    """

    def __init__(self, csv_path: Path | str) -> None:
        self._rows = _read_two_col_csv(Path(csv_path), "net_flow_usd_m")

    def __call__(self, bar: BarData) -> float:
        if not self._rows:
            return 0.0
        result = 0.0
        target = bar.timestamp.date()
        for ts, val in self._rows:
            if ts.date() <= target:
                result = val
            else:
                break
        return result


class FearGreedProvider:
    """Crypto Fear & Greed Index from alternative.me.

    Raw score is 0-100 (0 = extreme fear, 100 = extreme greed).
    Provider returns a CONTRARIAN-NORMALIZED score in [-1, +1]:
      * fg <= 25  -> +1.0  (extreme fear, accumulation phase, BUY)
      * fg >= 75  -> -1.0  (extreme greed, distribution phase, SELL)
      * 50        ->  0.0  (neutral)
      * linear interpolation between

    The strategy's filter expects long-positive / short-negative
    semantics, so this contrarian-flipped score plugs in as a
    sentiment filter where fear = good for longs, greed = bad.
    """

    def __init__(self, csv_path: Path | str) -> None:
        self._rows = _read_two_col_csv(Path(csv_path), "fear_greed")

    def __call__(self, bar: BarData) -> float:
        if not self._rows:
            return 0.0
        target = bar.timestamp.date()
        raw = 50.0
        for ts, val in self._rows:
            if ts.date() <= target:
                raw = val
            else:
                break
        # Map [0, 100] linearly to [+1, -1] (fear -> +1, greed -> -1)
        score = (50.0 - raw) / 50.0
        return max(-1.0, min(1.0, score))


class LthProxyProvider:
    """LTH-supply proxy from BTC daily Mayer Multiple percentile.

    Pre-computed by ``scripts.fetch_lth_proxy``. CSV value is already
    in [-1, +1] where +1 = strong accumulation (LTH buying), -1 =
    strong distribution (LTH selling). Provider just returns the
    current day's value.
    """

    def __init__(self, csv_path: Path | str) -> None:
        self._rows = _read_two_col_csv(Path(csv_path), "lth_proxy")

    def __call__(self, bar: BarData) -> float:
        if not self._rows:
            return 0.0
        target = bar.timestamp.date()
        result = 0.0
        for ts, val in self._rows:
            if ts.date() <= target:
                result = val
            else:
                break
        return max(-1.0, min(1.0, result))

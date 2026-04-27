"""
EVOLUTIONARY TRADING ALGO  //  backtest.replay
===================================
Bar replay sources. Synthetic GBM for testing; parquet/databento stubs.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from eta_engine.core.data_pipeline import BarData

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class BarReplay:
    """Iterable of BarData from various sources."""

    # ── Parquet ──

    @staticmethod
    def from_parquet(path: Path, symbol: str) -> Iterator[BarData]:
        """Stream BarData from a parquet file.

        TODO: implement with pyarrow.parquet.ParquetFile — iter_batches()
        TODO: expected schema: [timestamp (int64 ns), open, high, low, close, volume]
        TODO: filter by symbol column if present
        """
        raise NotImplementedError(
            f"from_parquet not implemented yet (path={path}, symbol={symbol}). "
            "Wire pyarrow.parquet.ParquetFile -> iter_batches -> BarData."
        )

    # ── Databento ──

    @staticmethod
    def from_databento(
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[BarData]:
        """Stream BarData from Databento OHLCV-1m dataset.

        TODO: import databento; db.Historical(api_key).timeseries.get_range(...)
        TODO: schema="ohlcv-1m", symbols=[symbol], start=start, end=end
        TODO: map DataFrame rows -> BarData
        """
        raise NotImplementedError(
            f"from_databento not implemented yet ({symbol} {start}..{end}). "
            "Wire databento.Historical.timeseries.get_range with schema=ohlcv-1m."
        )

    # ── Synthetic (WORKS) ──

    @staticmethod
    def synthetic_bars(
        n: int,
        start_price: float = 3500.0,
        drift: float = 0.0,
        vol: float = 0.02,
        symbol: str = "SYN",
        start: datetime | None = None,
        interval_minutes: int = 5,
        seed: int | None = 42,
    ) -> list[BarData]:
        """Generate GBM-synthetic OHLCV bars for backtest smoke-testing.

        Uses discrete GBM: S_{t+1} = S_t * exp((drift - vol^2/2) * dt + vol * sqrt(dt) * Z).

        Args:
            n: number of bars
            start_price: initial close
            drift: per-step expected return (decimal)
            vol: per-step volatility (decimal)
            symbol: BarData.symbol value
            start: timestamp of first bar (default: now UTC)
            interval_minutes: bar spacing
            seed: RNG seed (None = non-deterministic)
        """
        if n <= 0:
            return []
        rng = random.Random(seed)
        ts = start or datetime.now(UTC)
        step = timedelta(minutes=interval_minutes)

        bars: list[BarData] = []
        price = float(start_price)
        dt = 1.0  # one bar per step — drift/vol already per-step
        for i in range(n):
            z = rng.gauss(0.0, 1.0)
            log_ret = (drift - 0.5 * vol * vol) * dt + vol * math.sqrt(dt) * z
            new_price = max(price * math.exp(log_ret), 0.01)
            # Build OHLC: high/low jitter proportional to vol
            high_jitter = abs(rng.gauss(0.0, vol * 0.5)) * price
            low_jitter = abs(rng.gauss(0.0, vol * 0.5)) * price
            hi = max(price, new_price) + high_jitter
            lo = max(min(price, new_price) - low_jitter, 0.01)
            bars.append(
                BarData(
                    timestamp=ts + step * i,
                    symbol=symbol,
                    open=round(price, 4),
                    high=round(hi, 4),
                    low=round(lo, 4),
                    close=round(new_price, 4),
                    volume=round(1000.0 + abs(rng.gauss(0.0, 200.0)), 2),
                )
            )
            price = new_price
        return bars

    # ── Synthetic (JUMP-DIFFUSION for crypto-like regimes) ──

    @staticmethod
    def synthetic_bars_jump(
        n: int,
        start_price: float = 3500.0,
        drift: float = 0.0,
        vol: float = 0.02,
        symbol: str = "SYN",
        start: datetime | None = None,
        interval_minutes: int = 5,
        seed: int | None = 42,
        *,
        jump_intensity: float = 0.02,
        jump_mean: float = 0.0,
        jump_vol: float = 0.015,
        regime_persist: int = 48,
        bull_drift_boost: float = 0.0015,
        bear_drift_penalty: float = 0.0015,
    ) -> list[BarData]:
        """Jump-diffusion + two-state regime-switching bars.

        Merton-style jump-diffusion plus a hidden bull/bear Markov chain with
        ``regime_persist`` bar expected duration per regime. Used for Tier-B
        crypto bots where GBM tails are too thin to expose the ATR-bracket
        expectancy that the confluence scorer is built around.

        Args mirror ``synthetic_bars``; extras:
            jump_intensity: Poisson p per bar of a jump occurring
            jump_mean:      log-return bias when a jump fires
            jump_vol:       log-return stddev of the jump magnitude
            regime_persist: expected dwell time (bars) in each regime
            bull/bear drift adders applied per-bar conditional on regime
        """
        if n <= 0:
            return []
        rng = random.Random(seed)
        ts = start or datetime.now(UTC)
        step = timedelta(minutes=interval_minutes)

        bars: list[BarData] = []
        price = float(start_price)
        dt = 1.0
        regime = 1  # +1 bull, -1 bear
        switch_p = 1.0 / max(regime_persist, 1)
        for i in range(n):
            # regime switch (Markov)
            if rng.random() < switch_p:
                regime = -regime
            eff_drift = drift + (bull_drift_boost if regime > 0 else -bear_drift_penalty)
            z = rng.gauss(0.0, 1.0)
            log_ret = (eff_drift - 0.5 * vol * vol) * dt + vol * math.sqrt(dt) * z
            # Poisson jump
            if rng.random() < jump_intensity:
                jz = rng.gauss(0.0, 1.0)
                log_ret += jump_mean + jump_vol * jz
            new_price = max(price * math.exp(log_ret), 0.01)
            high_jitter = abs(rng.gauss(0.0, vol * 0.6)) * price
            low_jitter = abs(rng.gauss(0.0, vol * 0.6)) * price
            hi = max(price, new_price) + high_jitter
            lo = max(min(price, new_price) - low_jitter, 0.01)
            bars.append(
                BarData(
                    timestamp=ts + step * i,
                    symbol=symbol,
                    open=round(price, 4),
                    high=round(hi, 4),
                    low=round(lo, 4),
                    close=round(new_price, 4),
                    volume=round(1000.0 + abs(rng.gauss(0.0, 250.0)), 2),
                )
            )
            price = new_price
        return bars

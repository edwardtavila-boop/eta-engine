"""
EVOLUTIONARY TRADING ALGO  //  data.databento_client
========================================
Thin async facade around databento Historical SDK with billing tracker.

Real-network path uses the `databento` package (imported lazily so tests +
environments without creds stay importable). When the key is missing or the
package is absent, the methods drop back to yielding no rows while still
accruing the estimated cost (useful for dry-run forecasting).

Billing: warns when session cost crosses `cost_warn_threshold_usd`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator  # noqa: TC003 - used as runtime return annotation on async generators
from datetime import UTC, datetime
from typing import Any

from eta_engine.core.data_pipeline import BarData

log = logging.getLogger(__name__)

# USD cost estimates per schema. See Databento pricing docs; these are
# forecasting estimates only - the real SDK returns `cost_usd` per response.
_COST_PER_GB = {
    "ohlcv-1m": 0.15,
    "ohlcv-1s": 0.50,
    "trades": 1.20,
    "mbp-1": 2.50,
    "mbp-10": 5.00,
}
_AVG_BYTES_PER_BAR = 80
_AVG_BYTES_PER_TRADE = 96
_AVG_BYTES_PER_MBP = 240

# Default dataset used when none is specified. GLBX = CME Globex futures.
_DEFAULT_DATASET = "GLBX.MDP3"


class DataBentoClient:
    """Async client for Databento Historical.

    Real-network path is a thin wrapper around
    `databento.Historical(api_key).timeseries.get_range(...)`. When no key is
    configured (or the package is missing), `fetch_*` return empty async
    iterators while still accruing forecast cost against the billing tracker.
    """

    def __init__(
        self,
        api_key: str = "",
        cost_warn_threshold_usd: float = 10.0,
        dataset: str = _DEFAULT_DATASET,
    ) -> None:
        self.api_key = api_key
        self.cost_warn_threshold_usd = cost_warn_threshold_usd
        self.dataset = dataset
        self._cost_usd_accrued: float = 0.0
        self._client: Any = None

    # ------------------------------------------------------------------
    # Lazy SDK wiring
    # ------------------------------------------------------------------
    def _has_creds(self) -> bool:
        return bool(self.api_key)

    def _ensure_client(self) -> Any:  # noqa: ANN401 - databento.Historical imported lazily
        """Lazy-construct the databento.Historical client on first use."""
        if self._client is not None:
            return self._client
        if not self._has_creds():
            return None
        try:
            import databento  # noqa: PLC0415 - lazy import keeps module importable without databento
        except ImportError:
            log.warning("databento SDK not installed - dry-run fallback only")
            return None
        self._client = databento.Historical(self.api_key)
        return self._client

    # ------------------------------------------------------------------
    # Public facade
    # ------------------------------------------------------------------
    async def fetch_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        freq: str = "1m",
        dataset: str | None = None,
    ) -> AsyncIterator[BarData]:
        """Yield OHLCV bars in `[start, end)` via databento historical API.

        Cost is accrued up-front (forecast) regardless of fetch success so
        the billing tracker can warn before a real pull is kicked off.
        """
        schema = f"ohlcv-{freq}"
        estimated_rows = self._estimate_bar_count(start, end, freq)
        self._accrue_cost(schema, estimated_rows * _AVG_BYTES_PER_BAR)

        client = self._ensure_client()
        if client is None:
            # Dry-run: yield nothing, cost already accrued for forecast.
            if False:  # pragma: no cover - makes this an AsyncIterator
                yield BarData(timestamp=start, symbol=symbol, open=0.0, high=0.0, low=0.0, close=0.0, volume=0.0)
            return

        ds = dataset or self.dataset
        try:
            store = client.timeseries.get_range(
                dataset=ds,
                symbols=[symbol],
                schema=schema,
                start=start,
                end=end,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("databento fetch_bars failed: %s", e)
            if False:  # pragma: no cover
                yield BarData(timestamp=start, symbol=symbol, open=0.0, high=0.0, low=0.0, close=0.0, volume=0.0)
            return

        # Databento price fields are nano-fixed-point (divide by 1e9)
        for row in self._iter_rows(store):
            ts_ns = getattr(row, "ts_event", None) or row.get("ts_event")
            ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=UTC) if ts_ns else start
            yield BarData(
                timestamp=ts,
                symbol=symbol,
                open=_fp_px(row, "open"),
                high=_fp_px(row, "high"),
                low=_fp_px(row, "low"),
                close=_fp_px(row, "close"),
                volume=float(_field(row, "volume") or 0.0),
            )

    async def fetch_trades(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        dataset: str | None = None,
    ) -> AsyncIterator[dict]:
        """Yield tick-level trade dicts via databento 'trades' schema."""
        estimated_rows = self._estimate_trade_count(start, end)
        self._accrue_cost("trades", estimated_rows * _AVG_BYTES_PER_TRADE)

        client = self._ensure_client()
        if client is None:
            if False:  # pragma: no cover
                yield {"ts_event": start, "symbol": symbol, "price": 0.0, "size": 0.0, "side": "B"}
            return

        ds = dataset or self.dataset
        try:
            store = client.timeseries.get_range(
                dataset=ds,
                symbols=[symbol],
                schema="trades",
                start=start,
                end=end,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("databento fetch_trades failed: %s", e)
            if False:  # pragma: no cover
                yield {"ts_event": start, "symbol": symbol, "price": 0.0, "size": 0.0, "side": "B"}
            return

        for row in self._iter_rows(store):
            yield {
                "ts_event": _field(row, "ts_event"),
                "symbol": symbol,
                "price": _fp_px(row, "price"),
                "size": float(_field(row, "size") or 0.0),
                "side": _field(row, "side") or "",
            }

    async def fetch_mbp_level(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        levels: int = 10,
        dataset: str | None = None,
    ) -> AsyncIterator[dict]:
        """Yield market-by-price snapshots via databento 'mbp-N' schema."""
        schema = f"mbp-{levels}"
        estimated_rows = self._estimate_mbp_count(start, end)
        self._accrue_cost(schema, estimated_rows * _AVG_BYTES_PER_MBP)

        client = self._ensure_client()
        if client is None:
            if False:  # pragma: no cover
                yield {"ts_event": start, "symbol": symbol, "bids": [], "asks": []}
            return

        ds = dataset or self.dataset
        try:
            store = client.timeseries.get_range(
                dataset=ds,
                symbols=[symbol],
                schema=schema,
                start=start,
                end=end,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("databento fetch_mbp_level failed: %s", e)
            if False:  # pragma: no cover
                yield {"ts_event": start, "symbol": symbol, "bids": [], "asks": []}
            return

        for row in self._iter_rows(store):
            yield {
                "ts_event": _field(row, "ts_event"),
                "symbol": symbol,
                "bids": _collect_levels(row, "bid", levels),
                "asks": _collect_levels(row, "ask", levels),
            }

    # ------------------------------------------------------------------
    # Billing
    # ------------------------------------------------------------------
    @property
    def cost_usd_accrued(self) -> float:
        return round(self._cost_usd_accrued, 4)

    def reset_cost(self) -> None:
        self._cost_usd_accrued = 0.0

    def _accrue_cost(self, schema: str, total_bytes: int) -> None:
        per_gb = _COST_PER_GB.get(schema, 1.0)
        cost = per_gb * (total_bytes / (1024**3))
        prev = self._cost_usd_accrued
        self._cost_usd_accrued += cost
        if prev < self.cost_warn_threshold_usd <= self._cost_usd_accrued:
            log.warning(
                "DataBento session cost crossed $%.2f (now $%.2f)",
                self.cost_warn_threshold_usd,
                self._cost_usd_accrued,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_rows(store: Any) -> Any:  # noqa: ANN401 - databento DBNStore / list / ndarray union
        """Iterate a databento DBNStore lazily. Falls back to list/df attrs."""
        # DBNStore supports both iteration and .to_df()
        if hasattr(store, "__iter__"):
            return iter(store)
        if hasattr(store, "to_ndarray"):
            return iter(store.to_ndarray())
        return iter([])

    # ------------------------------------------------------------------
    # Estimators (used for billing forecasting)
    # ------------------------------------------------------------------
    @staticmethod
    def _estimate_bar_count(start: datetime, end: datetime, freq: str) -> int:
        secs = max((end - start).total_seconds(), 0.0)
        step = {"1s": 1, "1m": 60, "5m": 300, "1h": 3600}.get(freq, 60)
        return int(secs / step)

    @staticmethod
    def _estimate_trade_count(start: datetime, end: datetime) -> int:
        hours = max((end - start).total_seconds() / 3600.0, 0.0)
        return int(hours * 50_000)  # ~50k MNQ trades/hour rough average

    @staticmethod
    def _estimate_mbp_count(start: datetime, end: datetime) -> int:
        hours = max((end - start).total_seconds() / 3600.0, 0.0)
        return int(hours * 200_000)  # MBP events are dense


# ----------------------------------------------------------------------
# Row-field helpers - databento records are NamedTuples / numpy rows
# ----------------------------------------------------------------------
_PX_SCALE = 1e9  # databento fixed-point scaling


def _field(row: Any, name: str) -> Any:  # noqa: ANN401 - row is databento record / numpy row / dict
    """Extract a field from a databento row (supports dict / namedtuple / record)."""
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _fp_px(row: Any, name: str) -> float:  # noqa: ANN401 - row is databento record / numpy row / dict
    """Extract a fixed-point price field and divide by 1e9."""
    v = _field(row, name)
    if v is None:
        return 0.0
    try:
        return float(v) / _PX_SCALE
    except (TypeError, ValueError):
        return 0.0


def _collect_levels(row: Any, side: str, levels: int) -> list[list[float]]:  # noqa: ANN401 - row is databento record
    """Databento MBP rows use bid_px_00/ask_sz_00 style naming - pull N levels."""
    out: list[list[float]] = []
    for i in range(levels):
        px = _field(row, f"{side}_px_{i:02d}")
        sz = _field(row, f"{side}_sz_{i:02d}")
        if px is None or sz is None:
            break
        try:
            out.append([float(px) / _PX_SCALE, float(sz)])
        except (TypeError, ValueError):
            break
    return out

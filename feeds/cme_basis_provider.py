"""
EVOLUTIONARY TRADING ALGO  //  feeds.cme_basis_provider
========================================================
Basis-provider implementations for CME crypto micro futures.

A *basis provider* is a callable ``(bar) -> basis_in_bps | None`` that
maps a CME futures bar (MBT for BTC, MET for ETH) to the live premium
or discount of the future vs. its underlying spot index, expressed in
basis points::

    basis_bps = (futures_mid - spot_index) / spot_index * 10_000

The strategy ``MBTFundingBasisStrategy`` expects this callable shape and
falls back to a one-bar log return when *no* provider is wired — that
fallback is **not** basis and the strategy explicitly notes the gap.

Why this module exists
----------------------
Production today is in the degraded "log-return" mode (no provider
attached). This module exposes three concrete providers so callers can
make the choice explicit:

* ``CMEBasisProvider`` — the real provider. Reads BTC (or ETH, etc.) spot
  bars from a CSV or a callable and computes the actual basis.
* ``MockBasisProvider`` — deterministic, timestamp-keyed, for tests that
  need a known basis trajectory.
* ``LogReturnFallbackProvider`` — names the existing strategy fallback
  *explicitly*. Wiring this in lets tests / dashboards confirm "yes, we
  are deliberately running on log-return, not silently".

Generalization
--------------
Nothing here is hardcoded to MBT. ``CMEBasisProvider`` takes a generic
spot source and is intended to be reused for MET (ETH spot) and any
future CME crypto micro contract by pointing the source at the matching
spot file (``ETH_5m.csv``, ``SOL_5m.csv``, …).

Spot sources, in priority order (US-legal)
------------------------------------------
1. **CME CF Bitcoin Reference Rate (BRR)** — official daily settlement.
   Requires CME data feed. Canonical, but operationalization is pending.
2. **Coinbase BTC-USD bars** — already fetched by
   ``scripts/fetch_crypto_bars_coinbase.py``; minute-level granularity.
3. **Internal BTC bar feed** — ``data/crypto/history/BTC_5m.csv``;
   simplest input today and enough for backtest validation.
"""

from __future__ import annotations

import bisect
import csv
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Union

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.core.data_pipeline import BarData


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class BasisProvider(Protocol):
    """Structural type for any basis provider.

    The strategy stores the provider as ``Callable[[BarData], float | None]``.
    Implementations may return ``None`` when basis is unknown for the bar
    (e.g. timestamp falls outside the spot-data window) — the strategy
    interprets ``None`` as "skip this bar".
    """

    def __call__(self, bar: BarData) -> float | None:  # pragma: no cover - protocol
        ...


SpotSource = Union[str, Path, "Callable[[datetime], float | None]"]


# ---------------------------------------------------------------------------
# Real provider: futures bar vs spot reference
# ---------------------------------------------------------------------------


class CMEBasisProvider:
    """Compute (futures_close - spot_at_ts) / spot_at_ts * 10_000 in bps.

    The provider is symbol-agnostic — point it at the right spot source
    for the contract you're pricing:

    * MBT (CME Micro Bitcoin)  -> BTC spot CSV / callable
    * MET (CME Micro Ether)    -> ETH spot CSV / callable

    The future's mid is taken from ``bar.close`` (the strategy operates
    on closed-bar reads; this matches the existing fallback semantics).

    Parameters
    ----------
    btc_spot_source:
        Either a path to a CSV with columns ``time,open,high,low,close,volume``
        (epoch seconds in ``time``, same shape as ``data/crypto/history/BTC_5m.csv``)
        OR a callable ``(timestamp_utc) -> spot_price | None``.
    max_lookup_skew_seconds:
        Maximum tolerated gap between the bar timestamp and the matched
        spot tick. Defaults to one 5m bar (300s). If the nearest spot
        tick is further than this, the provider returns ``None`` and
        the strategy will skip the bar rather than poison its rolling
        window with a stale reading.
    """

    def __init__(
        self,
        btc_spot_source: SpotSource,
        *,
        max_lookup_skew_seconds: int = 300,
    ) -> None:
        self._max_skew = int(max_lookup_skew_seconds)
        if isinstance(btc_spot_source, (str, Path)):
            self._spot_path: Path | None = Path(btc_spot_source)
            self._spot_callable: Callable[[datetime], float | None] | None = None
            self._spot_times: list[int] = []
            self._spot_closes: list[float] = []
            self._load_csv(self._spot_path)
        elif callable(btc_spot_source):
            self._spot_path = None
            self._spot_callable = btc_spot_source
            self._spot_times = []
            self._spot_closes = []
        else:
            msg = (
                "btc_spot_source must be a path to a spot CSV or a "
                "Callable[[datetime], float | None]; got "
                f"{type(btc_spot_source).__name__}"
            )
            raise TypeError(msg)

    # -- loading --------------------------------------------------------

    def _load_csv(self, path: Path) -> None:
        if not path.exists():
            msg = f"Spot source CSV does not exist: {path}"
            raise FileNotFoundError(msg)
        with path.open(newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                t_raw = row.get("time") or row.get("timestamp") or row.get("timestamp_utc")
                c_raw = row.get("close") or row.get("price")
                if t_raw is None or c_raw is None:
                    continue
                try:
                    t = int(float(t_raw))
                except (TypeError, ValueError):
                    try:
                        parsed = datetime.fromisoformat(str(t_raw).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    t = int(parsed.astimezone(UTC).timestamp())
                try:
                    c = float(c_raw)
                except (TypeError, ValueError):
                    continue
                if c <= 0.0 or not math.isfinite(c):
                    continue
                self._spot_times.append(t)
                self._spot_closes.append(c)
        # Sort by time so bisect lookup is correct.
        if self._spot_times:
            paired = sorted(zip(self._spot_times, self._spot_closes, strict=True))
            self._spot_times = [p[0] for p in paired]
            self._spot_closes = [p[1] for p in paired]

    # -- lookup ---------------------------------------------------------

    def _spot_at(self, ts: datetime) -> float | None:
        if self._spot_callable is not None:
            try:
                return self._spot_callable(ts)
            except Exception:  # noqa: BLE001 - source isolation
                return None
        if not self._spot_times:
            return None
        # Convert bar timestamp to epoch seconds (UTC).
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        epoch = int(ts.astimezone(UTC).timestamp())
        idx = bisect.bisect_left(self._spot_times, epoch)
        candidates: list[int] = []
        if idx < len(self._spot_times):
            candidates.append(idx)
        if idx > 0:
            candidates.append(idx - 1)
        if not candidates:
            return None
        best_i = min(candidates, key=lambda i: abs(self._spot_times[i] - epoch))
        if abs(self._spot_times[best_i] - epoch) > self._max_skew:
            return None
        return self._spot_closes[best_i]

    # -- callable -------------------------------------------------------

    def __call__(self, bar: BarData) -> float | None:
        spot = self._spot_at(bar.timestamp)
        if spot is None or spot <= 0.0:
            return None
        if not math.isfinite(bar.close) or bar.close <= 0.0:
            return None
        return (bar.close - spot) / spot * 10_000.0


# ---------------------------------------------------------------------------
# Mock provider: deterministic, timestamp-keyed
# ---------------------------------------------------------------------------


class MockBasisProvider:
    """Deterministic provider for tests.

    Two construction modes:

    * ``MockBasisProvider({datetime: bps, ...})`` — exact-timestamp keys.
      Anything not in the map returns the ``default`` value.
    * ``MockBasisProvider(values=[...])`` — sequential per-call values,
      useful when the test only cares about the *order* of basis
      readings, not their timestamps. Returns ``default`` once the
      sequence is exhausted.
    """

    def __init__(
        self,
        mapping: dict[datetime, float] | None = None,
        *,
        values: list[float] | None = None,
        default: float | None = 0.0,
    ) -> None:
        self._mapping: dict[datetime, float] = dict(mapping or {})
        self._values: list[float] = list(values or [])
        self._idx: int = 0
        self._default = default

    def __call__(self, bar: BarData) -> float | None:
        ts = bar.timestamp
        if ts in self._mapping:
            return self._mapping[ts]
        if self._values and self._idx < len(self._values):
            v = self._values[self._idx]
            self._idx += 1
            return v
        return self._default


# ---------------------------------------------------------------------------
# Log-return fallback provider — names the implicit fallback explicitly
# ---------------------------------------------------------------------------


class LogReturnFallbackProvider:
    """Explicit provider that mirrors the strategy's silent fallback.

    The strategy currently does ``(close - prev_close) / prev_close * 10_000``
    when no provider is wired. That math is *not* basis — it's a one-bar
    return scaled to bps. This provider names that behavior so:

    * Production can opt in to the fallback explicitly via
      ``basis_provider_kind="log_return_fallback"`` and tests can assert
      "we know we are running the proxy, not real basis".
    * The honest-naming check in tests can compare this provider's output
      against the strategy's internal ``_basis_proxy`` output for the same
      bar and confirm they match exactly.

    The provider is stateful — it remembers the prior bar's close so the
    proxy can be computed without the strategy passing history through
    the callable interface.
    """

    def __init__(self) -> None:
        self._prev_close: float | None = None

    def __call__(self, bar: BarData) -> float | None:
        prev = self._prev_close
        # Always update the latest close for the next call before returning.
        # This is the same ordering the strategy's _basis_proxy uses (it
        # appends to history *after* computing the proxy).
        try:
            if prev is None or prev <= 0.0:
                return 0.0
            return (bar.close - prev) / prev * 10_000.0
        finally:
            self._prev_close = bar.close


# ---------------------------------------------------------------------------
# Factory dispatch — used by registry_strategy_bridge to pick a provider
# ---------------------------------------------------------------------------


# Default location for BTC 5m spot bars in this workspace. Centralized
# so that downstream callers don't hardcode the path.
DEFAULT_BTC_SPOT_CSV: Path = Path(
    r"C:\EvolutionaryTradingAlgo\data\crypto\history\BTC_5m.csv",
)
DEFAULT_ETH_SPOT_CSV: Path = Path(
    r"C:\EvolutionaryTradingAlgo\data\crypto\history\ETH_5m.csv",
)


def build_basis_provider(
    kind: str,
    *,
    spot_csv: str | Path | None = None,
) -> BasisProvider | None:
    """Build a basis provider from a registry-extras ``kind`` string.

    Returns ``None`` when the strategy should keep its internal log-return
    fallback (i.e. ``kind == "internal_log_return"``) — this is distinct
    from ``log_return_fallback`` which wires the explicitly-named version
    so it shows up in audits as "deliberately on the proxy".

    Recognized kinds:

    * ``"log_return_fallback"`` — wire the explicit log-return proxy.
      Honest-naming substitute for the silent fallback. Default for the
      mbt_funding_basis bot until real spot data is operationalized.
    * ``"cme_basis"`` — wire the real provider against the BTC spot CSV.
      Requires data at ``DEFAULT_BTC_SPOT_CSV`` (or override via
      ``spot_csv``).
    * ``"internal_log_return"`` — return ``None`` so the strategy keeps
      its built-in fallback. Equivalent to today's behavior, but explicit.
    """

    if kind == "log_return_fallback":
        return LogReturnFallbackProvider()
    if kind == "cme_basis":
        path = Path(spot_csv) if spot_csv else DEFAULT_BTC_SPOT_CSV
        if not path.exists():
            # Soft-fail to None so the strategy keeps the internal
            # fallback rather than raising during dispatch. The bridge
            # logs this; the docs explain how to populate the file.
            return None
        return CMEBasisProvider(path)
    if kind in ("internal_log_return", "", "none", None):
        return None
    msg = (
        f"Unknown basis_provider_kind={kind!r}. Expected one of: "
        "'log_return_fallback', 'cme_basis', 'internal_log_return'."
    )
    raise ValueError(msg)


__all__ = [
    "DEFAULT_BTC_SPOT_CSV",
    "DEFAULT_ETH_SPOT_CSV",
    "BasisProvider",
    "CMEBasisProvider",
    "LogReturnFallbackProvider",
    "MockBasisProvider",
    "build_basis_provider",
]

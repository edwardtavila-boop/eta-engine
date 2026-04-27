"""
EVOLUTIONARY TRADING ALGO  //  core.l2_capture
==================================
Level-2 order-book state machine + microstructure metrics.

Why in-house
------------
Venues differ on the wire format (Bybit vs Tradovate vs Binance) but
converge on the same update semantics: one initial SNAPSHOT plus a
stream of DELTA messages. This module is the venue-agnostic reducer:

   L2Update (snapshot | delta)  ->  L2OrderBookState  ->  L2Snapshot

The callers (``data/bybit_ws.py``, ``venues/tradovate_ws.py``, ...) do
the wire-format translation; everything downstream (features, risk,
confluence) only sees ``L2Snapshot`` and the metrics exposed here.

What it gives you
-----------------
  * ``L2OrderBookState`` -- mutable book. ``apply_snapshot(update)``
    replaces. ``apply_delta(update)`` merges: qty=0 removes, qty>0
    upserts. Monotonic sequence guard (``apply_delta`` skips a delta
    whose ``seq`` is <= the last applied, raises on a gap).
  * ``to_snapshot()`` -- immutable L2Snapshot sorted best-first.
  * Metrics: ``spread``, ``mid``, ``weighted_mid``, ``imbalance``,
    ``depth(k)``, ``notional_depth(k)``, ``microprice``.
  * ``L2CaptureSink`` -- small helper that buffers the last N snapshots
    (for feature back-fill) and exposes a ``csv_header`` / ``to_csv_row``
    pair for kill-log / tearsheet audit.

Design rules
------------
  * No venue-specific imports here.
  * Qty < 0 is invalid. Price <= 0 is invalid.
  * The state is stored in dicts (price -> qty) so updates are O(log n)
    via sort on snapshot read. The sort cost is paid at read time so
    the hot delta path stays O(1).
  * ``L2Snapshot`` (already in ``core.data_pipeline``) is the canonical
    read shape. We do not redefine it here.
"""

from __future__ import annotations

import csv
import io  # noqa: TC003  -- io.TextIOBase is a runtime annotation in write_csv
from collections import deque
from datetime import datetime  # noqa: TC003  -- pydantic needs it at runtime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, field_validator

from eta_engine.core.data_pipeline import L2Snapshot

if TYPE_CHECKING:
    from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Update model
# ---------------------------------------------------------------------------


class L2UpdateType(StrEnum):
    SNAPSHOT = "SNAPSHOT"
    DELTA = "DELTA"


class L2Update(BaseModel):
    """One wire-agnostic book update.

    ``bids`` and ``asks`` are lists of [price, qty] pairs. For DELTA
    messages, qty==0 means "remove this level" and qty>0 means
    "upsert this level". SNAPSHOT replaces the entire book.
    """

    timestamp: datetime
    symbol: str = Field(min_length=1)
    update_type: L2UpdateType
    bids: list[list[float]] = Field(default_factory=list)
    asks: list[list[float]] = Field(default_factory=list)
    seq: int | None = Field(
        default=None,
        description="Monotonic sequence id if the venue provides one",
    )

    @field_validator("bids", "asks")
    @classmethod
    def _validate_levels(cls, v: list[list[float]]) -> list[list[float]]:
        for lvl in v:
            if len(lvl) != 2:
                raise ValueError(f"level must be [price, qty]; got {lvl!r}")
            price, qty = lvl
            if price <= 0:
                raise ValueError(f"price must be > 0; got {price}")
            if qty < 0:
                raise ValueError(f"qty must be >= 0; got {qty}")
        return v


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SequenceGapError(RuntimeError):
    """Raised when a delta arrives with a non-contiguous sequence id."""


class CrossedBookError(RuntimeError):
    """Raised when best bid >= best ask after an update (indicates corruption)."""


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class L2OrderBookState:
    """Mutable in-memory L2 book for one symbol.

    Not thread-safe; callers that multiplex must wrap it in a lock or
    run one book per asyncio task. In the intended usage each venue
    feed owns one state object per subscribed symbol.
    """

    def __init__(
        self,
        *,
        symbol: str,
        strict_sequence: bool = True,
        max_depth_per_side: int | None = None,
    ) -> None:
        if not symbol:
            raise ValueError("symbol must be non-empty")
        if max_depth_per_side is not None and max_depth_per_side <= 0:
            raise ValueError("max_depth_per_side must be > 0 when provided")
        self.symbol = symbol
        self.strict_sequence = strict_sequence
        self.max_depth_per_side = max_depth_per_side
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._last_ts: datetime | None = None
        self._last_seq: int | None = None
        self._last_update_type: L2UpdateType | None = None

    # -- introspection -----------------------------------------------------

    @property
    def last_timestamp(self) -> datetime | None:
        return self._last_ts

    @property
    def last_seq(self) -> int | None:
        return self._last_seq

    @property
    def last_update_type(self) -> L2UpdateType | None:
        return self._last_update_type

    @property
    def has_both_sides(self) -> bool:
        return bool(self._bids) and bool(self._asks)

    # -- apply -------------------------------------------------------------

    def apply(self, update: L2Update) -> None:
        """Dispatch snapshot vs delta."""
        if update.symbol != self.symbol:
            raise ValueError(
                f"symbol mismatch: book is {self.symbol!r}, update is {update.symbol!r}",
            )
        if update.update_type == L2UpdateType.SNAPSHOT:
            self.apply_snapshot(update)
        else:
            self.apply_delta(update)

    def apply_snapshot(self, update: L2Update) -> None:
        """Replace the book entirely with the snapshot's levels."""
        if update.update_type != L2UpdateType.SNAPSHOT:
            raise ValueError("apply_snapshot requires update_type=SNAPSHOT")
        self._bids = {p: q for p, q in update.bids if q > 0}
        self._asks = {p: q for p, q in update.asks if q > 0}
        self._last_ts = update.timestamp
        self._last_seq = update.seq
        self._last_update_type = L2UpdateType.SNAPSHOT
        self._check_not_crossed()

    def apply_delta(self, update: L2Update) -> None:
        """Merge a delta into the current book."""
        if update.update_type != L2UpdateType.DELTA:
            raise ValueError("apply_delta requires update_type=DELTA")
        if self.strict_sequence and update.seq is not None and self._last_seq is not None:
            if update.seq <= self._last_seq:
                # Out-of-order / duplicate -- ignore silently, upstream ordering
                # guarantees we already applied this or newer state.
                return
            if update.seq != self._last_seq + 1:
                raise SequenceGapError(
                    f"sequence gap: last_seq={self._last_seq} next_seq={update.seq}",
                )

        for price, qty in update.bids:
            if qty == 0:
                self._bids.pop(price, None)
            else:
                self._bids[price] = qty
        for price, qty in update.asks:
            if qty == 0:
                self._asks.pop(price, None)
            else:
                self._asks[price] = qty

        self._last_ts = update.timestamp
        if update.seq is not None:
            self._last_seq = update.seq
        self._last_update_type = L2UpdateType.DELTA
        self._check_not_crossed()

    def _check_not_crossed(self) -> None:
        if not self.has_both_sides:
            return
        bb = max(self._bids)
        ba = min(self._asks)
        if bb >= ba:
            raise CrossedBookError(
                f"crossed book: best_bid={bb} >= best_ask={ba} on {self.symbol}",
            )

    # -- read --------------------------------------------------------------

    def _sorted_bids(self) -> list[tuple[float, float]]:
        out = sorted(self._bids.items(), key=lambda kv: -kv[0])
        if self.max_depth_per_side is not None:
            out = out[: self.max_depth_per_side]
        return out

    def _sorted_asks(self) -> list[tuple[float, float]]:
        out = sorted(self._asks.items(), key=lambda kv: kv[0])
        if self.max_depth_per_side is not None:
            out = out[: self.max_depth_per_side]
        return out

    def to_snapshot(self) -> L2Snapshot:
        """Immutable L2Snapshot of the current state (best first, lists)."""
        if self._last_ts is None:
            raise RuntimeError("no update has been applied yet; nothing to snapshot")
        bids = [[p, q] for p, q in self._sorted_bids()]
        asks = [[p, q] for p, q in self._sorted_asks()]
        return L2Snapshot(
            timestamp=self._last_ts,
            symbol=self.symbol,
            bids=bids,
            asks=asks,
        )

    # -- metrics -----------------------------------------------------------

    def best_bid(self) -> tuple[float, float] | None:
        if not self._bids:
            return None
        p = max(self._bids)
        return (p, self._bids[p])

    def best_ask(self) -> tuple[float, float] | None:
        if not self._asks:
            return None
        p = min(self._asks)
        return (p, self._asks[p])

    def spread(self) -> float | None:
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is None or ba is None:
            return None
        return ba[0] - bb[0]

    def mid(self) -> float | None:
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb[0] + ba[0]) / 2.0

    def weighted_mid(self) -> float | None:
        """Size-weighted mid at the top of book (VWAP of BBO)."""
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is None or ba is None:
            return None
        bp, bq = bb
        ap, aq = ba
        total = bq + aq
        if total == 0.0:
            return (bp + ap) / 2.0
        return (bp * aq + ap * bq) / total  # higher ask-size pulls toward bid

    def microprice(self) -> float | None:
        """Alias for weighted_mid. Included for vocabulary compatibility."""
        return self.weighted_mid()

    def imbalance(self, k: int = 5) -> float | None:
        """Order-flow imbalance at top-k depth: (bid_qty - ask_qty) / total.

        Returns in [-1, +1]. +1 means all size on the bid, -1 all on the ask.
        None if either side has no levels.
        """
        if k <= 0:
            raise ValueError("k must be > 0")
        if not self.has_both_sides:
            return None
        bid_qty = sum(q for _, q in self._sorted_bids()[:k])
        ask_qty = sum(q for _, q in self._sorted_asks()[:k])
        total = bid_qty + ask_qty
        if total == 0.0:
            return 0.0
        return (bid_qty - ask_qty) / total

    def depth(self, k: int, side: Literal["bid", "ask"]) -> float:
        """Total quantity in the top-k levels of one side."""
        if k <= 0:
            raise ValueError("k must be > 0")
        levels = self._sorted_bids() if side == "bid" else self._sorted_asks()
        return sum(q for _, q in levels[:k])

    def notional_depth(self, k: int, side: Literal["bid", "ask"]) -> float:
        """Top-k price * qty sum (USD-equivalent depth on linear products)."""
        if k <= 0:
            raise ValueError("k must be > 0")
        levels = self._sorted_bids() if side == "bid" else self._sorted_asks()
        return sum(p * q for p, q in levels[:k])


# ---------------------------------------------------------------------------
# Capture sink -- ring buffer + CSV export for audit / feature back-fill
# ---------------------------------------------------------------------------

_CSV_HEADER: tuple[str, ...] = (
    "ts",
    "symbol",
    "best_bid",
    "best_bid_qty",
    "best_ask",
    "best_ask_qty",
    "spread",
    "mid",
    "weighted_mid",
    "imbalance_top5",
    "depth_bid_top5",
    "depth_ask_top5",
)


class L2CaptureSink:
    """Ring buffer of recent L2Snapshots with a CSV audit export.

    The buffer is sized to ``maxlen`` and accessed oldest-first via
    ``iter_snapshots``. ``write_csv(out)`` flushes a header + one row per
    snapshot (never holds the buffer through the write; callers serialize
    mid-session via this helper for kill-log lineage).
    """

    def __init__(self, *, maxlen: int = 256) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be > 0")
        self._buf: deque[L2Snapshot] = deque(maxlen=maxlen)

    def record(self, snap: L2Snapshot) -> None:
        """Append a snapshot to the ring buffer."""
        self._buf.append(snap)

    def iter_snapshots(self) -> Iterable[L2Snapshot]:
        """Yield snapshots oldest-to-newest (read-only)."""
        return iter(list(self._buf))

    def __len__(self) -> int:
        return len(self._buf)

    @staticmethod
    def csv_header() -> tuple[str, ...]:
        return _CSV_HEADER

    def to_csv_rows(self) -> list[list[str]]:
        """Return rows ready to pass to csv.writer; one row per snapshot."""
        rows: list[list[str]] = []
        for snap in self.iter_snapshots():
            rows.append(_snapshot_to_row(snap))
        return rows

    def write_csv(self, out: io.TextIOBase) -> int:
        """Write header + rows to a text stream. Returns number of rows written."""
        w = csv.writer(out)
        w.writerow(_CSV_HEADER)
        n = 0
        for row in self.to_csv_rows():
            w.writerow(row)
            n += 1
        return n


def _snapshot_to_row(snap: L2Snapshot) -> list[str]:
    bb = snap.bids[0] if snap.bids else [None, None]
    ba = snap.asks[0] if snap.asks else [None, None]
    spread = snap.spread
    mid = snap.mid_price
    # Reuse book math on the snapshot without rebuilding state:
    w_mid = _snapshot_weighted_mid(snap)
    imb = _snapshot_imbalance(snap, k=5)
    depth_b = sum(q for _, q in snap.bids[:5]) if snap.bids else 0.0
    depth_a = sum(q for _, q in snap.asks[:5]) if snap.asks else 0.0
    return [
        snap.timestamp.isoformat(),
        snap.symbol,
        _fmt(bb[0]),
        _fmt(bb[1]),
        _fmt(ba[0]),
        _fmt(ba[1]),
        _fmt(spread),
        _fmt(mid),
        _fmt(w_mid),
        _fmt(imb),
        _fmt(depth_b),
        _fmt(depth_a),
    ]


def _fmt(v: float | None) -> str:
    return "" if v is None else f"{v:.10g}"


def _snapshot_weighted_mid(snap: L2Snapshot) -> float | None:
    if not snap.bids or not snap.asks:
        return None
    bp, bq = snap.bids[0]
    ap, aq = snap.asks[0]
    total = bq + aq
    if total == 0.0:
        return (bp + ap) / 2.0
    return (bp * aq + ap * bq) / total


def _snapshot_imbalance(snap: L2Snapshot, *, k: int = 5) -> float | None:
    if not snap.bids or not snap.asks:
        return None
    bid_qty = sum(q for _, q in snap.bids[:k])
    ask_qty = sum(q for _, q in snap.asks[:k])
    total = bid_qty + ask_qty
    if total == 0.0:
        return 0.0
    return (bid_qty - ask_qty) / total

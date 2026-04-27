"""Tests for eta_engine.core.l2_capture."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.core.data_pipeline import L2Snapshot
from eta_engine.core.l2_capture import (
    CrossedBookError,
    L2CaptureSink,
    L2OrderBookState,
    L2Update,
    L2UpdateType,
    SequenceGapError,
)

_T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _snap(
    symbol: str = "BTCUSDT",
    *,
    seq: int | None = 1,
    bids: list[list[float]] | None = None,
    asks: list[list[float]] | None = None,
    ts: datetime | None = None,
) -> L2Update:
    return L2Update(
        timestamp=ts or _T0,
        symbol=symbol,
        update_type=L2UpdateType.SNAPSHOT,
        bids=bids if bids is not None else [[100.0, 1.0], [99.0, 2.0]],
        asks=asks if asks is not None else [[101.0, 1.5], [102.0, 2.5]],
        seq=seq,
    )


def _delta(
    symbol: str = "BTCUSDT",
    *,
    seq: int | None,
    bids: list[list[float]] | None = None,
    asks: list[list[float]] | None = None,
    ts: datetime | None = None,
) -> L2Update:
    return L2Update(
        timestamp=ts or _T0,
        symbol=symbol,
        update_type=L2UpdateType.DELTA,
        bids=bids or [],
        asks=asks or [],
        seq=seq,
    )


# --------------------------------------------------------------------------- #
# L2Update validation
# --------------------------------------------------------------------------- #


def test_update_rejects_malformed_level_pair() -> None:
    with pytest.raises(ValueError, match="price, qty"):
        L2Update(
            timestamp=_T0,
            symbol="X",
            update_type=L2UpdateType.SNAPSHOT,
            bids=[[100.0]],
            asks=[],
        )


def test_update_rejects_non_positive_price() -> None:
    with pytest.raises(ValueError, match="price"):
        L2Update(
            timestamp=_T0,
            symbol="X",
            update_type=L2UpdateType.SNAPSHOT,
            bids=[[0.0, 1.0]],
            asks=[],
        )


def test_update_rejects_negative_qty() -> None:
    with pytest.raises(ValueError, match="qty"):
        L2Update(
            timestamp=_T0,
            symbol="X",
            update_type=L2UpdateType.SNAPSHOT,
            bids=[[100.0, -1.0]],
            asks=[],
        )


def test_update_allows_zero_qty_in_delta() -> None:
    # qty=0 is valid (signals remove). L2Update itself accepts >= 0.
    u = L2Update(
        timestamp=_T0,
        symbol="X",
        update_type=L2UpdateType.DELTA,
        bids=[[100.0, 0.0]],
        asks=[],
        seq=2,
    )
    assert u.bids == [[100.0, 0.0]]


# --------------------------------------------------------------------------- #
# Book state
# --------------------------------------------------------------------------- #


def test_book_rejects_blank_symbol() -> None:
    with pytest.raises(ValueError, match="symbol"):
        L2OrderBookState(symbol="")


def test_book_rejects_invalid_max_depth() -> None:
    with pytest.raises(ValueError, match="max_depth_per_side"):
        L2OrderBookState(symbol="X", max_depth_per_side=0)


def test_symbol_mismatch_raises() -> None:
    book = L2OrderBookState(symbol="AAA")
    with pytest.raises(ValueError, match="symbol"):
        book.apply(_snap(symbol="BBB"))


def test_to_snapshot_without_any_apply_raises() -> None:
    book = L2OrderBookState(symbol="X")
    with pytest.raises(RuntimeError, match="no update"):
        book.to_snapshot()


# --------------------------------------------------------------------------- #
# Snapshot apply
# --------------------------------------------------------------------------- #


def test_snapshot_replaces_book() -> None:
    book = L2OrderBookState(symbol="BTCUSDT")
    book.apply(_snap(bids=[[100.0, 1.0]], asks=[[101.0, 1.0]]))
    # Second snapshot completely replaces first, does not merge
    book.apply(_snap(bids=[[95.0, 5.0]], asks=[[96.0, 5.0]], seq=10))
    snap = book.to_snapshot()
    assert snap.bids == [[95.0, 5.0]]
    assert snap.asks == [[96.0, 5.0]]
    assert book.last_update_type == L2UpdateType.SNAPSHOT
    assert book.last_seq == 10


def test_apply_snapshot_requires_snapshot_type() -> None:
    book = L2OrderBookState(symbol="X")
    with pytest.raises(ValueError, match="SNAPSHOT"):
        book.apply_snapshot(_delta("X", seq=1))


def test_snapshot_drops_zero_qty_levels() -> None:
    book = L2OrderBookState(symbol="X")
    book.apply(_snap("X", bids=[[100.0, 1.0], [99.0, 0.0]], asks=[[101.0, 1.0]]))
    snap = book.to_snapshot()
    assert snap.bids == [[100.0, 1.0]]


# --------------------------------------------------------------------------- #
# Delta apply
# --------------------------------------------------------------------------- #


def test_delta_upserts_and_removes() -> None:
    book = L2OrderBookState(symbol="X")
    book.apply(_snap("X", bids=[[100.0, 1.0], [99.0, 2.0]], asks=[[101.0, 1.0], [102.0, 2.0]], seq=1))
    # Upsert 100 -> 5, remove 99, add 98
    book.apply(_delta("X", seq=2, bids=[[100.0, 5.0], [99.0, 0.0], [98.0, 3.0]]))
    snap = book.to_snapshot()
    bid_prices = [p for p, _ in snap.bids]
    assert bid_prices == [100.0, 98.0]  # 99 removed, sorted desc
    # qty at 100 updated to 5
    assert snap.bids[0] == [100.0, 5.0]


def test_apply_delta_requires_delta_type() -> None:
    book = L2OrderBookState(symbol="X")
    book.apply(_snap("X", seq=1))
    with pytest.raises(ValueError, match="DELTA"):
        book.apply_delta(_snap("X", seq=2))


def test_strict_sequence_raises_on_gap() -> None:
    book = L2OrderBookState(symbol="X", strict_sequence=True)
    book.apply(_snap("X", seq=1))
    with pytest.raises(SequenceGapError):
        book.apply(_delta("X", seq=5, bids=[[100.0, 1.0]]))


def test_strict_sequence_swallows_stale_delta() -> None:
    book = L2OrderBookState(symbol="X", strict_sequence=True)
    book.apply(_snap("X", seq=3))
    # seq=3 <= last_seq=3 -> silently skipped (duplicate/out-of-order)
    book.apply(_delta("X", seq=3, bids=[[999.0, 9.0]]))
    snap = book.to_snapshot()
    # The bid @ 999 must NOT have been applied
    assert all(p != 999.0 for p, _ in snap.bids)


def test_non_strict_sequence_accepts_gap() -> None:
    book = L2OrderBookState(symbol="X", strict_sequence=False)
    book.apply(_snap("X", seq=1))
    # No gap check -> seq=50 just merges
    book.apply(_delta("X", seq=50, bids=[[100.0, 5.0]]))
    assert book.last_seq == 50


def test_delta_without_seq_is_fine_when_last_seq_none() -> None:
    book = L2OrderBookState(symbol="X")
    book.apply(_snap("X", seq=None))
    book.apply(_delta("X", seq=None, bids=[[100.0, 5.0]]))
    snap = book.to_snapshot()
    assert [100.0, 5.0] in snap.bids


# --------------------------------------------------------------------------- #
# Crossed book
# --------------------------------------------------------------------------- #


def test_crossed_book_raises_on_snapshot() -> None:
    book = L2OrderBookState(symbol="X")
    with pytest.raises(CrossedBookError):
        book.apply(_snap("X", bids=[[101.0, 1.0]], asks=[[100.0, 1.0]]))


def test_crossed_book_raises_on_delta() -> None:
    book = L2OrderBookState(symbol="X")
    book.apply(_snap("X"))  # bb=100, ba=101
    with pytest.raises(CrossedBookError):
        # A delta that pushes the bid to 101 crosses against ask 101
        book.apply(_delta("X", seq=2, bids=[[101.5, 1.0]]))


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def test_metrics_on_simple_book() -> None:
    book = L2OrderBookState(symbol="X")
    book.apply(_snap("X", bids=[[100.0, 1.0]], asks=[[102.0, 3.0]]))
    assert book.best_bid() == (100.0, 1.0)
    assert book.best_ask() == (102.0, 3.0)
    assert book.spread() == pytest.approx(2.0)
    assert book.mid() == pytest.approx(101.0)
    # weighted_mid = (100*3 + 102*1) / (1+3) = 402/4 = 100.5 (pulled toward bid)
    assert book.weighted_mid() == pytest.approx(100.5)
    # imbalance (1-3) / 4 = -0.5 (ask-heavy)
    assert book.imbalance(k=5) == pytest.approx(-0.5)


def test_microprice_alias_matches_weighted_mid() -> None:
    book = L2OrderBookState(symbol="X")
    book.apply(_snap("X"))
    assert book.microprice() == book.weighted_mid()


def test_metrics_return_none_when_one_side_empty() -> None:
    book = L2OrderBookState(symbol="X")
    book.apply(_snap("X", bids=[[100.0, 1.0]], asks=[]))
    assert book.best_ask() is None
    assert book.spread() is None
    assert book.mid() is None
    assert book.weighted_mid() is None
    assert book.imbalance() is None


def test_depth_and_notional_depth() -> None:
    book = L2OrderBookState(symbol="X")
    book.apply(_snap("X", bids=[[100.0, 1.0], [99.0, 2.0], [98.0, 3.0]], asks=[[101.0, 2.0], [102.0, 1.0]]))
    assert book.depth(3, side="bid") == pytest.approx(6.0)
    assert book.depth(2, side="ask") == pytest.approx(3.0)
    # notional: 100*1 + 99*2 + 98*3 = 100 + 198 + 294 = 592
    assert book.notional_depth(3, side="bid") == pytest.approx(592.0)


def test_depth_rejects_invalid_k() -> None:
    book = L2OrderBookState(symbol="X")
    book.apply(_snap("X"))
    with pytest.raises(ValueError, match="k"):
        book.depth(0, side="bid")
    with pytest.raises(ValueError, match="k"):
        book.imbalance(0)
    with pytest.raises(ValueError, match="k"):
        book.notional_depth(-1, side="ask")


def test_max_depth_clamps_snapshot_output() -> None:
    book = L2OrderBookState(symbol="X", max_depth_per_side=2)
    book.apply(
        _snap(
            "X",
            bids=[[100.0, 1.0], [99.0, 2.0], [98.0, 3.0], [97.0, 4.0]],
            asks=[[101.0, 1.0], [102.0, 2.0], [103.0, 3.0]],
        )
    )
    snap = book.to_snapshot()
    assert len(snap.bids) == 2
    assert len(snap.asks) == 2


# --------------------------------------------------------------------------- #
# Snapshot sort order
# --------------------------------------------------------------------------- #


def test_snapshot_is_sorted_best_first() -> None:
    book = L2OrderBookState(symbol="X")
    book.apply(
        _snap("X", bids=[[98.0, 1.0], [100.0, 2.0], [99.0, 3.0]], asks=[[103.0, 1.0], [101.0, 2.0], [102.0, 3.0]])
    )
    snap = book.to_snapshot()
    assert [p for p, _ in snap.bids] == [100.0, 99.0, 98.0]
    assert [p for p, _ in snap.asks] == [101.0, 102.0, 103.0]


# --------------------------------------------------------------------------- #
# L2CaptureSink
# --------------------------------------------------------------------------- #


def test_sink_rejects_invalid_maxlen() -> None:
    with pytest.raises(ValueError, match="maxlen"):
        L2CaptureSink(maxlen=0)


def test_sink_ring_buffer_evicts_oldest() -> None:
    sink = L2CaptureSink(maxlen=3)
    for i in range(10):
        sink.record(
            L2Snapshot(
                timestamp=_T0 + timedelta(seconds=i),
                symbol="X",
                bids=[[100.0 - i, 1.0]],
                asks=[[101.0 + i, 1.0]],
            )
        )
    assert len(sink) == 3
    snaps = list(sink.iter_snapshots())
    # The three retained are i=7,8,9
    assert [s.timestamp for s in snaps] == [_T0 + timedelta(seconds=i) for i in (7, 8, 9)]


def test_sink_csv_header_matches_row_length() -> None:
    sink = L2CaptureSink(maxlen=2)
    sink.record(
        L2Snapshot(
            timestamp=_T0,
            symbol="X",
            bids=[[100.0, 1.0]],
            asks=[[101.0, 2.0]],
        )
    )
    header = L2CaptureSink.csv_header()
    rows = sink.to_csv_rows()
    assert len(rows) == 1
    assert len(rows[0]) == len(header)


def test_sink_write_csv_writes_header_plus_rows() -> None:
    sink = L2CaptureSink()
    sink.record(
        L2Snapshot(
            timestamp=_T0,
            symbol="X",
            bids=[[100.0, 1.0], [99.0, 2.0]],
            asks=[[101.0, 1.0], [102.0, 2.0]],
        )
    )
    sink.record(
        L2Snapshot(
            timestamp=_T0 + timedelta(seconds=5),
            symbol="X",
            bids=[[101.0, 1.0]],
            asks=[[102.0, 1.0]],
        )
    )
    buf = io.StringIO()
    n = sink.write_csv(buf)
    assert n == 2
    output = buf.getvalue().splitlines()
    # header + 2 rows
    assert len(output) == 3
    assert output[0].split(",")[0] == "ts"
    assert output[1].startswith(_T0.isoformat())


def test_sink_csv_handles_empty_side() -> None:
    sink = L2CaptureSink()
    sink.record(
        L2Snapshot(
            timestamp=_T0,
            symbol="X",
            bids=[[100.0, 1.0]],
            asks=[],
        )
    )
    rows = sink.to_csv_rows()
    # Missing side cells should be empty strings
    assert rows[0][4] == ""  # best_ask
    assert rows[0][5] == ""  # best_ask_qty


# --------------------------------------------------------------------------- #
# Delta stream integrity
# --------------------------------------------------------------------------- #


def test_stream_snapshot_then_three_deltas_matches_final_state() -> None:
    book = L2OrderBookState(symbol="X")
    book.apply(_snap("X", bids=[[100.0, 1.0], [99.0, 1.0]], asks=[[101.0, 1.0], [102.0, 1.0]], seq=1))
    book.apply(_delta("X", seq=2, bids=[[98.0, 5.0]]))
    book.apply(_delta("X", seq=3, asks=[[103.0, 7.0]]))
    book.apply(_delta("X", seq=4, bids=[[99.0, 0.0]]))  # remove 99

    snap = book.to_snapshot()
    bids = {p: q for p, q in snap.bids}
    asks = {p: q for p, q in snap.asks}
    assert bids == {100.0: 1.0, 98.0: 5.0}
    assert asks == {101.0: 1.0, 102.0: 1.0, 103.0: 7.0}
    assert book.last_seq == 4

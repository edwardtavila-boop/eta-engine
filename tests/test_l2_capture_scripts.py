from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from eta_engine.scripts import capture_depth_snapshots as depth
from eta_engine.scripts import capture_tick_stream as ticks


class _TickWriter:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record: dict) -> None:
        self.records.append(record)


class _Trade:
    def __init__(self, *, price: float, size: int) -> None:
        self.time = datetime.now(tz=UTC)
        self.price = price
        self.size = size
        self.exchange = "CME"
        self.specialConditions = ""
        self.pastLimit = False
        self.unreported = False


class _Ticker:
    def __init__(self) -> None:
        self.tickByTicks: list[_Trade] = []


def test_tick_stream_processes_only_new_ibkr_ticks() -> None:
    capture = ticks.TickStreamCapture(
        symbols=["MNQ"],
        host="127.0.0.1",
        port=4002,
        client_id=31,
    )
    writer = _TickWriter()
    capture.writers["MNQ"] = writer
    ticker = _Ticker()

    ticker.tickByTicks.append(_Trade(price=29000.25, size=1))
    capture._on_tick("MNQ", ticker)
    capture._on_tick("MNQ", ticker)
    ticker.tickByTicks.append(_Trade(price=29000.50, size=2))
    capture._on_tick("MNQ", ticker)

    assert [record["price"] for record in writer.records] == [29000.25, 29000.50]
    assert capture.stats()["MNQ"] == 2


class _DepthWriter:
    def __init__(self, capture: depth.DepthSnapshotCapture) -> None:
        self.capture = capture
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)
        self.capture.stop()


class _BookLevel:
    def __init__(self, price: float, size: int, market_maker: str = "CME") -> None:
        self.price = price
        self.size = size
        self.marketMaker = market_maker


class _DepthTicker:
    def __init__(self) -> None:
        self.updateEvent = _Event()
        self.domBids = [
            _BookLevel(29000.00, 10),
            _BookLevel(28999.75, 8),
            _BookLevel(28999.50, 6),
        ]
        self.domAsks = [
            _BookLevel(29000.25, 11),
            _BookLevel(29000.50, 9),
            _BookLevel(29000.75, 7),
        ]


class _Event:
    def __iadd__(self, _handler: Any) -> _Event:
        return self


class _FakeDepthIB:
    def __init__(self) -> None:
        self.requested: list[str] = []
        self.canceled: list[str] = []

    def reqMktDepth(self, contract, *, numRows: int, isSmartDepth: bool):  # noqa: N803
        assert numRows == 3
        assert isSmartDepth is False
        self.requested.append(contract.localSymbol)
        return _DepthTicker()

    def cancelMktDepth(self, contract, *, isSmartDepth: bool):  # noqa: N803
        assert isSmartDepth is False
        self.canceled.append(contract.localSymbol)

    def isConnected(self) -> bool:
        return True


def test_depth_snapshot_loop_uses_sync_ib_sleep_without_asyncio_run() -> None:
    capture = depth.DepthSnapshotCapture(
        symbols=["MNQ"],
        host="127.0.0.1",
        port=4002,
        client_id=32,
        depth_rows=3,
        snapshot_interval_ms=1,
        max_active_depth_requests=3,
        rotation_seconds=20.0,
    )
    writer = _DepthWriter(capture)
    capture.writers["MNQ"] = writer
    capture._tickers["MNQ"] = _DepthTicker()

    capture.snapshot_loop()

    assert len(writer.records) == 1
    assert writer.records[0]["spread"] == 0.25
    assert writer.records[0]["mid"] == 29000.125


def test_depth_capture_defaults_cover_priority_futures_books() -> None:
    assert depth._DEFAULT_SYMBOLS == ("MNQ", "NQ", "ES", "MES", "YM", "MYM", "M2K")


def test_depth_capture_batches_market_depth_requests_to_ibkr_limit() -> None:
    capture = depth.DepthSnapshotCapture(
        symbols=["MNQ", "NQ", "ES", "MES", "YM", "MYM", "M2K"],
        host="127.0.0.1",
        port=4002,
        client_id=32,
        depth_rows=3,
        snapshot_interval_ms=1000,
        max_active_depth_requests=3,
        rotation_seconds=20.0,
    )

    assert capture._batches == [
        ["MNQ", "NQ", "ES"],
        ["MES", "YM", "MYM"],
        ["M2K"],
    ]


def test_depth_capture_rotates_batches_cleanly() -> None:
    capture = depth.DepthSnapshotCapture(
        symbols=["MNQ", "NQ", "ES", "MES"],
        host="127.0.0.1",
        port=4002,
        client_id=32,
        depth_rows=3,
        snapshot_interval_ms=1000,
        max_active_depth_requests=2,
        rotation_seconds=5.0,
    )
    fake_ib = _FakeDepthIB()
    capture._ib = fake_ib
    capture._resolve = lambda sym: SimpleNamespace(exchange="CME", localSymbol=sym)  # type: ignore[method-assign]

    capture.subscribe()
    assert capture._active_symbols == ["MNQ", "NQ"]
    assert fake_ib.requested == ["MNQ", "NQ"]

    capture._active_batch_started = time.monotonic() - 6.0
    capture._rotate_if_due()

    assert capture._active_symbols == ["ES", "MES"]
    assert fake_ib.canceled == ["MNQ", "NQ"]
    assert fake_ib.requested == ["MNQ", "NQ", "ES", "MES"]

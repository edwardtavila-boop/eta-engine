from __future__ import annotations

from datetime import UTC, datetime

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


def test_depth_snapshot_loop_uses_sync_ib_sleep_without_asyncio_run() -> None:
    capture = depth.DepthSnapshotCapture(
        symbols=["MNQ"],
        host="127.0.0.1",
        port=4002,
        client_id=32,
        depth_rows=3,
        snapshot_interval_ms=1,
    )
    writer = _DepthWriter(capture)
    capture.writers["MNQ"] = writer
    capture._tickers["MNQ"] = _DepthTicker()

    capture.snapshot_loop()

    assert len(writer.records) == 1
    assert writer.records[0]["spread"] == 0.25
    assert writer.records[0]["mid"] == 29000.125

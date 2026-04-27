"""
EVOLUTIONARY TRADING ALGO  //  tests.test_dual_data_collector
=================================================
Async coverage for the MNQ + BTC + Jarvis JSONL collector.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

from eta_engine.obs.dual_data_collector import (
    CallableJarvisSource,
    CollectorConfig,
    DualDataCollector,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StaticStream:
    """Minimal TickSource that yields a fixed list of dicts."""

    def __init__(self, ticks: list[dict[str, Any]]) -> None:
        self._ticks = ticks

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        async def gen() -> AsyncIterator[dict[str, Any]]:
            for tick in self._ticks:
                yield tick
                await asyncio.sleep(0)

        return gen()


class _RaisingStream:
    """TickSource that raises after yielding one tick."""

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        async def gen() -> AsyncIterator[dict[str, Any]]:
            yield {"symbol": "MNQ", "close": 21500.0}
            await asyncio.sleep(0)
            raise RuntimeError("upstream feed exploded")

        return gen()


class _InfiniteStream:
    """TickSource that yields forever until cancelled."""

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        async def gen() -> AsyncIterator[dict[str, Any]]:
            i = 0
            while True:
                yield {"symbol": "BTC", "close": 60000.0 + i, "idx": i}
                i += 1
                await asyncio.sleep(0)

        return gen()


def _counter_jarvis(snaps_produced: list[int]) -> Callable[[], dict[str, Any]]:
    n = [0]

    def _snap() -> dict[str, Any]:
        n[0] += 1
        snaps_produced.append(n[0])
        return {"confluence": 6.5, "seq": n[0]}

    return _snap


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_mnq_and_btc_files_with_enrichment(tmp_path: Path) -> None:
    mnq = _StaticStream(
        [{"symbol": "MNQ", "close": 21500.0}, {"symbol": "MNQ", "close": 21502.0}],
    )
    btc = _StaticStream(
        [{"symbol": "BTC", "close": 60000.0}],
    )
    jarvis_snaps: list[int] = []
    jarvis = CallableJarvisSource(_counter_jarvis(jarvis_snaps))

    cfg = CollectorConfig(
        out_dir=tmp_path,
        jarvis_interval_s=0.01,
        max_ticks=5,  # small cap so the test terminates
    )
    collector = DualDataCollector(
        config=cfg,
        mnq_source=mnq,
        btc_source=btc,
        jarvis_source=jarvis,
    )
    stats = await asyncio.wait_for(collector.run(), timeout=2.0)

    # MNQ file
    mnq_path = tmp_path / "live_ticks_mnq.jsonl"
    assert mnq_path.exists()
    mnq_rows = _read_jsonl(mnq_path)
    assert len(mnq_rows) == 2
    for row in mnq_rows:
        assert row["_stream"] == "mnq"
        assert "_ts_written" in row
        assert row["symbol"] == "MNQ"

    # BTC file
    btc_path = tmp_path / "live_ticks_btc.jsonl"
    btc_rows = _read_jsonl(btc_path)
    assert len(btc_rows) == 1
    assert btc_rows[0]["_stream"] == "btc"

    # Jarvis file
    jarvis_path = tmp_path / "live_jarvis.jsonl"
    jarvis_rows = _read_jsonl(jarvis_path)
    assert len(jarvis_rows) >= 1
    assert jarvis_rows[0]["_stream"] == "jarvis"

    assert stats.mnq_ticks == 2
    assert stats.btc_ticks == 1
    assert stats.jarvis_ticks >= 1


@pytest.mark.asyncio
async def test_stats_as_dict_is_json_safe(tmp_path: Path) -> None:
    cfg = CollectorConfig(out_dir=tmp_path, jarvis_interval_s=0.01, max_ticks=3)
    collector = DualDataCollector(
        config=cfg,
        mnq_source=_StaticStream([{"symbol": "MNQ", "close": 1.0}]),
        btc_source=_StaticStream([{"symbol": "BTC", "close": 1.0}]),
        jarvis_source=CallableJarvisSource(lambda: {"x": 1}),
    )
    stats = await asyncio.wait_for(collector.run(), timeout=2.0)
    # Round-trip must succeed
    json.dumps(stats.as_dict())


# ---------------------------------------------------------------------------
# Stop semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_stop_event_terminates_run(tmp_path: Path) -> None:
    cfg = CollectorConfig(out_dir=tmp_path, jarvis_interval_s=0.05)
    stop_event = asyncio.Event()
    collector = DualDataCollector(
        config=cfg,
        mnq_source=_InfiniteStream(),
        btc_source=_InfiniteStream(),
        jarvis_source=CallableJarvisSource(lambda: {"seq": 1}),
        stop_event=stop_event,
    )

    async def _stop_after(delay: float) -> None:
        await asyncio.sleep(delay)
        stop_event.set()

    _, stats = await asyncio.gather(
        _stop_after(0.05),
        collector.run(),
    )
    # Should have at least started producing something before stop fired
    assert stats.mnq_ticks >= 0
    assert stats.jarvis_ticks >= 1


@pytest.mark.asyncio
async def test_hits_max_ticks_and_stops(tmp_path: Path) -> None:
    cfg = CollectorConfig(
        out_dir=tmp_path,
        jarvis_interval_s=0.01,
        max_ticks=4,
    )
    collector = DualDataCollector(
        config=cfg,
        mnq_source=_InfiniteStream(),
        btc_source=_InfiniteStream(),
        jarvis_source=CallableJarvisSource(lambda: {"seq": 1}),
    )
    stats = await asyncio.wait_for(collector.run(), timeout=2.0)
    total = stats.mnq_ticks + stats.btc_ticks + stats.jarvis_ticks
    assert total >= 4


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_exception_is_recorded_and_stops(tmp_path: Path) -> None:
    cfg = CollectorConfig(out_dir=tmp_path, jarvis_interval_s=0.01)
    collector = DualDataCollector(
        config=cfg,
        mnq_source=_RaisingStream(),
        btc_source=_StaticStream([{"symbol": "BTC", "close": 1.0}]),
        jarvis_source=CallableJarvisSource(lambda: {"x": 1}),
    )
    stats = await asyncio.wait_for(collector.run(), timeout=2.0)
    # The failing stream wrote at least one tick before blowing up
    assert stats.mnq_ticks == 1
    assert any(e.startswith("mnq:") for e in stats.errors)


@pytest.mark.asyncio
async def test_jarvis_exception_is_recorded_and_stops(tmp_path: Path) -> None:
    def _boom() -> dict[str, Any]:
        raise ValueError("jarvis down")

    cfg = CollectorConfig(out_dir=tmp_path, jarvis_interval_s=0.01)
    collector = DualDataCollector(
        config=cfg,
        mnq_source=_InfiniteStream(),
        btc_source=_InfiniteStream(),
        jarvis_source=CallableJarvisSource(_boom),
    )
    stats = await asyncio.wait_for(collector.run(), timeout=2.0)
    assert any(e.startswith("jarvis:") for e in stats.errors)


# ---------------------------------------------------------------------------
# Path properties
# ---------------------------------------------------------------------------


def test_path_properties_compose_from_config(tmp_path: Path) -> None:
    cfg = CollectorConfig(
        out_dir=tmp_path,
        mnq_filename="mnq.jsonl",
        btc_filename="btc.jsonl",
        jarvis_filename="jar.jsonl",
    )
    collector = DualDataCollector(
        config=cfg,
        mnq_source=_StaticStream([]),
        btc_source=_StaticStream([]),
        jarvis_source=CallableJarvisSource(lambda: {}),
    )
    assert collector.mnq_path == tmp_path / "mnq.jsonl"
    assert collector.btc_path == tmp_path / "btc.jsonl"
    assert collector.jarvis_path == tmp_path / "jar.jsonl"

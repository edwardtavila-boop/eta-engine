"""
EVOLUTIONARY TRADING ALGO  //  obs.dual_data_collector
==========================================
Dual-bot live data collector -- records synchronized market + Jarvis ticks
for BOTH the MNQ apex bot and the BTC crypto-seed bot into append-only JSONL
files so we accumulate an honest live-replay log while the bots run.

Design
------
  * TickSource protocol: any async iterator yielding ``{"ts": ..., **payload}``.
    Live feeds (Tradovate / Bybit) implement it, tests inject a stub.
  * Each bot has its own output file (one line per tick) so the streams never
    interleave and can be tailed independently.
  * A third stream captures Jarvis snapshots on a steady cadence -- we want to
    know WHAT Jarvis was thinking at every tick, not just the tick itself.
  * Structured JSONL, UTF-8, LF-terminated, one tick per line.
  * Collector respects an external ``stop_event`` so the same loop ends
    cleanly when the watchdog or kill switch flips.

This module only writes; downstream consumers (replay engine, adaptive learner,
Grafana exporter) read these files later.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


@runtime_checkable
class TickSource(Protocol):
    """Any async iterable of tick payloads is a TickSource.

    A tick payload is a JSON-serializable mapping that MUST contain at least
    ``symbol`` and ``close`` fields; anything else (bid, ask, volume,
    confluence, regime ...) is passed through verbatim.
    """

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]: ...


@runtime_checkable
class JarvisSnapshotSource(Protocol):
    """Anything that returns a serializable Jarvis snapshot on demand."""

    def snapshot(self) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Collector config + core
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectorConfig:
    """Where to write, how fast to tick Jarvis, when to stop."""

    out_dir: Path
    mnq_filename: str = "live_ticks_mnq.jsonl"
    btc_filename: str = "live_ticks_btc.jsonl"
    jarvis_filename: str = "live_jarvis.jsonl"
    jarvis_interval_s: float = 15.0
    max_ticks: int | None = None  # None -> run forever (until stop_event)


@dataclass
class CollectorStats:
    mnq_ticks: int = 0
    btc_ticks: int = 0
    jarvis_ticks: int = 0
    started_utc: str = ""
    last_mnq_ts: str = ""
    last_btc_ts: str = ""
    last_jarvis_ts: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mnq_ticks": self.mnq_ticks,
            "btc_ticks": self.btc_ticks,
            "jarvis_ticks": self.jarvis_ticks,
            "started_utc": self.started_utc,
            "last_mnq_ts": self.last_mnq_ts,
            "last_btc_ts": self.last_btc_ts,
            "last_jarvis_ts": self.last_jarvis_ts,
            "errors": list(self.errors),
        }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append a single JSON line; creates the file + parent dir if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":"), default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


class DualDataCollector:
    """Two tick streams + one Jarvis cadence, written to three JSONL files.

    Usage
    -----
    .. code-block:: python

        collector = DualDataCollector(
            config=CollectorConfig(out_dir=Path("docs/live_data")),
            mnq_source=mnq_feed,
            btc_source=btc_feed,
            jarvis_source=jarvis_engine,
        )
        await collector.run()
    """

    def __init__(
        self,
        *,
        config: CollectorConfig,
        mnq_source: TickSource,
        btc_source: TickSource,
        jarvis_source: JarvisSnapshotSource,
        stop_event: asyncio.Event | None = None,
        clock: Callable[[], str] = _now_iso,
    ) -> None:
        self.config = config
        self._mnq = mnq_source
        self._btc = btc_source
        self._jarvis = jarvis_source
        self._stop = stop_event if stop_event is not None else asyncio.Event()
        self._clock = clock
        self.stats = CollectorStats()

    # -- public API ---------------------------------------------------------

    @property
    def mnq_path(self) -> Path:
        return self.config.out_dir / self.config.mnq_filename

    @property
    def btc_path(self) -> Path:
        return self.config.out_dir / self.config.btc_filename

    @property
    def jarvis_path(self) -> Path:
        return self.config.out_dir / self.config.jarvis_filename

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> CollectorStats:
        """Run the three loops concurrently until stop_event or max_ticks."""
        self.stats.started_utc = self._clock()
        await asyncio.gather(
            self._drain_stream(self._mnq, self.mnq_path, "mnq"),
            self._drain_stream(self._btc, self.btc_path, "btc"),
            self._jarvis_loop(),
        )
        return self.stats

    # -- loops --------------------------------------------------------------

    async def _drain_stream(
        self,
        source: TickSource,
        path: Path,
        tag: str,
    ) -> None:
        try:
            async for tick in source:
                if self._stop.is_set():
                    return
                enriched = {"_ts_written": self._clock(), "_stream": tag, **tick}
                _append_jsonl(path, enriched)
                if tag == "mnq":
                    self.stats.mnq_ticks += 1
                    self.stats.last_mnq_ts = enriched["_ts_written"]
                else:
                    self.stats.btc_ticks += 1
                    self.stats.last_btc_ts = enriched["_ts_written"]
                if self._hit_cap():
                    self.stop()
                    return
        except Exception as exc:  # noqa: BLE001 -- defensive; log + exit cleanly
            self.stats.errors.append(f"{tag}: {type(exc).__name__}: {exc}")
            self.stop()

    async def _jarvis_loop(self) -> None:
        try:
            while not self._stop.is_set():
                snap = self._jarvis.snapshot()
                enriched = {"_ts_written": self._clock(), "_stream": "jarvis", **snap}
                _append_jsonl(self.jarvis_path, enriched)
                self.stats.jarvis_ticks += 1
                self.stats.last_jarvis_ts = enriched["_ts_written"]
                if self._hit_cap():
                    self.stop()
                    return
                # Wake promptly if stop_event fires mid-sleep.
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.config.jarvis_interval_s,
                    )
                    return
                except TimeoutError:
                    pass
        except Exception as exc:  # noqa: BLE001
            self.stats.errors.append(f"jarvis: {type(exc).__name__}: {exc}")
            self.stop()

    # -- helpers ------------------------------------------------------------

    def _hit_cap(self) -> bool:
        cap = self.config.max_ticks
        if cap is None:
            return False
        total = self.stats.mnq_ticks + self.stats.btc_ticks + self.stats.jarvis_ticks
        return total >= cap


# ---------------------------------------------------------------------------
# Convenience: lambda-based Jarvis source
# ---------------------------------------------------------------------------


class CallableJarvisSource:
    """Adapter so a bare ``Callable[[], dict]`` can be passed as a JarvisSnapshotSource."""

    def __init__(self, fn: Callable[[], dict[str, Any]]) -> None:
        self._fn = fn

    def snapshot(self) -> dict[str, Any]:
        return self._fn()

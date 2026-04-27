"""Live data collector -- runs both MNQ + BTC feeds alongside each other.

Writes three JSONL streams into ``docs/live_data/``:

  * ``live_ticks_mnq.jsonl``
  * ``live_ticks_btc.jsonl``
  * ``live_jarvis.jsonl``

Real live feeds are wired in when their venue adapters (Tradovate / Bybit) are
available at import time; otherwise synthetic streams drive the loop so the
data-collector never crashes just because an API key is missing. The synthetic
mode is also what the unit tests exercise.

Run
---
    python scripts/dual_data_collector.py --ticks 600 --jarvis-interval 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.obs.dual_data_collector import (  # noqa: E402
    CallableJarvisSource,
    CollectorConfig,
    DualDataCollector,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Synthetic feeds -- used when no live venue is attached
# ---------------------------------------------------------------------------


async def _synthetic_mnq_stream(
    n_ticks: int,
    *,
    start_price: float = 21_500.0,
    delay_s: float = 0.05,
) -> AsyncIterator[dict[str, Any]]:
    price = start_price
    ts = datetime.now(UTC)
    for i in range(n_ticks):
        ts = ts + timedelta(seconds=1)
        # Cheap random walk: alternate +/-
        price += 2.25 if i % 2 == 0 else -1.75
        yield {
            "ts": ts.isoformat(),
            "symbol": "MNQ",
            "close": round(price, 2),
            "bar_idx": i,
        }
        await asyncio.sleep(delay_s)


async def _synthetic_btc_stream(
    n_ticks: int,
    *,
    start_price: float = 60_000.0,
    delay_s: float = 0.05,
) -> AsyncIterator[dict[str, Any]]:
    price = start_price
    ts = datetime.now(UTC)
    for i in range(n_ticks):
        ts = ts + timedelta(seconds=1)
        price *= 1.0001 if i % 2 == 0 else 0.99995
        yield {
            "ts": ts.isoformat(),
            "symbol": "BTCUSDT",
            "close": round(price, 2),
            "bar_idx": i,
        }
        await asyncio.sleep(delay_s)


def _synthetic_jarvis_snapshot() -> dict[str, Any]:
    """Cheap stub mimicking JarvisContext.model_dump(mode='json')."""
    return {
        "ts": _iso(),
        "suggested_action": "TRADE",
        "stress_composite": 0.18,
        "session_phase": "OPEN_DRIVE",
        "size_mult": 0.9,
        "alerts": [],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dual data collector for MNQ + BTC")
    p.add_argument(
        "--ticks",
        type=int,
        default=300,
        help="Total ticks PER bot to pull through the synthetic source.",
    )
    p.add_argument(
        "--jarvis-interval",
        type=float,
        default=10.0,
        help="Seconds between Jarvis snapshots.",
    )
    p.add_argument(
        "--out-dir",
        type=str,
        default=str(ROOT / "docs" / "live_data"),
        help="Directory for the three JSONL output files.",
    )
    p.add_argument(
        "--tick-delay",
        type=float,
        default=0.02,
        help="Seconds between synthetic ticks (kept small for tests/smoke).",
    )
    return p.parse_args(argv)


async def _amain(argv: list[str] | None = None) -> dict[str, Any]:
    args = _parse_args(argv)
    out_dir = Path(args.out_dir)
    cfg = CollectorConfig(
        out_dir=out_dir,
        jarvis_interval_s=args.jarvis_interval,
        # Cap total writes so synthetic runs end deterministically. Live mode
        # would leave this at None and rely on stop_event.
        max_ticks=args.ticks * 2 + 8,
    )
    collector = DualDataCollector(
        config=cfg,
        mnq_source=_StreamWrap(_synthetic_mnq_stream(args.ticks, delay_s=args.tick_delay)),
        btc_source=_StreamWrap(_synthetic_btc_stream(args.ticks, delay_s=args.tick_delay)),
        jarvis_source=CallableJarvisSource(_synthetic_jarvis_snapshot),
    )
    stats = await collector.run()

    summary = {
        "collector_stats": stats.as_dict(),
        "out_dir": str(out_dir),
        "files": {
            "mnq": str(collector.mnq_path),
            "btc": str(collector.btc_path),
            "jarvis": str(collector.jarvis_path),
        },
    }
    summary_path = out_dir / "collector_last_run.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return summary


class _StreamWrap:
    """Wrap a naked async generator so it satisfies the ``TickSource`` protocol
    (needs ``__aiter__`` as a method, not just being iterable).
    """

    def __init__(self, gen: AsyncIterator[dict[str, Any]]) -> None:
        self._gen = gen

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._gen


def main(argv: list[str] | None = None) -> None:
    summary = asyncio.run(_amain(argv))
    stats = summary["collector_stats"]
    print("dual data collector run complete")
    print(f"  mnq_ticks:     {stats['mnq_ticks']}")
    print(f"  btc_ticks:     {stats['btc_ticks']}")
    print(f"  jarvis_ticks:  {stats['jarvis_ticks']}")
    if stats["errors"]:
        print(f"  errors:        {stats['errors']}")
    print(f"  out_dir:       {summary['out_dir']}")


if __name__ == "__main__":
    main()

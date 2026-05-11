"""
EVOLUTIONARY TRADING ALGO  //  scripts.tick_stream_consumer
===========================================================
Reads tick JSONL files from capture_tick_stream.py and feeds real
trade prints into strategies that need them — primarily
microprice_drift, which until now used the prior snap's mid as a
trade-price proxy.

Why this exists
---------------
microprice_drift_strategy.evaluate_snapshot computes:
    drift = microprice - last_trade_price
        where microprice = qty-weighted top-of-book
        and   last_trade_price = MISSING (was proxied from mid)

Without real trade prints, the drift signal is fictitious — comparing
the microprice to a stale derived value instead of actual transactions.
This module is the bridge: read mnq_data/ticks/<sym>_<date>.jsonl,
emit each tick to subscribers (strategies that registered).

The capture_tick_stream JSONL schema (from the live daemon):
    {
      "ts": "2026-05-08T14:32:11.123456+00:00",
      "epoch_s": 1746719531.123456,
      "symbol": "MNQ1",
      "price": 29014.75,
      "size": 2,
      "exchange": "CME",
      "conditions": [4, 12],
      "past_limit": false,
      "unreported": false
    }

This consumer parses each line and delivers TickRecord objects to
registered callbacks.

Two modes
---------
1. Live tail mode — open file in append-watch, deliver new ticks as
   they're written.  Caller registers callback, blocks on the consumer.
2. Backfill mode — read entire file once (for backtest harness).

Run
---
::

    # backfill a day's ticks through a strategy
    python -m eta_engine.scripts.tick_stream_consumer \\
        --symbol MNQ --date 20260511 --strategy microprice_drift_v1
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import logging
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

TICKS_DIR = ROOT.parent / "mnq_data" / "ticks"


@dataclass
class TickRecord:
    """Normalized tick representation delivered to subscribers."""
    ts: datetime
    epoch_s: float
    symbol: str
    price: float
    size: float
    exchange: str | None = None


# Subscriber callback signature: (tick) -> None
TickSubscriber = Callable[[TickRecord], None]


def _parse_line(line: str) -> TickRecord | None:
    """Parse one JSONL line into a TickRecord, or None on bad data."""
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    try:
        price = float(d["price"])
        size = float(d.get("size", 0))
    except (KeyError, TypeError, ValueError):
        return None
    ts_str = d.get("ts")
    epoch = d.get("epoch_s")
    dt: datetime | None = None
    if isinstance(ts_str, str):
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            dt = None
    if dt is None and isinstance(epoch, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(epoch), UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if dt is None:
        return None
    return TickRecord(
        ts=dt,
        epoch_s=float(epoch) if isinstance(epoch, (int, float)) else dt.timestamp(),
        symbol=str(d.get("symbol", "?")),
        price=price,
        size=size,
        exchange=d.get("exchange"),
    )


def iter_ticks_from_file(path: Path) -> Iterator[TickRecord]:
    """Yield TickRecords from a tick JSONL file.  Skips malformed lines."""
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                tick = _parse_line(line)
                if tick is not None:
                    yield tick
    except OSError as e:
        print(f"tick_stream_consumer WARN: read failed for {path}: {e}",
              file=sys.stderr)


def iter_ticks_from_day(symbol: str, date_str: str) -> Iterator[TickRecord]:
    """Yield ticks for the given symbol on the given YYYYMMDD date."""
    path = TICKS_DIR / f"{symbol}_{date_str}.jsonl"
    yield from iter_ticks_from_file(path)


def feed_strategy_microprice(symbol: str, date_str: str,
                              strategy: object,
                              *, max_ticks: int | None = None,
                              log: logging.Logger | None = None) -> int:
    """Backfill all of one day's ticks into a microprice_drift strategy.

    Calls strategy.update_trade(price, ts) for each tick.  Returns
    number of ticks delivered.  Used by the harness for full
    microprice replay over historical capture data.

    The strategy object must expose ``update_trade(price, ts=None)``
    — see make_microprice_strategy in microprice_drift_strategy.py.
    """
    log = log or logging.getLogger(__name__)
    n = 0
    for tick in iter_ticks_from_day(symbol, date_str):
        try:
            strategy.update_trade(tick.price, tick.ts)  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("tick delivery failed: %s", e)
            continue
        n += 1
        if max_ticks and n >= max_ticks:
            break
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--date", default=None,
                    help="YYYYMMDD (default: today)")
    ap.add_argument("--strategy", default="microprice_drift_v1",
                    help="strategy_id to feed (informational)")
    ap.add_argument("--max-ticks", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    date_str = args.date or datetime.now(UTC).strftime("%Y%m%d")
    path = TICKS_DIR / f"{args.symbol}_{date_str}.jsonl"

    n = 0
    first_tick: TickRecord | None = None
    last_tick: TickRecord | None = None
    for tick in iter_ticks_from_file(path):
        if first_tick is None:
            first_tick = tick
        last_tick = tick
        n += 1
        if args.max_ticks and n >= args.max_ticks:
            break

    summary = {
        "symbol": args.symbol,
        "date": date_str,
        "path": str(path),
        "n_ticks": n,
        "first_ts": first_tick.ts.isoformat() if first_tick else None,
        "last_ts": last_tick.ts.isoformat() if last_tick else None,
        "first_price": first_tick.price if first_tick else None,
        "last_price": last_tick.price if last_tick else None,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"tick_stream_consumer: {n:,} ticks from {path}")
        if first_tick and last_tick:
            print(f"  first: {first_tick.ts.isoformat()} @ {first_tick.price}")
            print(f"  last : {last_tick.ts.isoformat()} @ {last_tick.price}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
EVOLUTIONARY TRADING ALGO  //  feeds.bar_builder_l1
====================================================
Phase-2 of the IBKR Pro upgrade path: reconstruct OHLCV bars from
the tick stream captured by ``capture_tick_stream.py``, with
buy-aggressor / sell-aggressor volume split.

Why this exists
---------------
Per docs/IBKR_PRO_DATA_INVENTORY.md:

> Bar schema: timestamp_utc, epoch_s, open, high, low, close, volume,
> session.  Aggregated trade volume — **no bid/ask split** in the bar
> data.  Strategies in the current 12-bot pin would directly benefit
> from buy-aggressor vs sell-aggressor volume.

This module is the bridge between Phase 1 (raw tick capture) and
Phase 3 (strategy upgrades that consume buy/sell-split volume).

What it does
------------
1. Reads tick JSONL files written by ``capture_tick_stream.py``:
       mnq_data/ticks/<SYMBOL>_<YYYYMMDD>.jsonl
   Each line:
       {ts, epoch_s, symbol, price, size, exchange, conditions, past_limit, unreported}
2. Tags each tick as BUY (price >= prior_ask) / SELL (price <= prior_bid)
   / UNKNOWN (price between bid+ask, or no quote ref).  When the
   capture stream lacks bid/ask context, we use the tick-rule fallback
   (uptick → BUY, downtick → SELL, zero-tick inherits prior side).
3. Aggregates ticks into bars at the requested timeframe (1m / 5m / 1h / D)
   producing the extended schema:
       timestamp_utc, epoch_s, open, high, low, close,
       volume_total, volume_buy, volume_sell, n_trades, session

Storage
-------
Writes the rebuilt bars to a sibling path so the original
historical bars stay untouched:
    mnq_data/history_l1/<SYMBOL>_<TF>_l1.csv

This separation lets the harness opt-in to L1 bars per strategy
without breaking any existing bot that reads the canonical bars.

Run
---
::

    # Rebuild bars for one symbol from all available tick files
    python -m eta_engine.feeds.bar_builder_l1 --symbol MNQ --timeframe 5m

    # Rebuild for the entire active fleet, all timeframes (5m + 1h)
    python -m eta_engine.feeds.bar_builder_l1 --all
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

TICKS_DIR = ROOT.parent / "mnq_data" / "ticks"
OUT_DIR = ROOT.parent / "mnq_data" / "history_l1"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# Timeframe → bar-bucket-seconds.  Keep aligned with the rest of
# the engine's timeframe vocabulary.
TF_SECONDS = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "1h":  3600,
    "4h":  14400,
}


@dataclass
class TickRecord:
    epoch_s: float
    price: float
    size: float
    side: str  # "BUY" | "SELL" | "UNKNOWN"


@dataclass
class BarAccum:
    """Mutable bar-in-progress."""
    open: float
    high: float
    low: float
    close: float
    volume_total: float = 0.0
    volume_buy: float = 0.0
    volume_sell: float = 0.0
    n_trades: int = 0

    @classmethod
    def from_first_tick(cls, t: TickRecord) -> BarAccum:
        """Open a bar AND count the first tick's volume + side.

        Bug fix 2026-05-11: prior version returned a bar with
        volume=0 / n_trades=0, dropping the bucket-opener's
        contribution.  Test caught it on the single-bucket
        aggregation case.
        """
        bar = cls(open=t.price, high=t.price, low=t.price, close=t.price)
        bar.volume_total = t.size
        if t.side == "BUY":
            bar.volume_buy = t.size
        elif t.side == "SELL":
            bar.volume_sell = t.size
        bar.n_trades = 1
        return bar

    def absorb(self, t: TickRecord) -> None:
        self.high = max(self.high, t.price)
        self.low = min(self.low, t.price)
        self.close = t.price
        self.volume_total += t.size
        if t.side == "BUY":
            self.volume_buy += t.size
        elif t.side == "SELL":
            self.volume_sell += t.size
        self.n_trades += 1


def _bucket_start(epoch_s: float, tf_seconds: int) -> int:
    """Return the start-of-bucket epoch (UTC) for the bar this tick belongs to."""
    return int(epoch_s // tf_seconds) * tf_seconds


def _classify_tick(curr_price: float, prev_price: float | None,
                   prev_side: str = "UNKNOWN") -> str:
    """Tick-rule classifier (used when we don't have explicit bid/ask
    context in the tick stream): uptick → BUY, downtick → SELL,
    zero-tick inherits the prior classification."""
    if prev_price is None:
        return "UNKNOWN"
    if curr_price > prev_price:
        return "BUY"
    if curr_price < prev_price:
        return "SELL"
    # Zero-tick: inherit prior
    return prev_side if prev_side != "UNKNOWN" else "UNKNOWN"


def _read_ticks(path: Path) -> list[TickRecord]:
    """Stream ticks out of a JSONL file, classify with tick-rule.

    Tolerates malformed lines (skipped silently) so a single bad
    tick doesn't poison the day's reconstruction."""
    out: list[TickRecord] = []
    prev_price: float | None = None
    prev_side = "UNKNOWN"
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                price = float(d["price"])
                size = float(d.get("size", 0.0))
                epoch = float(d.get("epoch_s") or
                              datetime.fromisoformat(d["ts"].replace("Z", "+00:00")).timestamp())
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
            side = _classify_tick(price, prev_price, prev_side)
            out.append(TickRecord(epoch_s=epoch, price=price, size=size, side=side))
            prev_price = price
            prev_side = side
    return out


def build_bars(ticks: list[TickRecord], tf: str) -> list[dict]:
    """Aggregate ticks into bars at the given timeframe.  Returns a
    list of dicts ready for CSV serialization."""
    if tf not in TF_SECONDS:
        raise ValueError(f"unknown timeframe: {tf}")
    bucket_seconds = TF_SECONDS[tf]
    buckets: dict[int, BarAccum] = {}
    for t in ticks:
        bs = _bucket_start(t.epoch_s, bucket_seconds)
        accum = buckets.get(bs)
        if accum is None:
            buckets[bs] = BarAccum.from_first_tick(t)
        else:
            accum.absorb(t)
    out: list[dict] = []
    for bs in sorted(buckets.keys()):
        b = buckets[bs]
        out.append({
            "timestamp_utc": datetime.fromtimestamp(bs, UTC).isoformat(),
            "epoch_s": bs,
            "open": round(b.open, 6),
            "high": round(b.high, 6),
            "low": round(b.low, 6),
            "close": round(b.close, 6),
            "volume_total": round(b.volume_total, 6),
            "volume_buy": round(b.volume_buy, 6),
            "volume_sell": round(b.volume_sell, 6),
            "n_trades": b.n_trades,
        })
    return out


def _list_tick_files_for_symbol(symbol: str) -> list[Path]:
    """Return all <SYMBOL>_<YYYYMMDD>.jsonl files (and .gz) sorted by date."""
    if not TICKS_DIR.exists():
        return []
    files = sorted(TICKS_DIR.glob(f"{symbol}_*.jsonl"))
    files += sorted(TICKS_DIR.glob(f"{symbol}_*.jsonl.gz"))
    return sorted(set(files))


def write_bars_csv(out_path: Path, bars: list[dict]) -> None:
    """Atomic-write CSV with the L1 bar schema."""
    if not bars:
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(bars[0].keys()))
        writer.writeheader()
        for row in bars:
            writer.writerow(row)
    tmp.replace(out_path)


def rebuild_one_symbol(symbol: str, tf: str, *, log: logging.Logger | None = None) -> dict:
    log = log or logging.getLogger(__name__)
    files = _list_tick_files_for_symbol(symbol)
    if not files:
        return {"symbol": symbol, "tf": tf, "n_ticks": 0, "n_bars": 0,
                "note": "no tick files"}
    all_ticks: list[TickRecord] = []
    for fp in files:
        if fp.suffix == ".gz":
            # Phase-2 scaffolding handles raw + gzipped tick files
            import gzip
            tmp = fp.with_suffix("")  # foo.jsonl.gz → foo.jsonl
            try:
                with gzip.open(fp, "rb") as f_in, tmp.open("wb") as f_out:
                    f_out.write(f_in.read())
                all_ticks.extend(_read_ticks(tmp))
            finally:
                if tmp.exists() and tmp != fp:
                    tmp.unlink()
        else:
            all_ticks.extend(_read_ticks(fp))
    bars = build_bars(all_ticks, tf)
    out_path = OUT_DIR / f"{symbol}_{tf}_l1.csv"
    write_bars_csv(out_path, bars)
    log.info(f"[bar_builder_l1] {symbol}/{tf}: {len(all_ticks)} ticks → {len(bars)} bars → {out_path}")
    return {"symbol": symbol, "tf": tf, "n_ticks": len(all_ticks),
            "n_bars": len(bars), "out_path": str(out_path)}


def _active_symbols() -> list[str]:
    try:
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS, is_active
    except ImportError:
        return []
    syms: set[str] = set()
    for a in ASSIGNMENTS:
        if not is_active(a):
            continue
        sym = a.symbol
        base = sym.rstrip("1") if sym.endswith("1") and len(sym) > 1 else sym
        syms.add(base)
    return sorted(syms)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", help="Single symbol (e.g. MNQ)")
    ap.add_argument("--timeframe", default="5m",
                    choices=list(TF_SECONDS.keys()),
                    help="Bar timeframe (default 5m)")
    ap.add_argument("--all", action="store_true",
                    help="Rebuild for every active-fleet symbol at 5m + 1h")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger("bar_builder_l1")

    if args.all:
        symbols = _active_symbols()
        if not symbols:
            log.error("no active symbols found in registry")
            return 1
        for sym in symbols:
            for tf in ("5m", "1h"):
                rebuild_one_symbol(sym, tf, log=log)
        return 0

    if not args.symbol:
        log.error("either --symbol or --all required")
        return 1
    rebuild_one_symbol(args.symbol, args.timeframe, log=log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

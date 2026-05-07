"""Resample 5-minute OHLCV bars to 1-hour bars.

Why this exists
---------------
The audit's WalkForwardEngine resolves bar paths via ``_resolve_bar_path
(sym, "1h") -> MNQ_HISTORY_ROOT / f"{sym}1_1h.csv"``. For MBT and MET
(crypto-futures) the engine had ample 5-minute data (~109k rows / 564
days) but no 1-hour file, which is why ``mbt_sweep_reclaim``,
``met_sweep_reclaim`` and ``mbt_overnight_gap`` returned 0 trades in
the 2026-05-07 strict-gate audit.

This script aggregates 5m bars into 1h bars using lossless OHLCV math:
  open  = first 5m open in the hour
  high  = max(highs)
  low   = min(lows)
  close = last 5m close in the hour
  volume = sum(volumes)

Default behaviour: process MBT and MET only. Pass ``--symbol`` to target
others. Writes to ``mnq_data/history/{SYM}1_1h.csv`` matching the
front-month convention the engine resolves.

Usage:
    python -m eta_engine.scripts.resample_bars_5m_to_1h
    python -m eta_engine.scripts.resample_bars_5m_to_1h --symbols MBT MET NQ
    python -m eta_engine.scripts.resample_bars_5m_to_1h --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[2]
HISTORY_DIR = WORKSPACE / "mnq_data" / "history"


def resample_one(symbol: str, *, dry_run: bool = False) -> int:
    src = HISTORY_DIR / f"{symbol}1_5m.csv"
    if not src.exists():
        # Try without the "1" suffix (rare convention)
        src = HISTORY_DIR / f"{symbol}_5m.csv"
        if not src.exists():
            print(f"  {symbol}: no 5m source ({HISTORY_DIR / f'{symbol}1_5m.csv'} or bare); skipping", flush=True)
            return 0

    dst = HISTORY_DIR / f"{symbol}1_1h.csv"
    df = pd.read_csv(src)
    if df.empty:
        print(f"  {symbol}: source empty; skipping", flush=True)
        return 0

    # Time column may be epoch seconds (the bar files we have all use this).
    df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("dt").sort_index()

    # Resample to 1h with right-closed, right-labelled bars (matches IBKR
    # convention: a bar labelled 14:00 contains trades from 13:00 (excl)
    # to 14:00 (incl)). Drop any bar with no trades (volume=0 + flat OHLC).
    agg = df.resample("1h", label="right", closed="right").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["open", "high", "low", "close"])

    # Convert back to epoch-seconds time column to match the source schema.
    # ``DatetimeIndex.asi8`` returns int64 in the index's storage precision,
    # which can be either nanoseconds (datetime64[ns]) or seconds
    # (datetime64[s]) depending on pandas version + tz handling. Using
    # ``.view("int64") // <scale>`` based on dtype is fragile. The safe
    # vectorized path is to call .timestamp() on each Timestamp -- it
    # always returns a float of epoch SECONDS regardless of internal
    # precision. .astype('int64') at the end coerces.
    epoch_seconds = pd.Series(agg.index).map(lambda t: int(t.timestamp())).to_numpy()
    out_df = agg.reset_index(drop=True)
    out_df.insert(0, "time", epoch_seconds)
    out_df = out_df[["time", "open", "high", "low", "close", "volume"]]

    src_rows = len(df)
    out_rows = len(out_df)
    out_days = (
        (int(out_df["time"].iloc[-1]) - int(out_df["time"].iloc[0])) / 86400
        if out_rows else 0.0
    )
    print(f"  {symbol}: {src_rows} 5m -> {out_rows} 1h ({out_days:.1f} days)", flush=True)

    if dry_run:
        print(f"    [dry-run] would write to {dst}", flush=True)
    else:
        out_df.to_csv(dst, index=False)
        print(f"    wrote {dst}", flush=True)
    return out_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resample 5m OHLCV bars to 1h.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["MBT", "MET"],
        help="Symbols to resample (default: MBT MET).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without modifying files.",
    )
    args = parser.parse_args(argv)

    print(f"resampling 5m -> 1h: {args.symbols}", flush=True)
    total = 0
    for sym in args.symbols:
        total += resample_one(sym, dry_run=args.dry_run)
    print(f"\ndone: {total} 1h bars produced{' (dry-run)' if args.dry_run else ''}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

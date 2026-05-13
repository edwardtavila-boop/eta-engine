"""
EVOLUTIONARY TRADING ALGO  //  scripts.resample_btc_timeframes
================================================================
Synthesize higher / lower timeframes from existing OHLCV data.

The data library already has BTC at 1m, 5m, 1h, and daily. We're
missing 15m, 4h, and weekly — but those are pure aggregations of
data we already have. No API calls, no rate limits, no missing
bars.

Usage::

    # Synthesize all defaults (15m, 4h, 1W from existing sources)
    python -m eta_engine.scripts.resample_btc_timeframes

    # Specific symbol + tf
    python -m eta_engine.scripts.resample_btc_timeframes \\
        --symbol ETH --tf 4h --src-tf 1h

Output schema matches existing history files:
    time,open,high,low,close,volume

OHLCV resampling rules (canonical):
    open    = first bar's open
    high    = max(highs)
    low     = min(lows)
    close   = last bar's close
    volume  = sum(volumes)

Bar-boundary alignment:
    15m   → bars start on :00, :15, :30, :45 UTC
    4h    → bars start on 00, 04, 08, 12, 16, 20 UTC
    1W    → bars start on Monday 00:00 UTC (ISO standard)
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import CRYPTO_HISTORY_ROOT  # noqa: E402

_DEFAULT_HISTORY_ROOT = CRYPTO_HISTORY_ROOT

# (target_tf, default_source_tf): synthesizing 15m from 1m is more
# accurate but slower; from 5m is faster but coarser. Pick 5m for
# the default (15m = 3×5m), 1h for 4h, daily for weekly.
_DEFAULT_SOURCE: dict[str, str] = {
    "15m": "5m",
    "4h": "1h",
    "1W": "D",
}

_TF_TO_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "D": 86400,
    "1d": 86400,
    "1W": 86400 * 7,
    "W": 86400 * 7,
}


def _read_csv(path: Path) -> list[dict[str, float]]:
    """Read OHLCV CSV. Returns list of dicts with int time + floats."""
    if not path.exists():
        return []
    out: list[dict[str, float]] = []
    with path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                out.append(
                    {
                        "time": int(row["time"]),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row["volume"]),
                    }
                )
            except (ValueError, KeyError, TypeError):
                continue
    out.sort(key=lambda r: r["time"])
    return out


def _bucket_key(ts: int, target_tf: str) -> int:
    """Compute the canonical bucket-start unix timestamp for a bar at ts."""
    dt = datetime.fromtimestamp(ts, UTC)
    if target_tf == "15m":
        # Round down to nearest 15-minute boundary
        minute = (dt.minute // 15) * 15
        b = dt.replace(minute=minute, second=0, microsecond=0)
    elif target_tf == "4h":
        # Round down to nearest 4-hour boundary
        hour = (dt.hour // 4) * 4
        b = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    elif target_tf in ("1W", "W"):
        # Monday 00:00 UTC
        days_since_mon = dt.weekday()
        b = dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_mon)
    else:
        raise ValueError(f"unsupported target timeframe: {target_tf}")
    return int(b.timestamp())


def resample(
    source_rows: list[dict[str, float]],
    target_tf: str,
) -> list[dict[str, float]]:
    """Aggregate OHLCV bars into target_tf buckets. Canonical resampling."""
    if not source_rows:
        return []
    buckets: dict[int, list[dict[str, float]]] = defaultdict(list)
    for r in source_rows:
        key = _bucket_key(r["time"], target_tf)
        buckets[key].append(r)

    out: list[dict[str, float]] = []
    for bucket_ts in sorted(buckets):
        bars = sorted(buckets[bucket_ts], key=lambda x: x["time"])
        out.append(
            {
                "time": bucket_ts,
                "open": bars[0]["open"],
                "high": max(b["high"] for b in bars),
                "low": min(b["low"] for b in bars),
                "close": bars[-1]["close"],
                "volume": sum(b["volume"] for b in bars),
            }
        )
    return out


def _write_csv(path: Path, rows: list[dict[str, float]]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow([int(r["time"]), r["open"], r["high"], r["low"], r["close"], r["volume"]])
    return len(rows)


def synthesize_one(
    history_root: Path,
    symbol: str,
    target_tf: str,
    source_tf: str,
) -> int:
    """Synthesize one timeframe. Returns row count written."""
    src_path = history_root / f"{symbol.upper()}_{source_tf}.csv"
    out_path = history_root / f"{symbol.upper()}_{target_tf}.csv"
    print(f"[resample] {symbol}/{source_tf} -> {symbol}/{target_tf}")
    src_rows = _read_csv(src_path)
    if not src_rows:
        print(f"  source file missing/empty: {src_path}")
        return 0
    print(f"  source bars: {len(src_rows)}")
    out_rows = resample(src_rows, target_tf)
    n = _write_csv(out_path, out_rows)
    print(f"  wrote {n} bars to {out_path}")
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="BTC")
    p.add_argument("--tf", default=None, help="target tf (15m, 4h, 1W). Omit to synthesize all defaults.")
    p.add_argument("--src-tf", default=None, help="source tf (default: smallest practical for target)")
    p.add_argument("--root", type=Path, default=_DEFAULT_HISTORY_ROOT)
    args = p.parse_args()

    if args.tf:
        src_tf = args.src_tf or _DEFAULT_SOURCE.get(args.tf)
        if not src_tf:
            print(f"ERROR: no default source-tf for target {args.tf}; pass --src-tf")
            return 2
        n = synthesize_one(args.root, args.symbol, args.tf, src_tf)
        return 0 if n > 0 else 2

    # Default: synthesize all common targets for the symbol
    total = 0
    for tgt, src in _DEFAULT_SOURCE.items():
        n = synthesize_one(args.root, args.symbol, tgt, src)
        total += n
    print(f"[resample] total bars synthesized: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

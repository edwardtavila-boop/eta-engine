"""Convert Databento parquet cache to the history CSV format the
eta_engine data library expects (time,open,high,low,close,volume).

Reads from mnq_backtest/.cache/parquet/, writes to data/crypto/history/.

This unlocks the full Databento tape for crypto walk-forward retesting
without re-pulling from Databento (dormant per AGENTS.md — the cache IS
the existing data surface).

Usage
-----
    python -m eta_engine.scripts.convert_parquet_cache
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
PARQUET_CACHE = WORKSPACE / "mnq_backtest" / ".cache" / "parquet"
CRYPTO_HISTORY = WORKSPACE / "data" / "crypto" / "history"

CONVERSIONS: list[tuple[str, str, str]] = [
    ("BTCUSD_1m.parquet", "BTC", "1m"),
    ("BTC_YF_D.parquet", "BTC", "D"),
    ("ETH_YF_D.parquet", "ETH", "D"),
]


def main() -> int:
    import pandas as pd

    CRYPTO_HISTORY.mkdir(parents=True, exist_ok=True)

    for parquet_name, symbol, tf in CONVERSIONS:
        src = PARQUET_CACHE / parquet_name
        if not src.exists():
            print(f"SKIP: {src} not found")
            continue

        dst = CRYPTO_HISTORY / f"{symbol}_{tf}.csv"
        print(f"Converting {src.name} -> {dst}")

        df = pd.read_parquet(src)
        print(f"  Loaded {len(df)} rows, columns: {list(df.columns)}")

        # Some parquets have different column names
        col_map = {}
        for col in df.columns:
            low = col.lower()
            if low in ("open", "high", "low", "close", "volume"):
                col_map[col] = low
            elif low in ("ts_utc", "ts_event", "ts", "timestamp"):
                col_map[col] = "ts_utc"

        if "ts_utc" not in col_map.values():
            print(f"  WARNING: no timestamp column found in {parquet_name}, skipping")
            continue

        rows = []
        for _, row in df.iterrows():
            ts_col = next(c for c, v in col_map.items() if v == "ts_utc")
            o_col = next(c for c, v in col_map.items() if v == "open")
            h_col = next(c for c, v in col_map.items() if v == "high")
            l_col = next(c for c, v in col_map.items() if v == "low")
            c_col = next(c for c, v in col_map.items() if v == "close")
            rows.append({
                "time": int(float(row[ts_col]) / 1e9),
                "open": float(row[o_col]),
                "high": float(row[h_col]),
                "low": float(row[l_col]),
                "close": float(row[c_col]),
                "volume": float(row.get("volume", 0)) if "volume" in df.columns else 0.0,
            })

        with dst.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "open", "high", "low", "close", "volume"])
            for r in rows:
                w.writerow([r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]])

        print(f"  Wrote {len(rows)} rows to {dst}")

    print("\nNext: python -m eta_engine.scripts.data_health_check")
    return 0


if __name__ == "__main__":
    sys.exit(main())

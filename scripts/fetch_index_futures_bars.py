"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_index_futures_bars
================================================================
Index-futures bar fetcher (MNQ / NQ / ES / MES).

User mandate (2026-04-27): extend MNQ + NQ 5m / 1m history so the
foundation supercharge sweep can validate intraday strategies.

Data sources tried
------------------
1. **yfinance** — easiest US-friendly source. Limits:
   * 1m: only last 7-30 days
   * 5m: only last 60 days
   * 15m / 1h: 60-730 days
   So yfinance is great for 1h+ history but gates 1m/5m extension.

2. **IBKR Client Portal Gateway** — requires the gateway running
   locally + authenticated session. Capable of returning years of
   1m/5m bars. The fetcher writes a stub that detects gateway
   availability and falls through to yfinance otherwise.

Output
------
Writes CSVs to ``C:/mnq_data/history/{SYMBOL}_{TF}.csv`` matching
the existing schema:

    time,open,high,low,close,volume

Usage
-----
    # Fetch 60 days of MNQ 5m via yfinance (default)
    python -m eta_engine.scripts.fetch_index_futures_bars \\
        --symbol MNQ --timeframe 5m

    # Fetch 730 days of NQ 1h
    python -m eta_engine.scripts.fetch_index_futures_bars \\
        --symbol NQ --timeframe 1h --period 730d

Notes for IBKR upgrade path
---------------------------
When the user has IBKR Client Portal Gateway running:
1. Modify ``_fetch_via_ibkr`` (currently a stub) following the
   pattern in ``fetch_ibkr_crypto_bars.py``.
2. Re-run with ``--source ibkr`` flag.

For now this fetcher uses yfinance which gives us up to ~60 days
of 5m and ~730 days of 1h. That moves MNQ 5m from the current
107 days to ~167 days (when we re-fetch + merge), and adds NQ
parity automatically.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


# Symbol → yfinance ticker mapping. Continuous-front-month for futures.
_YF_SYMBOL: dict[str, str] = {
    "MNQ": "MNQ=F",
    "NQ": "NQ=F",
    "ES": "ES=F",
    "MES": "MES=F",
}

# yfinance period limits per timeframe (1m: 7-30d max; 5m: 60d; 1h: 730d)
_YF_PERIOD_BY_TF: dict[str, str] = {
    "1m": "7d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "1h": "730d",
    "1d": "max",
}


def _fetch_via_yfinance(symbol: str, timeframe: str, period: str) -> list[dict]:
    """Fetch via yfinance. Returns rows in our canonical schema."""
    import yfinance as yf

    ticker = _YF_SYMBOL.get(symbol)
    if ticker is None:
        print(f"ERROR: no yfinance mapping for {symbol}")
        return []

    print(f"[yfinance] {ticker} {timeframe} period={period}")
    df = yf.Ticker(ticker).history(period=period, interval=timeframe)
    if df is None or len(df) == 0:
        print("[yfinance] empty dataframe")
        return []

    rows: list[dict] = []
    for ts, r in df.iterrows():
        # yfinance index is timezone-aware (NY time for futures)
        ts_utc = ts.tz_convert(UTC) if ts.tzinfo else ts.tz_localize(UTC)
        rows.append({
            "time": int(ts_utc.timestamp()),
            "open": float(r["Open"]),
            "high": float(r["High"]),
            "low": float(r["Low"]),
            "close": float(r["Close"]),
            "volume": float(r.get("Volume", 0.0)),
        })
    return rows


def _fetch_via_ibkr(symbol: str, timeframe: str, period: str) -> list[dict]:
    """IBKR Client Portal Gateway fetcher.

    STUB: Mirror the pattern from ``fetch_ibkr_crypto_bars.py`` to
    populate. Currently returns empty so caller can fall back to
    yfinance.
    """
    print(f"[ibkr] STUB — implement when gateway running ({symbol} {timeframe})")
    print(f"[ibkr] period requested: {period}")
    return []


def _merge_with_existing(
    out_path: Path, new_rows: list[dict],
) -> tuple[list[dict], int, int]:
    """Merge new rows with any existing CSV at out_path.
    Returns (merged_rows, n_existing, n_new_unique)."""
    existing: list[dict] = []
    if out_path.exists():
        try:
            with out_path.open("r", encoding="utf-8") as f:
                r = csv.DictReader(f)
                for row in r:
                    try:
                        existing.append({
                            "time": int(row["time"]),
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": float(row["close"]),
                            "volume": float(row.get("volume", 0.0)),
                        })
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            existing = []
    seen = {r["time"] for r in existing}
    new_unique = [r for r in new_rows if r["time"] not in seen]
    merged = existing + new_unique
    merged.sort(key=lambda r: r["time"])
    return merged, len(existing), len(new_unique)


def _write_csv(path: Path, rows: list[dict]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow([
                int(r["time"]), r["open"], r["high"],
                r["low"], r["close"], r["volume"],
            ])
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="MNQ", choices=sorted(_YF_SYMBOL))
    p.add_argument("--timeframe", default="5m",
                   choices=sorted(_YF_PERIOD_BY_TF))
    p.add_argument(
        "--period", default=None,
        help="yfinance period string (e.g. '60d', '730d'). Defaults to "
             "the max for the timeframe.",
    )
    p.add_argument("--source", default="yfinance",
                   choices=["yfinance", "ibkr"])
    p.add_argument(
        "--out", type=Path, default=None,
        help="Output CSV path. Default: C:/mnq_data/history/{SYMBOL}1_{TF}.csv",
    )
    p.add_argument("--no-merge", action="store_true",
                   help="Overwrite existing file instead of merging")
    args = p.parse_args()

    period = args.period or _YF_PERIOD_BY_TF[args.timeframe]
    out_path = args.out or Path(
        rf"C:\mnq_data\history\{args.symbol}1_{args.timeframe}.csv",
    )

    print(f"[index-futures] {args.symbol} {args.timeframe}  source={args.source}")
    print(f"[index-futures] period={period}  out={out_path}")
    print(f"[index-futures] timestamp={datetime.now(UTC).isoformat()}")

    if args.source == "ibkr":
        rows = _fetch_via_ibkr(args.symbol, args.timeframe, period)
        if not rows:
            print("[index-futures] IBKR returned no rows; falling back to yfinance")
            rows = _fetch_via_yfinance(args.symbol, args.timeframe, period)
    else:
        rows = _fetch_via_yfinance(args.symbol, args.timeframe, period)

    if not rows:
        print("[index-futures] zero rows fetched")
        return 2

    if args.no_merge:
        n = _write_csv(out_path, rows)
        print(f"[index-futures] OVERWROTE {n} rows -> {out_path}")
        return 0

    merged, n_existing, n_new = _merge_with_existing(out_path, rows)
    n = _write_csv(out_path, merged)
    print(
        f"[index-futures] merged: existing={n_existing} new={n_new} "
        f"total={n} -> {out_path}"
    )
    if merged:
        first = datetime.fromtimestamp(merged[0]["time"], UTC).date()
        last = datetime.fromtimestamp(merged[-1]["time"], UTC).date()
        days = (merged[-1]["time"] - merged[0]["time"]) / 86400
        print(f"[index-futures] coverage: {first} -> {last} ({days:.1f} days)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

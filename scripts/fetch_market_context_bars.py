"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_market_context_bars
=================================================================
Refresh macro/volatility context bars used by futures strategies.

This is intentionally separate from ``fetch_index_futures_bars``:
DXY and VIX are context indexes, not tradeable futures contracts in
the ETA broker-routing sense. Data writes stay under the canonical
workspace history root:

    C:\\EvolutionaryTradingAlgo\\mnq_data\\history\\{SYMBOL}_{TF}.csv

Supported public Yahoo symbols:
* DXY -> DX-Y.NYB
* VIX -> ^VIX

Usage
-----
    python -m eta_engine.scripts.fetch_market_context_bars --symbol DXY --timeframe 5m
    python -m eta_engine.scripts.fetch_market_context_bars --symbol VIX --timeframe 1m
"""

from __future__ import annotations

# ruff: noqa: E402, I001 -- standalone script amends sys.path before eta_engine imports.

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import MNQ_HISTORY_ROOT  # noqa: E402

_YF_SYMBOL: dict[str, str] = {
    "DXY": "DX-Y.NYB",
    "VIX": "^VIX",
}

_YF_INTERVAL_BY_TF: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "D": "1d",
}

_YF_PERIOD_BY_TF: dict[str, str] = {
    "1m": "7d",
    "5m": "60d",
    "D": "max",
}


def _output_timeframe(timeframe: str) -> str:
    return "D" if timeframe.lower() in {"d", "1d"} else timeframe


def _fetch_via_yfinance(symbol: str, timeframe: str, period: str) -> list[dict[str, Any]]:
    """Fetch context bars via yfinance in ETA history CSV schema."""
    import yfinance as yf

    symbol = symbol.upper()
    output_tf = _output_timeframe(timeframe)
    ticker = _YF_SYMBOL.get(symbol)
    interval = _YF_INTERVAL_BY_TF.get(output_tf)
    if ticker is None or interval is None:
        print(f"ERROR: unsupported context feed {symbol}/{timeframe}")
        return []

    print(f"[yfinance] {ticker} {output_tf} period={period} interval={interval}")
    df = yf.Ticker(ticker).history(period=period, interval=interval)
    if df is None or len(df) == 0:
        print("[yfinance] empty dataframe")
        return []

    rows: list[dict[str, Any]] = []
    for ts, r in df.iterrows():
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


def _read_existing(path: Path) -> list[dict[str, Any]]:
    existing: list[dict[str, Any]] = []
    if not path.exists():
        return existing
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    existing.append({
                        "time": int(float(row["time"])),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume", 0.0) or 0.0),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        return []
    return existing


def _merge_with_existing(path: Path, new_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    existing = _read_existing(path)
    seen = {int(row["time"]) for row in existing}
    new_unique = [row for row in new_rows if int(row["time"]) not in seen]
    merged = existing + new_unique
    merged.sort(key=lambda row: int(row["time"]))
    return merged, len(existing), len(new_unique)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        for row in rows:
            writer.writerow([
                int(row["time"]),
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
            ])
    return len(rows)


def _summary(
    path: Path,
    symbol: str,
    timeframe: str,
    rows: list[dict[str, Any]],
    existing: int,
    new: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "out": str(path),
        "existing_rows": existing,
        "new_rows": new,
        "total_rows": len(rows),
    }
    if rows:
        out["first"] = datetime.fromtimestamp(int(rows[0]["time"]), UTC).isoformat()
        out["last"] = datetime.fromtimestamp(int(rows[-1]["time"]), UTC).isoformat()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="DXY", choices=sorted(_YF_SYMBOL))
    parser.add_argument("--timeframe", default="5m", choices=sorted(_YF_INTERVAL_BY_TF))
    parser.add_argument("--period", default=None, help="Yahoo period string; defaults to max safe period for timeframe")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--no-merge", action="store_true", help="overwrite instead of merging")
    parser.add_argument("--json", action="store_true", help="emit machine-readable summary")
    args = parser.parse_args(argv)

    symbol = args.symbol.upper()
    timeframe = _output_timeframe(args.timeframe)
    period = args.period or _YF_PERIOD_BY_TF[timeframe]
    out_path = args.out or (MNQ_HISTORY_ROOT / f"{symbol}_{timeframe}.csv")

    print(f"[context-bars] {symbol} {timeframe} out={out_path}")
    rows = _fetch_via_yfinance(symbol, timeframe, period)
    if not rows:
        print("[context-bars] zero rows fetched")
        return 2

    if args.no_merge:
        total = _write_csv(out_path, rows)
        summary = _summary(out_path, symbol, timeframe, rows, 0, total)
    else:
        merged, existing, new = _merge_with_existing(out_path, rows)
        _write_csv(out_path, merged)
        summary = _summary(out_path, symbol, timeframe, merged, existing, new)

    print(
        f"[context-bars] merged: existing={summary['existing_rows']} "
        f"new={summary['new_rows']} total={summary['total_rows']} -> {out_path}"
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

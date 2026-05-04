"""Fetch 1yr+ BTC/ETH daily bars via yfinance (Coinbase REST limits to ~31d).

Saves to data/crypto/history/BTC_D.csv and ETH_D.csv in the canonical
ETA history schema (time,open,high,low,close,volume).

Usage:  python scripts/fetch_crypto_daily_yahoo.py [--symbols BTC,ETH]
"""

import argparse
import csv
from datetime import datetime, UTC
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("yfinance not installed. Run: pip install yfinance")
    raise SystemExit(1)


HISTORY_ROOT = Path(__file__).resolve().parents[1] / "data" / "crypto" / "history"
HISTORY_ROOT.mkdir(parents=True, exist_ok=True)

SYMBOL_MAP = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
}


def fetch_daily(symbol: str, years: int = 2) -> list[dict]:
    """Fetch daily bars from yfinance. Returns canonical schema rows."""
    ticker = SYMBOL_MAP.get(symbol.upper(), f"{symbol.upper()}-USD")
    print(f"[yfinance] Fetching {ticker} daily (period={years}y)...")
    
    try:
        df = yf.download(ticker, period=f"{years}y", interval="1d", progress=False)
    except Exception as e:
        print(f"[yfinance] Download error for {ticker}: {e}")
        return []
    
    if df.empty:
        print(f"[yfinance] Empty dataframe for {ticker}")
        return []
    
    rows: list[dict] = []
    # Handle yfinance MultiIndex columns (newer versions)
    for idx, row in df.iterrows():
        ts = idx.to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        try:
            o = float(row["Open"].iloc[0] if hasattr(row["Open"], "iloc") else row["Open"])
            h = float(row["High"].iloc[0] if hasattr(row["High"], "iloc") else row["High"])
            l = float(row["Low"].iloc[0] if hasattr(row["Low"], "iloc") else row["Low"])
            c = float(row["Close"].iloc[0] if hasattr(row["Close"], "iloc") else row["Close"])
            v = float(row["Volume"].iloc[0] if hasattr(row["Volume"], "iloc") else row["Volume"])
        except (TypeError, ValueError, IndexError):
            continue
        rows.append({
            "time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": round(o, 2),
            "high": round(h, 2),
            "low": round(l, 2),
            "close": round(c, 2),
            "volume": round(v, 2),
        })
    
    print(f"[yfinance] {len(rows)} daily rows for {ticker} ({rows[0]['time'][:10]} to {rows[-1]['time'][:10]})")
    return rows


def write_csv(symbol: str, rows: list[dict]) -> Path:
    """Write canonical ETA CSV to data/crypto/history/{SYM}_D.csv."""
    out = HISTORY_ROOT / f"{symbol.upper()}_D.csv"
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["time", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"[yfinance] Wrote {len(rows)} rows to {out}")
    return out


def main():
    parser = argparse.ArgumentParser(description="Fetch crypto daily bars via yfinance")
    parser.add_argument("--symbols", default="BTC,ETH", help="Comma-separated symbols")
    parser.add_argument("--years", type=int, default=2, help="Years of history")
    args = parser.parse_args()
    
    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    for sym in symbols:
        if sym not in SYMBOL_MAP and f"{sym}-USD" not in SYMBOL_MAP.values():
            print(f"[yfinance] Unknown symbol: {sym}, trying {sym}-USD")
        rows = fetch_daily(sym, args.years)
        if rows:
            write_csv(sym, rows)
        else:
            print(f"[yfinance] No data for {sym}")


if __name__ == "__main__":
    main()

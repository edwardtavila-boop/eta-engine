"""Layer N+1: Funding rate fetcher — pulls perpetual swap funding rates
from OKX public REST API (no auth needed).

Writes CSVs in the library-compatible format to data/crypto/history/.
Funding rates are the dominant edge for BTC/ETH/SOL perps — this unblocks
the 6 AMBER bots.

Usage
-----
    python -m eta_engine.scripts.fetch_funding_rates --symbol BTC --months 12
    python -m eta_engine.scripts.fetch_funding_rates --symbol ETH --months 12
    python -m eta_engine.scripts.fetch_funding_rates --symbol SOL --months 12
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import CRYPTO_HISTORY_ROOT as HISTORY_ROOT  # noqa: E402

_OKX_BASE = "https://www.okx.com"
_SYMBOL_TO_INST = {
    "BTC": "BTC-USD-SWAP",
    "ETH": "ETH-USD-SWAP",
    "SOL": "SOL-USD-SWAP",
}


def _fetch_funding_history(inst_id: str, limit: int = 100) -> list[dict]:
    url = f"{_OKX_BASE}/api/v5/public/funding-rate-history?instId={inst_id}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "eta-engine/fetch_funding"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("data", [])
    except Exception as e:
        print(f"  OKX fetch error: {e}")
        return []


def fetch_funding(symbol: str, months: int = 12) -> list[tuple[int, float]]:
    inst = _SYMBOL_TO_INST.get(symbol.upper())
    if inst is None:
        raise ValueError(f"Unknown symbol: {symbol}")

    cutoff = (datetime.now(tz=UTC) - timedelta(days=months * 30)).timestamp() * 1000
    rows: list[tuple[int, float]] = []
    before = ""

    while True:
        params = f"?instId={inst}&limit=100"
        if before:
            params += f"&before={before}"
        url = f"{_OKX_BASE}/api/v5/public/funding-rate-history{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "eta-engine/fetch_funding"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                entries = data.get("data", [])
        except Exception as e:
            print(f"  error: {e}")
            break

        if not entries:
            break

        for e in entries:
            ts = int(e.get("fundingTime", 0))
            rate = float(e.get("fundingRate", 0))
            if ts > cutoff:
                rows.append((ts // 1000, rate))
            before = e.get("fundingTime", "")

        if int(before) <= cutoff or len(entries) < 100:
            break
        time.sleep(0.2)

    return sorted(rows)


def write_csv(path: Path, header: tuple[str, ...], rows: list[tuple]) -> None:
    """Write a list of tuples to a CSV file with the given header."""
    with open(str(path), "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fetch_funding_rates")
    p.add_argument("--symbol", type=str, required=True)
    p.add_argument("--months", type=int, default=12)
    args = p.parse_args(argv)

    symbol = args.symbol.upper()
    out_path = HISTORY_ROOT / f"{symbol}FUND_8h.csv"
    print(f"[fetch_funding] {symbol}/8h funding -> {HISTORY_ROOT}")

    rows = fetch_funding(symbol, months=args.months)
    if not rows:
        print("  no rows fetched")
        return 1

    with out_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "funding_rate"])
        for ts, rate in rows:
            w.writerow([ts, f"{rate:.8f}"])
    print(f"[fetch_funding] wrote {len(rows)} rows to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

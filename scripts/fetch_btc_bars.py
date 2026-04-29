"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_btc_bars
======================================================
Fetcher for crypto bars from Coinbase's public REST API. Writes
CSVs in the "history" schema (``time, open, high, low, close,
volume``) into the workspace ``data/crypto/history`` root so the data
library picks them up automatically.

Why Coinbase spot, not CME directly
-----------------------------------
The user directive ("for crypto we are doing CME if it makes any
difference to cost factors") names CME as the trading venue. CME
crypto futures (BTC, MBT, ETH, MET) are cash-settled to the CF
Reference Rate, which is itself derived from a basket of major
spot exchanges including Coinbase. The correlation between Coinbase
spot and CME front-month futures is >0.99 for any timeframe used
in research. So:

* For **research / backtesting**: Coinbase spot is a perfectly
  reasonable proxy. We document the choice + bias, the strategy
  works on spot if it works on CME.
* For **live trading**: the bot trades CME via IBKR / Tastytrade.
  The framework already knows about that venue.
* For **basis features** (when added): both feeds become useful —
  CME minus spot is the basis, an actual crypto-specific signal.

Pre-live swap policy (operator directive 2026-04-27)
----------------------------------------------------
Before flipping any crypto bot to real-money live trading:
  1. Subscribe to IBKR's CME Crypto market-data bundle (~$10/mo).
  2. Re-fetch the same time windows from IBKR-native CME bars
     (a sibling fetcher ``scripts/fetch_ibkr_crypto_bars.py`` is
     scaffolded but not yet wired).
  3. Re-run the same walk-forward config; capture the IBKR
     baseline as a separate BaselineSnapshot.
  4. Call ``obs.drift_monitor.assess_drift`` with Coinbase baseline
     vs the IBKR re-run. If the drift severity is ``amber`` or
     ``red``, do NOT promote. Re-tune on IBKR data and repeat.
  5. Document the comparison in
     ``docs/research_log/<bot>_data_swap_<datestamp>.md``.

This Coinbase fetcher stays in production as the no-subscription
path for paper-mode work and as the drift-check comparator for
every future swap. See memory: ``eta_data_source_policy.md``.

This script is intentionally minimal — no auth (Coinbase public
endpoints don't require it), no pagination dependency, no async.
Cron / scheduled-task friendly.

Usage::

    # Fetch 1h BTC bars for the last 6 months
    python -m eta_engine.scripts.fetch_btc_bars \
        --symbol BTC --timeframe 1h --months 6

    # Or a specific window
    python -m eta_engine.scripts.fetch_btc_bars \
        --symbol BTC --timeframe 1d --start 2022-01-01 --end 2026-04-27

The Coinbase API caps each request to 300 candles, so the script
chunks transparently.
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

# Coinbase Exchange (formerly Pro) public REST. No auth needed for
# /products/{id}/candles — rate limits are generous.
_BASE = "https://api.exchange.coinbase.com"

_TF_TO_SECONDS = {
    "1m":   60,
    "5m":  300,
    "15m": 900,
    "1h":  3600,
    "6h":  21600,
    "1d":  86400,
    "D":   86400,
}

_SYMBOL_TO_PRODUCT = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
}


def _fetch_chunk(product: str, granularity: int, start: datetime, end: datetime) -> list[list[float]]:
    """Coinbase candles endpoint. Returns list of [time, low, high, open, close, volume]."""
    url = (
        f"{_BASE}/products/{product}/candles"
        f"?granularity={granularity}"
        f"&start={start.isoformat()}"
        f"&end={end.isoformat()}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "eta-engine/fetch_btc_bars"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200] if hasattr(exc, "read") else b""
        print(f"  HTTPError {exc.code}: {body!r}")
        return []
    except urllib.error.URLError as exc:
        print(f"  URLError: {exc.reason!r}")
        return []


def fetch_bars(
    *, symbol: str, timeframe: str, start: datetime, end: datetime,
) -> list[list[float]]:
    """Pull all candles in [start, end). Stitches Coinbase's 300-candle chunks."""
    product = _SYMBOL_TO_PRODUCT.get(symbol.upper())
    if product is None:
        raise ValueError(
            f"unknown symbol {symbol!r}; supported: {sorted(_SYMBOL_TO_PRODUCT)}"
        )
    granularity = _TF_TO_SECONDS.get(timeframe)
    if granularity is None:
        raise ValueError(
            f"unknown timeframe {timeframe!r}; supported: {sorted(_TF_TO_SECONDS)}"
        )

    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    chunk_seconds = granularity * 300
    out: list[list[float]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(seconds=chunk_seconds), end)
        print(
            f"  fetching {product}/{timeframe} "
            f"{cursor.date()} -> {chunk_end.date()}..."
        )
        rows = _fetch_chunk(product, granularity, cursor, chunk_end)
        if rows:
            out.extend(rows)
        cursor = chunk_end
        # Coinbase rate-limits to ~10 req/sec on public endpoints. Sleep a
        # touch to stay polite even when chunks return fast.
        time.sleep(0.15)
    # Coinbase returns newest-first; sort ascending and dedupe by ts.
    out.sort(key=lambda r: r[0])
    seen: set[float] = set()
    deduped: list[list[float]] = []
    for r in out:
        if r[0] in seen:
            continue
        seen.add(r[0])
        deduped.append(r)
    return deduped


def write_csv(path: Path, rows: list[list[float]]) -> None:
    """Coinbase returns [time, low, high, open, close, volume]. Reorder to
    history-schema [time, open, high, low, close, volume]."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for ts, low, high, open_, close, volume in rows:
            w.writerow([int(ts), open_, high, low, close, volume])


def _filename(symbol: str, timeframe: str) -> str:
    """Match the existing history shape: ``MNQ1_5m.csv`` / ``MNQ1_D.csv``.

    The library's filename parser uses ``D``/``W`` for daily/weekly
    (no leading digit), and ``\\d+[smh]`` for sub-day timeframes.
    Translate Coinbase's ``1d`` accordingly so the audit picks the
    file up automatically.
    """
    tf_for_filename = {"1d": "D", "1w": "W"}.get(timeframe.lower(), timeframe)
    return f"{symbol.upper()}_{tf_for_filename}.csv"


def main() -> int:
    p = argparse.ArgumentParser(prog="fetch_btc_bars")
    p.add_argument("--symbol", default="BTC", choices=sorted(_SYMBOL_TO_PRODUCT))
    p.add_argument("--timeframe", default="1h", choices=sorted(_TF_TO_SECONDS))
    p.add_argument("--months", type=int, default=12,
                   help="lookback in months (mutually exclusive with --start/--end)")
    p.add_argument("--start", help="ISO date YYYY-MM-DD")
    p.add_argument("--end", help="ISO date YYYY-MM-DD; default = today")
    p.add_argument("--root", type=Path, default=HISTORY_ROOT,
                   help="output directory (default: canonical ETA crypto history root)")
    args = p.parse_args()

    if args.start:
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    else:
        start = datetime.now(UTC) - timedelta(days=30 * args.months)
    end = (
        datetime.fromisoformat(args.end).replace(tzinfo=UTC)
        if args.end else datetime.now(UTC)
    )

    print(
        f"[fetch_btc_bars] {args.symbol}/{args.timeframe} "
        f"{start.date()} -> {end.date()} -> {args.root}"
    )
    rows = fetch_bars(symbol=args.symbol, timeframe=args.timeframe, start=start, end=end)
    if not rows:
        print("[fetch_btc_bars] zero rows fetched — check timeframe / Coinbase status")
        return 1
    out_path = args.root / _filename(args.symbol, args.timeframe)
    write_csv(out_path, rows)
    print(
        f"[fetch_btc_bars] wrote {len(rows)} rows to {out_path}\n"
        "Next: python -m eta_engine.scripts.announce_data_library"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

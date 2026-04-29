"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_funding_rates
==========================================================
Fetcher for perpetual-futures funding rates from OKX's public REST
API. Writes CSVs into the workspace ``data/crypto/history`` root using the
filename convention ``<SYMBOL>FUND_8h.csv`` (e.g. ``BTCFUND_8h.csv``).

Why OKX (not Binance)
---------------------
Binance returns HTTP 451 from US IPs ("Service unavailable from a
restricted location") for both spot and futures public endpoints.
OKX serves its public funding-rate history globally without auth.
Coinbase has a newer derivatives venue but their funding history
endpoint is auth-gated.

OKX endpoint:
``GET /api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100``
Pagination via ``before`` (newer than the given ts). Funding
publishes every 8h on perps, same cadence as Binance.

The schema we write
-------------------
The ``data.library`` parser expects two on-disk shapes (see
``data.library``). For funding we re-use the **history** shape:

    time, open, high, low, close, volume

with the funding rate stored in all four price columns (open=high=
low=close=rate) and volume=0. That makes funding bars look
syntactically like price bars to the existing parser, so the
library picks them up without a special case. The ``audit`` layer
treats them differently: a ``DataRequirement(kind="funding")``
maps to ``library.get(symbol="<X>FUND", timeframe="8h")``.

Filename collision is impossible: real perp symbols don't have
"FUND" in them.

Usage::

    python -m eta_engine.scripts.fetch_funding_rates \
        --symbol BTC --months 24
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

_BASE = "https://www.okx.com/api/v5/public/funding-rate-history"

_SYMBOL_TO_OKX = {
    "BTC": "BTC-USDT-SWAP",
    "ETH": "ETH-USDT-SWAP",
    "SOL": "SOL-USDT-SWAP",
    "XRP": "XRP-USDT-SWAP",
}


def _fetch_chunk(inst_id: str, after_ms: int | None) -> list[dict]:
    """OKX returns up to 100 events per call, newest-first.

    ``after`` filters for events with fundingTime LESS than the
    cursor — so paginating backward in time means setting ``after``
    to the oldest ts seen so far.
    """
    url = f"{_BASE}?instId={inst_id}&limit=100"
    if after_ms is not None:
        url += f"&after={after_ms}"
    req = urllib.request.Request(url, headers={"User-Agent": "eta-engine/fetch_funding"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("code") != "0":
                print(f"  OKX error: {payload.get('msg')!r}")
                return []
            return payload.get("data") or []
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200] if hasattr(exc, "read") else b""
        print(f"  HTTPError {exc.code}: {body!r}")
        return []
    except urllib.error.URLError as exc:
        print(f"  URLError: {exc.reason!r}")
        return []


def fetch_funding(*, symbol: str, start: datetime, end: datetime) -> list[tuple[int, float]]:
    """Return list of (epoch_seconds, rate) tuples in ascending time.

    OKX paginates newest-first and tops out at ~3 months of history
    on the public endpoint regardless of the cursor — that's a
    documented limit. For longer history, a separate paid feed
    (Coinglass / Laevitas) would be needed.
    """
    inst_id = _SYMBOL_TO_OKX.get(symbol.upper())
    if inst_id is None:
        raise ValueError(f"unsupported {symbol}; pick from {sorted(_SYMBOL_TO_OKX)}")

    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    out: list[tuple[int, float]] = []
    cursor: int | None = end_ms  # walk backward from end
    page = 0
    while True:
        page += 1
        rows = _fetch_chunk(inst_id, cursor)
        if not rows:
            break
        oldest_ts = None
        for r in rows:
            ts_ms = int(r["fundingTime"])
            if ts_ms < start_ms:
                continue
            if ts_ms > end_ms:
                continue
            ts_s = ts_ms // 1000
            rate = float(r["fundingRate"])
            out.append((ts_s, rate))
            if oldest_ts is None or ts_ms < oldest_ts:
                oldest_ts = ts_ms
        if oldest_ts is None or oldest_ts <= start_ms:
            break
        cursor = oldest_ts
        if page > 50:  # safety: ~5000 events back
            print(f"  hit page cap at {page}, stopping")
            break
        time.sleep(0.15)
    print(
        f"  fetched {len(out)} {inst_id} rows over {page} pages"
    )

    out.sort()
    seen: set[int] = set()
    deduped: list[tuple[int, float]] = []
    for ts, rate in out:
        if ts in seen:
            continue
        seen.add(ts)
        deduped.append((ts, rate))
    return deduped


def write_csv(path: Path, rows: list[tuple[int, float]]) -> None:
    """history-shape CSV with rate stored in OHLC. volume=0 always."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for ts, rate in rows:
            w.writerow([ts, rate, rate, rate, rate, 0])


def main() -> int:
    p = argparse.ArgumentParser(prog="fetch_funding_rates")
    p.add_argument("--symbol", default="BTC", choices=sorted(_SYMBOL_TO_OKX))
    p.add_argument("--months", type=int, default=24)
    p.add_argument("--start", help="ISO date YYYY-MM-DD")
    p.add_argument("--end", help="ISO date YYYY-MM-DD; default = today")
    p.add_argument("--root", type=Path, default=HISTORY_ROOT)
    args = p.parse_args()

    if args.start:
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    else:
        start = datetime.now(UTC) - timedelta(days=30 * args.months)
    end = (
        datetime.fromisoformat(args.end).replace(tzinfo=UTC)
        if args.end else datetime.now(UTC)
    )

    print(f"[fetch_funding_rates] {args.symbol} {start.date()} -> {end.date()}")
    rows = fetch_funding(symbol=args.symbol, start=start, end=end)
    if not rows:
        print("[fetch_funding_rates] zero rows fetched")
        return 1
    out_path = args.root / f"{args.symbol.upper()}FUND_8h.csv"
    write_csv(out_path, rows)
    print(f"[fetch_funding_rates] wrote {len(rows)} funding rates to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

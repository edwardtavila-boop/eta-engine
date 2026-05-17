"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_btc_open_interest
================================================================
Pull BTC perpetual-futures Open Interest history from Binance.

Open Interest = total number of outstanding derivative positions.
Rising OI alongside rising price = new money entering longs (real
trend confirmation). Rising OI alongside falling price = new
shorts piling on (capitulation potential). FALLING OI in either
direction = position unwinds (trend exhaustion / squeeze).

This is a UNIQUE signal — uncorrelated with price, ETF flows,
on-chain, or sentiment. It tells you about the LEVERAGE side of
the market, which moves prices in the short-term faster than any
of the existing tracked drivers.

Source: Binance Futures public API (no auth required)
    GET https://fapi.binance.com/futures/data/openInterestHist
        ?symbol=BTCUSDT&period=1h&limit=500

Limit per request is 500 candles. For 5 years of 1h history we
need ~88 chunks, paginated by cursor.

Output schema (CSV, drops in to data library as BTCOI/<tf>):
    time,open_interest_btc,open_interest_usd

Usage::

    # Default: BTC 1h, last 24 months
    python -m eta_engine.scripts.fetch_btc_open_interest

    # Specific window
    python -m eta_engine.scripts.fetch_btc_open_interest \\
        --start 2023-01-01 --end 2026-04-27 --tf 1h
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

from eta_engine.scripts import workspace_roots  # noqa: E402
from eta_engine.scripts.workspace_roots import CRYPTO_HISTORY_ROOT  # noqa: E402

# Binance is US-geo-blocked; Bybit is the US-friendly default.
# Bybit OI history: https://api.bybit.com/v5/market/open-interest
_BYBIT_BASE = "https://api.bybit.com/v5/market/open-interest"
_BINANCE_BASE = "https://fapi.binance.com/futures/data/openInterestHist"
_USER_AGENT = "eta-engine/fetch_btc_open_interest"

# Bybit's intervalTime takes specific values
_TF_TO_BYBIT_INTERVAL: dict[str, str] = {
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}

_TF_TO_PERIOD: dict[str, str] = {
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "12h": "12h",
    "1d": "1d",
}


def _fetch_chunk_bybit(
    symbol: str,
    period: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    """Bybit OI history (US-friendly). Returns 200 most recent records
    in [startTime, endTime]. Schema differs from Binance — normalize
    in the caller.
    """
    interval = _TF_TO_BYBIT_INTERVAL.get(period)
    if interval is None:
        return []
    url = (
        f"{_BYBIT_BASE}?category=linear&symbol={symbol}"
        f"&intervalTime={interval}&startTime={start_ms}&endTime={end_ms}&limit=200"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                return []
            result = data.get("result") or {}
            return result.get("list") or []
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200] if hasattr(exc, "read") else b""
        print(f"  Bybit HTTP {exc.code}: {body!r}")
        return []
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"  Bybit fetch error: {exc!r}")
        return []


def _fetch_chunk_binance(
    symbol: str,
    period: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    """Binance OI history (geo-blocked from US — fallback only)."""
    url = f"{_BINANCE_BASE}?symbol={symbol}&period={period}&startTime={start_ms}&endTime={end_ms}&limit=500"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, list) else []
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200] if hasattr(exc, "read") else b""
        print(f"  Binance HTTP {exc.code}: {body!r}")
        return []
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"  Binance fetch error: {exc!r}")
        return []


def _normalize_bybit(rows: list[dict]) -> list[dict]:
    """Bybit row shape: {timestamp: '...', openInterest: '...'}.
    Returns list of {time, sumOpenInterest, sumOpenInterestValue}.
    Bybit returns OI in BASE currency; USD value not directly given,
    so we leave open_interest_usd as 0 (price-multiplied later if needed).
    """
    out: list[dict] = []
    for r in rows:
        try:
            ts_ms = int(r["timestamp"])
            oi = float(r["openInterest"])
        except (KeyError, ValueError, TypeError):
            continue
        out.append({"timestamp": ts_ms, "sumOpenInterest": str(oi), "sumOpenInterestValue": "0"})
    return out


def _fetch_chunk(symbol: str, period: str, start_ms: int, end_ms: int) -> list[dict]:
    """Try Bybit first (US-friendly), fall back to Binance."""
    rows = _fetch_chunk_bybit(symbol, period, start_ms, end_ms)
    if rows:
        return _normalize_bybit(rows)
    rows = _fetch_chunk_binance(symbol, period, start_ms, end_ms)
    return rows


def fetch_oi(
    symbol: str = "BTCUSDT",
    period: str = "1h",
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict]:
    """Fetch OI history in chunks. Returns list of dicts with time +
    sumOpenInterest + sumOpenInterestValue."""
    if start is None:
        start = datetime.now(UTC) - timedelta(days=365)
    if end is None:
        end = datetime.now(UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    # Binance returns candles UP TO endTime, so we cursor backward.
    # Each call returns 500 candles; we walk back until we hit start.
    all_rows: list[dict] = []
    seen: set[int] = set()
    cursor_end_ms = int(end.timestamp() * 1000)
    start_ms = int(start.timestamp() * 1000)
    while cursor_end_ms > start_ms:
        chunk_start_ms = cursor_end_ms - 500 * _period_to_ms(period)
        chunk_start_ms = max(chunk_start_ms, start_ms)
        print(
            f"  {symbol}/{period} "
            f"{datetime.fromtimestamp(chunk_start_ms / 1000, UTC).date()} -> "
            f"{datetime.fromtimestamp(cursor_end_ms / 1000, UTC).date()}"
        )
        rows = _fetch_chunk(symbol, period, chunk_start_ms, cursor_end_ms)
        if not rows:
            break
        for r in rows:
            ts = int(r["timestamp"])
            if ts in seen:
                continue
            seen.add(ts)
            all_rows.append(
                {
                    "time": ts // 1000,
                    "open_interest_btc": float(r["sumOpenInterest"]),
                    "open_interest_usd": float(r["sumOpenInterestValue"]),
                }
            )
        # Advance cursor: oldest timestamp in this chunk - 1
        oldest_ts = min(int(r["timestamp"]) for r in rows)
        if oldest_ts <= start_ms:
            break
        cursor_end_ms = oldest_ts - 1
        time.sleep(0.20)  # polite rate-limit

    all_rows.sort(key=lambda r: r["time"])
    return all_rows


def _period_to_ms(period: str) -> int:
    suffix = period[-1]
    n = int(period[:-1])
    if suffix == "m":
        return n * 60 * 1000
    if suffix == "h":
        return n * 3600 * 1000
    if suffix == "d":
        return n * 86400 * 1000
    raise ValueError(f"unknown period: {period}")


def write_csv(path: Path, rows: list[dict]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open_interest_btc", "open_interest_usd"])
        for r in rows:
            w.writerow([int(r["time"]), r["open_interest_btc"], r["open_interest_usd"]])
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--tf", default="1h", choices=sorted(_TF_TO_PERIOD))
    p.add_argument("--months", type=int, default=24)
    p.add_argument("--start", type=str, default=None)
    p.add_argument("--end", type=str, default=None)
    p.add_argument("--out", type=Path, default=CRYPTO_HISTORY_ROOT / "BTCOI_1h.csv")
    args = p.parse_args(argv)
    try:
        args.out = workspace_roots.resolve_under_workspace(args.out, label="--out")
    except ValueError as exc:
        p.error(str(exc))

    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else datetime.now(UTC)
    if args.start:
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    else:
        start = end - timedelta(days=30 * args.months)

    period = _TF_TO_PERIOD[args.tf]
    print(f"[oi] {args.symbol}/{period} {start.date()} -> {end.date()}")
    rows = fetch_oi(args.symbol, period, start, end)
    if not rows:
        print("[oi] zero rows fetched")
        return 2
    n = write_csv(args.out, rows)
    last = rows[-1]
    print(
        f"[oi] wrote {n} rows to {args.out}; "
        f"last={datetime.fromtimestamp(last['time'], UTC).date()} "
        f"OI={last['open_interest_btc']:.0f} BTC "
        f"(${last['open_interest_usd'] / 1e9:.2f}B)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

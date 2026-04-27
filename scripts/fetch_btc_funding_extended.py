"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_btc_funding_extended
==================================================================
Extend BTC perpetual-futures funding-rate history via Binance.

The existing BTCFUND_8h.csv has only 96 days. Funding-rate filters
need much more history to be statistically valid. Binance's funding
rate is paid every 8 hours and has been published since 2019, so
~7 years of history is freely available.

Source: Binance Futures public API
    GET https://fapi.binance.com/fapi/v1/fundingRate
        ?symbol=BTCUSDT&startTime=...&limit=1000

Limit per request is 1000. For 7 years × 365 days × 3 fundings/day
= ~7,650 records, paginated by cursor.

Output schema (drops in to data library as BTCFUND/8h):
    time,funding_rate

Usage::

    python -m eta_engine.scripts.fetch_btc_funding_extended
    python -m eta_engine.scripts.fetch_btc_funding_extended --years 5
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


# Binance + Bybit are US-geo-blocked. BitMEX is fully US-friendly
# AND has the longest history (XBTUSD funding back to 2016-05-13).
# That's the WINNER source.
# BitMEX funding history: https://www.bitmex.com/api/v1/funding
_BITMEX_BASE = "https://www.bitmex.com/api/v1/funding"
_BYBIT_BASE = "https://api.bybit.com/v5/market/funding/history"
_BINANCE_BASE = "https://fapi.binance.com/fapi/v1/fundingRate"
_USER_AGENT = "eta-engine/fetch_btc_funding_extended"

# BitMEX symbol mapping. XBTUSD = inverse perpetual (the canonical
# 10-year-history BTC funding contract).
_BITMEX_SYMBOL_MAP: dict[str, str] = {
    "BTCUSDT": "XBTUSD",
    "BTCUSD": "XBTUSD",
    "BTC": "XBTUSD",
    "ETHUSDT": "ETHUSD",
    "ETHUSD": "ETHUSD",
    "ETH": "ETHUSD",
}


def _fetch_chunk_bitmex(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """BitMEX funding history (US-friendly, 10-year history).

    BitMEX accepts startTime / endTime as ISO timestamps and `count`
    up to 500 records per call. Returns oldest-first by default
    (perfect for forward-cursor pagination).

    Schema: {timestamp, symbol, fundingInterval, fundingRate, fundingRateDaily}
    """
    bitmex_sym = _BITMEX_SYMBOL_MAP.get(symbol)
    if bitmex_sym is None:
        return []
    # BitMEX accepts plain YYYY-MM-DD (or ISO with Z suffix). Avoid
    # ISO+offset (+00:00) — BitMEX rejects it as invalid.
    start_d = datetime.fromtimestamp(start_ms / 1000, UTC).strftime("%Y-%m-%d")
    end_d = datetime.fromtimestamp(end_ms / 1000, UTC).strftime("%Y-%m-%d")
    url = (
        f"{_BITMEX_BASE}?symbol={bitmex_sym}"
        f"&startTime={start_d}&endTime={end_d}&count=500"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, list):
                return []
            out = []
            for r in data:
                try:
                    # Convert ISO timestamp to ms since epoch
                    ts = datetime.fromisoformat(
                        r["timestamp"].replace("Z", "+00:00"),
                    )
                    out.append({
                        "fundingTime": int(ts.timestamp() * 1000),
                        "fundingRate": float(r["fundingRate"]),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
            return out
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200] if hasattr(exc, "read") else b""
        print(f"  BitMEX HTTP {exc.code}: {body!r}")
        return []
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"  BitMEX fetch error: {exc!r}")
        return []


def _fetch_chunk_bybit(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Bybit funding history (US-friendly). Returns up to 200 records."""
    url = (
        f"{_BYBIT_BASE}?category=linear&symbol={symbol}"
        f"&startTime={start_ms}&endTime={end_ms}&limit=200"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                return []
            result = data.get("result") or {}
            rows = result.get("list") or []
            # Normalize Bybit shape -> Binance shape
            out = []
            for r in rows:
                try:
                    out.append({
                        "fundingTime": int(r["fundingRateTimestamp"]),
                        "fundingRate": float(r["fundingRate"]),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
            return out
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200] if hasattr(exc, "read") else b""
        print(f"  Bybit HTTP {exc.code}: {body!r}")
        return []
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"  Bybit fetch error: {exc!r}")
        return []


def _fetch_chunk_binance(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Binance funding (geo-blocked from US — fallback only)."""
    url = (
        f"{_BINANCE_BASE}?symbol={symbol}&startTime={start_ms}&endTime={end_ms}&limit=1000"
    )
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


def _fetch_chunk(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Try BitMEX first (US-friendly, 10y history), then Bybit, then Binance.

    BitMEX is the WINNER for US users — confirmed working 2026-04-27
    with funding back to 2016-05-13 for XBTUSD.
    """
    rows = _fetch_chunk_bitmex(symbol, start_ms, end_ms)
    if rows:
        return rows
    rows = _fetch_chunk_bybit(symbol, start_ms, end_ms)
    if rows:
        return rows
    return _fetch_chunk_binance(symbol, start_ms, end_ms)


def fetch_funding(
    symbol: str = "BTCUSDT",
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict]:
    """Fetch funding-rate history. Returns list of dicts with time + rate."""
    if start is None:
        start = datetime.now(UTC) - timedelta(days=365 * 5)
    if end is None:
        end = datetime.now(UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    all_rows: list[dict] = []
    seen: set[int] = set()
    cursor_start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    # Funding events are 8h apart. BitMEX gives 500 per call ≈ 166
    # days; pick 150 days to stay safely under that and avoid trim.
    chunk_ms = 150 * 86400 * 1000
    while cursor_start_ms < end_ms:
        chunk_end_ms = min(cursor_start_ms + chunk_ms, end_ms)
        print(
            f"  {symbol} "
            f"{datetime.fromtimestamp(cursor_start_ms/1000, UTC).date()} -> "
            f"{datetime.fromtimestamp(chunk_end_ms/1000, UTC).date()}"
        )
        rows = _fetch_chunk(symbol, cursor_start_ms, chunk_end_ms)
        if not rows:
            cursor_start_ms = chunk_end_ms
            continue
        new_count_before = len(all_rows)
        for r in rows:
            ts = int(r["fundingTime"])
            if ts in seen:
                continue
            seen.add(ts)
            try:
                rate = float(r["fundingRate"])
            except (KeyError, ValueError):
                continue
            all_rows.append({"time": ts // 1000, "funding_rate": rate})
        new_rows_added = len(all_rows) - new_count_before
        # Advance: latest ts in chunk + 1. Guard against the
        # rate-limited / duplicate-data case where the chunk returns
        # rows but ALL are already seen — without this guard, the
        # cursor wouldn't advance and we'd loop forever.
        latest_ts = max(int(r["fundingTime"]) for r in rows)
        if new_rows_added == 0 and latest_ts < chunk_end_ms:
            # Force-advance past this chunk to avoid stuck cursor
            cursor_start_ms = chunk_end_ms
        else:
            cursor_start_ms = max(latest_ts + 1, cursor_start_ms + 1)
        time.sleep(0.20)

    all_rows.sort(key=lambda r: r["time"])
    return all_rows


def write_csv(path: Path, rows: list[dict]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "funding_rate"])
        for r in rows:
            w.writerow([int(r["time"]), r["funding_rate"]])
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--years", type=int, default=5)
    p.add_argument("--out", type=Path,
                   default=Path(r"C:\crypto_data\history\BTCFUND_8h.csv"))
    args = p.parse_args()

    end = datetime.now(UTC)
    start = end - timedelta(days=365 * args.years)
    print(f"[funding] {args.symbol} {start.date()} -> {end.date()}")
    rows = fetch_funding(args.symbol, start, end)
    if not rows:
        print("[funding] zero rows fetched")
        return 2
    n = write_csv(args.out, rows)
    last = rows[-1]
    print(
        f"[funding] wrote {n} rows to {args.out}; "
        f"last={datetime.fromtimestamp(last['time'], UTC).date()} "
        f"rate={last['funding_rate']*100:+.4f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

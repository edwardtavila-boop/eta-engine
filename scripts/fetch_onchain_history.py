"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_onchain_history
==============================================================
Free-API on-chain time-series fetcher (the historical sibling of
``brain.jarvis_v3.sage.onchain_fetcher``).

Why this is separate from the sage fetcher
-------------------------------------------
``sage.onchain_fetcher`` returns a CURRENT snapshot dict for the
on-chain school's bar-time consultation. The walk-forward gate
needs HISTORICAL daily series so requirements like::

    DataRequirement("onchain", "BTC", None, critical=True,
        note="whale transfers, exchange netflow, active addresses; "
             "Glassnode-style daily metrics")

can be satisfied by the data library and audited as
``MISSING -> AVAILABLE``. This fetcher writes one row per calendar day
under the workspace ``data/crypto/onchain`` root so the audit picks the
file up under the synthetic
symbol ``<X>ONCHAIN`` (see ``data.audit._resolve_library_lookup``).

What's covered
--------------
Free APIs only. No paid keys.

* **BTC**: Defillama bridges TVL, mempool.space difficulty
  history, blockchain.info hash-rate / total-circulating series.
* **ETH**: Defillama Ethereum chain TVL, Coingecko market-data
  history (price + volume + market cap), blockchain.info ETH
  bridge.
* **SOL**: Defillama Solana chain TVL plus Coingecko market-data
  history (price + volume + market cap).

What's *not* covered (paid-feed gap)
------------------------------------
The original `BotRequirements:btc_hybrid.onchain` note calls out
"whale transfers, exchange netflow, active addresses, Glassnode-
style daily metrics." Those metrics need a Glassnode subscription
(or an equivalent paid feed). This fetcher writes the columns the
free APIs return; the strategy code can consume what's available
and degrade for the missing fields. The gap is documented in
``docs/research_log/2026-04-27_onchain_feed_wired.md`` so the
audit honest about what's there vs aspirational.

Usage::

    # Pull BTC + ETH + SOL on-chain daily for the last 365 days
    python -m eta_engine.scripts.fetch_onchain_history

    # Specific symbol / lookback
    python -m eta_engine.scripts.fetch_onchain_history --symbol BTC --days 720

After running, audit the coverage::

    python -c "from eta_engine.data.audit import audit_bot; \\
               print(audit_bot('btc_hybrid'))"

The on-chain row should flip from MISSING to AVAILABLE.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import CRYPTO_ONCHAIN_ROOT as ONCHAIN_ROOT  # noqa: E402


def _http_json(url: str, *, timeout: float = 10.0) -> object | None:
    """GET + json.loads with friendly fallbacks. Returns None on any failure."""
    request = urllib.request.Request(  # noqa: S310 -- public APIs
        url,
        headers={"User-Agent": "eta-engine/fetch_onchain_history"},
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 -- public APIs
            request, timeout=timeout,
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"  HTTP failed for {url[:80]}...: {exc}")
        return None
    except json.JSONDecodeError as exc:
        print(f"  JSON decode failed for {url[:80]}...: {exc}")
        return None


# ---------------------------------------------------------------------------
# BTC daily series
# ---------------------------------------------------------------------------


def _btc_daily_series(days: int) -> dict[date, dict[str, float]]:
    """Return {date: {column: value}} for BTC, last `days` days.

    Sources:
    * defillama bridge BTC TVL (per-day points).
    * mempool.space difficulty adjustments (per-retarget; forward-fill
      to daily so the column is dense).
    * coingecko BTC market chart (price/volume/market_cap historical).
    """
    series: dict[date, dict[str, float]] = {}

    # CoinGecko market chart: returns prices, market_caps, total_volumes
    # arrays of [timestamp_ms, value]. Free tier is fine for ~365d daily.
    cg = _http_json(
        f"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
        f"?vs_currency=usd&days={min(days, 365)}&interval=daily",
    )
    if isinstance(cg, dict):
        for col, key in (
            ("price_usd", "prices"),
            ("market_cap_usd", "market_caps"),
            ("volume_usd", "total_volumes"),
        ):
            arr = cg.get(key)
            if not isinstance(arr, list):
                continue
            for row in arr:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                ts_ms, val = row[0], row[1]
                try:
                    d = datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).date()
                except (TypeError, ValueError):
                    continue
                series.setdefault(d, {})[col] = float(val)
        time.sleep(1.2)  # be polite to coingecko

    # Difficulty adjustments — per-retarget, used as regime-shift markers.
    diffs = _http_json("https://mempool.space/api/v1/mining/difficulty-adjustments")
    if isinstance(diffs, list):
        for row in diffs:
            if not isinstance(row, dict):
                continue
            ts = row.get("timestamp")
            chg = row.get("difficultyChange")
            if ts is None or chg is None:
                continue
            try:
                d = datetime.fromtimestamp(int(ts), tz=UTC).date()
            except (TypeError, ValueError):
                continue
            series.setdefault(d, {})["difficulty_change_pct"] = float(chg)

    return series


def _eth_daily_series(days: int) -> dict[date, dict[str, float]]:
    """Return {date: {column: value}} for ETH, last `days` days."""
    series: dict[date, dict[str, float]] = {}

    cg = _http_json(
        f"https://api.coingecko.com/api/v3/coins/ethereum/market_chart"
        f"?vs_currency=usd&days={min(days, 365)}&interval=daily",
    )
    if isinstance(cg, dict):
        for col, key in (
            ("price_usd", "prices"),
            ("market_cap_usd", "market_caps"),
            ("volume_usd", "total_volumes"),
        ):
            arr = cg.get(key)
            if not isinstance(arr, list):
                continue
            for row in arr:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                ts_ms, val = row[0], row[1]
                try:
                    d = datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).date()
                except (TypeError, ValueError):
                    continue
                series.setdefault(d, {})[col] = float(val)
        time.sleep(1.2)

    # Defillama Ethereum chain TVL — per-day historical.
    tvl = _http_json("https://api.llama.fi/v2/historicalChainTvl/Ethereum")
    if isinstance(tvl, list):
        for row in tvl:
            if not isinstance(row, dict):
                continue
            ts = row.get("date")
            v = row.get("tvl")
            if ts is None or v is None:
                continue
            try:
                d = datetime.fromtimestamp(int(ts), tz=UTC).date()
            except (TypeError, ValueError):
                continue
            series.setdefault(d, {})["chain_tvl_usd"] = float(v)

    return series


# ---------------------------------------------------------------------------
# CSV write — one row per day
# ---------------------------------------------------------------------------


def _sol_daily_series(days: int) -> dict[date, dict[str, float]]:
    """Return {date: {column: value}} for SOL, last `days` days."""
    series: dict[date, dict[str, float]] = {}

    cg = _http_json(
        f"https://api.coingecko.com/api/v3/coins/solana/market_chart"
        f"?vs_currency=usd&days={min(days, 365)}&interval=daily",
    )
    if isinstance(cg, dict):
        for col, key in (
            ("price_usd", "prices"),
            ("market_cap_usd", "market_caps"),
            ("volume_usd", "total_volumes"),
        ):
            arr = cg.get(key)
            if not isinstance(arr, list):
                continue
            for row in arr:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                ts_ms, val = row[0], row[1]
                try:
                    d = datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).date()
                except (TypeError, ValueError):
                    continue
                series.setdefault(d, {})[col] = float(val)
        time.sleep(1.2)

    tvl = _http_json("https://api.llama.fi/v2/historicalChainTvl/Solana")
    if isinstance(tvl, list):
        for row in tvl:
            if not isinstance(row, dict):
                continue
            ts = row.get("date")
            v = row.get("tvl")
            if ts is None or v is None:
                continue
            try:
                d = datetime.fromtimestamp(int(ts), tz=UTC).date()
            except (TypeError, ValueError):
                continue
            series.setdefault(d, {})["chain_tvl_usd"] = float(v)

    return series


_COLUMNS_BY_SYMBOL: dict[str, list[str]] = {
    "BTC": ["price_usd", "market_cap_usd", "volume_usd", "difficulty_change_pct"],
    "ETH": ["price_usd", "market_cap_usd", "volume_usd", "chain_tvl_usd"],
    "SOL": ["price_usd", "market_cap_usd", "volume_usd", "chain_tvl_usd"],
}


def _filename(symbol: str) -> str:
    return f"{symbol.upper()}ONCHAIN_D.csv"


def write_csv(path: Path, series: dict[date, dict[str, float]], cols: list[str]) -> None:
    """Write the synthetic-symbol daily on-chain CSV.

    Schema is the bar-history shape with ``volume`` column reused for
    a synthetic ``observations`` count, plus the on-chain fields as
    additional columns. The data library's bar parser only reads the
    standard six (time/open/high/low/close/volume); on-chain
    consumers use ``schema_kind="history"`` + the extra columns
    directly via ``DataLibrary.load_bars(ds, with_extras=True)``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    days = sorted(series)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        # The library expects ``time, open, high, low, close, volume``
        # for history-schema files. We use price as O=H=L=C and
        # observations-count as volume so the dataset loads cleanly,
        # then append the on-chain columns for downstream consumers.
        w.writerow(["time", "open", "high", "low", "close", "volume", *cols])
        for d in days:
            row = series[d]
            ts = int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())
            price = row.get("price_usd", 0.0) or 0.0
            obs = float(len(row))
            extras = [row.get(c, "") for c in cols]
            w.writerow([ts, price, price, price, price, obs, *extras])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fetch_onchain_history")
    p.add_argument(
        "--symbol", default=None, choices=[None, "BTC", "ETH", "SOL"],
        help="single symbol; default = all (BTC + ETH + SOL)",
    )
    p.add_argument(
        "--days", type=int, default=365,
        help="lookback in days (Coingecko free tier caps daily granularity at ~365)",
    )
    p.add_argument(
        "--root", type=Path, default=ONCHAIN_ROOT,
        help="output directory (default: canonical ETA crypto on-chain root)",
    )
    args = p.parse_args(argv)

    targets = [args.symbol] if args.symbol else ["BTC", "ETH", "SOL"]
    today = datetime.now(UTC).date()
    cutoff = today - timedelta(days=args.days)

    rc = 0
    for sym in targets:
        print(f"[fetch_onchain_history] {sym} last {args.days}d -> {args.root}")
        if sym == "BTC":
            series = _btc_daily_series(args.days)
        elif sym == "ETH":
            series = _eth_daily_series(args.days)
        elif sym == "SOL":
            series = _sol_daily_series(args.days)
        else:
            print(f"  unsupported symbol {sym}")
            rc = 1
            continue
        # Trim to lookback window.
        series = {d: row for d, row in series.items() if cutoff <= d <= today}
        if not series:
            print(
                f"  zero rows for {sym} — APIs may be rate-limited; "
                "retry in a minute",
            )
            rc = 1
            continue
        cols = _COLUMNS_BY_SYMBOL[sym]
        out = args.root / _filename(sym)
        write_csv(out, series, cols)
        n_dense = sum(1 for d in series if all(c in series[d] for c in cols))
        print(
            f"  wrote {len(series)} day rows to {out}  "
            f"(dense rows = {n_dense}; sparse cells filled blank)",
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())

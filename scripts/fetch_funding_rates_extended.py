"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_funding_rates_extended
==================================================================
Multi-symbol funding-rate backfill for BTC / ETH / SOL — replaces the
199-row truncated history that the 2026-05-07 fleet audit flagged as
statistically empty (CI [-0.332, +0.716] for ``funding_rate_btc``).

Why this script exists
----------------------
The existing files under ``data/crypto/history/`` —
``BTCFUND_8h.csv``, ``ETHFUND_8h.csv``, ``SOLFUND_8h.csv`` — each
contain ~199 rows (~33 days at the 8h funding cadence).  Bootstrap
analysis on `funding_rate_*` strategies needs >=365 days to produce
non-degenerate confidence intervals.

Sister script ``scripts/fetch_btc_funding_extended.py`` already
implements a BitMEX-first fetcher for BTC (and ETHUSD inverse-perp);
it is preserved as the audit anchor.  This script generalises across
all three required symbols and adds a policy-clean, US-legal-only
aggregated source so the operator no longer depends on a single
exchange's perp listings.

Source selection (US-person policy)
-----------------------------------
Per workspace ``CLAUDE.md`` hard rule #2 ("US-legal venue routing
only; no live routing to offshore venues for US-person flows") and
the venue gate in ``eta_engine.venues.router``, **Binance, Bybit,
OKX, Hyperliquid, and Deribit are blocked as primary funding-rate
sources for US-person research workflows**.  This leaves:

* **CoinGlass aggregated index** — preferred. The OI-weighted
  funding rate is a market-aggregate index across all major venues
  (Binance / Bybit / OKX / BitMEX / dYdX / Hyperliquid), not tied to
  any single blocked venue.  Free tier requires an API key (sign up
  at https://www.coinglass.com/pricing) and rate-limits to ~30 req/
  min; paid tiers ($29-99/mo) lift the rate-limit and unlock deeper
  historical windows.  3 symbols × 1 page each → 3 requests, well
  under the free-tier budget.
* **BitMEX direct** — fallback for BTC & ETH only.  BitMEX is
  US-friendly and publishes XBTUSD funding back to 2016 + ETHUSD
  funding from 2018.  It does **not** list a SOL inverse perpetual,
  so the BitMEX path raises ``UnsupportedSymbolError`` for SOL.

Deliberately **not** implemented:

* Binance fapi (US-blocked)
* Bybit v5 (US-blocked since 2024 enforcement; previously a
  fallback in ``fetch_btc_funding_extended``)
* OKX public (US-blocked)

If neither source covers a requested symbol, the script exits with a
clear error message rather than silently degrading.

Usage
-----
::

    # CoinGlass (recommended) — needs API key
    set CRYPTO_FUNDING_API_KEY=<your-coinglass-key>
    python -m eta_engine.scripts.fetch_funding_rates_extended \\
        --symbols BTC ETH SOL --days 365 --source coinglass

    # BitMEX (no key required, BTC + ETH only)
    python -m eta_engine.scripts.fetch_funding_rates_extended \\
        --symbols BTC ETH --days 365 --source bitmex

    # Idempotent re-run: existing rows are preserved, new rows merged
    # (dedup by timestamp). Safe to schedule daily.

Operator runbook
----------------
* **Cost**: CoinGlass free tier is sufficient for the initial 365-day
  backfill (3 requests, < 30 req/min). For 5-year deep backfill or
  high-frequency refresh, upgrade to the $29/mo Hobbyist tier.
* **Required env var**: ``CRYPTO_FUNDING_API_KEY`` (or pass
  ``--api-key`` on the command line) when ``--source coinglass``.
* **Expected runtime**: ~2-5 seconds for 365 days × 3 symbols on
  CoinGlass; ~30-60 seconds for the same window on BitMEX
  (paginated 500 records / call ≈ 166 days per chunk).
* **Idempotent**: re-running merges new rows into the existing CSV;
  dedup is by exact timestamp.  Safe to wire into Task Scheduler.
* **Output validation**: every written row has ``time`` (epoch
  seconds, int), ``funding_rate`` (float), is sorted ascending, and
  has no future timestamps.

CSV schema
----------
``time,funding_rate``  — epoch seconds, float per the existing
history library convention.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402
from eta_engine.scripts.workspace_roots import (  # noqa: E402
    CRYPTO_HISTORY_ROOT,
    ensure_dir,
)

logger = logging.getLogger("eta_engine.fetch_funding_rates_extended")

# ── source endpoints ──────────────────────────────────────────────────
_COINGLASS_BASE = "https://open-api-v4.coinglass.com"
_COINGLASS_OI_WEIGHT_PATH = "/api/futures/funding-rate/oi-weight-history"
_BITMEX_BASE = "https://www.bitmex.com/api/v1/funding"
_USER_AGENT = "eta-engine/fetch_funding_rates_extended"

# ── symbol mappings ──────────────────────────────────────────────────
# CoinGlass uses bare ticker for the OI-weighted aggregate index.
_COINGLASS_SYMBOLS: dict[str, str] = {
    "BTC": "BTC",
    "ETH": "ETH",
    "SOL": "SOL",
}

# BitMEX: only inverse perps with multi-year history. SOL is intentionally
# absent — BitMEX does not list a SOL inverse perpetual.
_BITMEX_SYMBOLS: dict[str, str] = {
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
}

_FUNDING_INTERVAL_SECONDS = 8 * 3600


class UnsupportedSymbolError(ValueError):
    """Raised when a source cannot supply funding for a symbol."""


class MissingApiKeyError(RuntimeError):
    """Raised when CoinGlass is selected without ``CRYPTO_FUNDING_API_KEY``."""


# ── HTTP helpers ──────────────────────────────────────────────────────


def _http_get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> object:
    """Wrapper around urllib that returns parsed JSON or raises a typed error."""
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("User-Agent", _USER_AGENT)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── CoinGlass adapter (aggregated OI-weighted index) ─────────────────


def _fetch_coinglass(symbol: str, days: int, api_key: str) -> list[tuple[int, float]]:
    """OI-weighted aggregate funding rate across exchanges.

    CoinGlass v4 returns an array of points sorted ascending. Each
    point shape::

        {"time": 1717459200000, "close": 0.0001234}

    where ``time`` is milliseconds since epoch and ``close`` is the
    OI-weighted funding rate at that 8h boundary.
    """
    cg_symbol = _COINGLASS_SYMBOLS.get(symbol)
    if cg_symbol is None:
        raise UnsupportedSymbolError(
            f"CoinGlass does not have a configured aggregate for {symbol!r}; supported: {sorted(_COINGLASS_SYMBOLS)}"
        )
    end_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    # Add a small buffer so we slightly over-fetch and let dedup trim.
    start_ms = end_ms - int((days + 1) * 86400 * 1000)
    url = (
        f"{_COINGLASS_BASE}{_COINGLASS_OI_WEIGHT_PATH}"
        f"?symbol={cg_symbol}&interval=8h"
        f"&start_time={start_ms}&end_time={end_ms}&limit=4500"
    )
    headers = {"coinglassSecret": api_key, "accept": "application/json"}

    try:
        payload = _http_get_json(url, headers=headers)
    except urllib.error.HTTPError as exc:
        body = b""
        if hasattr(exc, "read"):
            try:
                body = exc.read()[:200]
            except Exception:  # noqa: BLE001
                body = b""
        logger.error("CoinGlass HTTP %s for %s: %r", exc.code, symbol, body)
        return []
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.error("CoinGlass fetch error for %s: %r", symbol, exc)
        return []

    if not isinstance(payload, dict):
        logger.error("CoinGlass returned non-object payload for %s: %r", symbol, payload)
        return []
    if str(payload.get("code", "0")) not in {"0", "00000"}:
        logger.error("CoinGlass api-error for %s: %s", symbol, payload.get("msg"))
        return []
    data = payload.get("data") or []
    rows: list[tuple[int, float]] = []
    for entry in data:
        ts_ms = entry.get("time") if isinstance(entry, dict) else None
        rate = entry.get("close") if isinstance(entry, dict) else None
        if ts_ms is None or rate is None:
            continue
        try:
            rows.append((int(ts_ms) // 1000, float(rate)))
        except (TypeError, ValueError):
            continue
    return rows


# ── BitMEX adapter (direct, US-friendly, no key) ──────────────────────


def _fetch_bitmex(symbol: str, days: int) -> list[tuple[int, float]]:
    """BitMEX funding history. Paginates 500 records per call."""
    bitmex_sym = _BITMEX_SYMBOLS.get(symbol)
    if bitmex_sym is None:
        raise UnsupportedSymbolError(
            f"BitMEX has no inverse perpetual for {symbol!r}; "
            f"supported: {sorted(_BITMEX_SYMBOLS)}. Use --source coinglass."
        )
    end_dt = datetime.now(tz=UTC)
    start_dt = end_dt - timedelta(days=days)

    rows: list[tuple[int, float]] = []
    seen_ts: set[int] = set()
    cursor = start_dt
    chunk_days = 150  # 500 records * 8h ≈ 166 days; stay safely under
    while cursor < end_dt:
        chunk_end = min(cursor + timedelta(days=chunk_days), end_dt)
        url = (
            f"{_BITMEX_BASE}?symbol={bitmex_sym}"
            f"&startTime={cursor.strftime('%Y-%m-%d')}"
            f"&endTime={chunk_end.strftime('%Y-%m-%d')}"
            f"&count=500"
        )
        try:
            payload = _http_get_json(url, timeout=30)
        except urllib.error.HTTPError as exc:
            logger.error("BitMEX HTTP %s for %s", exc.code, symbol)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.error("BitMEX fetch error for %s: %r", symbol, exc)
            break

        if not isinstance(payload, list) or not payload:
            cursor = chunk_end
            continue

        latest_ts_in_chunk: int | None = None
        for entry in payload:
            try:
                ts_iso = entry["timestamp"].replace("Z", "+00:00")
                ts = int(datetime.fromisoformat(ts_iso).timestamp())
                rate = float(entry["fundingRate"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts in seen_ts:
                continue
            seen_ts.add(ts)
            rows.append((ts, rate))
            if latest_ts_in_chunk is None or ts > latest_ts_in_chunk:
                latest_ts_in_chunk = ts

        # Advance cursor past the chunk to avoid an infinite loop when a
        # chunk returns only already-seen rows.
        if latest_ts_in_chunk is None:
            cursor = chunk_end
        else:
            cursor = max(
                datetime.fromtimestamp(latest_ts_in_chunk, tz=UTC) + timedelta(seconds=1),
                cursor + timedelta(days=1),
            )
        time.sleep(0.2)
    return rows


# ── source dispatch ──────────────────────────────────────────────────


def fetch_funding_rates(
    symbol: str,
    days: int,
    source: str,
    api_key: str | None = None,
) -> list[tuple[int, float]]:
    """Dispatch to the configured source. Returns ``[(ts_seconds, rate), ...]``."""
    sym = symbol.upper()
    if source == "coinglass":
        if not api_key:
            raise MissingApiKeyError("CoinGlass requires an API key. Set CRYPTO_FUNDING_API_KEY or pass --api-key.")
        rows = _fetch_coinglass(sym, days, api_key)
    elif source == "bitmex":
        rows = _fetch_bitmex(sym, days)
    else:
        raise ValueError(f"Unknown source {source!r}; supported: coinglass, bitmex")
    return rows


# ── CSV merge / validation ───────────────────────────────────────────


def _read_existing(path: Path) -> dict[int, float]:
    """Read an existing FUND CSV into a {ts -> rate} dict. Missing file -> {}."""
    if not path.exists():
        return {}
    out: dict[int, float] = {}
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                ts = int(row["time"])
                rate = float(row["funding_rate"])
            except (KeyError, TypeError, ValueError):
                continue
            out[ts] = rate
    return out


def _validate_rows(rows: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Drop invalid rows: NaN/inf rates, future timestamps, non-positive ts."""
    now = int(datetime.now(tz=UTC).timestamp())
    out: list[tuple[int, float]] = []
    for ts, rate in rows:
        if ts <= 0 or ts > now:
            continue
        if rate != rate:  # NaN
            continue
        if rate in (float("inf"), float("-inf")):
            continue
        out.append((ts, rate))
    return out


def merge_and_write(
    out_path: Path,
    fetched: list[tuple[int, float]],
) -> tuple[int, int, int]:
    """Idempotent merge: existing ∪ fetched, dedup by ts, sort ascending.

    Returns ``(rows_before, rows_added, rows_after)``.
    """
    fetched = _validate_rows(fetched)
    existing = _read_existing(out_path)
    rows_before = len(existing)

    merged = dict(existing)  # copy
    added = 0
    for ts, rate in fetched:
        if ts not in merged:
            added += 1
        merged[ts] = rate  # latest fetch wins for duplicates (same ts)

    sorted_rows = sorted(merged.items())  # ascending by ts
    ensure_dir(out_path.parent)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "funding_rate"])
        for ts, rate in sorted_rows:
            w.writerow([ts, f"{rate:.10f}"])
    return rows_before, added, len(sorted_rows)


# ── CLI ──────────────────────────────────────────────────────────────


def _coverage_str(out_path: Path) -> str:
    """Best-effort first/last timestamp summary of a written FUND CSV."""
    if not out_path.exists():
        return "(empty)"
    rows = _read_existing(out_path)
    if not rows:
        return "(empty)"
    first = min(rows)
    last = max(rows)
    span_days = (last - first) / 86400.0
    return (
        f"{datetime.fromtimestamp(first, tz=UTC).date()} -> "
        f"{datetime.fromtimestamp(last, tz=UTC).date()} ({span_days:.1f} days)"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fetch_funding_rates_extended", description=__doc__)
    p.add_argument(
        "--symbols",
        nargs="+",
        default=["BTC", "ETH", "SOL"],
        help="Symbol tickers to backfill (default: BTC ETH SOL).",
    )
    p.add_argument(
        "--days",
        type=int,
        default=365,
        help="Lookback window in days (default: 365).",
    )
    p.add_argument(
        "--source",
        choices=["coinglass", "bitmex"],
        default="coinglass",
        help="Funding-rate source (default: coinglass aggregated index).",
    )
    p.add_argument(
        "--api-key",
        default=None,
        help="CoinGlass API key. Falls back to $CRYPTO_FUNDING_API_KEY env.",
    )
    p.add_argument(
        "--root",
        type=Path,
        default=CRYPTO_HISTORY_ROOT,
        help="Output directory (default: workspace data/crypto/history).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Python log level (default: INFO).",
    )
    args = p.parse_args(argv)
    try:
        args.root = workspace_roots.resolve_under_workspace(args.root, label="--root")
    except ValueError as exc:
        p.error(str(exc))

    logging.basicConfig(level=args.log_level.upper(), format="%(message)s")

    api_key = args.api_key or os.environ.get("CRYPTO_FUNDING_API_KEY")

    rc = 0
    for symbol in args.symbols:
        sym = symbol.upper()
        out_path = args.root / f"{sym}FUND_8h.csv"
        logger.info("[funding] %s via %s -> %s", sym, args.source, out_path)
        try:
            fetched = fetch_funding_rates(sym, args.days, args.source, api_key)
        except UnsupportedSymbolError as exc:
            logger.error("[funding] %s skipped: %s", sym, exc)
            rc = 2
            continue
        except MissingApiKeyError as exc:
            logger.error("[funding] %s aborted: %s", sym, exc)
            return 3

        if not fetched:
            logger.warning("[funding] %s: zero rows fetched (source returned empty)", sym)
            rc = max(rc, 1)
            continue

        before, added, after = merge_and_write(out_path, fetched)
        logger.info(
            "[funding] %s wrote %d rows (was %d, +%d new); coverage=%s",
            sym,
            after,
            before,
            added,
            _coverage_str(out_path),
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

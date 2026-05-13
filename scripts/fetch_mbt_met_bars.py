"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_mbt_met_bars
==========================================================
CME crypto micro-futures bar fetcher (MBT — Micro Bitcoin, MET — Micro Ether).

Why this exists
---------------
The walk-forward lab harness needs ~18 months of 5-minute OHLCV bars
for MBT and MET to validate three new crypto-micro strategies. The
canonical ETA history root has zero coverage for these symbols today
(2026-05-07). The promotion gate refuses to let strategies forward
without bar history at the canonical path.

Pattern mirrored
----------------
This is a sibling of ``fetch_ibkr_crypto_bars.py`` (PAXOS spot crypto
via Client Portal Gateway). Key differences:

* Targets CME futures (MBT/MET) not PAXOS spot — uses the
  ``/trsrv/futures`` endpoint to resolve the front-month conid
  rather than a hard-coded conid.
* Writes to ``mnq_data/history/{SYMBOL}1_{TF}.csv`` — the futures
  history root the lab harness ``_resolve_bar_path`` checks first.
* Output format matches feeds.strategy_lab.engine._load_ohlcv:
  CSV header ``time,open,high,low,close,volume`` with epoch-second
  ``time``.
* Idempotent: merges new rows into any existing CSV at the same path.

Pre-flight requirements
-----------------------
1. **IBKR Client Portal Gateway running** at the configured base URL
   (default ``https://127.0.0.1:5000/v1/api``).
2. **Authenticated session** — visit ``https://127.0.0.1:5000`` and
   log in. ``/iserver/auth/status`` should report ``authenticated=true``.
3. **CME Crypto market-data subscription** active on the account.
   Without it, ``/marketdata/history`` returns empty payloads silently.

Usage
-----
::

    # Fetch 18 months of 5m MBT + MET bars (default)
    python -m eta_engine.scripts.fetch_mbt_met_bars \\
        --symbols MBT MET --days 540

    # Dry run — print planned chunked requests, do not execute
    python -m eta_engine.scripts.fetch_mbt_met_bars \\
        --symbols MBT --days 540 --dry-run

    # Custom timeframe / output root override
    python -m eta_engine.scripts.fetch_mbt_met_bars \\
        --symbols MET --timeframe 1h --days 365

Gotchas
-------
* CME crypto futures roll quarterly. ``/trsrv/futures`` returns the
  front-month conid by default; historical bars from the front-month
  contract therefore reflect rolling near-month data. For a
  continuous-front-month series across rolls, IBKR stitches
  internally — verify the merged CSV by sampling around expected roll
  dates (Mar / Jun / Sep / Dec).
* Conids are NOT stable across contract months. Cache them per run
  rather than persisting them.
* IBKR Client Portal historical endpoint caps at ~1000 bars per
  request. For 5m bars over 540 days that is ~155k bars → ~175
  chunked requests per symbol. Pacing: 0.2s sleep between chunks.
"""

from __future__ import annotations

# ruff: noqa: E402, I001 -- standalone script amends sys.path before eta_engine imports.

import argparse
import csv
import json
import logging
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import MNQ_HISTORY_ROOT  # noqa: E402

log = logging.getLogger("fetch_mbt_met_bars")

# ─── Defaults ─────────────────────────────────────────────────────

_DEFAULT_BASE_URL = "https://127.0.0.1:5000/v1/api"

# Supported symbols. Mirrors ``venues.ibkr_live.FUTURES_MAP`` for the
# CME crypto micros. The exchange must be CME for ``/trsrv/futures``
# to surface the right contracts.
_SUPPORTED_SYMBOLS: tuple[str, ...] = ("MBT", "MET")
_EXCHANGE = "CME"

# IBKR Client Portal historical bar sizes.
_TF_TO_IBKR_BAR: dict[str, str] = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}

# Bar duration in seconds for chunk math.
_BAR_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

# Approx bar count per chunk; Client Portal caps near 1000.
_CHUNK_BAR_LIMIT = 900


# ─── Network helpers ──────────────────────────────────────────────


@dataclass(frozen=True)
class _ChunkRequest:
    conid: int
    bar: str
    period: str
    end_ms: int


def _make_ctx() -> ssl.SSLContext:
    """SSL context that accepts the Client Portal Gateway's self-signed cert."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _http_get_json(url: str, *, timeout: float = 15.0) -> Any:  # noqa: ANN401 -- gateway returns arbitrary JSON
    """GET ``url`` and return decoded JSON. Returns None on any failure."""
    request = urllib.request.Request(  # noqa: S310 -- localhost gateway
        url,
        headers={"User-Agent": "eta-engine/fetch_mbt_met_bars"},
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 -- localhost gateway
            request,
            timeout=timeout,
            context=_make_ctx(),
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200] if hasattr(exc, "read") else b""
        print(f"  HTTPError {exc.code}: {body!r}")
        return None
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  URLError: {exc}")
        return None
    except json.JSONDecodeError as exc:
        print(f"  JSONDecodeError: {exc}")
        return None


def resolve_front_month_conid(
    symbol: str,
    *,
    base_url: str = _DEFAULT_BASE_URL,
) -> int | None:
    """Resolve the front-month conid for a CME futures symbol.

    Calls ``/trsrv/futures?symbols=<SYM>``; the response is a dict mapping
    each symbol to a list of contracts ordered by expiry. The first
    non-expired contract is the front month.
    """
    url = f"{base_url}/trsrv/futures?symbols={symbol.upper()}&exchange={_EXCHANGE}"
    payload = _http_get_json(url)
    if not isinstance(payload, dict):
        return None
    contracts = payload.get(symbol.upper())
    if not isinstance(contracts, list) or not contracts:
        return None
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    # Pick the earliest expiry that is still in the future.
    candidates = [c for c in contracts if isinstance(c, dict) and isinstance(c.get("expirationDate"), int | str)]

    def _exp_ms(c: dict) -> int:
        raw = c.get("expirationDate")
        if isinstance(raw, int):
            return raw
        try:
            return int(
                datetime.strptime(str(raw), "%Y%m%d")
                .replace(
                    tzinfo=UTC,
                )
                .timestamp()
                * 1000
            )
        except (TypeError, ValueError):
            return 0

    future_only = [c for c in candidates if _exp_ms(c) > now_ms]
    target_pool = future_only or candidates
    target_pool.sort(key=_exp_ms)
    if not target_pool:
        return None
    front = target_pool[0]
    conid = front.get("conid")
    return int(conid) if isinstance(conid, int | str) and str(conid).isdigit() else None


def _fetch_chunk(
    base_url: str,
    req: _ChunkRequest,
    *,
    timeout: float = 15.0,
) -> list[dict]:
    """Hit ``/iserver/marketdata/history`` once. Returns the raw bar list."""
    qs = f"?conid={req.conid}&period={req.period}&bar={req.bar}&startTime={req.end_ms}"
    url = f"{base_url}/iserver/marketdata/history{qs}"
    payload = _http_get_json(url, timeout=timeout)
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return data


# ─── Window math ──────────────────────────────────────────────────


def _period_for_chunk(bar: str) -> str:
    """How many units of history one chunk asks for."""
    if bar.endswith("min"):
        unit = bar[:-3]
        n = int(unit) if unit else 1
        minutes = _CHUNK_BAR_LIMIT * n
        if minutes >= 60:
            return f"{minutes // 60}h"
        return f"{minutes}min"
    if bar == "1h":
        return f"{_CHUNK_BAR_LIMIT}h"
    if bar == "4h":
        return f"{(_CHUNK_BAR_LIMIT * 4)}h"
    if bar == "1d":
        return f"{_CHUNK_BAR_LIMIT}d"
    return f"{_CHUNK_BAR_LIMIT}d"


def plan_chunks(
    *,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> list[tuple[str, datetime]]:
    """Plan the (period, cursor) pairs the fetcher will request.

    Pure function; useful for ``--dry-run`` and tests.
    """
    bar = _TF_TO_IBKR_BAR[timeframe]
    bar_secs = _BAR_SECONDS[timeframe]
    period = _period_for_chunk(bar)
    plan: list[tuple[str, datetime]] = []
    cursor = end
    # Cap at a generous safety to avoid runaway loops in degenerate inputs.
    max_chunks = 5000
    while cursor > start and len(plan) < max_chunks:
        plan.append((period, cursor))
        cursor = cursor - timedelta(seconds=bar_secs * _CHUNK_BAR_LIMIT)
    return plan


# ─── Top-level fetch ──────────────────────────────────────────────


def fetch_bars(
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    base_url: str = _DEFAULT_BASE_URL,
    conid: int | None = None,
) -> list[dict]:
    """Pull all bars in [start, end). Stitches IBKR's chunked responses.

    If ``conid`` is None, resolves the front-month via ``/trsrv/futures``.
    """
    bar = _TF_TO_IBKR_BAR.get(timeframe)
    if bar is None:
        raise ValueError(
            f"unknown timeframe {timeframe!r}; supported: {sorted(_TF_TO_IBKR_BAR)}",
        )
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    if conid is None:
        conid = resolve_front_month_conid(symbol, base_url=base_url)
    if conid is None:
        print(
            f"[fetch_mbt_met_bars] could not resolve front-month conid for "
            f"{symbol} on {_EXCHANGE} — gateway authenticated?",
        )
        return []

    period = _period_for_chunk(bar)
    bar_secs = _BAR_SECONDS[timeframe]

    out: list[dict] = []
    cursor = end
    while cursor > start:
        end_ms = int(cursor.timestamp() * 1000)
        req = _ChunkRequest(conid=conid, bar=bar, period=period, end_ms=end_ms)
        print(
            f"  fetching {symbol}/{timeframe} <= {cursor.isoformat()} (conid={conid}, period={period}) ...",
        )
        rows = _fetch_chunk(base_url, req)
        if not rows:
            break
        out.extend(rows)
        oldest = min(rows, key=lambda r: r.get("t", 0))
        oldest_ms = int(oldest.get("t", 0))
        if oldest_ms == 0:
            break
        prev_cursor = cursor
        cursor = datetime.fromtimestamp(oldest_ms / 1000, tz=UTC) - timedelta(
            seconds=bar_secs,
        )
        if cursor >= prev_cursor:
            break
        time.sleep(0.2)

    out.sort(key=lambda r: r.get("t", 0))
    seen: set[int] = set()
    deduped: list[dict] = []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    for r in out:
        ts = int(r.get("t", 0))
        if ts == 0 or ts in seen:
            continue
        if ts < start_ms or ts >= end_ms:
            continue
        seen.add(ts)
        deduped.append(r)
    return deduped


# ─── CSV write — match feeds.strategy_lab.engine._load_ohlcv ─────


def canonical_bar_path(symbol: str, timeframe: str) -> Path:
    """Match the canonical futures-history filename convention.

    feeds.strategy_lab.engine._resolve_bar_path checks
    ``<HISTORY>/{SYMBOL}1_{TF}.csv`` first for futures.
    """
    tf_for_filename = {"1d": "D", "1w": "W"}.get(
        timeframe.lower(),
        timeframe,
    )
    return MNQ_HISTORY_ROOT / f"{symbol.upper()}1_{tf_for_filename}.csv"


def _normalize_rows(raw: list[dict]) -> list[dict]:
    """Convert IBKR ``{t,o,h,l,c,v}`` (ms) to canonical ``{time,...}`` (s)."""
    rows: list[dict] = []
    for r in raw:
        ts_ms = int(r.get("t", 0))
        if ts_ms <= 0:
            continue
        rows.append(
            {
                "time": ts_ms // 1000,
                "open": float(r.get("o", 0.0)),
                "high": float(r.get("h", 0.0)),
                "low": float(r.get("l", 0.0)),
                "close": float(r.get("c", 0.0)),
                "volume": float(r.get("v", 0.0)),
            }
        )
    return rows


def merge_with_existing(
    out_path: Path,
    new_rows: list[dict],
) -> tuple[list[dict], int, int]:
    """Merge ``new_rows`` (canonical schema) with any existing CSV.

    Returns ``(merged_rows, n_existing, n_new_unique)``.
    """
    existing: list[dict] = []
    if out_path.exists():
        try:
            with out_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        existing.append(
                            {
                                "time": int(row["time"]),
                                "open": float(row["open"]),
                                "high": float(row["high"]),
                                "low": float(row["low"]),
                                "close": float(row["close"]),
                                "volume": float(row.get("volume", 0.0)),
                            }
                        )
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            existing = []
    seen = {r["time"] for r in existing}
    new_unique = [r for r in new_rows if r["time"] not in seen]
    merged = existing + new_unique
    merged.sort(key=lambda r: r["time"])
    return merged, len(existing), len(new_unique)


def write_csv(path: Path, rows: list[dict]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow(
                [
                    int(r["time"]),
                    r["open"],
                    r["high"],
                    r["low"],
                    r["close"],
                    r["volume"],
                ]
            )
    return len(rows)


def report_gaps(rows: list[dict], timeframe: str) -> list[tuple[int, int]]:
    """Return a list of (gap_start_ts, gap_end_ts) where consecutive bars
    are spaced >2x the timeframe apart (excluding weekend / session breaks
    is left to downstream tooling — this is a coarse signal)."""
    if not rows:
        return []
    bar_secs = _BAR_SECONDS[timeframe]
    threshold = bar_secs * 2
    gaps: list[tuple[int, int]] = []
    for prev, curr in zip(rows, rows[1:], strict=False):
        delta = int(curr["time"]) - int(prev["time"])
        if delta > threshold:
            gaps.append((int(prev["time"]), int(curr["time"])))
    return gaps


# ─── CLI ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fetch_mbt_met_bars",
        description=(
            "Fetch CME crypto micro futures (MBT, MET) historical bars "
            "from IBKR Client Portal Gateway for the lab harness."
        ),
    )
    p.add_argument(
        "--symbols",
        nargs="+",
        default=list(_SUPPORTED_SYMBOLS),
        choices=list(_SUPPORTED_SYMBOLS),
        help="Symbols to fetch (default: MBT MET).",
    )
    p.add_argument(
        "--timeframe",
        default="5m",
        choices=sorted(_TF_TO_IBKR_BAR),
        help="Bar size (default: 5m).",
    )
    p.add_argument(
        "--days",
        type=int,
        default=540,
        help="Lookback in days (default: 540 ≈ 18 months).",
    )
    p.add_argument(
        "--end",
        default=None,
        help="ISO date YYYY-MM-DD; default = now (UTC).",
    )
    p.add_argument(
        "--root",
        type=Path,
        default=MNQ_HISTORY_ROOT,
        help="Output history root (default: canonical mnq_data/history).",
    )
    p.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help="IBKR Client Portal Gateway base URL.",
    )
    p.add_argument(
        "--no-merge",
        action="store_true",
        help="Overwrite existing CSV instead of merging new rows.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=("Print planned chunked requests and target CSV paths without hitting the gateway or writing files."),
    )
    return p


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    end_dt = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else datetime.now(UTC)
    start_dt = end_dt - timedelta(days=int(args.days))

    print(
        f"[fetch_mbt_met_bars] symbols={args.symbols} tf={args.timeframe} "
        f"window={start_dt.date()} -> {end_dt.date()} ({args.days}d)",
    )
    print(f"[fetch_mbt_met_bars] root={args.root}  base_url={args.base_url}")

    bar_secs = _BAR_SECONDS[args.timeframe]
    # Convert bar count to per-symbol expectation (calendar-time, before
    # session masking — the operator-side check confirms actual coverage).
    expected_bars_per_symbol = int(args.days * 86400 / bar_secs)
    print(
        f"[fetch_mbt_met_bars] expected ~{expected_bars_per_symbol:,} calendar-time bars per symbol (pre-session-mask)",
    )

    if args.dry_run:
        plan = plan_chunks(
            timeframe=args.timeframe,
            start=start_dt,
            end=end_dt,
        )
        for sym in args.symbols:
            out_path = canonical_bar_path(sym, args.timeframe)
            if args.root != MNQ_HISTORY_ROOT:
                tf_for_filename = {"1d": "D", "1w": "W"}.get(
                    args.timeframe.lower(),
                    args.timeframe,
                )
                out_path = args.root / f"{sym}1_{tf_for_filename}.csv"
            print(
                f"[dry-run] {sym}: {len(plan)} chunked requests -> {out_path}",
            )
            for i, (period, cursor) in enumerate(plan[:3]):
                print(
                    f"  [{i + 1}/{len(plan)}] period={period} end={cursor.isoformat()}",
                )
            if len(plan) > 3:
                print(f"  ... ({len(plan) - 3} more)")
        return 0

    rc = 0
    for sym in args.symbols:
        out_path = canonical_bar_path(sym, args.timeframe)
        if args.root != MNQ_HISTORY_ROOT:
            tf_for_filename = {"1d": "D", "1w": "W"}.get(
                args.timeframe.lower(),
                args.timeframe,
            )
            out_path = args.root / f"{sym}1_{tf_for_filename}.csv"

        print(f"\n[fetch_mbt_met_bars] === {sym} ===")
        raw = fetch_bars(
            symbol=sym,
            timeframe=args.timeframe,
            start=start_dt,
            end=end_dt,
            base_url=args.base_url,
        )
        if not raw:
            print(
                f"[fetch_mbt_met_bars] {sym}: zero rows fetched.\n"
                "  Likely causes:\n"
                "    1. Client Portal Gateway not running at base_url\n"
                "    2. Session not authenticated (visit base_url in browser)\n"
                "    3. CME Crypto market-data subscription not active\n"
                "    4. Front-month conid resolution failed",
            )
            rc = 1
            continue

        new_rows = _normalize_rows(raw)
        if args.no_merge:
            n = write_csv(out_path, new_rows)
            print(f"[fetch_mbt_met_bars] {sym}: OVERWROTE {n} rows -> {out_path}")
        else:
            merged, n_existing, n_new = merge_with_existing(out_path, new_rows)
            n = write_csv(out_path, merged)
            print(
                f"[fetch_mbt_met_bars] {sym}: merged existing={n_existing} new={n_new} total={n} -> {out_path}",
            )

        gaps = report_gaps(new_rows, args.timeframe)
        if gaps:
            print(
                f"[fetch_mbt_met_bars] {sym}: detected {len(gaps)} intra-window gap(s) >2x bar size",
            )
            for gs, ge in gaps[:5]:
                print(
                    "  gap "
                    f"{datetime.fromtimestamp(gs, UTC).isoformat()} -> "
                    f"{datetime.fromtimestamp(ge, UTC).isoformat()}",
                )

        if new_rows:
            first = datetime.fromtimestamp(new_rows[0]["time"], UTC).date()
            last = datetime.fromtimestamp(new_rows[-1]["time"], UTC).date()
            span_days = (new_rows[-1]["time"] - new_rows[0]["time"]) / 86400
            print(
                f"[fetch_mbt_met_bars] {sym}: coverage {first} -> {last} "
                f"({span_days:.1f} days, {len(new_rows)} new bars)",
            )

    return rc


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())

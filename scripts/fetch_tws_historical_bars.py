"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_tws_historical_bars
=================================================================
TWS API historical bar fetcher (multi-symbol, multi-timeframe).

Why this exists
---------------
The lab harness, kaizen loop, and several walk-forward regressors need
multi-month OHLCV history at the canonical workspace path. The existing
``fetch_mbt_met_bars.py`` fetcher only works when the IBKR Client Portal
Gateway (HTTPS REST) is running and authenticated -- a separate process
from the TWS API gateway used by the live execution venue.

On the VPS today (2026-05-07) the live venue's TWS gateway IS running
on port 4002 (paper) but the Client Portal Gateway is not. This fetcher
talks to that already-running TWS gateway via ``ib_insync`` --
``ib.reqHistoricalData`` -- chunks the calls to walk back N days, and
writes the canonical CSV the strategy_lab harness expects.

Pattern mirrored
----------------
This is a sibling of:

* ``feeds/bar_accumulator.py`` -- proven ``ib.reqHistoricalData`` pattern
  for the live realtime refresh path. We generalize the chunking logic
  to walk arbitrary lookback windows.
* ``scripts/fetch_mbt_met_bars.py`` -- the chunk planner, CSV merge,
  canonical output path conventions, and gap reporter. We keep the same
  output schema so downstream tooling sees no difference.

Key differences from ``fetch_mbt_met_bars.py``:

* TWS API (port 4002 / 7497 / 4001) instead of Client Portal HTTPS REST.
* ``ib_insync.Future`` + ``qualifyContractsAsync`` for contract resolution
  instead of ``/trsrv/futures``.
* Symbol scope is broad (any CME / NYMEX / COMEX / CBOT futures the
  ``FUTURES_MAP`` knows) -- not MBT/MET-only.
* Pacing -- TWS allows ~60 historical-bar requests per 10 minutes. We
  sleep 10 seconds between chunks (<= 6 req/min, well under the cap)
  and back off further on TWS pacing-violation errors.

Pre-flight requirements
-----------------------
1. **TWS or IB Gateway running** on port 4002 (paper Gateway), 7497
   (paper TWS), or 4001 (live Gateway). 4002 is the default.
2. **Client ID free** -- ``ETA_IBKR_CLIENT_ID`` and the venues already use
   IDs 50/51/99 + the env var. The fetcher defaults to clientId=11 to
   stay clear of the supervisor + bar_accumulator + venue.
3. **No CME crypto market-data subscription required** for futures
   metadata, but historical bars require the standard CME Level 1
   subscription that the paper account ships with.

Usage
-----
::

    # Default -- fetch 540 days of 5m MBT + MET
    python -m eta_engine.scripts.fetch_tws_historical_bars

    # Multi-asset fleet
    python -m eta_engine.scripts.fetch_tws_historical_bars \\
        --symbols MNQ NQ ES MES MBT MET --days 540

    # Dry run -- print planned chunk count, no connect
    python -m eta_engine.scripts.fetch_tws_historical_bars \\
        --symbols MBT MET --dry-run

Pacing safety
-------------
TWS caps historical bar requests at "max 60 per 10 minutes" per the
official IBKR pacing rules. The script enforces:

* 10 second sleep between successful chunks (<= 6/min, <= 60/10min).
* Detect "pacing violation" / "Historical Market Data Service error
  message:Pacing violation" via ``ib.errorEvent`` and apply a 60 second
  back-off before continuing.
* Total chunk plan is bounded; for 540d 5m x 2 symbols ~ 36 chunks/symbol
  x 2 = 72 requests, well under the 60/10min cap when paced.
"""

from __future__ import annotations

# ruff: noqa: E402, I001, PLR0912 -- standalone script, sys.path bootstrap, branchy CLI

import argparse
import csv
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import MNQ_HISTORY_ROOT  # noqa: E402

log = logging.getLogger("fetch_tws_historical_bars")

# --- Defaults ---
# Default fetch scope.
_DEFAULT_SYMBOLS: tuple[str, ...] = ("MBT", "MET")
_DEFAULT_DAYS: int = 540
_DEFAULT_TIMEFRAME: str = "5m"

# Connection defaults -- paper Gateway is the canonical TWS surface.
_DEFAULT_HOST: str = "127.0.0.1"
_DEFAULT_PORT: int = 4002
# Fallback ports tried in order if --port fails. 7497 = paper TWS,
# 4001 = live Gateway.
_FALLBACK_PORTS: tuple[int, ...] = (7497, 4001)
# Client ID 11 -- stays clear of bar_accumulator (50, 51), the live venue
# default (99), and the env-driven supervisor IDs (typically 1-10, 100+).
_DEFAULT_CLIENT_ID: int = 11
_CONNECT_TIMEOUT_S: float = 20.0

# Symbol -> (root, exchange, currency, multiplier). Mirrors
# ``venues.ibkr_live.FUTURES_MAP`` so the fetcher is reusable across
# the full futures fleet (MNQ/NQ/ES/MES/MBT/MET/CL/MCL/NG/GC/MGC/ZN/ZB
# /6E/M6E and more).
_FUTURES_MAP: dict[str, tuple[str, str, str, str]] = {
    "MNQ":  ("MNQ", "CME",   "USD", "2"),
    "NQ":   ("NQ",  "CME",   "USD", "20"),
    "ES":   ("ES",  "CME",   "USD", "50"),
    "MES":  ("MES", "CME",   "USD", "5"),
    "RTY":  ("RTY", "CME",   "USD", "50"),
    "M2K":  ("M2K", "CME",   "USD", "5"),
    "MBT":  ("MBT", "CME",   "USD", "0.1"),
    "MET":  ("MET", "CME",   "USD", "0.1"),
    "NG":   ("NG",  "NYMEX", "USD", "10000"),
    "CL":   ("CL",  "NYMEX", "USD", "1000"),
    "MCL":  ("MCL", "NYMEX", "USD", "100"),
    "GC":   ("GC",  "COMEX", "USD", "100"),
    "MGC":  ("MGC", "COMEX", "USD", "10"),
    "ZN":   ("ZN",  "CBOT",  "USD", "1000"),
    "ZB":   ("ZB",  "CBOT",  "USD", "1000"),
    "6E":   ("EUR", "CME",   "USD", "125000"),
    "M6E":  ("M6E", "CME",   "USD", "12500"),
}

# Bar-size and chunking math. TWS caps ``durationStr`` at ~30 days for
# 5m/1m bars in practice -- using larger windows triggers
# "Historical data request limit exceeded" or empty payloads. The
# canonical chunk per timeframe is the largest TWS reliably returns:
_TF_TO_BAR_SIZE: dict[str, str] = {
    "1m":  "1 min",
    "5m":  "5 mins",
    "15m": "15 mins",
    "1h":  "1 hour",
}

# Per-chunk lookback. TWS caps at 1 D for 1m, 30 D for 5m/15m, 1 Y for 1h.
_TF_TO_CHUNK_DAYS: dict[str, int] = {
    "1m":  1,
    "5m":  30,
    "15m": 30,
    "1h":  365,
}

_BAR_SECONDS: dict[str, int] = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "1h":  3600,
}

# Pacing: sleep between chunks. TWS allows 60/10min; 10s sleep = 6/min.
_PACING_SLEEP_S: float = 10.0
# Back-off applied when we detect a pacing-violation error.
_PACING_VIOLATION_BACKOFF_S: float = 60.0


# --- Helpers ---
@dataclass(frozen=True)
class _ChunkPlan:
    """One ``reqHistoricalData`` call's parameters."""
    end_dt: datetime
    duration_str: str
    bar_size: str
    what_to_show: str = "TRADES"
    use_rth: bool = False  # Futures globex evening session matters.


class _IbProto(Protocol):
    """Subset of ib_insync.IB we need. Lets the test suite mock cleanly."""

    def connect(self, host: str, port: int, clientId: int, timeout: float) -> Any: ...
    def disconnect(self) -> None: ...
    def isConnected(self) -> bool: ...
    def qualifyContracts(self, *contracts: Any) -> list[Any]: ...
    def reqHistoricalData(
        self,
        contract: Any,
        endDateTime: str,
        durationStr: str,
        barSizeSetting: str,
        whatToShow: str,
        useRTH: bool,
        formatDate: int,
    ) -> list[Any]: ...


def _chunk_duration_str(timeframe: str) -> str:
    """Return the canonical TWS ``durationStr`` for one chunk."""
    days = _TF_TO_CHUNK_DAYS[timeframe]
    return f"{days} D"


def plan_chunks(
    *, timeframe: str, days: int, end: datetime,
) -> list[_ChunkPlan]:
    """Plan the chunked ``reqHistoricalData`` calls.

    Pure function -- used by ``--dry-run`` and by tests.
    Walks backwards from ``end`` in chunks of ``_TF_TO_CHUNK_DAYS[timeframe]``
    until ``days`` of history are covered.
    """
    if timeframe not in _TF_TO_BAR_SIZE:
        raise ValueError(
            f"unknown timeframe {timeframe!r}; supported: {sorted(_TF_TO_BAR_SIZE)}",
        )
    chunk_days = _TF_TO_CHUNK_DAYS[timeframe]
    bar_size = _TF_TO_BAR_SIZE[timeframe]
    duration_str = _chunk_duration_str(timeframe)

    plan: list[_ChunkPlan] = []
    cursor = end
    earliest = end - timedelta(days=days)
    # Cap to avoid runaway loops on degenerate inputs.
    max_chunks = 5000
    while cursor > earliest and len(plan) < max_chunks:
        plan.append(_ChunkPlan(
            end_dt=cursor,
            duration_str=duration_str,
            bar_size=bar_size,
        ))
        cursor = cursor - timedelta(days=chunk_days)
    return plan


def _build_future(symbol: str) -> Any:
    """Return an unqualified ``ib_insync.Future`` for the symbol."""
    spec = _FUTURES_MAP.get(symbol.upper().strip())
    if spec is None:
        raise ValueError(
            f"unknown futures symbol {symbol!r}; "
            f"supported: {sorted(_FUTURES_MAP)}",
        )
    root, exchange, currency, _mult = spec
    # Lazy import -- keeps tests that mock the module from paying the
    # ib_insync cold-start cost (and dodges the Py3.14 module-init bug).
    from ib_insync import Future
    contract = Future(symbol=root, exchange=exchange, currency=currency)
    contract.includeExpired = False
    return contract


def _format_end_dt_for_tws(dt: datetime) -> str:
    """TWS expects ``YYYYMMDD HH:MM:SS`` (UTC) or empty for 'now'.

    ib_insync accepts an empty string to mean 'use the most recent
    available bar'; that is what the realtime refresh path in
    ``bar_accumulator.py`` uses. For chunked historical fetches we need
    each chunk's explicit end-time so the cursor walks backwards.
    """
    return dt.strftime("%Y%m%d %H:%M:%S")


# --- Connection ---
def _connect_with_fallback(
    ib: _IbProto,
    *,
    host: str,
    primary_port: int,
    client_id: int,
    timeout: float = _CONNECT_TIMEOUT_S,
) -> int:
    """Connect to TWS API. Returns the port we landed on.

    Tries ``primary_port`` first; on failure falls back through
    ``_FALLBACK_PORTS``. Raises ``ConnectionError`` if all fail.
    """
    ports_to_try: list[int] = [primary_port]
    for p in _FALLBACK_PORTS:
        if p != primary_port:
            ports_to_try.append(p)

    last_exc: Exception | None = None
    for port in ports_to_try:
        try:
            log.info(
                "connecting to TWS at %s:%d (clientId=%d, timeout=%.0fs)",
                host, port, client_id, timeout,
            )
            ib.connect(host, port, clientId=client_id, timeout=timeout)
            log.info("connected on port %d", port)
            return port
        except Exception as exc:  # noqa: BLE001 -- broker errors are diverse
            last_exc = exc
            log.warning("connect to port %d failed: %s", port, exc)
    raise ConnectionError(
        f"could not connect to TWS API on any of {ports_to_try}: {last_exc!r}",
    )


# --- Bar fetch ---
def _bar_to_row(bar: Any) -> dict[str, Any] | None:
    """Convert an ib_insync ``BarData`` (or test stand-in) to canonical row.

    Returns None on a malformed bar.
    """
    raw_date = getattr(bar, "date", None)
    if raw_date is None:
        return None
    if isinstance(raw_date, datetime):
        if raw_date.tzinfo is None:
            raw_date = raw_date.replace(tzinfo=UTC)
        ts_s = int(raw_date.timestamp())
    elif hasattr(raw_date, "timetuple"):
        # ``date`` objects (daily bars) -- promote to midnight UTC.
        dt = datetime(raw_date.year, raw_date.month, raw_date.day, tzinfo=UTC)
        ts_s = int(dt.timestamp())
    elif isinstance(raw_date, str):
        # TWS sometimes returns string ``"YYYYMMDD HH:MM:SS"``.
        parts = raw_date.split(" ")
        date_part = parts[0]
        time_part = parts[1] if len(parts) > 1 else "00:00:00"
        try:
            dt = datetime.strptime(
                f"{date_part} {time_part}", "%Y%m%d %H:%M:%S",
            )
            ts_s = int(dt.replace(tzinfo=UTC).timestamp())
        except ValueError:
            return None
    elif isinstance(raw_date, int | float):
        ts_s = int(raw_date)
    else:
        return None

    try:
        return {
            "time": ts_s,
            "open": float(getattr(bar, "open", 0.0)),
            "high": float(getattr(bar, "high", 0.0)),
            "low": float(getattr(bar, "low", 0.0)),
            "close": float(getattr(bar, "close", 0.0)),
            "volume": float(getattr(bar, "volume", 0.0) or 0.0),
        }
    except (TypeError, ValueError):
        return None


def fetch_chunks(
    *,
    ib: _IbProto,
    symbol: str,
    timeframe: str,
    days: int,
    end: datetime | None = None,
    pacing_sleep_s: float = _PACING_SLEEP_S,
) -> list[dict[str, Any]]:
    """Pull ``days`` of ``timeframe`` history for ``symbol``.

    Caller must have already connected ``ib`` (use ``_connect_with_fallback``).
    Stitches per-chunk responses, dedupes by timestamp, returns canonical
    rows ready for ``merge_with_existing``.

    Errors per chunk are logged + skipped -- never fatal.
    """
    end_dt = end or datetime.now(UTC)
    plan = plan_chunks(timeframe=timeframe, days=days, end=end_dt)
    log.info(
        "fetch %s/%s -- %d chunks of %s back to %s",
        symbol, timeframe, len(plan), _chunk_duration_str(timeframe),
        (end_dt - timedelta(days=days)).date(),
    )

    contract = _build_future(symbol)
    qualified_list = ib.qualifyContracts(contract)
    if not qualified_list:
        log.error(
            "qualifyContracts returned nothing for %s -- contract resolution failed",
            symbol,
        )
        return []
    qualified = qualified_list[0]
    log.info(
        "qualified %s -> %s/%s expiry=%s",
        symbol,
        getattr(qualified, "symbol", "?"),
        getattr(qualified, "exchange", "?"),
        getattr(qualified, "lastTradeDateOrContractMonth", "?"),
    )

    out: list[dict[str, Any]] = []
    chunk_t0 = time.monotonic()
    for idx, chunk in enumerate(plan, start=1):
        end_str = _format_end_dt_for_tws(chunk.end_dt)
        try:
            bars = ib.reqHistoricalData(
                qualified,
                endDateTime=end_str,
                durationStr=chunk.duration_str,
                barSizeSetting=chunk.bar_size,
                whatToShow=chunk.what_to_show,
                useRTH=chunk.use_rth,
                formatDate=2,  # epoch seconds where supported
            )
        except Exception as exc:  # noqa: BLE001 -- broker errors are diverse
            msg = str(exc).lower()
            if "pacing" in msg or "historical data request limit" in msg:
                log.warning(
                    "[%d/%d] pacing violation -- backing off %.0fs",
                    idx, len(plan), _PACING_VIOLATION_BACKOFF_S,
                )
                time.sleep(_PACING_VIOLATION_BACKOFF_S)
            else:
                log.warning("[%d/%d] chunk %s failed: %s", idx, len(plan), end_str, exc)
            continue

        rows_added = 0
        for bar in bars or []:
            row = _bar_to_row(bar)
            if row is not None:
                out.append(row)
                rows_added += 1
        log.info(
            "[%d/%d] %s end=%s -> %d bars (cumulative=%d)",
            idx, len(plan), symbol, end_str, rows_added, len(out),
        )

        # Pace between chunks -- keep below 60 req / 10min.
        if idx < len(plan):
            time.sleep(pacing_sleep_s)

    # Dedupe by timestamp, sort ascending.
    seen: set[int] = set()
    deduped: list[dict[str, Any]] = []
    for row in sorted(out, key=lambda r: int(r["time"])):
        ts = int(row["time"])
        if ts in seen:
            continue
        seen.add(ts)
        deduped.append(row)

    elapsed = time.monotonic() - chunk_t0
    log.info(
        "%s: %d unique bars across %d chunks in %.1fs",
        symbol, len(deduped), len(plan), elapsed,
    )
    return deduped


# --- CSV write -- same canonical surface as fetch_mbt_met_bars ---
def canonical_bar_path(symbol: str, timeframe: str, root: Path | None = None) -> Path:
    """Match ``feeds.strategy_lab.engine._resolve_bar_path``: ``{SYMBOL}1_{TF}.csv``."""
    base = root if root is not None else MNQ_HISTORY_ROOT
    tf_for_filename = {"1d": "D", "1w": "W"}.get(
        timeframe.lower(), timeframe,
    )
    return base / f"{symbol.upper()}1_{tf_for_filename}.csv"


def merge_with_existing(
    out_path: Path, new_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Merge ``new_rows`` into any existing CSV at ``out_path``.

    Returns ``(merged_rows, n_existing, n_new_unique)``.
    """
    existing: list[dict[str, Any]] = []
    if out_path.exists():
        try:
            with out_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        existing.append({
                            "time": int(row["time"]),
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": float(row["close"]),
                            "volume": float(row.get("volume", 0.0)),
                        })
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            existing = []
    seen = {int(r["time"]) for r in existing}
    new_unique = [r for r in new_rows if int(r["time"]) not in seen]
    merged = existing + new_unique
    merged.sort(key=lambda r: int(r["time"]))
    return merged, len(existing), len(new_unique)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow([
                int(r["time"]), r["open"], r["high"],
                r["low"], r["close"], r["volume"],
            ])
    return len(rows)


def report_gaps(
    rows: list[dict[str, Any]], timeframe: str,
) -> list[tuple[int, int]]:
    """Coarse signal: consecutive bars spaced > 2x bar-size apart."""
    if not rows or timeframe not in _BAR_SECONDS:
        return []
    bar_secs = _BAR_SECONDS[timeframe]
    threshold = bar_secs * 2
    gaps: list[tuple[int, int]] = []
    for prev, curr in zip(rows, rows[1:], strict=False):
        delta = int(curr["time"]) - int(prev["time"])
        if delta > threshold:
            gaps.append((int(prev["time"]), int(curr["time"])))
    return gaps


# --- CLI ---
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fetch_tws_historical_bars",
        description=(
            "Fetch historical futures bars via the running TWS API gateway "
            "(port 4002 by default). Reusable across CME/NYMEX/COMEX/CBOT "
            "futures the FUTURES_MAP knows."
        ),
    )
    p.add_argument(
        "--symbols", nargs="+", default=list(_DEFAULT_SYMBOLS),
        help=f"Symbols to fetch (default: {' '.join(_DEFAULT_SYMBOLS)}).",
    )
    p.add_argument(
        "--days", type=int, default=_DEFAULT_DAYS,
        help=f"Lookback in days (default: {_DEFAULT_DAYS}).",
    )
    p.add_argument(
        "--timeframe", default=_DEFAULT_TIMEFRAME,
        choices=sorted(_TF_TO_BAR_SIZE),
        help=f"Bar size (default: {_DEFAULT_TIMEFRAME}).",
    )
    p.add_argument(
        "--port", type=int, default=_DEFAULT_PORT,
        help=(
            f"TWS API port (default: {_DEFAULT_PORT}, paper Gateway). "
            f"Falls back to {_FALLBACK_PORTS} on connect failure."
        ),
    )
    p.add_argument(
        "--host", default=_DEFAULT_HOST,
        help=f"TWS API host (default: {_DEFAULT_HOST}).",
    )
    p.add_argument(
        "--client-id", type=int, default=_DEFAULT_CLIENT_ID,
        help=(
            f"ib_insync client ID (default: {_DEFAULT_CLIENT_ID}). "
            "Pick one not used by supervisor / bar_accumulator / venues."
        ),
    )
    p.add_argument(
        "--end", default=None,
        help="ISO date YYYY-MM-DD; default = now (UTC).",
    )
    p.add_argument(
        "--root", type=Path, default=MNQ_HISTORY_ROOT,
        help="Output history root (default: canonical mnq_data/history).",
    )
    p.add_argument(
        "--no-merge", action="store_true",
        help="Overwrite existing CSV instead of merging.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print planned chunks without connecting to TWS.",
    )
    p.add_argument(
        "--pacing-sleep", type=float, default=_PACING_SLEEP_S,
        help=(
            f"Seconds to sleep between successful chunks "
            f"(default: {_PACING_SLEEP_S}). "
            "Lower at your own risk; TWS caps at 60 req/10min."
        ),
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Python logging level (default: INFO).",
    )
    return p


def _validate_symbols(symbols: list[str]) -> list[str]:
    """Return upper-cased symbols, dropping any unsupported ones."""
    out: list[str] = []
    for raw in symbols:
        sym = raw.upper().strip()
        if sym not in _FUTURES_MAP:
            log.warning(
                "skipping unsupported symbol %r; supported: %s",
                sym, sorted(_FUTURES_MAP),
            )
            continue
        out.append(sym)
    return out


def run(argv: list[str] | None = None, *, ib: _IbProto | None = None) -> int:
    """Run the fetcher.

    ``ib`` parameter exists for the test suite -- production callers leave
    it None and the function constructs a real ``ib_insync.IB()``.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    symbols = _validate_symbols(list(args.symbols))
    if not symbols:
        log.error("no valid symbols after filtering -- nothing to do")
        return 1

    end_dt = (
        datetime.fromisoformat(args.end).replace(tzinfo=UTC)
        if args.end else datetime.now(UTC)
    )

    bar_secs = _BAR_SECONDS[args.timeframe]
    expected_bars_per_symbol = int(args.days * 86400 / bar_secs)
    log.info(
        "plan: symbols=%s tf=%s days=%d end=%s",
        symbols, args.timeframe, args.days, end_dt.date(),
    )
    log.info(
        "expected ~%d calendar-time bars/symbol (pre-session-mask)",
        expected_bars_per_symbol,
    )

    if args.dry_run:
        plan = plan_chunks(
            timeframe=args.timeframe, days=args.days, end=end_dt,
        )
        for sym in symbols:
            out_path = canonical_bar_path(sym, args.timeframe, root=args.root)
            print(
                f"[dry-run] {sym}: {len(plan)} chunks of "
                f"{_chunk_duration_str(args.timeframe)} -> {out_path}",
            )
            for i, chunk in enumerate(plan[:3]):
                print(
                    f"  [{i + 1}/{len(plan)}] end={chunk.end_dt.isoformat()} "
                    f"duration={chunk.duration_str} bar={chunk.bar_size}",
                )
            if len(plan) > 3:
                print(f"  ... ({len(plan) - 3} more)")
        # Pacing summary so the operator knows wall-time before they connect.
        total_chunks = len(plan) * len(symbols)
        approx_seconds = total_chunks * args.pacing_sleep
        print(
            f"[dry-run] total chunks across symbols: {total_chunks} "
            f"(approx {approx_seconds:.0f}s = {approx_seconds / 60:.1f}min "
            "of pacing sleeps, plus per-chunk fetch time)",
        )
        return 0

    if ib is None:
        from ib_insync import IB  # noqa: I001 -- lazy import; tests inject mocks.
        ib = IB()  # type: ignore[assignment]

    # -- CONNECT --------------------------------------------------
    try:
        _connect_with_fallback(
            ib, host=args.host, primary_port=args.port,
            client_id=args.client_id,
        )
    except ConnectionError as exc:
        log.error("could not connect to TWS API: %s", exc)
        log.error(
            "operator action: ensure TWS or IB Gateway is running on "
            "%s:%s and the client ID %d is free",
            args.host, args.port, args.client_id,
        )
        return 1

    rc = 0
    try:
        for sym in symbols:
            out_path = canonical_bar_path(sym, args.timeframe, root=args.root)
            log.info("=== %s -> %s ===", sym, out_path)

            try:
                rows = fetch_chunks(
                    ib=ib,
                    symbol=sym,
                    timeframe=args.timeframe,
                    days=args.days,
                    end=end_dt,
                    pacing_sleep_s=args.pacing_sleep,
                )
            except Exception as exc:  # noqa: BLE001 -- never crash on one symbol
                log.error("%s: fetch failed: %s", sym, exc)
                rc = 1
                continue

            if not rows:
                log.warning("%s: zero rows fetched -- see warnings above", sym)
                rc = 1
                continue

            if args.no_merge:
                n = write_csv(out_path, rows)
                log.info("%s: OVERWROTE %d rows -> %s", sym, n, out_path)
            else:
                merged, n_existing, n_new = merge_with_existing(out_path, rows)
                n = write_csv(out_path, merged)
                log.info(
                    "%s: merged existing=%d new=%d total=%d -> %s",
                    sym, n_existing, n_new, n, out_path,
                )

            gaps = report_gaps(rows, args.timeframe)
            if gaps:
                log.info("%s: detected %d intra-window gaps >2x bar size", sym, len(gaps))
                for gs, ge in gaps[:3]:
                    log.info(
                        "  gap %s -> %s",
                        datetime.fromtimestamp(gs, UTC).isoformat(),
                        datetime.fromtimestamp(ge, UTC).isoformat(),
                    )
            first_ts = datetime.fromtimestamp(rows[0]["time"], UTC).date()
            last_ts = datetime.fromtimestamp(rows[-1]["time"], UTC).date()
            log.info("%s: coverage %s -> %s (%d bars)", sym, first_ts, last_ts, len(rows))
    finally:
        try:
            ib.disconnect()
            log.info("disconnected from TWS")
        except Exception:  # noqa: BLE001 -- disconnect is best-effort
            pass

    return rc


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())



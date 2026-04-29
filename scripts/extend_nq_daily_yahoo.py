"""
EVOLUTIONARY TRADING ALGO  //  scripts.extend_nq_daily_yahoo
=============================================================
Append fresh NQ / MNQ daily bars from Yahoo Finance to the
on-disk parquet/CSV history under the workspace ``mnq_data/history`` root.

Why this script
---------------
The walk-forward bar count is the gating factor for DSR pass —
strategies promote when at least ~10 OOS windows go positive, and
window count = (total_days - window) / step. Per the 2026-04-27
state, MNQ 5m has ~107 days (too short) and NQ daily has 27 years
(plenty). The fastest unblock is to keep daily history fresh so
the next strategy promotion runs on data that ends today, not on
2026-04-13 stale snapshots.

Yahoo Finance is the canonical free daily source for futures-
adjacent symbols. ``NQ=F`` and ``ES=F`` are continuous front-month
contracts; for our purposes (DRB on daily bars where we just need
range high/low) the continuous-front splice is acceptable.

Why not 5m / 1m
---------------
Yahoo doesn't backfill futures intraday. The 5m gap stays open
until either (a) a TradingView Desktop pull lands or (b) the
Databento mandate is unparked. This script handles only the
daily / weekly path — explicitly out of scope for intraday
backfill.

Usage
-----
    python -m eta_engine.scripts.extend_nq_daily_yahoo \\
        [--symbol NQ=F] [--out <workspace>\\mnq_data\\history\\NQ1_D.csv] \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import MNQ_HISTORY_ROOT  # noqa: E402

# ---------------------------------------------------------------------------
# Yahoo bridge
# ---------------------------------------------------------------------------


def _fetch_yahoo_daily(symbol: str, start_date: datetime) -> list[dict[str, Any]]:
    """Fetch daily bars from Yahoo. Returns rows in the on-disk schema.

    Schema: ``{"time": <unix_ts>, "open", "high", "low", "close", "volume"}``.

    Raises RuntimeError on network or parsing failure — caller decides
    whether that's fatal (live cron) or just a no-op (local dev).
    """
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except ImportError as e:
        raise RuntimeError(
            "yfinance not installed. pip install yfinance",
        ) from e

    try:
        df = yf.download(
            symbol,
            start=start_date.date().isoformat(),
            interval="1d",
            auto_adjust=False,
            progress=False,
        )
    except Exception as e:  # noqa: BLE001 — yfinance surface
        raise RuntimeError(f"Yahoo download failed for {symbol}: {e!r}") from e

    if df is None or df.empty:
        return []

    def _cell(row: Any, col: str) -> float:  # noqa: ANN401 - yfinance row
        """Extract one column from a yfinance row, flat or MultiIndex."""
        v = row.get(col)
        if v is None or hasattr(v, "shape"):
            v = row.get((col, symbol))
        return float(v) if v is not None else 0.0

    rows: list[dict[str, Any]] = []
    for ts, row in df.iterrows():
        # yfinance returns a DataFrame indexed by Timestamp; ``ts`` IS
        # the index value, ``row`` is the per-day Series.
        unix_ts = int(ts.replace(tzinfo=UTC).timestamp())
        rows.append({
            "time": unix_ts,
            "open": _cell(row, "Open"),
            "high": _cell(row, "High"),
            "low": _cell(row, "Low"),
            "close": _cell(row, "Close"),
            "volume": int(_cell(row, "Volume")),
        })
    return rows


# ---------------------------------------------------------------------------
# CSV roundtrip
# ---------------------------------------------------------------------------


def _read_existing(out_path: Path) -> tuple[int | None, int]:
    """Return (last_unix_ts, n_rows) for the existing CSV.

    last_unix_ts is None when the file is missing or has no data rows.
    """
    if not out_path.exists():
        return None, 0
    last_ts: int | None = None
    n = 0
    with out_path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                last_ts = int(row["time"])
            except (KeyError, ValueError, TypeError):
                continue
            n += 1
    return last_ts, n


def _append_rows(out_path: Path, rows: list[dict[str, Any]]) -> int:
    """Append rows to the CSV, creating the header if the file is new."""
    if not rows:
        return 0
    header = ["time", "open", "high", "low", "close", "volume"]
    needs_header = not out_path.exists() or out_path.stat().st_size == 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if needs_header:
            w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in header})
    return len(rows)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def extend(
    symbol: str, out_path: Path, *, dry_run: bool = False,
) -> dict[str, Any]:
    """Run the extend; return a summary dict (also suitable for a JSON log)."""
    last_ts, n_existing = _read_existing(out_path)
    if last_ts is None:
        # Fall back to "all of 2025" if the file is empty — operators
        # should bootstrap with a separate one-shot manual pull instead
        # of having this cron mass-fetch decades on a fresh machine.
        start = datetime(2025, 1, 1, tzinfo=UTC)
    else:
        # Start one day after the last on-disk bar.
        start = datetime.fromtimestamp(last_ts, UTC).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )

    print(f"[fetch] symbol={symbol} start={start.date()} existing_rows={n_existing}")
    fetched = _fetch_yahoo_daily(symbol, start)
    # Filter out anything <= last_ts so we never duplicate.
    if last_ts is not None:
        fetched = [r for r in fetched if r["time"] > last_ts]
    print(f"[fetch] fresh rows: {len(fetched)}")

    summary: dict[str, Any] = {
        "symbol": symbol,
        "out": str(out_path),
        "existing_rows": n_existing,
        "fresh_rows": len(fetched),
        "dry_run": dry_run,
        "ran_at_utc": datetime.now(UTC).isoformat(),
    }
    if dry_run or not fetched:
        return summary

    written = _append_rows(out_path, fetched)
    summary["written"] = written
    last_unix = max(r["time"] for r in fetched)
    summary["new_last_date"] = datetime.fromtimestamp(last_unix, UTC).date().isoformat()
    print(f"[ok] wrote {written} rows; new last date = {summary['new_last_date']}")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--symbol", default="NQ=F",
        help="Yahoo Finance ticker (default: NQ=F continuous-front).",
    )
    p.add_argument(
        "--out", type=Path, default=MNQ_HISTORY_ROOT / "NQ1_D.csv",
        help="Output CSV. Appended in-place; header written if file is new.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Fetch + report but do NOT modify disk.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        summary = extend(args.symbol, args.out, dry_run=args.dry_run)
    except RuntimeError as e:
        print(f"[fatal] {e}", file=sys.stderr)
        return 2
    print("\n=== summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

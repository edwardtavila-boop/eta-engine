"""Refresh canonical NQ/MNQ index-futures bars for replay and dashboard truth.

This is a data-only heartbeat. It refreshes public/IBKR-fallback futures bars
under ``C:\\EvolutionaryTradingAlgo\\mnq_data\\history`` and writes a machine
readable status document. It never submits, cancels, flattens, promotes, or
otherwise touches order routing.
"""

from __future__ import annotations

# ruff: noqa: E402, I001 -- standalone script amends sys.path before eta_engine imports.

import argparse
import contextlib
import io
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.feeds.fetch_index_futures_bars import (  # noqa: E402
    _YF_PERIOD_BY_TF,
    _YF_SYMBOL,
    _fetch_via_ibkr,
    _fetch_via_yfinance,
    _merge_with_existing,
    _write_csv,
)
from eta_engine.scripts.workspace_roots import (  # noqa: E402
    ETA_INDEX_FUTURES_BAR_REFRESH_STATUS_PATH,
    MNQ_HISTORY_ROOT,
    WORKSPACE_ROOT,
    ensure_parent,
)

DEFAULT_SYMBOLS = ("NQ", "MNQ")
DEFAULT_TIMEFRAME = "5m"
DEFAULT_SOURCE = "yfinance"
SCHEMA_VERSION = "2026-05-15.index-futures-refresh.v1"
TRUTH_NOTE = (
    "Market-data refresh only; not broker PnL/proof. "
    "Never submits, cancels, flattens, promotes, or acknowledges orders."
)


def _assert_canonical_path(path: Path) -> Path:
    resolved = path.resolve()
    workspace = WORKSPACE_ROOT.resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError(f"Refusing non-canonical ETA path: {path}")
    return resolved


def _coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"start_utc": None, "end_utc": None, "latest_age_minutes": None}
    first_ts = int(rows[0]["time"])
    last_ts = int(rows[-1]["time"])
    latest = datetime.fromtimestamp(last_ts, UTC)
    age_minutes = max(0.0, (datetime.now(UTC) - latest).total_seconds() / 60)
    return {
        "start_utc": datetime.fromtimestamp(first_ts, UTC).isoformat(),
        "end_utc": latest.isoformat(),
        "latest_age_minutes": round(age_minutes, 1),
    }


def refresh_symbol(
    symbol: str,
    *,
    timeframe: str = DEFAULT_TIMEFRAME,
    period: str | None = None,
    source: str = DEFAULT_SOURCE,
    history_root: Path = MNQ_HISTORY_ROOT,
) -> dict[str, Any]:
    """Refresh one continuous futures symbol into canonical history."""
    normalized = symbol.upper()
    if normalized not in _YF_SYMBOL:
        return {"symbol": normalized, "ok": False, "error": "unsupported symbol"}

    selected_period = period or _YF_PERIOD_BY_TF[timeframe]
    out_path = _assert_canonical_path(history_root / f"{normalized}1_{timeframe}.csv")

    diagnostics = io.StringIO()
    with contextlib.redirect_stdout(diagnostics):
        if source == "ibkr":
            fetched = _fetch_via_ibkr(normalized, timeframe, selected_period)
            source_used = "ibkr"
            if not fetched:
                fetched = _fetch_via_yfinance(normalized, timeframe, selected_period)
                source_used = "yfinance_fallback"
        else:
            fetched = _fetch_via_yfinance(normalized, timeframe, selected_period)
            source_used = "yfinance"

    if not fetched:
        return {
            "symbol": normalized,
            "ok": False,
            "source": source_used,
            "timeframe": timeframe,
            "period": selected_period,
            "path": str(out_path),
            "error": "zero rows fetched",
            "diagnostics": diagnostics.getvalue().strip().splitlines()[-6:],
        }

    merged, existing_count, new_unique_count = _merge_with_existing(out_path, fetched)
    rows_written = _write_csv(out_path, merged)

    return {
        "symbol": normalized,
        "ok": rows_written > 0,
        "source": source_used,
        "timeframe": timeframe,
        "period": selected_period,
        "path": str(out_path),
        "rows_existing": existing_count,
        "rows_fetched": len(fetched),
        "rows_new_unique": new_unique_count,
        "rows_total": rows_written,
        "coverage": _coverage(merged),
        "diagnostics": diagnostics.getvalue().strip().splitlines()[-6:],
    }


def build_summary(results: list[dict[str, Any]], elapsed_ms: int) -> dict[str, Any]:
    ok_count = sum(1 for result in results if result.get("ok"))
    latest_ages = [
        result.get("coverage", {}).get("latest_age_minutes")
        for result in results
        if result.get("coverage", {}).get("latest_age_minutes") is not None
    ]
    max_latest_age = max(latest_ages) if latest_ages else None
    if ok_count == len(results):
        status = "PASS"
    elif ok_count > 0:
        status = "PARTIAL"
    else:
        status = "FAILED"
    return {
        "status": status,
        "ok_count": ok_count,
        "symbol_count": len(results),
        "elapsed_ms": elapsed_ms,
        "max_latest_age_minutes": max_latest_age,
        "order_action_allowed": False,
        "broker_backed": False,
        "truth_note": TRUTH_NOTE,
    }


def run_refresh(
    *,
    symbols: list[str],
    timeframe: str,
    period: str | None,
    source: str,
    history_root: Path,
) -> dict[str, Any]:
    started = time.time()
    results: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            results.append(
                refresh_symbol(
                    symbol,
                    timeframe=timeframe,
                    period=period,
                    source=source,
                    history_root=history_root,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- one bad symbol must not hide the rest
            results.append({"symbol": symbol.upper(), "ok": False, "error": str(exc)})

    elapsed_ms = int((time.time() - started) * 1000)
    return {
        "kind": "eta_index_futures_bar_refresh",
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "summary": build_summary(results, elapsed_ms),
        "symbols": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="refresh_index_futures_bars")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=list(DEFAULT_SYMBOLS),
        choices=sorted(_YF_SYMBOL),
        help="Continuous index-futures roots to refresh. Default: NQ MNQ.",
    )
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME, choices=sorted(_YF_PERIOD_BY_TF))
    parser.add_argument("--period", default=None)
    parser.add_argument("--source", default=DEFAULT_SOURCE, choices=("yfinance", "ibkr"))
    parser.add_argument(
        "--history-root",
        type=Path,
        default=MNQ_HISTORY_ROOT,
        help="Canonical output directory. Defaults to C:\\EvolutionaryTradingAlgo\\mnq_data\\history.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON summary to stdout.")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional JSON status path. Defaults to stdout only unless supplied.",
    )
    parser.add_argument(
        "--write-default-status",
        action="store_true",
        help="Also write var\\eta_engine\\state\\index_futures_bar_refresh_latest.json.",
    )
    args = parser.parse_args()

    payload = run_refresh(
        symbols=args.symbols,
        timeframe=args.timeframe,
        period=args.period,
        source=args.source,
        history_root=_assert_canonical_path(args.history_root),
    )

    out_paths: list[Path] = []
    if args.write_default_status:
        out_paths.append(ETA_INDEX_FUTURES_BAR_REFRESH_STATUS_PATH)
    if args.out is not None:
        out_paths.append(args.out)
    for out_path in out_paths:
        safe_path = _assert_canonical_path(out_path)
        ensure_parent(safe_path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        summary = payload["summary"]
        print(
            "[refresh_index_futures_bars] "
            f"{summary['status']} {summary['ok_count']}/{summary['symbol_count']} "
            f"in {summary['elapsed_ms']}ms"
        )
        for result in payload["symbols"]:
            marker = "OK" if result.get("ok") else "FAIL"
            print(
                "[refresh_index_futures_bars] "
                f"{result['symbol']} {marker} new={result.get('rows_new_unique', 0)} "
                f"total={result.get('rows_total', 0)} latest={result.get('coverage', {}).get('end_utc')}"
            )

    return 0 if payload["summary"]["ok_count"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

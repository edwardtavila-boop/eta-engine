"""Sage backtester (Wave-5 #16, 2026-04-27).

For each closed trade in the journal, replay the sage on the entry bar
and record (sage_conviction, alignment_score, realized_R) tuples. The
output dataset is the foundation for:
  * outcome-learned weight learning (EdgeTracker.observe)
  * sage-edge dashboards
  * v22 promotion-gate scoring

Usage::

    python -m eta_engine.brain.jarvis_v3.sage.backtester \\
        --journal state/burn_in/closed_trades.jsonl \\
        --bars-source state/bars/         \\
        --output state/sage/backtest.json
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("sage_backtester")


def replay_one_trade(
    *,
    bars_at_entry: list[dict[str, Any]],
    side: str,
    realized_r: float,
    symbol: str = "",
) -> dict[str, Any]:
    """Run the sage on the bars window at entry, return summary dict."""
    from eta_engine.brain.jarvis_v3.sage import MarketContext, consult_sage

    ctx = MarketContext(bars=bars_at_entry, side=side, symbol=symbol)
    # Don't use cache or apply edge weights during backtest -- we want
    # the deterministic baseline, and we're FEEDING the edge tracker.
    report = consult_sage(ctx, parallel=False, use_cache=False, apply_edge_weights=False)

    # Optionally feed the EdgeTracker so weights learn from each trade
    try:
        from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker

        tracker = default_tracker()
        for school_name, verdict in report.per_school.items():
            tracker.observe(
                school=school_name,
                school_bias=verdict.bias.value,
                entry_side=side,
                realized_r=realized_r,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("edge tracker observe failed: %s", exc)

    return {
        "symbol": symbol,
        "side": side,
        "realized_r": realized_r,
        "composite_bias": report.composite_bias.value,
        "conviction": report.conviction,
        "alignment_score": report.alignment_score,
        "consensus_pct": report.consensus_pct,
        "schools_aligned": report.schools_aligned_with_entry,
        "schools_disagree": report.schools_disagreeing_with_entry,
    }


def replay_trades_iter(
    trades: list[dict[str, Any]],
    *,
    bars_lookup: Callable[[str, str], list[dict[str, Any]]] | None = None,
    parallel: bool = False,
) -> list[dict[str, Any]]:
    """For each trade dict {symbol, side, entry_ts, realized_r}, run the
    sage on bars at entry. ``bars_lookup`` is a callable
    ``(symbol, entry_ts) -> list[bar dict]``.

    Returns a list of summary dicts (one per trade) suitable for analysis
    or dumping to JSON. Pass ``parallel=True`` to use ThreadPoolExecutor.
    """
    out: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    for t in trades:
        if bars_lookup is None:
            logger.warning("no bars_lookup provided -- skipping %s", t.get("symbol"))
            continue
        try:
            bars = bars_lookup(t["symbol"], t["entry_ts"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("bars_lookup failed for %s: %s", t.get("symbol"), exc)
            continue
        if not bars or len(bars) < 30:
            continue
        jobs.append(
            {
                "bars": bars,
                "side": t["side"],
                "realized_r": float(t["realized_r"]),
                "symbol": t["symbol"],
            }
        )

    if not jobs:
        return out

    if parallel and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            futures = {ex.submit(_replay_job, j): j for j in jobs}
            for f in as_completed(futures):
                try:
                    out.append(f.result())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("replay_job failed: %s", exc)
    else:
        for j in jobs:
            out.append(_replay_job(j))
    return out


def _replay_job(j: dict[str, Any]) -> dict[str, Any]:
    return replay_one_trade(
        bars_at_entry=j["bars"],
        side=j["side"],
        realized_r=j["realized_r"],
        symbol=j["symbol"],
    )


def _read_json_or_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("trades") or data.get("rows") or data.get("data") or [] if isinstance(data, dict) else data
        return [row for row in rows if isinstance(row, dict)]
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("skipping invalid jsonl line in %s", path)
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _normalize_trade(row: dict[str, Any]) -> dict[str, Any] | None:
    symbol = row.get("symbol") or row.get("ticker") or row.get("instrument")
    side = row.get("side") or row.get("direction")
    entry_ts = row.get("entry_ts") or row.get("entry_time") or row.get("opened_at") or row.get("ts")
    realized_r = row.get("realized_r")
    if realized_r is None:
        realized_r = row.get("r_multiple") or row.get("r")
    if symbol is None or side is None or entry_ts is None or realized_r is None:
        return None
    return {
        "symbol": str(symbol),
        "side": str(side).lower(),
        "entry_ts": str(entry_ts),
        "realized_r": float(realized_r),
    }


def load_closed_trades(path: Path) -> list[dict[str, Any]]:
    """Load closed trades from JSON/JSONL or a simple SQLite journal."""
    if path.suffix.lower() in {".json", ".jsonl", ".ndjson"}:
        raw_rows = _read_json_or_jsonl(path)
    elif path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
        raw_rows = _read_sqlite_rows(path)
    else:
        raise ValueError(f"unsupported journal format: {path.suffix}")
    trades = []
    for row in raw_rows:
        trade = _normalize_trade(row)
        if trade is not None:
            trades.append(trade)
    return trades


def _read_sqlite_rows(path: Path) -> list[dict[str, Any]]:
    import sqlite3

    rows: list[dict[str, Any]] = []
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        tables = [
            r["name"]
            for r in con.execute(
                "select name from sqlite_master where type='table' order by name",
            )
        ]
        for table in tables:
            try:
                sample = con.execute(f"select * from {table} limit 5000").fetchall()
            except sqlite3.Error:
                continue
            for row in sample:
                d = dict(row)
                if _normalize_trade(d) is not None:
                    rows.append(d)
            if rows:
                break
    finally:
        con.close()
    return rows


def _bar_ts(bar: dict[str, Any]) -> str:
    return str(bar.get("ts") or bar.get("timestamp") or bar.get("time") or "")


def _load_bars_file(path: Path) -> list[dict[str, Any]]:
    rows = _read_json_or_jsonl(path)
    return sorted(rows, key=_bar_ts)


def build_file_bars_lookup(
    bars_source: Path,
    *,
    window_bars: int = 120,
) -> Callable[[str, str], list[dict[str, Any]]]:
    """Build a bars lookup from files named SYMBOL.json/jsonl.

    The lookup is intentionally boring and deterministic; production
    data connectors can still call replay_trades_iter directly.
    """
    cache: dict[str, list[dict[str, Any]]] = {}

    def _lookup(symbol: str, entry_ts: str) -> list[dict[str, Any]]:
        key = symbol.upper()
        if key not in cache:
            candidates = [
                bars_source / f"{symbol}.jsonl",
                bars_source / f"{symbol.upper()}.jsonl",
                bars_source / f"{symbol}.json",
                bars_source / f"{symbol.upper()}.json",
            ]
            path = next((p for p in candidates if p.exists()), None)
            if path is None:
                return []
            cache[key] = _load_bars_file(path)
        # Bisect for O(log n) windowing instead of O(n) linear filter
        timestamps = [_bar_ts(b) for b in cache[key]]
        idx = bisect.bisect_right(timestamps, str(entry_ts))
        return cache[key][max(0, idx - window_bars) : idx]

    return _lookup


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--journal", type=Path, required=False, default=None, help="Decision journal SQLite file (or JSONL)")
    p.add_argument(
        "--bars-source",
        type=Path,
        required=False,
        default=None,
        help="Directory containing SYMBOL.jsonl/json bar files",
    )
    p.add_argument("--output", type=Path, default=Path("state/sage/backtest.json"))
    p.add_argument("--window-bars", type=int, default=120)
    p.add_argument("--parallel", action="store_true", help="Replay trades with ThreadPoolExecutor")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.journal is None or not args.journal.exists():
        logger.warning(
            "no journal at %s -- no replay run emitted",
            args.journal,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "status": "no_journal",
                    "n_trades": 0,
                    "n_replayed": 0,
                    "summary": {},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return 0

    trades = load_closed_trades(args.journal)
    bars_lookup = (
        build_file_bars_lookup(args.bars_source, window_bars=args.window_bars)
        if args.bars_source is not None and args.bars_source.exists()
        else None
    )
    replayed = replay_trades_iter(trades, bars_lookup=bars_lookup, parallel=args.parallel)
    avg_r = sum(float(row["realized_r"]) for row in replayed) / len(replayed) if replayed else 0.0
    avg_alignment = sum(float(row["alignment_score"]) for row in replayed) / len(replayed) if replayed else 0.0
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "status": "ok" if replayed else "no_replayable_trades",
        "n_trades": len(trades),
        "n_replayed": len(replayed),
        "summary": {
            "avg_realized_r": round(avg_r, 4),
            "avg_alignment_score": round(avg_alignment, 4),
        },
        "rows": replayed,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # MLflow experiment tracking
    try:
        from eta_engine.brain.jarvis_v3.sage.mlflow_tracker import track_backtest

        with track_backtest(
            name=f"sage_backtest_{datetime.now(UTC).strftime('%Y%m%d_%H%M')}",
            params={
                "window_bars": args.window_bars,
                "n_trades": len(trades),
                "n_replayed": len(replayed),
                "parallel": args.parallel,
            },
        ) as run:
            run.log_metrics(
                {
                    "avg_realized_r": round(avg_r, 4),
                    "avg_alignment_score": round(avg_alignment, 4),
                }
            )
            run.log_artifact(args.output)
    except Exception:  # noqa: BLE001
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())

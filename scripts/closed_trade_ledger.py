"""Build the canonical closed-trade ledger summary.

The supervisor writes append-only close records. This script normalizes
those JSONL rows into a small schema-backed status artifact used by the
public ops surface and prop-live readiness gate.

Wave-25 (2026-05-13) added data-source classification:
  - test_fixture       — known test bot IDs (t1, propagate_bot, etc.)
  - historical_unverified — records from the legacy in-repo archive
                            (eta_engine/state/jarvis_intel/...)  # HISTORICAL-PATH-OK
  - live_unverified    — records from the canonical state path that
                          lack an explicit data_source tag
  - live / paper / backtest — explicit data_source values from records
                              tagged at the write site (forward-only;
                              older records will not have these tags)

By default, the ledger now EXCLUDES test fixtures and historical
unverified records. Audits making prop-launch decisions must default
to ``live`` + ``paper`` only; backtest-emitted records that polluted
the legacy archive previously inflated composite scores 17x for some
bots (m2k 1151 trades vs. ~4 actual live).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from eta_engine.scripts import workspace_roots  # noqa: E402

SCHEMA_VERSION = 2  # bumped: records now carry data_source classification
DEFAULT_OUT = workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH
DEFAULT_RECENT_LIMIT = 50

# Known test/fixture bot IDs that have polluted the canonical
# trade_closes.jsonl. These records are excluded from production
# audits by default. Update when new test fixtures are introduced.
TEST_BOT_IDS = frozenset(
    {
        "t1",
        "t2",
        "t3",
        "propagate_bot",
        "test_bot",
        "fake_bot",
        "mock_bot",
        "fixture_bot",
        "smoke_bot",
        "demo_bot",
    },
)

# Recognized data_source classifications. Used for filtering at audit
# time and for tagging at write time.
DATA_SOURCE_LIVE = "live"  # real fills from a live broker connection
DATA_SOURCE_PAPER = "paper"  # paper-trading simulator with realistic fills
DATA_SOURCE_BACKTEST = "backtest"  # historical replay
DATA_SOURCE_LIVE_UNVERIFIED = "live_unverified"  # canonical path, no tag
DATA_SOURCE_HISTORICAL_UNVERIFIED = "historical_unverified"  # legacy archive
DATA_SOURCE_TEST_FIXTURE = "test_fixture"  # known test bot IDs

# Production audit default: only records we can defend as live or paper.
# Any record without a tag and from the canonical path is suspect; only
# explicit ``live`` and ``paper`` records pass.
DEFAULT_PRODUCTION_DATA_SOURCES = frozenset({DATA_SOURCE_LIVE, DATA_SOURCE_PAPER})


def _parse_ts(raw: Any) -> datetime | None:  # noqa: ANN401
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _as_float(value: Any, default: float = 0.0) -> float:  # noqa: ANN401
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any) -> dict[str, Any]:  # noqa: ANN401
    return value if isinstance(value, dict) else {}


def _default_source_paths() -> list[Path]:
    return [
        workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH,
        workspace_roots.ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH,
    ]


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                payload["_source_path"] = str(path)
                rows.append(payload)
    return rows


def classify_data_source(row: dict[str, Any]) -> str:
    """Classify a trade-close record by its origin.

    Order of precedence (most specific first):
      1. Explicit ``data_source`` field on the record (forward-tagged).
      2. Test bot IDs (t1, propagate_bot, etc.) → test_fixture.
      3. Source path is the legacy in-repo archive
         (``eta_engine/state/jarvis_intel/...``) → historical_unverified.  # HISTORICAL-PATH-OK
      4. Source path is the canonical workspace state path → live_unverified.
      5. Anything else → live_unverified (defensive default).

    The classification is used both for filtering (audits drop test
    fixtures and historical-unverified records by default) and for
    transparent reporting (per_data_source counts in the report).
    """
    explicit = str(row.get("data_source") or "").strip().lower()
    if explicit in {
        DATA_SOURCE_LIVE,
        DATA_SOURCE_PAPER,
        DATA_SOURCE_BACKTEST,
        DATA_SOURCE_LIVE_UNVERIFIED,
        DATA_SOURCE_HISTORICAL_UNVERIFIED,
        DATA_SOURCE_TEST_FIXTURE,
    }:
        return explicit

    bot_id = str(row.get("bot_id") or "").strip().lower()
    if bot_id in TEST_BOT_IDS:
        return DATA_SOURCE_TEST_FIXTURE

    source_path = str(row.get("_source_path") or "").replace("\\", "/").lower()
    # The legacy archive lives inside the repo at eta_engine/state/.  # HISTORICAL-PATH-OK
    # The canonical live state lives at .../var/eta_engine/state/.
    if "/eta_engine/state/jarvis_intel/" in source_path and "/var/" not in source_path:  # HISTORICAL-PATH-OK
        return DATA_SOURCE_HISTORICAL_UNVERIFIED
    return DATA_SOURCE_LIVE_UNVERIFIED


def load_close_records(
    *,
    source_paths: list[Path] | None = None,
    since_days: int | None = None,
    bot_filter: str | None = None,
    data_sources: frozenset[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    """Load close records, optionally filtered by data_source.

    ``data_sources`` is a set of acceptable data_source classifications.
    None or empty means no filter (legacy behaviour). Production callers
    should pass ``DEFAULT_PRODUCTION_DATA_SOURCES`` to avoid pollution
    from the legacy archive and test fixtures.
    """
    cutoff = datetime.now(UTC) - timedelta(days=since_days) if since_days else None
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for path in source_paths or _default_source_paths():
        for row in _iter_jsonl(path):
            ts = _parse_ts(row.get("ts"))
            if cutoff and (ts is None or ts < cutoff):
                continue
            bot_id = str(row.get("bot_id") or "")
            if bot_filter and bot_id != bot_filter:
                continue
            classification = classify_data_source(row)
            row["_data_source"] = classification
            if data_sources and classification not in data_sources:
                continue
            extra = _as_dict(row.get("extra"))
            key = "|".join(
                [
                    str(row.get("signal_id") or ""),
                    bot_id,
                    str(row.get("ts") or ""),
                    str(extra.get("close_ts") or ""),
                    str(row.get("realized_r") or ""),
                ],
            )
            if key in seen:
                continue
            seen.add(key)
            records.append(row)
    records.sort(key=lambda row: str(row.get("ts") or ""))
    return records


def _normalize_close(row: dict[str, Any]) -> dict[str, Any]:
    extra = _as_dict(row.get("extra"))
    realized_r = _as_float(row.get("realized_r"))
    realized_pnl = _as_float(row.get("realized_pnl", extra.get("realized_pnl")))
    return {
        "ts": row.get("ts") or extra.get("close_ts") or "",
        "close_ts": extra.get("close_ts") or row.get("ts") or "",
        "signal_id": row.get("signal_id") or "",
        "bot_id": row.get("bot_id") or "",
        "symbol": extra.get("symbol") or "",
        "side": extra.get("side") or "",
        "qty": _as_float(extra.get("qty")),
        "fill_price": _as_float(extra.get("fill_price")),
        "realized_pnl": round(realized_pnl, 4),
        "realized_r": round(realized_r, 4),
        "regime": row.get("regime") or "",
        "session": row.get("session") or "",
        "action_taken": row.get("action_taken") or "",
        "data_source": row.get("_data_source") or classify_data_source(row),
        "source_path": row.get("_source_path") or "",
    }


def _stats_for(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [row for row in rows if _as_float(row.get("realized_r")) > 0]
    losses = [row for row in rows if _as_float(row.get("realized_r")) < 0]
    flats = len(rows) - len(wins) - len(losses)
    gross_profit = sum(max(_as_float(row.get("realized_pnl")), 0.0) for row in rows)
    gross_loss = abs(sum(min(_as_float(row.get("realized_pnl")), 0.0) for row in rows))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    return {
        "closed_trade_count": len(rows),
        "winning_trade_count": len(wins),
        "losing_trade_count": len(losses),
        "flat_trade_count": flats,
        "win_rate_pct": round((len(wins) / len(rows)) * 100, 2) if rows else None,
        "total_realized_pnl": round(sum(_as_float(row.get("realized_pnl")) for row in rows), 2),
        "cumulative_r": round(sum(_as_float(row.get("realized_r")) for row in rows), 4),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
    }


def _per_bot_stats(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("bot_id") or "unknown")].append(row)
    return {bot_id: _stats_for(bot_rows) for bot_id, bot_rows in sorted(grouped.items())}


def build_ledger_report(
    *,
    source_paths: list[Path] | None = None,
    since_days: int | None = None,
    bot_filter: str | None = None,
    recent_limit: int = DEFAULT_RECENT_LIMIT,
    data_sources: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    paths = source_paths or _default_source_paths()
    # Pre-classification pass over ALL records so we can report the
    # full pollution picture even when filtering kicks in.
    all_raw = load_close_records(
        source_paths=paths,
        since_days=since_days,
        bot_filter=bot_filter,
        data_sources=None,  # no filter on the diagnostic pass
    )
    per_data_source_full = defaultdict(int)
    for row in all_raw:
        per_data_source_full[row.get("_data_source") or "?"] += 1

    # Production filter — defaults to live + paper if caller didn't specify.
    effective_filter = data_sources if data_sources is not None else DEFAULT_PRODUCTION_DATA_SOURCES
    raw_records = [row for row in all_raw if row.get("_data_source") in effective_filter]
    closes = [_normalize_close(row) for row in raw_records]
    stats = _stats_for(closes)
    existing_sources = [str(path) for path in paths if path.exists()]
    source_status = "READY" if workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH.exists() else "LEGACY_FALLBACK_USED"
    if not existing_sources:
        source_status = "MISSING"
    return {
        "kind": "eta_closed_trade_ledger",
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source_status": source_status,
        "source_paths": [str(path) for path in paths],
        "active_source_paths": existing_sources,
        "since_days": since_days,
        "bot_filter": bot_filter,
        "data_sources_filter": sorted(effective_filter) if effective_filter else None,
        "per_data_source_unfiltered": dict(sorted(per_data_source_full.items())),
        **stats,
        "per_bot": _per_bot_stats(closes),
        "recent_closes": closes[-recent_limit:] if recent_limit > 0 else [],
    }


def write_report(report: dict[str, Any], path: Path = DEFAULT_OUT) -> Path:
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the canonical closed-trade ledger summary")
    parser.add_argument("--source", action="append", type=Path, help="JSONL close source; repeatable")
    parser.add_argument("--since-days", type=int, default=None)
    parser.add_argument("--bot", default=None)
    parser.add_argument("--recent-limit", type=int, default=DEFAULT_RECENT_LIMIT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument(
        "--data-source",
        action="append",
        choices=[
            DATA_SOURCE_LIVE,
            DATA_SOURCE_PAPER,
            DATA_SOURCE_BACKTEST,
            DATA_SOURCE_LIVE_UNVERIFIED,
            DATA_SOURCE_HISTORICAL_UNVERIFIED,
            DATA_SOURCE_TEST_FIXTURE,
        ],
        help=(
            "Data source classification(s) to INCLUDE; repeatable. "
            "Default = live + paper (drops legacy archive + test fixtures). "
            "Pass --include-all to disable filtering."
        ),
    )
    parser.add_argument(
        "--include-all",
        action="store_true",
        help="Include ALL data sources (legacy/test/historical). Use only for diagnostic dumps.",
    )
    args = parser.parse_args(argv)

    if args.include_all:
        data_sources = None
    elif args.data_source:
        data_sources = frozenset(args.data_source)
    else:
        data_sources = DEFAULT_PRODUCTION_DATA_SOURCES

    report = build_ledger_report(
        source_paths=args.source,
        since_days=args.since_days,
        bot_filter=args.bot,
        recent_limit=args.recent_limit,
        data_sources=data_sources,
    )
    out_path = None if args.no_write else write_report(report, args.out)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(f"closed-trade ledger: {report['closed_trade_count']} closes (filter={report['data_sources_filter']})")
        print(f"win rate: {report['win_rate_pct']}%")
        print(f"total PnL: {report['total_realized_pnl']:+.2f}")
        print(f"cumulative R: {report['cumulative_r']:+.4f}R")
        print(f"source status: {report['source_status']}")
        print("per data_source (UNFILTERED -- shows full pollution picture):")
        for ds, n in report.get("per_data_source_unfiltered", {}).items():
            print(f"  {ds}: {n}")
        if out_path is not None:
            print(f"wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

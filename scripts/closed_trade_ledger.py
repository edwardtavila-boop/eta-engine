"""Build the canonical closed-trade ledger summary.

The supervisor writes append-only close records. This script normalizes
those JSONL rows into a small schema-backed status artifact used by the
public ops surface and prop-live readiness gate.
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

SCHEMA_VERSION = 1
DEFAULT_OUT = workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH
DEFAULT_RECENT_LIMIT = 50


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


def load_close_records(
    *,
    source_paths: list[Path] | None = None,
    since_days: int | None = None,
    bot_filter: str | None = None,
) -> list[dict[str, Any]]:
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
) -> dict[str, Any]:
    paths = source_paths or _default_source_paths()
    raw_records = load_close_records(
        source_paths=paths,
        since_days=since_days,
        bot_filter=bot_filter,
    )
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
    args = parser.parse_args(argv)

    report = build_ledger_report(
        source_paths=args.source,
        since_days=args.since_days,
        bot_filter=args.bot,
        recent_limit=args.recent_limit,
    )
    out_path = None if args.no_write else write_report(report, args.out)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(f"closed-trade ledger: {report['closed_trade_count']} closes")
        print(f"win rate: {report['win_rate_pct']}%")
        print(f"total PnL: {report['total_realized_pnl']:+.2f}")
        print(f"cumulative R: {report['cumulative_r']:+.4f}R")
        print(f"source status: {report['source_status']}")
        if out_path is not None:
            print(f"wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

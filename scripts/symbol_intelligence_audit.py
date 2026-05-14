"""Audit the ETA symbol-intelligence data spine.

This script answers the practical operator question: for each priority symbol,
do we have enough joined evidence to reason about price movement, events,
decisions, and realized outcomes?
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from eta_engine.data.symbol_intel import SymbolIntelQuality, SymbolIntelRecord, SymbolIntelStore  # noqa: E402
from eta_engine.scripts import workspace_roots  # noqa: E402

PRIORITY_SYMBOLS = ("MNQ1", "NQ1", "ES1", "MES1", "YM1", "MYM1")
REQUIRED_COMPONENTS = ("bars", "events", "decisions", "outcomes", "quality")
OPTIONAL_COMPONENTS = ("news", "book")
_COMPONENT_RECORD_TYPES = {
    "bars": "bar",
    "events": "macro_event",
    "decisions": "decision",
    "outcomes": "outcome",
    "quality": "quality",
    "news": "news",
    "book": "book",
}


@dataclass(frozen=True)
class SymbolIntelCoverage:
    symbol: str
    status: str
    score: float
    components: dict[str, bool] = field(default_factory=dict)
    optional_components: dict[str, bool] = field(default_factory=dict)
    missing_required: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    latest_record_utc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _latest_record_time(records: list[SymbolIntelRecord]) -> datetime | None:
    if not records:
        return None
    return max(rec.ts_utc for rec in records)


def _has_record(store: SymbolIntelStore, *, record_type: str, symbol: str) -> tuple[bool, datetime | None]:
    rows = list(store.iter_records(record_type=record_type, symbol=symbol))
    return bool(rows), _latest_record_time(rows)


def inspect_symbol(
    symbol: str,
    *,
    store: SymbolIntelStore | None = None,
    now: datetime | None = None,
) -> SymbolIntelCoverage:
    del now  # Reserved for freshness windows once live providers are enabled.
    symbol = symbol.upper().strip()
    store = store or SymbolIntelStore()

    latest: list[datetime] = []
    components: dict[str, bool] = {}
    for component in REQUIRED_COMPONENTS:
        ok, ts = _has_record(store, record_type=_COMPONENT_RECORD_TYPES[component], symbol=symbol)
        components[component] = ok
        if ts is not None:
            latest.append(ts)

    optional_components: dict[str, bool] = {}
    for component in OPTIONAL_COMPONENTS:
        ok, ts = _has_record(store, record_type=_COMPONENT_RECORD_TYPES[component], symbol=symbol)
        optional_components[component] = ok
        if ts is not None:
            latest.append(ts)

    missing_required = [name for name, ok in components.items() if not ok]
    missing_optional = [name for name, ok in optional_components.items() if not ok]
    score = round((len(REQUIRED_COMPONENTS) - len(missing_required)) / len(REQUIRED_COMPONENTS), 4)
    if not missing_required:
        status = "green"
    elif score >= 0.6:
        status = "amber"
    else:
        status = "red"

    latest_ts = max(latest).isoformat() if latest else None
    return SymbolIntelCoverage(
        symbol=symbol,
        status=status,
        score=score,
        components=components,
        optional_components=optional_components,
        missing_required=missing_required,
        missing_optional=missing_optional,
        latest_record_utc=latest_ts,
    )


def _overall_status(rows: list[SymbolIntelCoverage]) -> str:
    if not rows:
        return "red"
    statuses = {row.status for row in rows}
    if "red" in statuses:
        return "red"
    if "amber" in statuses:
        return "amber"
    return "green"


def run_audit(
    *,
    symbols: list[str] | tuple[str, ...] = PRIORITY_SYMBOLS,
    store: SymbolIntelStore | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(tz=UTC)
    store = store or SymbolIntelStore()
    rows = [inspect_symbol(symbol, store=store, now=now) for symbol in symbols]
    return {
        "kind": "eta_symbol_intelligence_audit",
        "generated_at_utc": now.isoformat(),
        "data_lake_root": str(store.root),
        "overall_status": _overall_status(rows),
        "required_components": list(REQUIRED_COMPONENTS),
        "optional_components": list(OPTIONAL_COMPONENTS),
        "symbols": [row.to_dict() for row in rows],
    }


def write_snapshot(
    payload: dict[str, Any],
    *,
    path: Path = workspace_roots.ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH,
) -> Path:
    workspace_roots.ensure_parent(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _parse_ts(raw: Any) -> datetime:
    if not raw:
        return datetime.now(tz=UTC)
    value = str(raw).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iter_trade_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    if not isinstance(raw, dict):
        return []
    for key in ("closed_trades", "recent_closes", "trades", "rows", "items"):
        value = raw.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _load_yaml_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(text) or {}
        events = raw.get("events", []) if isinstance(raw, dict) else []
        return [row for row in events if isinstance(row, dict)]
    except Exception:
        events: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("- "):
                if current:
                    events.append(current)
                current = {}
                line = line[2:].strip()
            if ":" in line and current is not None:
                key, value = line.split(":", 1)
                value = value.strip().strip('"')
                current[key.strip()] = None if value == "null" else value
        if current:
            events.append(current)
        return events


def _existing_payload_values(store: SymbolIntelStore, *, record_type: str, payload_key: str) -> set[str]:
    return {
        str(rec.payload[payload_key])
        for rec in store.iter_records(record_type=record_type)
        if payload_key in rec.payload
    }


def backfill_bars_from_history(
    *,
    history_root: Path = workspace_roots.MNQ_HISTORY_ROOT,
    store: SymbolIntelStore | None = None,
    symbols: list[str] | tuple[str, ...] = PRIORITY_SYMBOLS,
) -> int:
    if not history_root.exists():
        return 0
    store = store or SymbolIntelStore()
    existing_paths = _existing_payload_values(store, record_type="bar", payload_key="dataset_path")
    count = 0
    for symbol in symbols:
        for path in sorted(history_root.glob(f"{symbol.upper()}_*.csv")):
            dataset_path = str(path.resolve())
            if dataset_path in existing_paths:
                continue
            timeframe = path.stem.removeprefix(f"{symbol.upper()}_")
            stat = path.stat()
            ts = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            rec = SymbolIntelRecord(
                record_type="bar",
                ts_utc=ts,
                symbol=symbol,
                source="csv_history",
                payload={
                    "dataset_path": dataset_path,
                    "timeframe": timeframe,
                    "bytes": stat.st_size,
                    "last_modified_utc": ts.isoformat(),
                },
                quality=SymbolIntelQuality(confidence=0.75, is_reconciled=True),
            )
            store.append(rec)
            existing_paths.add(dataset_path)
            count += 1
    return count


def backfill_events_from_calendar(
    *,
    calendar_path: Path = workspace_roots.ETA_RUNTIME_STATE_DIR / "event_calendar.yaml",
    store: SymbolIntelStore | None = None,
    symbols: list[str] | tuple[str, ...] = PRIORITY_SYMBOLS,
) -> int:
    store = store or SymbolIntelStore()
    target_symbols = [symbol.upper().strip() for symbol in symbols]
    existing_keys = _existing_payload_values(store, record_type="macro_event", payload_key="event_key")
    count = 0
    for event in _load_yaml_events(calendar_path):
        event_symbol = event.get("symbol")
        if event_symbol:
            raw_symbol = str(event_symbol).upper().strip()
            affected = [symbol for symbol in target_symbols if symbol == raw_symbol or symbol.rstrip("1") == raw_symbol]
        else:
            affected = target_symbols
        for symbol in affected:
            event_key = f"{event.get('ts_utc')}|{event.get('kind')}|{symbol}"
            if event_key in existing_keys:
                continue
            rec = SymbolIntelRecord(
                record_type="macro_event",
                ts_utc=_parse_ts(event.get("ts_utc")),
                symbol=symbol,
                source="event_calendar",
                payload={
                    "event_key": event_key,
                    "kind": event.get("kind"),
                    "severity": event.get("severity"),
                    "source_symbol": event_symbol,
                },
                quality=SymbolIntelQuality(confidence=0.65, is_reconciled=True),
            )
            store.append(rec)
            existing_keys.add(event_key)
            count += 1
    return count


def default_bot_symbol_map() -> dict[str, str]:
    try:
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS

        return {assignment.bot_id: assignment.symbol.upper().strip() for assignment in ASSIGNMENTS}
    except Exception:
        return {}


def _decision_symbols(row: dict[str, Any], bot_symbol_map: dict[str, str]) -> set[str]:
    symbols: set[str] = set()
    for key in ("symbol", "ticker", "contract"):
        if row.get(key):
            symbols.add(str(row[key]).upper().strip())
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    for key in ("symbol", "ticker", "contract"):
        if metadata.get(key):
            symbols.add(str(metadata[key]).upper().strip())
    bot_ids: set[str] = set()
    for key in ("bot", "bot_id", "bot_a", "bot_b"):
        if metadata.get(key):
            bot_ids.add(str(metadata[key]))
        if row.get(key):
            bot_ids.add(str(row[key]))
    for link in row.get("links") or []:
        if isinstance(link, str) and link.startswith("bot:"):
            bot_ids.add(link.split(":", 1)[1])
    for bot_id in bot_ids:
        mapped = bot_symbol_map.get(bot_id)
        if mapped:
            symbols.add(mapped)
    return symbols


def backfill_decisions_from_journal(
    *,
    journal_path: Path = workspace_roots.ETA_RUNTIME_DECISION_JOURNAL_PATH,
    store: SymbolIntelStore | None = None,
    symbols: list[str] | tuple[str, ...] = PRIORITY_SYMBOLS,
    bot_symbol_map: dict[str, str] | None = None,
) -> int:
    if not journal_path.exists():
        return 0
    store = store or SymbolIntelStore()
    bot_symbol_map = bot_symbol_map or default_bot_symbol_map()
    target_symbols = {symbol.upper().strip() for symbol in symbols}
    existing_keys = _existing_payload_values(store, record_type="decision", payload_key="decision_key")
    count = 0
    with journal_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            for symbol in sorted(_decision_symbols(row, bot_symbol_map) & target_symbols):
                decision_key = f"{row.get('ts')}|{row.get('actor')}|{row.get('intent')}|{symbol}"
                if decision_key in existing_keys:
                    continue
                rec = SymbolIntelRecord(
                    record_type="decision",
                    ts_utc=_parse_ts(row.get("ts") or row.get("timestamp")),
                    symbol=symbol,
                    source="jarvis_decision_journal",
                    payload={
                        "decision_key": decision_key,
                        "actor": row.get("actor"),
                        "intent": row.get("intent"),
                        "outcome": row.get("outcome"),
                        "rationale": row.get("rationale"),
                        "links": row.get("links"),
                    },
                    quality=SymbolIntelQuality(confidence=0.8, is_reconciled=True),
                )
                store.append(rec)
                existing_keys.add(decision_key)
                count += 1
    return count


def backfill_quality_from_audit(
    *,
    store: SymbolIntelStore | None = None,
    symbols: list[str] | tuple[str, ...] = PRIORITY_SYMBOLS,
    now: datetime | None = None,
) -> int:
    store = store or SymbolIntelStore()
    now = now or datetime.now(tz=UTC)
    existing_keys = _existing_payload_values(store, record_type="quality", payload_key="quality_key")
    count = 0
    for symbol in symbols:
        coverage = inspect_symbol(symbol, store=store, now=now)
        quality_key = f"{now.date().isoformat()}|{symbol.upper().strip()}"
        if quality_key in existing_keys:
            continue
        rec = SymbolIntelRecord(
            record_type="quality",
            ts_utc=now,
            symbol=symbol,
            source="symbol_intelligence_audit",
            payload={
                "quality_key": quality_key,
                "pre_quality_score": coverage.score,
                "missing_required": coverage.missing_required,
                "missing_optional": coverage.missing_optional,
                "components": coverage.components,
            },
            quality=SymbolIntelQuality(confidence=0.9, is_reconciled=True),
        )
        store.append(rec)
        existing_keys.add(quality_key)
        count += 1
    return count


def bootstrap_existing_truth_surfaces(
    *,
    store: SymbolIntelStore | None = None,
    symbols: list[str] | tuple[str, ...] = PRIORITY_SYMBOLS,
) -> dict[str, int]:
    store = store or SymbolIntelStore()
    counts = {
        "bars": backfill_bars_from_history(store=store, symbols=symbols),
        "events": backfill_events_from_calendar(store=store, symbols=symbols),
        "decisions": backfill_decisions_from_journal(store=store, symbols=symbols),
        "outcomes": backfill_outcomes_from_closed_trade_ledger(store=store),
    }
    counts["quality"] = backfill_quality_from_audit(store=store, symbols=symbols)
    return counts


def _row_symbol(row: dict[str, Any]) -> str | None:
    symbol = row.get("symbol") or row.get("contract") or row.get("ticker")
    return str(symbol).upper().strip() if symbol else None


def _dedupe_key(row: dict[str, Any]) -> str:
    return "|".join(
        str(
            row.get(key)
            or row.get(key.replace("_", ""))
            or ""
        )
        for key in ("bot_id", "bot", "symbol", "signal_id", "close_ts", "exit_time_utc", "ts", "fill_price")
    )


def backfill_outcomes_from_closed_trade_ledger(
    *,
    source_path: Path = workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH,
    store: SymbolIntelStore | None = None,
) -> int:
    if not source_path.exists():
        return 0
    store = store or SymbolIntelStore()
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    existing = {
        str(rec.payload.get("dedupe_key"))
        for rec in store.iter_records(record_type="outcome")
        if rec.payload.get("dedupe_key")
    }
    count = 0
    for row in _iter_trade_rows(raw):
        symbol = _row_symbol(row)
        if not symbol:
            continue
        dedupe_key = _dedupe_key(row)
        if dedupe_key in existing:
            continue
        ts = _parse_ts(row.get("exit_time_utc") or row.get("close_ts") or row.get("ts") or row.get("time"))
        payload = {
            "dedupe_key": dedupe_key,
            "bot": row.get("bot") or row.get("bot_id"),
            "side": row.get("side"),
            "qty": row.get("qty"),
            "entry_price": row.get("entry_price"),
            "exit_price": row.get("exit_price") or row.get("fill_price"),
            "fill_price": row.get("fill_price"),
            "realized_pnl": row.get("realized_pnl"),
            "r_multiple": row.get("r_multiple") or row.get("realized_r"),
            "signal_id": row.get("signal_id"),
            "data_source": row.get("data_source"),
        }
        rec = SymbolIntelRecord(
            record_type="outcome",
            ts_utc=ts,
            symbol=symbol,
            source="broker_ledger",
            payload={key: value for key, value in payload.items() if value is not None},
            quality=SymbolIntelQuality(confidence=0.85, is_reconciled=True),
        )
        store.append(rec)
        existing.add(dedupe_key)
        count += 1
    return count


def _format_text(payload: dict[str, Any]) -> str:
    lines = [
        f"Symbol intelligence: {payload['overall_status'].upper()}",
        f"Data lake: {payload['data_lake_root']}",
        "",
        f"{'Symbol':<8} {'Status':<8} {'Score':<6} Missing required",
        "-" * 72,
    ]
    for row in payload["symbols"]:
        missing = ", ".join(row["missing_required"]) or "-"
        lines.append(f"{row['symbol']:<8} {row['status']:<8} {row['score']:<6.2f} {missing}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="symbol_intelligence_audit")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--write", action="store_true", help="write the canonical snapshot")
    parser.add_argument("--backfill-outcomes", action="store_true", help="backfill outcomes from closed trade ledger")
    parser.add_argument("--bootstrap-existing", action="store_true", help="backfill existing bars, events, decisions, outcomes")
    parser.add_argument("--symbol", action="append", dest="symbols", help="symbol to audit, repeatable")
    args = parser.parse_args(argv)

    store = SymbolIntelStore()
    backfilled = 0
    bootstrap_counts: dict[str, int] | None = None
    if args.bootstrap_existing:
        bootstrap_counts = bootstrap_existing_truth_surfaces(store=store, symbols=args.symbols or list(PRIORITY_SYMBOLS))
        backfilled = sum(bootstrap_counts.values())
    elif args.backfill_outcomes:
        backfilled = backfill_outcomes_from_closed_trade_ledger(store=store)
    payload = run_audit(symbols=args.symbols or list(PRIORITY_SYMBOLS), store=store)
    if backfilled:
        payload["backfilled_outcomes"] = backfilled
    if bootstrap_counts is not None:
        payload["bootstrap_counts"] = bootstrap_counts
    if args.write:
        payload["snapshot_path"] = str(write_snapshot(payload))

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_format_text(payload))
        if args.write:
            print(f"\nSnapshot: {payload['snapshot_path']}")
        if backfilled:
            print(f"Backfilled outcomes: {backfilled}")
    return 0 if payload["overall_status"] in {"green", "amber"} else 1


if __name__ == "__main__":
    sys.exit(main())

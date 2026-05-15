"""Audit the ETA symbol-intelligence data spine.

This script answers the practical operator question: for each priority symbol,
do we have enough joined evidence to reason about price movement, events,
decisions, and realized outcomes?
"""

from __future__ import annotations

import argparse
import contextlib
import csv
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

from eta_engine.data.market_news import fetch_google_news_headlines, query_for_symbol  # noqa: E402
from eta_engine.data.symbol_intel import SymbolIntelQuality, SymbolIntelRecord, SymbolIntelStore  # noqa: E402
from eta_engine.scripts import workspace_roots  # noqa: E402

PRIORITY_SYMBOLS = ("MNQ1", "NQ1", "ES1", "MES1", "YM1", "MYM1")
REQUIRED_COMPONENTS = ("bars", "events", "decisions", "outcomes", "quality")
OPTIONAL_COMPONENTS = ("news", "book")
FUTURE_RECORD_TOLERANCE_SECONDS = 300
SCHEDULED_FUTURE_RECORD_TYPES = frozenset({"macro_event"})
FUTURES_ROOT_ALIASES = {
    "MNQ": "MNQ1",
    "NQ": "NQ1",
    "MES": "MES1",
    "ES": "ES1",
    "MYM": "MYM1",
    "YM": "YM1",
}
_SIBLING_SYMBOL_ALIASES = {
    "MNQ1": ("NQ1",),
    "NQ1": ("MNQ1",),
    "MES1": ("ES1",),
    "ES1": ("MES1",),
    "MYM1": ("YM1",),
    "YM1": ("MYM1",),
}
LEGACY_BOT_SYMBOL_FALLBACKS = {
    # Historical close streams predate the registry entry but still carry
    # valid paper outcomes that should inform coverage audits.
    "mes_confluence": "MES1",
}
_COMPONENT_RECORD_TYPES = {
    "bars": "bar",
    "events": "macro_event",
    "decisions": "decision",
    "outcomes": "outcome",
    "quality": "quality",
    "news": "news",
    "book": "book",
}

_DEPTH_ROOT_ALIASES = {
    "MNQ1": "MNQ",
    "NQ1": "NQ",
    "ES1": "ES",
    "MES1": "MES",
    "YM1": "YM",
    "MYM1": "MYM",
    "6E1": "6E",
    "CL1": "CL",
    "MCL1": "MCL",
    "NG1": "NG",
    "GC1": "GC",
    "MGC1": "MGC",
    "BTC1": "BTC",
    "MBT1": "MBT",
    "ETH1": "ETH",
    "MET1": "MET",
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
    future_record_count: int = 0
    future_record_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _latest_record_time(records: list[SymbolIntelRecord]) -> datetime | None:
    if not records:
        return None
    return max(rec.ts_utc for rec in records)


def _is_future_record(rec: SymbolIntelRecord, *, now: datetime) -> bool:
    return (rec.ts_utc - now).total_seconds() > FUTURE_RECORD_TOLERANCE_SECONDS


def _records_for_component(
    store: SymbolIntelStore,
    *,
    record_type: str,
    symbol: str,
    now: datetime,
) -> tuple[bool, datetime | None, int]:
    rows = list(store.iter_records(record_type=record_type, symbol=symbol))
    future_rows = [rec for rec in rows if _is_future_record(rec, now=now)]
    valid_rows = [rec for rec in rows if not _is_future_record(rec, now=now)]
    component_rows = rows if record_type in SCHEDULED_FUTURE_RECORD_TYPES else valid_rows
    anomaly_count = 0 if record_type in SCHEDULED_FUTURE_RECORD_TYPES else len(future_rows)
    return bool(component_rows), _latest_record_time(valid_rows), anomaly_count


def inspect_symbol(
    symbol: str,
    *,
    store: SymbolIntelStore | None = None,
    now: datetime | None = None,
) -> SymbolIntelCoverage:
    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    symbol = symbol.upper().strip()
    store = store or SymbolIntelStore()

    latest: list[datetime] = []
    components: dict[str, bool] = {}
    future_record_count = 0
    future_record_types: list[str] = []
    for component in REQUIRED_COMPONENTS:
        record_type = _COMPONENT_RECORD_TYPES[component]
        ok, ts, future_count = _records_for_component(
            store,
            record_type=record_type,
            symbol=symbol,
            now=now,
        )
        components[component] = ok
        if future_count:
            future_record_count += future_count
            future_record_types.append(record_type)
        if ts is not None:
            latest.append(ts)

    optional_components: dict[str, bool] = {}
    for component in OPTIONAL_COMPONENTS:
        record_type = _COMPONENT_RECORD_TYPES[component]
        ok, ts, future_count = _records_for_component(
            store,
            record_type=record_type,
            symbol=symbol,
            now=now,
        )
        optional_components[component] = ok
        if future_count:
            future_record_count += future_count
            future_record_types.append(record_type)
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
        future_record_count=future_record_count,
        future_record_types=sorted(set(future_record_types)),
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
    overall_status = _overall_status(rows)
    average_score_pct = round(100 * sum(row.score for row in rows) / len(rows)) if rows else 0
    return {
        "schema": "eta.symbol_intelligence.audit.v1",
        "kind": "eta_symbol_intelligence_audit",
        "generated_at_utc": now.isoformat(),
        "data_lake_root": str(store.root),
        "overall_status": overall_status,
        "status": overall_status.upper(),
        "average_score_pct": average_score_pct,
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


def _parse_ts(raw: object) -> datetime:
    if not raw:
        return datetime.now(tz=UTC)
    value = str(raw).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iter_trade_rows(raw: object) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    if not isinstance(raw, dict):
        return []
    for key in ("closed_trades", "recent_closes", "trades", "rows", "items"):
        value = raw.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _iter_trade_rows_from_path(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
        return rows
    return _iter_trade_rows(json.loads(path.read_text(encoding="utf-8")))


def _row_scopes(row: dict[str, Any]) -> list[dict[str, Any]]:
    scopes = [row]
    for key in ("extra", "payload", "metadata"):
        value = row.get(key)
        if isinstance(value, dict):
            scopes.append(value)
    return scopes


def _first_row_value(row: dict[str, Any], *keys: str) -> object | None:
    for scope in _row_scopes(row):
        for key in keys:
            value = scope.get(key)
            if value is not None and value != "":
                return value
    return None


def _row_bot_ids(row: dict[str, Any]) -> set[str]:
    bot_ids: set[str] = set()
    for scope in _row_scopes(row):
        for key in ("bot", "bot_id", "subsystem", "strategy", "bot_a", "bot_b"):
            value = scope.get(key)
            if value:
                bot_ids.add(str(value))
    for link in row.get("links") or []:
        if isinstance(link, str) and link.startswith("bot:"):
            bot_ids.add(link.split(":", 1)[1])
    return bot_ids


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


def _read_last_jsonl_dict(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size <= 0:
                return None
            tail_bytes = min(size, 64 * 1024)
            fh.seek(-tail_bytes, 2)
            chunk = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        if not line.strip():
            continue
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(line)
            if isinstance(payload, dict):
                return payload
        break
    return None


def _depth_symbol_candidates(symbol: str) -> list[str]:
    normalized = _normalize_symbol(symbol)
    root = _DEPTH_ROOT_ALIASES.get(normalized, normalized)
    candidates = [root.upper()]
    if normalized.upper() not in candidates:
        candidates.append(normalized.upper())
    return candidates


def _latest_csv_bar_timestamp(path: Path) -> datetime | None:
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except Exception:
        return None
    parsed: list[datetime] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = (
            row.get("ts")
            or row.get("timestamp")
            or row.get("datetime")
            or row.get("date")
            or row.get("time")
        )
        if raw:
            with contextlib.suppress(Exception):
                parsed.append(_parse_ts(raw))
    return max(parsed) if parsed else None


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
            file_modified_ts = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            ts = _latest_csv_bar_timestamp(path) or file_modified_ts
            rec = SymbolIntelRecord(
                record_type="bar",
                ts_utc=ts,
                symbol=symbol,
                source="csv_history",
                payload={
                    "dataset_path": dataset_path,
                    "timeframe": timeframe,
                    "bytes": stat.st_size,
                    "bar_ts_utc": ts.isoformat(),
                    "last_modified_utc": file_modified_ts.isoformat(),
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


def backfill_news_from_public_feeds(
    *,
    store: SymbolIntelStore | None = None,
    symbols: list[str] | tuple[str, ...] = PRIORITY_SYMBOLS,
    now: datetime | None = None,
    limit_per_symbol: int = 5,
    max_age_hours: float = 48.0,
) -> int:
    store = store or SymbolIntelStore()
    now = now or datetime.now(tz=UTC)
    existing_keys = _existing_payload_values(store, record_type="news", payload_key="news_key")
    count = 0
    for symbol in symbols:
        query = query_for_symbol(symbol)
        if not query:
            continue
        headlines = fetch_google_news_headlines(
            query,
            limit=limit_per_symbol,
            max_age_hours=max_age_hours,
            now=now,
        )
        for item in headlines:
            news_key = f"{symbol.upper().strip()}|{item.published_at_utc.isoformat()}|{item.url}"
            if news_key in existing_keys:
                continue
            rec = SymbolIntelRecord(
                record_type="news",
                ts_utc=item.published_at_utc,
                symbol=symbol,
                source=item.provider,
                payload={
                    "news_key": news_key,
                    "query": item.query,
                    "headline": item.headline,
                    "publisher": item.publisher,
                    "url": item.url,
                    "snippet": item.snippet,
                    "published_at_utc": item.published_at_utc.isoformat(),
                },
                quality=SymbolIntelQuality(confidence=0.6, is_reconciled=True),
            )
            store.append(rec)
            existing_keys.add(news_key)
            count += 1
    return count


def backfill_book_from_depth_snapshots(
    *,
    depth_root: Path = workspace_roots.MNQ_DATA_ROOT / "depth",
    store: SymbolIntelStore | None = None,
    symbols: list[str] | tuple[str, ...] = PRIORITY_SYMBOLS,
) -> int:
    if not depth_root.exists():
        return 0
    store = store or SymbolIntelStore()
    existing_keys = _existing_payload_values(store, record_type="book", payload_key="snapshot_key")
    count = 0
    for symbol in symbols:
        latest_path: Path | None = None
        for candidate in _depth_symbol_candidates(symbol):
            paths = sorted(
                depth_root.glob(f"{candidate}_*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if paths:
                latest_path = paths[0]
                break
        if latest_path is None:
            continue
        latest = _read_last_jsonl_dict(latest_path)
        if latest is None:
            continue
        bids = latest.get("bids") if isinstance(latest.get("bids"), list) else []
        asks = latest.get("asks") if isinstance(latest.get("asks"), list) else []
        if not bids or not asks:
            continue
        ts = _parse_ts(latest.get("ts") or latest.get("timestamp"))
        snapshot_key = f"{symbol.upper().strip()}|{latest_path.name}|{ts.isoformat()}"
        if snapshot_key in existing_keys:
            continue
        best_bid = bids[0].get("price") if isinstance(bids[0], dict) else None
        best_ask = asks[0].get("price") if isinstance(asks[0], dict) else None
        rec = SymbolIntelRecord(
            record_type="book",
            ts_utc=ts,
            symbol=symbol,
            source="ibkr_depth_capture",
            payload={
                "snapshot_key": snapshot_key,
                "snapshot_path": str(latest_path),
                "bid_levels": len(bids),
                "ask_levels": len(asks),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": latest.get("spread"),
                "mid": latest.get("mid"),
            },
            quality=SymbolIntelQuality(
                confidence=0.75 if min(len(bids), len(asks)) >= 3 else 0.55,
                is_reconciled=True,
            ),
        )
        store.append(rec)
        existing_keys.add(snapshot_key)
        count += 1
    return count


def default_bot_symbol_map() -> dict[str, str]:
    try:
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS

        mapped = {assignment.bot_id: _normalize_symbol(assignment.symbol) for assignment in ASSIGNMENTS}
        return {**LEGACY_BOT_SYMBOL_FALLBACKS, **mapped}
    except Exception:
        return dict(LEGACY_BOT_SYMBOL_FALLBACKS)


def _normalize_symbol(raw: object) -> str:
    symbol = str(raw).upper().strip()
    return FUTURES_ROOT_ALIASES.get(symbol, symbol)


def _symbol_with_siblings(raw: object) -> set[str]:
    symbol = _normalize_symbol(raw)
    if not symbol:
        return set()
    return {symbol, *_SIBLING_SYMBOL_ALIASES.get(symbol, ())}


def _decision_symbols(row: dict[str, Any], bot_symbol_map: dict[str, str]) -> set[str]:
    symbols: set[str] = set()
    for scope in _row_scopes(row):
        for key in ("symbol", "ticker", "contract", "root_symbol", "instrument"):
            if scope.get(key):
                symbols.update(_symbol_with_siblings(scope[key]))
    for bot_id in _row_bot_ids(row):
        mapped = bot_symbol_map.get(bot_id)
        if mapped:
            symbols.update(_symbol_with_siblings(mapped))
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
    target_symbols = {_normalize_symbol(symbol) for symbol in symbols}
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


def backfill_decisions_from_shadow_signals(
    *,
    shadow_signals_path: Path = workspace_roots.ETA_JARVIS_SHADOW_SIGNALS_PATH,
    store: SymbolIntelStore | None = None,
    symbols: list[str] | tuple[str, ...] = PRIORITY_SYMBOLS,
    bot_symbol_map: dict[str, str] | None = None,
) -> int:
    store = store or SymbolIntelStore()
    bot_symbol_map = bot_symbol_map or default_bot_symbol_map()
    target_symbols = {_normalize_symbol(symbol) for symbol in symbols}
    existing_keys = _existing_payload_values(store, record_type="decision", payload_key="decision_key")
    count = 0
    for row in _iter_trade_rows_from_path(shadow_signals_path):
        for symbol in sorted(_decision_symbols(row, bot_symbol_map) & target_symbols):
            decision_key = f"shadow|{row.get('signal_id') or row.get('ts')}|{symbol}"
            if decision_key in existing_keys:
                continue
            rec = SymbolIntelRecord(
                record_type="decision",
                ts_utc=_parse_ts(row.get("ts") or row.get("timestamp")),
                symbol=symbol,
                source="jarvis_shadow_signal",
                payload={
                    "decision_key": decision_key,
                    "bot": row.get("bot") or row.get("bot_id"),
                    "signal_id": row.get("signal_id"),
                    "side": row.get("side"),
                    "qty_intended": row.get("qty_intended"),
                    "lifecycle": row.get("lifecycle"),
                    "route_target": row.get("route_target"),
                    "route_reason": row.get("route_reason"),
                    "prospective_loss_usd": row.get("prospective_loss_usd"),
                },
                quality=SymbolIntelQuality(confidence=0.75, is_reconciled=True),
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
    journal_decisions = backfill_decisions_from_journal(store=store, symbols=symbols)
    shadow_decisions = backfill_decisions_from_shadow_signals(store=store, symbols=symbols)
    counts = {
        "bars": backfill_bars_from_history(store=store, symbols=symbols),
        "events": backfill_events_from_calendar(store=store, symbols=symbols),
        "news": backfill_news_from_public_feeds(store=store, symbols=symbols),
        "book": backfill_book_from_depth_snapshots(store=store, symbols=symbols),
        "decisions": journal_decisions + shadow_decisions,
        "journal_decisions": journal_decisions,
        "shadow_decisions": shadow_decisions,
        "outcomes": backfill_outcomes_from_closed_trade_ledger(store=store, symbols=symbols),
    }
    counts["quality"] = backfill_quality_from_audit(store=store, symbols=symbols)
    return counts


def _row_symbol(row: dict[str, Any], *, bot_symbol_map: dict[str, str] | None = None) -> str | None:
    symbol = _first_row_value(row, "symbol", "contract", "ticker", "root_symbol", "instrument")
    if not symbol and bot_symbol_map:
        for bot_id in _row_bot_ids(row):
            symbol = bot_symbol_map.get(bot_id)
            if symbol:
                break
    return _normalize_symbol(symbol) if symbol else None


def _dedupe_key(row: dict[str, Any]) -> str:
    return "|".join(
        str(
            _first_row_value(row, key, key.replace("_", ""))
            or ""
        )
        for key in ("bot_id", "bot", "symbol", "signal_id", "close_ts", "exit_time_utc", "ts", "fill_price")
    )


def backfill_outcomes_from_closed_trade_ledger(
    *,
    source_path: Path = workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH,
    source_paths: list[Path] | tuple[Path, ...] | None = None,
    store: SymbolIntelStore | None = None,
    symbols: list[str] | tuple[str, ...] | None = None,
    bot_symbol_map: dict[str, str] | None = None,
) -> int:
    store = store or SymbolIntelStore()
    bot_symbol_map = bot_symbol_map or default_bot_symbol_map()
    target_symbols = {_normalize_symbol(symbol) for symbol in symbols} if symbols else None
    if source_paths is not None:
        paths = list(source_paths)
    elif source_path == workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH:
        paths = [source_path, workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH]
    else:
        paths = [source_path]
    existing = {
        str(rec.payload.get("dedupe_key"))
        for rec in store.iter_records(record_type="outcome")
        if rec.payload.get("dedupe_key")
    }
    count = 0
    for path in paths:
        for row in _iter_trade_rows_from_path(path):
            symbol = _row_symbol(row, bot_symbol_map=bot_symbol_map)
            if not symbol:
                continue
            base_symbol = symbol
            row_symbols = _symbol_with_siblings(base_symbol) if target_symbols is not None else {base_symbol}
            for symbol in sorted(row_symbols):
                if target_symbols is not None and symbol not in target_symbols:
                    continue
                dedupe_key = f"{path.name}|{_dedupe_key(row)}|{symbol}"
                if dedupe_key in existing:
                    continue
                ts = _parse_ts(_first_row_value(row, "exit_time_utc", "close_ts", "ts", "time"))
                payload = {
                    "dedupe_key": dedupe_key,
                    "source_path": str(path),
                    "bot": _first_row_value(row, "bot", "bot_id", "subsystem"),
                    "side": _first_row_value(row, "side", "direction"),
                    "qty": _first_row_value(row, "qty", "quantity"),
                    "entry_price": _first_row_value(row, "entry_price", "entry"),
                    "exit_price": _first_row_value(row, "exit_price", "fill_price"),
                    "fill_price": _first_row_value(row, "fill_price"),
                    "realized_pnl": _first_row_value(row, "realized_pnl", "pnl"),
                    "r_multiple": _first_row_value(row, "r_multiple", "realized_r"),
                    "signal_id": _first_row_value(row, "signal_id"),
                    "data_source": _first_row_value(row, "data_source"),
                    "source_symbol": base_symbol,
                    "symbol_alias_reason": "sibling_contract_equivalence" if symbol != base_symbol else None,
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
    parser.add_argument(
        "--bootstrap-existing",
        action="store_true",
        help="backfill existing bars, events, news, book, decisions, outcomes",
    )
    parser.add_argument("--symbol", action="append", dest="symbols", help="symbol to audit, repeatable")
    args = parser.parse_args(argv)

    store = SymbolIntelStore()
    backfilled = 0
    bootstrap_counts: dict[str, int] | None = None
    if args.bootstrap_existing:
        bootstrap_counts = bootstrap_existing_truth_surfaces(
            store=store,
            symbols=args.symbols or list(PRIORITY_SYMBOLS),
        )
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

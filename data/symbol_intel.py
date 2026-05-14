"""Canonical symbol-intelligence records and data-lake storage.

Broker fills, bars, depth, news, macro events, Jarvis decisions, and trade
outcomes all share one append-only envelope so downstream audits can join them
by symbol/time without caring which provider produced the source payload.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.scripts import workspace_roots

SCHEMA_VERSION = "eta.symbol_intel.v1"
VALID_RECORD_TYPES = frozenset(
    {
        "bar",
        "tick",
        "book",
        "news",
        "macro_event",
        "decision",
        "outcome",
        "quality",
    }
)

_PARTITIONS = {
    "bar": "bars",
    "tick": "ticks",
    "book": "book",
    "news": "news",
    "macro_event": "events",
    "decision": "decisions",
    "outcome": "outcomes",
    "quality": "quality",
}


@dataclass(frozen=True)
class SymbolIntelQuality:
    confidence: float = 0.0
    latency_ms: int | None = None
    is_stale: bool = False
    is_reconciled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SymbolIntelQuality":
        data = raw or {}
        latency_raw = data.get("latency_ms")
        return cls(
            confidence=float(data.get("confidence", 0.0) or 0.0),
            latency_ms=int(latency_raw) if latency_raw is not None else None,
            is_stale=bool(data.get("is_stale", False)),
            is_reconciled=bool(data.get("is_reconciled", False)),
        )


@dataclass(frozen=True)
class SymbolIntelRecord:
    record_type: str
    ts_utc: datetime
    symbol: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    quality: SymbolIntelQuality = field(default_factory=SymbolIntelQuality)
    schema: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.record_type not in VALID_RECORD_TYPES:
            raise ValueError(f"unsupported symbol-intel record_type: {self.record_type}")
        if self.ts_utc.tzinfo is None:
            raise ValueError("ts_utc must be timezone-aware")
        if not self.symbol.strip():
            raise ValueError("symbol is required")
        if not self.source.strip():
            raise ValueError("source is required")
        object.__setattr__(self, "symbol", self.symbol.upper().strip())
        object.__setattr__(self, "source", self.source.lower().strip())
        object.__setattr__(self, "ts_utc", self.ts_utc.astimezone(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "record_type": self.record_type,
            "ts_utc": self.ts_utc.isoformat(),
            "symbol": self.symbol,
            "source": self.source,
            "payload": self.payload,
            "quality": self.quality.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SymbolIntelRecord":
        if raw.get("schema") != SCHEMA_VERSION:
            raise ValueError(f"unsupported symbol-intel schema: {raw.get('schema')}")
        ts_raw = str(raw["ts_utc"]).replace("Z", "+00:00")
        return cls(
            record_type=str(raw["record_type"]),
            ts_utc=datetime.fromisoformat(ts_raw),
            symbol=str(raw["symbol"]),
            source=str(raw["source"]),
            payload=dict(raw.get("payload") or {}),
            quality=SymbolIntelQuality.from_dict(raw.get("quality")),
        )


def _safe_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value.strip())
    return cleaned or "unknown"


class SymbolIntelStore:
    """Append-only JSONL store rooted under the canonical ETA workspace."""

    def __init__(self, root: Path | None = None, *, canonical_root: Path | None = None) -> None:
        resolved_root = (root or workspace_roots.ETA_DATA_LAKE_ROOT).resolve()
        resolved_canonical = (
            canonical_root or (resolved_root if root is not None else workspace_roots.WORKSPACE_ROOT)
        ).resolve()
        self.root = resolved_root
        self.canonical_root = resolved_canonical
        try:
            self.root.relative_to(self.canonical_root)
        except ValueError as exc:
            raise ValueError(f"symbol-intel root outside canonical root: {self.root}") from exc

    def partition_path(self, rec: SymbolIntelRecord) -> Path:
        day = rec.ts_utc.date().isoformat()
        bucket = _PARTITIONS[rec.record_type]
        source = _safe_part(rec.source)
        symbol = _safe_part(rec.symbol)
        if rec.record_type == "quality":
            return self.root / bucket / f"{day}.jsonl"
        if rec.record_type in {"news", "macro_event"}:
            return self.root / bucket / source / f"{day}.jsonl"
        return self.root / bucket / source / symbol / f"{day}.jsonl"

    def append(self, rec: SymbolIntelRecord) -> Path:
        path = self.partition_path(rec)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec.to_dict(), separators=(",", ":"), sort_keys=True) + "\n")
        return path

    def iter_records(self, *, record_type: str, symbol: str | None = None) -> Iterable[SymbolIntelRecord]:
        bucket = _PARTITIONS[record_type]
        base = self.root / bucket
        if not base.exists():
            return iter(())
        wanted_symbol = symbol.upper().strip() if symbol else None

        def _iter() -> Iterable[SymbolIntelRecord]:
            for path in sorted(base.rglob("*.jsonl")):
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        rec = SymbolIntelRecord.from_dict(json.loads(line))
                        if wanted_symbol is None or rec.symbol == wanted_symbol:
                            yield rec

        return _iter()

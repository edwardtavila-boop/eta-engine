# Symbol Intelligence Data Flywheel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the full-project ETA data spine: canonical symbol intelligence records, data lake storage, read-only data coverage audits, quality snapshots, decision/outcome backfill, and a phased path for broker, futures, crypto, macro, news, filing, and order-flow feeds.

**Architecture:** Start with one normalized append-only record/store under `C:\EvolutionaryTradingAlgo\var\eta_engine\data_lake`, then layer audits and quality scoring on top before activating paid/live collectors. Broker execution truth remains IBKR/Tastytrade; Databento stays dormant until an operator-approved code-and-docs activation batch; news/macro are risk and context overlays until post-trade attribution proves value.

**Tech Stack:** Python stdlib dataclasses/json/pathlib, existing `eta_engine.scripts.workspace_roots`, existing `eta_engine.data.library/audit/requirements`, pytest, existing status-page/dashboard API surfaces.

---

## Scope Check

The full project scope spans several independent subsystems:

- Data spine and canonical storage.
- Existing-data inventory and quality scoring.
- Decision/outcome backfill.
- Futures market-data collectors.
- Crypto/equity/news/macro/filing collectors.
- Dashboard and Jarvis integration.
- Strategy attribution and promotion gates.

This plan deliberately implements the **data spine first**. Paid provider collectors and strategy-scoring changes are future gated tasks that require quality snapshots and operator approval. This keeps the first batch safe, testable, and useful without mutating live routing.

## File Structure

- Create `eta_engine/data/symbol_intel.py`: normalized record model, safe canonical data-lake pathing, JSONL append/read store.
- Create `eta_engine/scripts/symbol_intelligence_audit.py`: read-only audit that reports symbol-level coverage and data-intelligence readiness.
- Create `eta_engine/tests/test_symbol_intel.py`: unit tests for record serialization, partition paths, path containment, and append/read behavior.
- Create `eta_engine/tests/test_symbol_intelligence_audit.py`: unit tests for coverage scoring and JSON report behavior.
- Modify `eta_engine/scripts/workspace_roots.py`: add `ETA_DATA_LAKE_ROOT` and `ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH`.
- Later modify `eta_engine/deploy/scripts/dashboard_api.py`: expose the read-only snapshot.
- Later modify `eta_engine/deploy/status_page/index.html`: render a compact "Data Intelligence" card.

## Data Universes

The final ETA data scope is divided into staged universes:

- `U0_INTERNAL`: broker fills, positions, brackets, Jarvis decisions, close ledger, calibration labels.
- `U1_FUTURES_CORE`: MNQ, NQ, ES, MES, YM, MYM, RTY, M2K, VIX, DXY.
- `U2_FUTURES_EXPANDED`: CL, MCL, GC, MGC, NG, ZN, ZB, 6E, M6E, MBT, MET.
- `U3_CRYPTO`: BTC, ETH, SOL spot/perp context, funding, basis, open interest, on-chain proxies.
- `U4_EQUITY_ETF`: SPY, QQQ, DIA, IWM, sector ETFs, IBIT/crypto ETFs, major beta references.
- `U5_MACRO_EVENTS`: CPI, FOMC, NFP, Fed speakers, Treasury auctions, EIA, OPEC, expiry/witching.
- `U6_NEWS_FILINGS`: market headlines, ticker/entity sentiment, SEC filings, earnings, 13F, ETF flows.
- `U7_ORDER_FLOW`: tick-by-tick trades, top-of-book, spread, depth, MBP/MBO, bid/ask volume split.

## Task 1: Canonical Data-Lake Roots

**Files:**
- Modify: `C:\EvolutionaryTradingAlgo\eta_engine\scripts\workspace_roots.py`
- Test: `C:\EvolutionaryTradingAlgo\eta_engine\tests\test_symbol_intel.py`

- [ ] **Step 1: Write the failing root-path test**

Add this test file with the first containment assertion:

```python
from pathlib import Path

from eta_engine.scripts import workspace_roots


def test_symbol_intelligence_paths_stay_under_workspace():
    root = workspace_roots.WORKSPACE_ROOT.resolve()
    data_lake = workspace_roots.ETA_DATA_LAKE_ROOT.resolve()
    snapshot = workspace_roots.ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH.resolve()

    assert str(data_lake).startswith(str(root))
    assert str(snapshot).startswith(str(root))
    assert data_lake == root / "var" / "eta_engine" / "data_lake"
    assert snapshot == root / "var" / "eta_engine" / "state" / "symbol_intelligence_latest.json"
```

- [ ] **Step 2: Run the failing test**

Run:

```powershell
python -m pytest tests/test_symbol_intel.py::test_symbol_intelligence_paths_stay_under_workspace -q
```

Expected: FAIL with `AttributeError` for `ETA_DATA_LAKE_ROOT`.

- [ ] **Step 3: Add the canonical paths**

In `eta_engine/scripts/workspace_roots.py`, add these constants near the existing `ETA_RUNTIME_STATE_DIR` constants:

```python
ETA_DATA_LAKE_ROOT = ROOT_VAR_DIR / "eta_engine" / "data_lake"
ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "symbol_intelligence_latest.json"
```

- [ ] **Step 4: Run the root-path test again**

Run:

```powershell
python -m pytest tests/test_symbol_intel.py::test_symbol_intelligence_paths_stay_under_workspace -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

Run:

```powershell
git add scripts/workspace_roots.py tests/test_symbol_intel.py
git commit -m "feat(data): add symbol intelligence data lake roots"
```

## Task 2: Symbol Intelligence Record Model

**Files:**
- Create: `C:\EvolutionaryTradingAlgo\eta_engine\data\symbol_intel.py`
- Modify: `C:\EvolutionaryTradingAlgo\eta_engine\tests\test_symbol_intel.py`

- [ ] **Step 1: Add failing serialization tests**

Append these tests:

```python
from datetime import UTC, datetime

from eta_engine.data.symbol_intel import SymbolIntelQuality, SymbolIntelRecord


def test_symbol_intel_record_serializes_with_stable_schema():
    rec = SymbolIntelRecord(
        record_type="bar",
        ts_utc=datetime(2026, 5, 14, 14, 30, tzinfo=UTC),
        symbol="mnq1",
        source="ibkr",
        payload={"close": 29250.25},
        quality=SymbolIntelQuality(confidence=0.95, is_reconciled=True),
    )

    data = rec.to_dict()

    assert data["schema"] == "eta.symbol_intel.v1"
    assert data["record_type"] == "bar"
    assert data["ts_utc"] == "2026-05-14T14:30:00+00:00"
    assert data["symbol"] == "MNQ1"
    assert data["source"] == "ibkr"
    assert data["payload"] == {"close": 29250.25}
    assert data["quality"]["confidence"] == 0.95
    assert data["quality"]["is_reconciled"] is True


def test_symbol_intel_record_round_trips_from_dict():
    raw = {
        "schema": "eta.symbol_intel.v1",
        "record_type": "news",
        "ts_utc": "2026-05-14T15:00:00+00:00",
        "symbol": "NQ",
        "source": "operator",
        "payload": {"headline": "FOMC risk window"},
        "quality": {"confidence": 0.7, "latency_ms": 25, "is_stale": False, "is_reconciled": False},
    }

    rec = SymbolIntelRecord.from_dict(raw)

    assert rec.record_type == "news"
    assert rec.ts_utc == datetime(2026, 5, 14, 15, 0, tzinfo=UTC)
    assert rec.symbol == "NQ"
    assert rec.to_dict() == raw
```

- [ ] **Step 2: Run the failing tests**

Run:

```powershell
python -m pytest tests/test_symbol_intel.py -q
```

Expected: FAIL with `ModuleNotFoundError` or missing class errors.

- [ ] **Step 3: Implement the record model**

Create `eta_engine/data/symbol_intel.py`:

```python
"""Canonical symbol-intelligence records and data-lake storage.

This module is intentionally provider-neutral. Broker fills, bars, depth,
news, macro events, Jarvis decisions, and trade outcomes all share one
append-only envelope so downstream audits can join them by symbol/time.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

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
        return cls(
            confidence=float(data.get("confidence", 0.0) or 0.0),
            latency_ms=data.get("latency_ms"),
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
```

- [ ] **Step 4: Run serialization tests**

Run:

```powershell
python -m pytest tests/test_symbol_intel.py -q
```

Expected: PASS for the current tests.

- [ ] **Step 5: Commit Task 2**

Run:

```powershell
git add data/symbol_intel.py tests/test_symbol_intel.py
git commit -m "feat(data): add symbol intelligence record model"
```

## Task 3: Append-Only Symbol Intelligence Store

**Files:**
- Modify: `C:\EvolutionaryTradingAlgo\eta_engine\data\symbol_intel.py`
- Modify: `C:\EvolutionaryTradingAlgo\eta_engine\tests\test_symbol_intel.py`

- [ ] **Step 1: Add failing store tests**

Append:

```python
import json
from datetime import UTC, datetime

import pytest

from eta_engine.data.symbol_intel import SymbolIntelRecord, SymbolIntelStore


def test_symbol_intel_store_partitions_and_reads_records(tmp_path):
    store = SymbolIntelStore(root=tmp_path)
    rec = SymbolIntelRecord(
        record_type="outcome",
        ts_utc=datetime(2026, 5, 14, 20, 5, tzinfo=UTC),
        symbol="MNQ1",
        source="jarvis",
        payload={"realized_pnl": 42.5},
    )

    path = store.append(rec)
    rows = list(store.iter_records(record_type="outcome", symbol="MNQ1"))

    assert path == tmp_path / "outcomes" / "jarvis" / "MNQ1" / "2026-05-14.jsonl"
    assert rows == [rec]
    assert json.loads(path.read_text(encoding="utf-8").strip()) == rec.to_dict()


def test_symbol_intel_store_rejects_paths_outside_workspace(tmp_path):
    escape = tmp_path / ".." / "outside"
    with pytest.raises(ValueError, match="outside canonical root"):
        SymbolIntelStore(root=escape, canonical_root=tmp_path)
```

- [ ] **Step 2: Run the failing store tests**

Run:

```powershell
python -m pytest tests/test_symbol_intel.py::test_symbol_intel_store_partitions_and_reads_records tests/test_symbol_intel.py::test_symbol_intel_store_rejects_paths_outside_workspace -q
```

Expected: FAIL because `SymbolIntelStore` does not exist.

- [ ] **Step 3: Implement the store**

Append to `eta_engine/data/symbol_intel.py`:

```python
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


class SymbolIntelStore:
    """Append-only JSONL store rooted under the canonical ETA workspace."""

    def __init__(self, root: Path | None = None, *, canonical_root: Path | None = None) -> None:
        self.root = (root or workspace_roots.ETA_DATA_LAKE_ROOT).resolve()
        self.canonical_root = (canonical_root or workspace_roots.WORKSPACE_ROOT).resolve()
        if not str(self.root).startswith(str(self.canonical_root)):
            raise ValueError(f"symbol-intel root outside canonical root: {self.root}")

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
```

- [ ] **Step 4: Run all symbol-intel tests**

Run:

```powershell
python -m pytest tests/test_symbol_intel.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

Run:

```powershell
git add data/symbol_intel.py tests/test_symbol_intel.py
git commit -m "feat(data): add symbol intelligence store"
```

## Task 4: Symbol Intelligence Audit Model

**Files:**
- Create: `C:\EvolutionaryTradingAlgo\eta_engine\scripts\symbol_intelligence_audit.py`
- Create: `C:\EvolutionaryTradingAlgo\eta_engine\tests\test_symbol_intelligence_audit.py`

- [ ] **Step 1: Add failing audit score test**

Create `tests/test_symbol_intelligence_audit.py`:

```python
from eta_engine.scripts.symbol_intelligence_audit import (
    REQUIRED_COMPONENTS,
    SymbolIntelCoverage,
    score_symbol,
)


def test_score_symbol_counts_required_components():
    coverage = SymbolIntelCoverage(
        symbol="MNQ",
        components={
            "bars": True,
            "events": True,
            "decisions": True,
            "outcomes": False,
            "quality": False,
            "news": False,
            "book": False,
        },
        notes=[],
    )

    score = score_symbol(coverage)

    assert REQUIRED_COMPONENTS == ("bars", "events", "decisions", "outcomes", "quality")
    assert score["symbol"] == "MNQ"
    assert score["required_ready"] == 3
    assert score["required_total"] == 5
    assert score["score_pct"] == 60
    assert score["status"] == "AMBER"
```

- [ ] **Step 2: Run the failing audit test**

Run:

```powershell
python -m pytest tests/test_symbol_intelligence_audit.py -q
```

Expected: FAIL because `symbol_intelligence_audit.py` does not exist.

- [ ] **Step 3: Implement the audit model**

Create `eta_engine/scripts/symbol_intelligence_audit.py`:

```python
"""Read-only data intelligence audit for active ETA symbols."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.data.library import DataLibrary, default_library
from eta_engine.data.symbol_intel import SymbolIntelStore
from eta_engine.scripts import workspace_roots

PRIORITY_SYMBOLS = ("MNQ", "NQ", "ES", "MES", "YM", "MYM")
REQUIRED_COMPONENTS = ("bars", "events", "decisions", "outcomes", "quality")
OPTIONAL_COMPONENTS = ("news", "book")


@dataclass
class SymbolIntelCoverage:
    symbol: str
    components: dict[str, bool]
    notes: list[str] = field(default_factory=list)


def _has_bars(lib: DataLibrary, symbol: str) -> bool:
    candidates = (symbol, f"{symbol}1")
    frames = ("1m", "5m", "1h", "D")
    return any(lib.get(symbol=s, timeframe=tf) is not None for s in candidates for tf in frames)


def _store_has(store: SymbolIntelStore, record_type: str, symbol: str) -> bool:
    return any(True for _ in store.iter_records(record_type=record_type, symbol=symbol))


def inspect_symbol(symbol: str, *, library: DataLibrary | None = None, store: SymbolIntelStore | None = None) -> SymbolIntelCoverage:
    lib = library or default_library()
    symbol_store = store or SymbolIntelStore()
    sym = symbol.upper().strip()
    components = {
        "bars": _has_bars(lib, sym),
        "events": any(True for _ in symbol_store.iter_records(record_type="macro_event", symbol=sym)),
        "decisions": _store_has(symbol_store, "decision", sym),
        "outcomes": _store_has(symbol_store, "outcome", sym),
        "quality": any(True for _ in symbol_store.iter_records(record_type="quality")),
        "news": any(True for _ in symbol_store.iter_records(record_type="news", symbol=sym)),
        "book": _store_has(symbol_store, "book", sym),
    }
    notes: list[str] = []
    if not components["bars"]:
        notes.append("missing canonical bar coverage")
    if not components["outcomes"]:
        notes.append("no closed-trade outcome records backfilled yet")
    if not components["quality"]:
        notes.append("no daily quality snapshot emitted yet")
    return SymbolIntelCoverage(symbol=sym, components=components, notes=notes)


def score_symbol(coverage: SymbolIntelCoverage) -> dict[str, Any]:
    required_ready = sum(1 for key in REQUIRED_COMPONENTS if coverage.components.get(key))
    required_total = len(REQUIRED_COMPONENTS)
    score_pct = round(100 * required_ready / required_total)
    if score_pct >= 90:
        status = "GREEN"
    elif score_pct >= 50:
        status = "AMBER"
    else:
        status = "RED"
    return {
        "symbol": coverage.symbol,
        "status": status,
        "score_pct": score_pct,
        "required_ready": required_ready,
        "required_total": required_total,
        "components": dict(coverage.components),
        "notes": list(coverage.notes),
    }


def run_audit(symbols: tuple[str, ...] = PRIORITY_SYMBOLS) -> dict[str, Any]:
    store = SymbolIntelStore()
    lib = default_library()
    rows = [score_symbol(inspect_symbol(sym, library=lib, store=store)) for sym in symbols]
    avg_score = round(sum(row["score_pct"] for row in rows) / len(rows)) if rows else 0
    return {
        "schema": "eta.symbol_intelligence.audit.v1",
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "status": "GREEN" if avg_score >= 90 else "AMBER" if avg_score >= 50 else "RED",
        "average_score_pct": avg_score,
        "symbols": rows,
        "universes": {
            "internal": "broker fills, positions, decisions, close ledger",
            "futures_core": "MNQ/NQ/ES/MES/YM/MYM first",
            "news_macro": "risk filter until attribution proves signal value",
            "order_flow": "capture before strategy activation",
        },
    }


def write_snapshot(payload: dict[str, Any], path: Path | None = None) -> Path:
    target = path or workspace_roots.ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--symbols", default=",".join(PRIORITY_SYMBOLS))
    args = parser.parse_args(argv)

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    payload = run_audit(symbols=symbols)
    if args.write:
        write_snapshot(payload)
    print(json.dumps(payload, indent=2, sort_keys=True) if args.json else _format_text(payload))
    return 0 if payload["status"] != "RED" else 1


def _format_text(payload: dict[str, Any]) -> str:
    lines = [
        "Symbol Intelligence Audit",
        f"Status: {payload['status']} | Average: {payload['average_score_pct']}%",
        "",
    ]
    for row in payload["symbols"]:
        lines.append(f"{row['symbol']}: {row['status']} {row['score_pct']}% - {', '.join(row['notes']) or 'ready'}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the audit test**

Run:

```powershell
python -m pytest tests/test_symbol_intelligence_audit.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

Run:

```powershell
git add scripts/symbol_intelligence_audit.py tests/test_symbol_intelligence_audit.py
git commit -m "feat(data): add symbol intelligence audit"
```

## Task 5: Backfill Decisions And Outcomes From Existing State

**Files:**
- Modify: `C:\EvolutionaryTradingAlgo\eta_engine\scripts\symbol_intelligence_audit.py`
- Modify: `C:\EvolutionaryTradingAlgo\eta_engine\tests\test_symbol_intelligence_audit.py`

- [ ] **Step 1: Add failing backfill extraction test**

Append:

```python
import json
from datetime import UTC, datetime

from eta_engine.data.symbol_intel import SymbolIntelStore
from eta_engine.scripts.symbol_intelligence_audit import backfill_outcomes_from_closed_trade_ledger


def test_backfill_outcomes_from_closed_trade_ledger(tmp_path):
    ledger = tmp_path / "closed_trade_ledger_latest.json"
    ledger.write_text(
        json.dumps(
            {
                "close_history": [
                    {
                        "symbol": "MNQ1",
                        "bot_id": "mnq_futures_sage",
                        "strategy": "approve_full",
                        "side": "SELL",
                        "qty": 1,
                        "entry_price": 29390.0,
                        "exit_price": 29401.0,
                        "realized_pnl": 22.0,
                        "r_multiple": 0.44,
                        "time": "2026-05-14T15:10:00+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = SymbolIntelStore(root=tmp_path / "lake", canonical_root=tmp_path)

    count = backfill_outcomes_from_closed_trade_ledger(ledger, store=store)
    rows = list(store.iter_records(record_type="outcome", symbol="MNQ1"))

    assert count == 1
    assert rows[0].payload["bot_id"] == "mnq_futures_sage"
    assert rows[0].payload["entry_price"] == 29390.0
    assert rows[0].payload["exit_price"] == 29401.0
```

- [ ] **Step 2: Run the failing backfill test**

Run:

```powershell
python -m pytest tests/test_symbol_intelligence_audit.py::test_backfill_outcomes_from_closed_trade_ledger -q
```

Expected: FAIL because the backfill function is missing.

- [ ] **Step 3: Implement outcome backfill**

Add imports and function in `scripts/symbol_intelligence_audit.py`:

```python
from eta_engine.data.symbol_intel import SymbolIntelRecord
```

```python
def _parse_record_time(raw: object) -> datetime:
    text = str(raw or "").strip()
    if not text:
        return datetime.now(tz=UTC)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(tz=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iter_close_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("close_history", "closes", "recent_closes", "rows"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def backfill_outcomes_from_closed_trade_ledger(path: Path | None = None, *, store: SymbolIntelStore | None = None) -> int:
    source = path or workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH
    if not source.exists():
        return 0
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    symbol_store = store or SymbolIntelStore()
    count = 0
    for row in _iter_close_rows(payload):
        symbol = str(row.get("symbol") or row.get("contract") or "").strip()
        if not symbol:
            continue
        rec = SymbolIntelRecord(
            record_type="outcome",
            ts_utc=_parse_record_time(row.get("time") or row.get("ts_utc") or row.get("exit_time")),
            symbol=symbol,
            source="jarvis",
            payload=row,
        )
        symbol_store.append(rec)
        count += 1
    return count
```

- [ ] **Step 4: Run audit tests**

Run:

```powershell
python -m pytest tests/test_symbol_intelligence_audit.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 5**

Run:

```powershell
git add scripts/symbol_intelligence_audit.py tests/test_symbol_intelligence_audit.py
git commit -m "feat(data): backfill symbol outcomes from close ledger"
```

## Task 6: Quality Snapshot Emission

**Files:**
- Modify: `C:\EvolutionaryTradingAlgo\eta_engine\scripts\symbol_intelligence_audit.py`
- Modify: `C:\EvolutionaryTradingAlgo\eta_engine\tests\test_symbol_intelligence_audit.py`

- [ ] **Step 1: Add failing snapshot-write test**

Append:

```python
from eta_engine.scripts.symbol_intelligence_audit import write_snapshot


def test_write_snapshot_creates_parent_and_payload(tmp_path):
    target = tmp_path / "state" / "symbol_intelligence_latest.json"
    payload = {"schema": "eta.symbol_intelligence.audit.v1", "status": "AMBER"}

    out = write_snapshot(payload, path=target)

    assert out == target
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == payload
```

- [ ] **Step 2: Run snapshot test**

Run:

```powershell
python -m pytest tests/test_symbol_intelligence_audit.py::test_write_snapshot_creates_parent_and_payload -q
```

Expected: PASS if Task 4 implementation already included `write_snapshot`. If it fails, add the exact `write_snapshot` implementation shown in Task 4.

- [ ] **Step 3: Add CLI smoke with write**

Run:

```powershell
python -m eta_engine.scripts.symbol_intelligence_audit --json --write
```

Expected: JSON printed and `C:\EvolutionaryTradingAlgo\var\eta_engine\state\symbol_intelligence_latest.json` created.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
python -m pytest tests/test_symbol_intel.py tests/test_symbol_intelligence_audit.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 6**

Run:

```powershell
git add scripts/symbol_intelligence_audit.py tests/test_symbol_intelligence_audit.py
git commit -m "feat(data): emit symbol intelligence snapshot"
```

## Task 7: Dashboard API Read-Only Exposure

**Files:**
- Modify: `C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\dashboard_api.py`
- Modify: `C:\EvolutionaryTradingAlgo\eta_engine\tests\test_dashboard_api.py`

- [ ] **Step 1: Add failing API payload test**

In `tests/test_dashboard_api.py`, add a focused test near other snapshot-loading tests:

```python
def test_master_status_includes_symbol_intelligence_snapshot(tmp_path, monkeypatch):
    import json

    from eta_engine.scripts import workspace_roots
    from eta_engine.deploy.scripts import dashboard_api

    snap = tmp_path / "symbol_intelligence_latest.json"
    snap.write_text(
        json.dumps(
            {
                "schema": "eta.symbol_intelligence.audit.v1",
                "status": "AMBER",
                "average_score_pct": 60,
                "symbols": [{"symbol": "MNQ", "status": "AMBER", "score_pct": 60}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(workspace_roots, "ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH", snap)

    payload = dashboard_api._load_symbol_intelligence_snapshot()

    assert payload["status"] == "AMBER"
    assert payload["average_score_pct"] == 60
    assert payload["symbols"][0]["symbol"] == "MNQ"
```

- [ ] **Step 2: Run the failing API test**

Run:

```powershell
python -m pytest tests/test_dashboard_api.py::test_master_status_includes_symbol_intelligence_snapshot -q
```

Expected: FAIL because `_load_symbol_intelligence_snapshot` is missing.

- [ ] **Step 3: Implement the loader**

In `deploy/scripts/dashboard_api.py`, add a helper near other snapshot loaders:

```python
def _load_symbol_intelligence_snapshot() -> dict[str, Any]:
    path = workspace_roots.ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH
    if not path.exists():
        return {
            "schema": "eta.symbol_intelligence.audit.v1",
            "status": "UNKNOWN",
            "average_score_pct": 0,
            "symbols": [],
            "notes": ["symbol intelligence snapshot has not been generated"],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "schema": "eta.symbol_intelligence.audit.v1",
            "status": "ERROR",
            "average_score_pct": 0,
            "symbols": [],
            "notes": ["symbol intelligence snapshot is unreadable"],
        }
    return payload if isinstance(payload, dict) else {
        "schema": "eta.symbol_intelligence.audit.v1",
        "status": "ERROR",
        "average_score_pct": 0,
        "symbols": [],
        "notes": ["symbol intelligence snapshot has invalid shape"],
    }
```

Add it to the master/status payload under a read-only key:

```python
"symbol_intelligence": _load_symbol_intelligence_snapshot(),
```

- [ ] **Step 4: Run dashboard tests**

Run:

```powershell
python -m pytest tests/test_dashboard_api.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 7**

Run:

```powershell
git add deploy/scripts/dashboard_api.py tests/test_dashboard_api.py
git commit -m "feat(ops): expose symbol intelligence status"
```

## Task 8: Dashboard Card

**Files:**
- Modify: `C:\EvolutionaryTradingAlgo\eta_engine\deploy\status_page\index.html`
- Test: existing browser/manual dashboard verification plus dashboard API tests.

- [ ] **Step 1: Find the current status-card render pattern**

Run:

```powershell
Select-String -Path deploy/status_page/index.html -Pattern "Paper Live|Risk Level|Open Book|Exit Cover|Tape|render" -Context 2,3
```

Expected: identify the existing card/grid render function used for read-only status tiles.

- [ ] **Step 2: Add a read-only card**

Add a compact card using the same DOM style as neighboring cards:

```javascript
const symbolIntel = payload.symbol_intelligence || {};
const symbolIntelStatus = symbolIntel.status || "UNKNOWN";
const symbolIntelScore = Number(symbolIntel.average_score_pct || 0);
```

Render copy:

```text
Data Intelligence
AMBER
60% coverage across priority futures symbols
```

Use symbol rows for detail:

```javascript
const symbolRows = Array.isArray(symbolIntel.symbols) ? symbolIntel.symbols.slice(0, 6) : [];
```

Do not add buttons, live controls, or trade-affecting UI.

- [ ] **Step 3: Run dashboard tests**

Run:

```powershell
python -m pytest tests/test_dashboard_api.py -q
```

Expected: PASS.

- [ ] **Step 4: Browser verify**

Open the local/public dashboard and confirm:

- The card appears.
- It does not block existing status rendering.
- It reads `UNKNOWN` gracefully when no snapshot exists.
- It reads `AMBER/GREEN/RED` when snapshot exists.

- [ ] **Step 5: Commit Task 8**

Run:

```powershell
git add deploy/status_page/index.html
git commit -m "feat(ops): show data intelligence card"
```

## Task 9: Futures Core Collector Activation Plan

**Files:**
- Create: `C:\EvolutionaryTradingAlgo\eta_engine\docs\DATA_PROVIDER_ACTIVATION_RUNBOOK.md`

- [ ] **Step 1: Create the runbook**

Write the runbook with these exact sections:

```markdown
# Data Provider Activation Runbook

## Rule

No paid/live provider is activated unless the operator approves a paired code
and docs change. Broker routing remains unchanged unless the task explicitly
targets broker routing.

## Phase A - Internal Truth First

- Run `python -m eta_engine.scripts.symbol_intelligence_audit --json --write`.
- Verify `var/eta_engine/state/symbol_intelligence_latest.json`.
- Verify close/outcome backfill before adding new feeds.

## Phase B - IBKR Real-Time Capture

- Confirm CME Group Level 1 is active for MNQ/NQ/ES/MES.
- Confirm paper account shares live market data.
- Capture L1 top-of-book, ticks, spread, and quote rate.
- Do not enable strategy decisions from L2/order-flow features yet.

## Phase C - Databento Historical Research

- Operator must approve Databento activation.
- Start with usage-based or Standard.
- Pull OHLCV/trades/definitions/statistics for MNQ/NQ/ES/MES/YM/MYM.
- Store normalized records in `var/eta_engine/data_lake`.
- Compare against IBKR bars before research use.

## Phase D - Macro And News

- Use FRED/BLS/EIA/Treasury for public macro series and event context.
- Use Benzinga-style paid news only after event storage and dedupe exist.
- Sentiment remains advisory until post-trade attribution shows edge.

## Phase E - Order Flow

- Capture before trading.
- Use order-flow features first as filters.
- Promote to strategy input only after closed-trade attribution improves.
```

- [ ] **Step 2: Commit Task 9**

Run:

```powershell
git add docs/DATA_PROVIDER_ACTIVATION_RUNBOOK.md
git commit -m "docs(data): add provider activation runbook"
```

## Task 10: Full Verification Batch

**Files:**
- All touched files from Tasks 1-9.

- [ ] **Step 1: Run focused unit tests**

Run:

```powershell
python -m pytest tests/test_symbol_intel.py tests/test_symbol_intelligence_audit.py -q
```

Expected: all pass.

- [ ] **Step 2: Run dashboard API tests if dashboard was touched**

Run:

```powershell
python -m pytest tests/test_dashboard_api.py -q
```

Expected: all pass.

- [ ] **Step 3: Run the CLI smoke**

Run:

```powershell
python -m eta_engine.scripts.symbol_intelligence_audit --json --write
```

Expected:

- Exit code `0` if average score is AMBER/GREEN, or `1` if RED.
- JSON is printed.
- `C:\EvolutionaryTradingAlgo\var\eta_engine\state\symbol_intelligence_latest.json` exists.

- [ ] **Step 4: Inspect git status**

Run:

```powershell
git status --short
```

Expected: only intended files are staged/modified. Existing unrelated dirty files must not be reverted.

- [ ] **Step 5: Final commit if any verification fixes were needed**

Run:

```powershell
git add <only-files-touched-by-this-plan>
git commit -m "fix(data): verify symbol intelligence flywheel"
```

## Later Expansion Backlog

These are separate future plans after the first data spine is green:

- `plan-databento-futures-research`: activate Databento historical OHLCV/trades/definitions/statistics for futures.
- `plan-ibkr-l1-capture`: capture top-of-book, spread, quote rate, and tick stream from IBKR.
- `plan-order-flow-v1`: derive bid/ask volume split and order-book imbalance, then keep it read-only.
- `plan-news-macro-v1`: automate FRED/BLS/EIA/Treasury series plus event calendar reconciliation.
- `plan-benzinga-news-v1`: integrate paid news with dedupe, ticker/entity tagging, and attribution.
- `plan-sec-edgar-v1`: add SEC filings for ETF/equity context and earnings/corporate-action windows.
- `plan-jarvis-attribution-v1`: feed symbol snapshots into Jarvis explainers and retune queue without live-routing changes.

## Self-Review

- Spec coverage: Tasks 1-8 implement canonical data lake, schema, store, audit, backfill, quality snapshot, and dashboard visibility. Task 9 documents provider activation. Task 10 verifies the batch.
- Scope control: No task activates Databento, news, L2, or live routing. Paid/live providers are gated behind later plans.
- Placeholder scan: No unresolved placeholder wording is used inside task steps.
- Type consistency: `SymbolIntelRecord`, `SymbolIntelQuality`, `SymbolIntelStore`, `SymbolIntelCoverage`, `score_symbol`, `inspect_symbol`, `run_audit`, `write_snapshot`, and `backfill_outcomes_from_closed_trade_ledger` are defined before later use.

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eta_engine.data.symbol_intel import SymbolIntelQuality, SymbolIntelRecord, SymbolIntelStore
from eta_engine.scripts import workspace_roots


def test_symbol_intelligence_paths_stay_under_workspace():
    root = workspace_roots.WORKSPACE_ROOT.resolve()
    data_lake = workspace_roots.ETA_DATA_LAKE_ROOT.resolve()
    snapshot = workspace_roots.ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH.resolve()

    assert str(data_lake).startswith(str(root))
    assert str(snapshot).startswith(str(root))
    assert data_lake == root / "var" / "eta_engine" / "data_lake"
    assert snapshot == root / "var" / "eta_engine" / "state" / "symbol_intelligence_latest.json"


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

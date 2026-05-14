import json
from datetime import UTC, datetime

from eta_engine.data.symbol_intel import SymbolIntelRecord, SymbolIntelStore
from eta_engine.scripts.symbol_intelligence_audit import (
    backfill_bars_from_history,
    backfill_decisions_from_journal,
    backfill_events_from_calendar,
    backfill_outcomes_from_closed_trade_ledger,
    backfill_quality_from_audit,
    inspect_symbol,
    run_audit,
    write_snapshot,
)


def _record(record_type: str, symbol: str = "MNQ1", *, payload: dict | None = None) -> SymbolIntelRecord:
    return SymbolIntelRecord(
        record_type=record_type,
        ts_utc=datetime(2026, 5, 14, 14, 30, tzinfo=UTC),
        symbol=symbol,
        source="test",
        payload=payload or {"ok": True},
    )


def test_inspect_symbol_counts_required_components(tmp_path):
    store = SymbolIntelStore(root=tmp_path)
    for record_type in ("bar", "macro_event", "decision", "outcome", "quality"):
        store.append(_record(record_type))

    coverage = inspect_symbol("MNQ1", store=store, now=datetime(2026, 5, 14, 15, 0, tzinfo=UTC))

    assert coverage.symbol == "MNQ1"
    assert coverage.status == "green"
    assert coverage.score == 1.0
    assert coverage.components["bars"] is True
    assert coverage.components["events"] is True
    assert coverage.components["decisions"] is True
    assert coverage.components["outcomes"] is True
    assert coverage.components["quality"] is True
    assert coverage.optional_components["news"] is False
    assert coverage.optional_components["book"] is False


def test_run_audit_surfaces_missing_components(tmp_path):
    store = SymbolIntelStore(root=tmp_path)
    store.append(_record("bar", "MNQ1"))
    store.append(_record("decision", "MNQ1"))

    payload = run_audit(symbols=["MNQ1"], store=store, now=datetime(2026, 5, 14, 15, 0, tzinfo=UTC))

    assert payload["schema"] == "eta.symbol_intelligence.audit.v1"
    assert payload["overall_status"] == "red"
    assert payload["status"] == "RED"
    assert payload["average_score_pct"] == 40
    assert payload["symbols"][0]["status"] == "red"
    assert payload["symbols"][0]["missing_required"] == ["events", "outcomes", "quality"]


def test_backfill_outcomes_from_closed_trade_ledger(tmp_path):
    store = SymbolIntelStore(root=tmp_path / "lake")
    ledger = tmp_path / "closed_trades.json"
    ledger.write_text(
        json.dumps(
            {
                "closed_trades": [
                    {
                        "bot": "volume_profile_mnq",
                        "symbol": "MNQ1",
                        "side": "SELL",
                        "qty": 1,
                        "entry_price": 29200.0,
                        "exit_price": 29224.5,
                        "realized_pnl": 49.0,
                        "r_multiple": 0.98,
                        "exit_time_utc": "2026-05-14T18:45:00+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    count = backfill_outcomes_from_closed_trade_ledger(source_path=ledger, store=store)
    rows = list(store.iter_records(record_type="outcome", symbol="MNQ1"))

    assert count == 1
    assert len(rows) == 1
    assert rows[0].source == "broker_ledger"
    assert rows[0].payload["bot"] == "volume_profile_mnq"
    assert rows[0].payload["entry_price"] == 29200.0
    assert rows[0].payload["exit_price"] == 29224.5


def test_write_snapshot_creates_parent_and_payload(tmp_path):
    path = tmp_path / "state" / "symbol_intelligence_latest.json"
    payload = {"overall_status": "amber", "symbols": []}

    written = write_snapshot(payload, path=path)

    assert written == path
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_existing_truth_surface_backfills_seed_auditable_components(tmp_path):
    store = SymbolIntelStore(root=tmp_path / "lake")
    history = tmp_path / "history"
    history.mkdir()
    (history / "MNQ1_5m.csv").write_text("ts,open,high,low,close\n2026-05-14,1,2,0,1\n", encoding="utf-8")
    calendar = tmp_path / "event_calendar.yaml"
    calendar.write_text(
        """
events:
  - ts_utc: "2026-05-14T12:30:00Z"
    kind: CPI
    symbol: null
    severity: 3
""".strip(),
        encoding="utf-8",
    )
    journal = tmp_path / "decision_journal.jsonl"
    journal.write_text(
        json.dumps(
            {
                "ts": "2026-05-14T14:31:00Z",
                "actor": "JARVIS",
                "intent": "approve volume profile",
                "outcome": "APPROVED",
                "links": ["bot:volume_profile_mnq"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert backfill_bars_from_history(history_root=history, store=store, symbols=["MNQ1"]) == 1
    assert backfill_events_from_calendar(calendar_path=calendar, store=store, symbols=["MNQ1"]) == 1
    assert (
        backfill_decisions_from_journal(
            journal_path=journal,
            store=store,
            symbols=["MNQ1"],
            bot_symbol_map={"volume_profile_mnq": "MNQ1"},
        )
        == 1
    )
    assert backfill_quality_from_audit(store=store, symbols=["MNQ1"], now=datetime(2026, 5, 14, 15, 0, tzinfo=UTC)) == 1

    coverage = inspect_symbol("MNQ1", store=store, now=datetime(2026, 5, 14, 15, 0, tzinfo=UTC))

    assert coverage.components["bars"] is True
    assert coverage.components["events"] is True
    assert coverage.components["decisions"] is True
    assert coverage.components["quality"] is True

import json
from datetime import UTC, datetime

from eta_engine.data.symbol_intel import SymbolIntelRecord, SymbolIntelStore
from eta_engine.scripts.symbol_intelligence_collector import (
    acquire_lock,
    run_collection,
)


def test_symbol_intelligence_collector_writes_status_and_snapshot(tmp_path):
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
    closed_trades = tmp_path / "closed_trade_ledger_latest.json"
    closed_trades.write_text(
        json.dumps(
            {
                "recent_closes": [
                    {
                        "bot_id": "volume_profile_mnq",
                        "symbol": "MNQ1",
                        "close_ts": "2026-05-14T15:00:00Z",
                        "side": "SELL",
                        "qty": 1,
                        "fill_price": 29225.0,
                        "realized_pnl": 50.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    status_path = tmp_path / "state" / "symbol_intelligence_collector_latest.json"
    snapshot_path = tmp_path / "state" / "symbol_intelligence_latest.json"

    payload = run_collection(
        store=store,
        symbols=["MNQ1"],
        now=datetime(2026, 5, 14, 16, 0, tzinfo=UTC),
        status_path=status_path,
        snapshot_path=snapshot_path,
        history_root=history,
        calendar_path=calendar,
        journal_path=journal,
        shadow_signals_path=tmp_path / "missing_shadow_signals.jsonl",
        closed_trade_path=closed_trades,
        tws_watchdog_path=tmp_path / "missing_tws.json",
        ibgateway_reauth_path=tmp_path / "missing_reauth.json",
        bot_symbol_map={"volume_profile_mnq": "MNQ1"},
    )

    assert payload["status"] == "ok"
    assert payload["audit"]["overall_status"] == "green"
    assert payload["bootstrap_counts"]["bars"] == 1
    assert payload["bootstrap_counts"]["events"] == 1
    assert payload["bootstrap_counts"]["decisions"] == 1
    assert payload["bootstrap_counts"]["outcomes"] == 1
    assert json.loads(status_path.read_text(encoding="utf-8")) == payload
    assert json.loads(snapshot_path.read_text(encoding="utf-8"))["overall_status"] == "green"


def test_symbol_intelligence_collector_lock_rejects_live_duplicate(tmp_path):
    lock_path = tmp_path / "collector.lock"

    with acquire_lock(lock_path, stale_after_seconds=3600):
        try:
            with acquire_lock(lock_path, stale_after_seconds=3600):
                raise AssertionError("duplicate lock should not be acquired")
        except RuntimeError as exc:
            assert "already running" in str(exc)


def test_symbol_intelligence_collector_lock_reclaims_stale_file(tmp_path):
    lock_path = tmp_path / "collector.lock"
    lock_path.write_text(
        json.dumps({"pid": 123, "started_at_utc": "2026-05-14T10:00:00+00:00"}),
        encoding="utf-8",
    )

    with acquire_lock(
        lock_path,
        stale_after_seconds=60,
        now=datetime(2026, 5, 14, 10, 5, tzinfo=UTC),
    ):
        assert lock_path.exists()


def test_symbol_intelligence_collector_marks_gateway_down_as_degraded(tmp_path):
    store = SymbolIntelStore(root=tmp_path / "lake")
    for record_type in ("bar", "macro_event", "decision", "outcome", "quality"):
        store.append(
            SymbolIntelRecord(
                record_type=record_type,
                ts_utc=datetime(2026, 5, 14, 16, 0, tzinfo=UTC),
                symbol="MNQ1",
                source="test",
                payload={"ok": True},
            )
        )
    tws_path = tmp_path / "state" / "tws_watchdog.json"
    tws_path.parent.mkdir(parents=True)
    tws_path.write_text(json.dumps({"healthy": False, "details": {"socket_ok": False}}), encoding="utf-8")

    payload = run_collection(
        store=store,
        symbols=["MNQ1"],
        now=datetime(2026, 5, 14, 16, 0, tzinfo=UTC),
        status_path=tmp_path / "state" / "collector.json",
        snapshot_path=tmp_path / "state" / "snapshot.json",
        history_root=tmp_path / "missing_history",
        calendar_path=tmp_path / "missing_calendar.yaml",
        journal_path=tmp_path / "missing_decisions.jsonl",
        shadow_signals_path=tmp_path / "missing_shadow_signals.jsonl",
        closed_trade_path=tmp_path / "missing_closes.json",
        tws_watchdog_path=tws_path,
        ibgateway_reauth_path=tmp_path / "missing_reauth.json",
    )

    assert payload["audit"]["overall_status"] == "green"
    assert payload["status"] == "degraded_gateway"

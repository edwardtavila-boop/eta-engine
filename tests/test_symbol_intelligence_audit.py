import json
from datetime import UTC, datetime

from eta_engine.data.market_news import NewsHeadline
from eta_engine.data.symbol_intel import SymbolIntelRecord, SymbolIntelStore
from eta_engine.scripts.symbol_intelligence_audit import (
    backfill_bars_from_history,
    backfill_book_from_depth_snapshots,
    backfill_decisions_from_journal,
    backfill_decisions_from_shadow_signals,
    backfill_events_from_calendar,
    backfill_news_from_public_feeds,
    backfill_outcomes_from_closed_trade_ledger,
    backfill_quality_from_audit,
    default_bot_symbol_map,
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


def test_inspect_symbol_ignores_future_records_for_required_coverage(tmp_path):
    store = SymbolIntelStore(root=tmp_path)
    store.append(
        SymbolIntelRecord(
            record_type="bar",
            ts_utc=datetime(2026, 5, 15, 14, 30, tzinfo=UTC),
            symbol="MNQ1",
            source="test",
            payload={"future": True},
        )
    )
    for record_type in ("macro_event", "decision", "outcome", "quality"):
        store.append(_record(record_type))

    coverage = inspect_symbol("MNQ1", store=store, now=datetime(2026, 5, 14, 15, 0, tzinfo=UTC))

    assert coverage.components["bars"] is False
    assert coverage.missing_required == ["bars"]
    assert coverage.future_record_count == 1
    assert coverage.future_record_types == ["bar"]
    assert coverage.latest_record_utc == "2026-05-14T14:30:00+00:00"


def test_backfill_bars_prefers_csv_timestamp_over_future_file_mtime(tmp_path):
    store = SymbolIntelStore(root=tmp_path / "lake")
    history = tmp_path / "history"
    history.mkdir()
    bar_file = history / "MNQ1_5m.csv"
    bar_file.write_text("ts,open,high,low,close\n2026-05-14T14:35:00Z,1,2,0,1\n", encoding="utf-8")

    assert backfill_bars_from_history(history_root=history, store=store, symbols=["MNQ1"]) == 1

    coverage = inspect_symbol("MNQ1", store=store, now=datetime(2026, 5, 14, 15, 0, tzinfo=UTC))
    records = list(store.iter_records(record_type="bar", symbol="MNQ1"))

    assert coverage.components["bars"] is True
    assert coverage.future_record_count == 0
    assert records[0].payload["bar_ts_utc"] == "2026-05-14T14:35:00+00:00"


def test_backfill_news_from_public_feeds(tmp_path, monkeypatch):
    store = SymbolIntelStore(root=tmp_path / "lake")

    def _fake_headlines(query: str, *, limit: int, max_age_hours: float, now: datetime):
        assert "Nasdaq" in query
        return [
            NewsHeadline(
                headline="Nasdaq futures rise ahead of CPI",
                url="https://example.com/nasdaq-cpi",
                publisher="Reuters",
                published_at_utc=datetime(2026, 5, 14, 14, 0, tzinfo=UTC),
                query=query,
                snippet="Nasdaq futures moved higher before the CPI print.",
            )
        ]

    monkeypatch.setattr(
        "eta_engine.scripts.symbol_intelligence_audit.fetch_google_news_headlines",
        _fake_headlines,
    )

    count = backfill_news_from_public_feeds(
        store=store,
        symbols=["MNQ1"],
        now=datetime(2026, 5, 14, 16, 0, tzinfo=UTC),
    )
    rows = list(store.iter_records(record_type="news", symbol="MNQ1"))

    assert count == 1
    assert len(rows) == 1
    assert rows[0].source == "google_news_rss"
    assert rows[0].payload["publisher"] == "Reuters"
    assert rows[0].payload["headline"] == "Nasdaq futures rise ahead of CPI"


def test_backfill_book_from_depth_snapshots_reads_latest_book(tmp_path):
    store = SymbolIntelStore(root=tmp_path / "lake")
    depth_root = tmp_path / "depth"
    depth_root.mkdir()
    (depth_root / "MNQ_20260514.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-05-14T15:59:59+00:00",
                "symbol": "MNQ",
                "bids": [{"price": 21001.25, "size": 12}, {"price": 21001.0, "size": 8}],
                "asks": [{"price": 21001.5, "size": 9}, {"price": 21001.75, "size": 7}],
                "spread": 0.25,
                "mid": 21001.375,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    count = backfill_book_from_depth_snapshots(
        depth_root=depth_root,
        store=store,
        symbols=["MNQ1"],
    )
    rows = list(store.iter_records(record_type="book", symbol="MNQ1"))

    assert count == 1
    assert len(rows) == 1
    assert rows[0].source == "ibkr_depth_capture"
    assert rows[0].payload["bid_levels"] == 2
    assert rows[0].payload["ask_levels"] == 2
    assert rows[0].payload["best_bid"] == 21001.25
    assert rows[0].payload["best_ask"] == 21001.5


def test_future_macro_events_count_as_scheduled_coverage_not_anomalies(tmp_path):
    store = SymbolIntelStore(root=tmp_path)
    for record_type in ("bar", "decision", "outcome", "quality"):
        store.append(_record(record_type))
    store.append(
        SymbolIntelRecord(
            record_type="macro_event",
            ts_utc=datetime(2026, 5, 15, 14, 30, tzinfo=UTC),
            symbol="MNQ1",
            source="calendar",
            payload={"scheduled": True},
        )
    )

    coverage = inspect_symbol("MNQ1", store=store, now=datetime(2026, 5, 14, 15, 0, tzinfo=UTC))

    assert coverage.components["events"] is True
    assert coverage.future_record_count == 0
    assert coverage.status == "green"
    assert coverage.latest_record_utc == "2026-05-14T14:30:00+00:00"


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


def test_backfill_decisions_from_shadow_signals(tmp_path):
    store = SymbolIntelStore(root=tmp_path / "lake")
    shadow_signals = tmp_path / "shadow_signals.jsonl"
    shadow_signals.write_text(
        json.dumps(
            {
                "ts": "2026-05-13T06:33:18.571042+00:00",
                "bot_id": "nq_futures_sage",
                "signal_id": "nq_futures_sage_2026-05-13T06:33:18.570908+00:00",
                "symbol": "NQ1",
                "side": "BUY",
                "qty_intended": 1,
                "lifecycle": "EVAL_PAPER",
                "route_target": "paper",
                "route_reason": "lifecycle_eval_paper",
                "prospective_loss_usd": 250.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    count = backfill_decisions_from_shadow_signals(
        shadow_signals_path=shadow_signals,
        store=store,
        symbols=["NQ1"],
    )
    rows = list(store.iter_records(record_type="decision", symbol="NQ1"))

    assert count == 1
    assert len(rows) == 1
    assert rows[0].source == "jarvis_shadow_signal"
    assert rows[0].payload["bot"] == "nq_futures_sage"
    assert rows[0].payload["route_target"] == "paper"


def test_backfill_outcomes_from_live_close_stream_nested_payload(tmp_path):
    store = SymbolIntelStore(root=tmp_path / "lake")
    trade_closes = tmp_path / "trade_closes.jsonl"
    trade_closes.write_text(
        json.dumps(
            {
                "bot_id": "mes_sweep_reclaim",
                "signal_id": "mes_sweep_reclaim_093c3b2b",
                "ts": "2026-05-13T06:30:55.276560+00:00",
                "realized_r": -1.0,
                "data_source": "live",
                "extra": {
                    "symbol": "MES1",
                    "side": "BUY",
                    "qty": 5.0,
                    "fill_price": 52.25,
                    "realized_pnl": -37.5,
                    "close_ts": "2026-05-13T06:30:55.127312+00:00",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    count = backfill_outcomes_from_closed_trade_ledger(
        source_paths=[trade_closes],
        store=store,
        symbols=["MES1"],
        bot_symbol_map={"mes_sweep_reclaim": "MES1"},
    )
    rows = list(store.iter_records(record_type="outcome", symbol="MES1"))

    assert count == 1
    assert len(rows) == 1
    assert rows[0].payload["bot"] == "mes_sweep_reclaim"
    assert rows[0].payload["fill_price"] == 52.25
    assert rows[0].payload["realized_pnl"] == -37.5
    assert rows[0].payload["r_multiple"] == -1.0


def test_backfill_outcomes_infers_symbol_from_bot_map(tmp_path):
    store = SymbolIntelStore(root=tmp_path / "lake")
    trade_closes = tmp_path / "trade_closes.jsonl"
    trade_closes.write_text(
        json.dumps(
            {
                "bot_id": "nq_futures_sage",
                "signal_id": "nq_futures_sage_abc123",
                "ts": "2026-05-13T06:30:55+00:00",
                "side": "SELL",
                "qty": 1,
                "fill_price": 21150.25,
                "realized_pnl": 82.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    count = backfill_outcomes_from_closed_trade_ledger(
        source_paths=[trade_closes],
        store=store,
        symbols=["NQ1"],
        bot_symbol_map={"nq_futures_sage": "NQ1"},
    )
    rows = list(store.iter_records(record_type="outcome", symbol="NQ1"))

    assert count == 1
    assert len(rows) == 1
    assert rows[0].payload["bot"] == "nq_futures_sage"
    assert rows[0].payload["exit_price"] == 21150.25


def test_default_bot_symbol_map_includes_legacy_mes_confluence():
    assert default_bot_symbol_map()["mes_confluence"] == "MES1"


def test_backfill_outcomes_infers_legacy_mes_confluence_symbol(tmp_path):
    store = SymbolIntelStore(root=tmp_path / "lake")
    trade_closes = tmp_path / "trade_closes.jsonl"
    trade_closes.write_text(
        json.dumps(
            {
                "bot_id": "mes_confluence",
                "signal_id": "mes_confluence_102b4868",
                "ts": "2026-05-04T12:35:36+00:00",
                "realized_r": 0.0006,
                "data_source": "paper",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    count = backfill_outcomes_from_closed_trade_ledger(
        source_paths=[trade_closes],
        store=store,
        symbols=["MES1"],
    )
    rows = list(store.iter_records(record_type="outcome", symbol="MES1"))

    assert count == 1
    assert len(rows) == 1
    assert rows[0].payload["bot"] == "mes_confluence"


def test_backfill_outcomes_normalizes_root_symbol_aliases(tmp_path):
    store = SymbolIntelStore(root=tmp_path / "lake")
    trade_closes = tmp_path / "trade_closes.jsonl"
    trade_closes.write_text(
        json.dumps(
            {
                "bot_id": "ym_sweep_reclaim",
                "signal_id": "ym_sweep_reclaim_db756962",
                "ts": "2026-05-05T03:16:47+00:00",
                "realized_r": 0.25,
                "extra": {
                    "symbol": "YM",
                    "realized_pnl": 125.0,
                    "close_ts": "2026-05-05T03:16:46+00:00",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    count = backfill_outcomes_from_closed_trade_ledger(
        source_paths=[trade_closes],
        store=store,
        symbols=["YM1"],
    )
    rows = list(store.iter_records(record_type="outcome", symbol="YM1"))

    assert count == 1
    assert len(rows) == 1
    assert rows[0].payload["bot"] == "ym_sweep_reclaim"
    assert rows[0].payload["realized_pnl"] == 125.0


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

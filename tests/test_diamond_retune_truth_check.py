from __future__ import annotations

import json
from datetime import UTC, datetime

from eta_engine.scripts import diamond_retune_truth_check as check


def test_normalize_retune_truth_supports_public_and_local_shapes() -> None:
    public_payload = {
        "focus_bot": "mnq_futures_sage",
        "focus_issue": "broker_pnl_negative",
        "focus_state": "COLLECT_MORE_SAMPLE",
        "focus_strategy_kind": "orb_sage_gated",
        "focus_best_session": "close",
        "focus_worst_session": "overnight",
        "focus_command": "python -m eta_engine.scripts.run_research_grid --bots mnq_futures_sage",
        "summary": {
            "broker_truth_focus_closed_trade_count": 141,
            "broker_truth_focus_total_realized_pnl": -1939.75,
            "broker_truth_focus_profit_factor": 0.3951,
            "safe_to_mutate_live": False,
        },
    }
    local_payload = {
        "summary": {
            "broker_truth_focus_bot_id": "mnq_futures_sage",
            "broker_truth_focus_issue_code": "broker_pnl_negative",
            "broker_truth_focus_state": "COLLECT_MORE_SAMPLE",
            "broker_truth_focus_strategy_kind": "orb_sage_gated",
            "broker_truth_focus_best_session": "close",
            "broker_truth_focus_worst_session": "overnight",
            "broker_truth_focus_next_command": "python -m eta_engine.scripts.run_research_grid --bots mnq_futures_sage",
            "broker_truth_focus_closed_trade_count": 141,
            "broker_truth_focus_total_realized_pnl": -1939.75,
            "broker_truth_focus_profit_factor": 0.3951,
            "safe_to_mutate_live": False,
        },
    }

    assert check.normalize_retune_truth(public_payload) == check.normalize_retune_truth(local_payload)


def test_build_report_flags_public_local_focus_mismatch(tmp_path, monkeypatch) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)
    (state_root / "diamond_retune_status_latest.json").write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-05-15T19:08:24+00:00",
                "summary": {
                    "broker_truth_focus_bot_id": "mcl_sweep_reclaim",
                    "broker_truth_focus_issue_code": "broker_pnl_negative",
                    "broker_truth_focus_state": "KEEP_RETUNING",
                    "broker_truth_focus_strategy_kind": "confluence_scorecard",
                    "broker_truth_focus_best_session": "overnight",
                    "broker_truth_focus_worst_session": "afternoon",
                    "broker_truth_focus_next_command": (
                        "python -m eta_engine.scripts.run_research_grid "
                        "--bots mcl_sweep_reclaim"
                    ),
                    "broker_truth_focus_closed_trade_count": 5,
                    "broker_truth_focus_total_realized_pnl": -151.0,
                    "broker_truth_focus_profit_factor": 0.0,
                    "safe_to_mutate_live": False,
                },
            },
        ),
        encoding="utf-8",
    )
    (state_root / "closed_trade_ledger_latest.json").write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-05-15T19:08:15+00:00",
                "closed_trade_count": 99,
                "total_realized_pnl": 2964.3,
            },
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        check,
        "_probe_public_surface",
        lambda url, timeout_s, now: check.TruthSurface(
            label="public_retune_truth",
            source=url,
            available=True,
            readable=True,
            status_code=200,
            observed_ts="2026-05-15T19:09:00+00:00",
            age_seconds=0.0,
            normalized={
                "focus_bot": "mnq_futures_sage",
                "focus_issue": "broker_pnl_negative",
                "focus_state": "COLLECT_MORE_SAMPLE",
                "focus_strategy_kind": "orb_sage_gated",
                "focus_best_session": "close",
                "focus_worst_session": "overnight",
                "focus_command": "python -m eta_engine.scripts.run_research_grid --bots mnq_futures_sage",
                "focus_closed_trade_count": 141,
                "focus_total_realized_pnl": -1939.75,
                "focus_profit_factor": 0.3951,
                "safe_to_mutate_live": False,
            },
            summary={"broker_truth_focus_bot_id": "mnq_futures_sage"},
        ),
    )
    monkeypatch.setattr(
        check,
        "_probe_public_broker_state",
        lambda url, timeout_s, now, focus_normalized: check.TruthSurface(
            label="public_broker_close_truth",
            source=url,
            available=True,
            readable=True,
            status_code=200,
            observed_ts="2026-05-15T19:09:05+00:00",
            age_seconds=0.0,
            normalized={
                "focus_bot": "mnq_futures_sage",
                "focus_issue": "broker_pnl_negative",
                "focus_state": "COLLECT_MORE_SAMPLE",
                "focus_closed_trade_count": 141,
                "focus_total_realized_pnl": -1939.75,
                "focus_profit_factor": 0.3951,
                "broker_mtd_pnl": 24158.0,
                "today_realized_pnl": -1751.49,
                "total_unrealized_pnl": 3791.49,
                "open_position_count": 4,
                "reporting_timezone": "America/New_York",
                "close_windows": {"mtd": {"closed_outcome_count": 234}},
                "focus_recent_outcomes_mtd": [],
                "mtd_pnl_map": {"limit": 5, "top_winners": [], "top_losers": []},
            },
            summary={"focus_bot": "mnq_futures_sage", "broker_mtd_pnl": 24158.0},
        ),
    )
    monkeypatch.setattr(
        check,
        "_local_bot_evidence_audit",
        lambda bot_id: {
            "bot_id": bot_id,
            "total_rows": 1267 if bot_id == "mnq_futures_sage" else 5,
            "by_data_source": {"historical_unverified": 1267} if bot_id == "mnq_futures_sage" else {"live": 5},
            "rows_with_realized_pnl": 0 if bot_id == "mnq_futures_sage" else 5,
            "rows_with_close_ts": 0 if bot_id == "mnq_futures_sage" else 5,
            "rows_with_nonempty_extra": 0 if bot_id == "mnq_futures_sage" else 5,
            "rows_with_fill_metadata": 0 if bot_id == "mnq_futures_sage" else 5,
            "historical_unverified_rows": 1267 if bot_id == "mnq_futures_sage" else 0,
            "historical_rows_with_fill_metadata": 0,
        },
    )
    monkeypatch.setattr(
        check,
        "_trade_close_source_audit",
        lambda bot_id: {
            "bot_id": bot_id,
            "canonical": {
                "path": str(state_root / "jarvis_intel" / "trade_closes.jsonl"),
                "exists": True,
                "line_count": 808,
                "bot_row_count": 0,
                "bot_rows_with_explicit_data_source": 0,
                "last_write_utc": "2026-05-15T18:55:25+00:00",
                "bot_latest_ts": None,
                "file_size_bytes": 379241,
            },
            "legacy": {
                "path": str(tmp_path / "eta_engine" / "state" / "jarvis_intel" / "trade_closes.jsonl"),
                "exists": True,
                "line_count": 43450,
                "bot_row_count": 1267 if bot_id == "mnq_futures_sage" else 5,
                "bot_rows_with_explicit_data_source": 0,
                "last_write_utc": "2026-05-12T20:49:10+00:00",
                "bot_latest_ts": "2026-05-12T20:33:35+00:00",
                "file_size_bytes": 22810619,
            },
        },
    )

    report = check.build_diamond_retune_truth_report(
        state_root=state_root,
        public_url="https://ops.example.com/api/jarvis/diamond_retune_status",
        now=datetime(2026, 5, 15, 19, 10, tzinfo=UTC),
    )

    assert report["healthy"] is False
    assert report["status"] == "warning"
    assert report["diagnosis"] == "public_local_focus_mismatch"
    assert report["mismatch_count"] >= 1
    assert any(item["field"] == "focus_bot" for item in report["field_mismatches"])
    assert report["local_closed_trade_ledger"]["closed_trade_count"] == 99
    assert report["public_broker_close_truth"]["summary"]["broker_mtd_pnl"] == 24158.0
    assert report["public_focus_local_evidence_audit"]["historical_unverified_rows"] == 1267
    assert report["public_focus_trade_close_source_audit"]["canonical"]["line_count"] == 808
    assert report["public_focus_provenance_gap"]["status"] == "material_gap"
    assert report["public_focus_provenance_gap"]["diagnosis"] == "public_broker_proof_exceeds_local_canonical"
    assert report["public_focus_provenance_gap"]["public_focus_closed_trade_count"] == 141
    assert report["public_focus_provenance_gap"]["canonical_bot_row_count"] == 0
    assert report["public_focus_provenance_gap"]["gap_count"] == 141
    assert any("blind reclassification is unsafe" in item for item in report["warnings"])
    assert any("Do not blindly reclassify" in item for item in report["action_items"])
    assert any("canonical trade_closes source is thin" in item for item in report["warnings"])
    assert any("Refresh or repair the canonical trade_closes writer" in item for item in report["action_items"])
    assert any("materially exceeds the local canonical trade_closes sample" in item for item in report["warnings"])


def test_build_report_flags_public_focus_provenance_gap_even_when_focus_matches(tmp_path, monkeypatch) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)
    matching_payload = {
        "generated_at_utc": "2026-05-15T19:08:24+00:00",
        "summary": {
            "broker_truth_focus_bot_id": "mnq_futures_sage",
            "broker_truth_focus_issue_code": "broker_pnl_negative",
            "broker_truth_focus_state": "COLLECT_MORE_SAMPLE",
            "broker_truth_focus_strategy_kind": "orb_sage_gated",
            "broker_truth_focus_best_session": "close",
            "broker_truth_focus_worst_session": "overnight",
            "broker_truth_focus_next_command": (
                "python -m eta_engine.scripts.run_research_grid "
                "--bots mnq_futures_sage"
            ),
            "broker_truth_focus_closed_trade_count": 141,
            "broker_truth_focus_total_realized_pnl": -1939.75,
            "broker_truth_focus_profit_factor": 0.3951,
            "safe_to_mutate_live": False,
        },
    }
    (state_root / "diamond_retune_status_latest.json").write_text(
        json.dumps(matching_payload),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        check,
        "_probe_public_surface",
        lambda url, timeout_s, now: check.TruthSurface(
            label="public_retune_truth",
            source=url,
            available=True,
            readable=True,
            status_code=200,
            observed_ts="2026-05-15T19:09:00+00:00",
            age_seconds=0.0,
            normalized=check.normalize_retune_truth(matching_payload),
            summary={"broker_truth_focus_bot_id": "mnq_futures_sage"},
        ),
    )
    monkeypatch.setattr(
        check,
        "_probe_public_broker_state",
        lambda url, timeout_s, now, focus_normalized: check.TruthSurface(
            label="public_broker_close_truth",
            source=url,
            available=True,
            readable=True,
            status_code=200,
            observed_ts="2026-05-15T19:09:05+00:00",
            age_seconds=0.0,
            normalized={
                "focus_bot": "mnq_futures_sage",
                "focus_issue": "broker_pnl_negative",
                "focus_state": "COLLECT_MORE_SAMPLE",
                "focus_closed_trade_count": 141,
                "focus_total_realized_pnl": -1939.75,
                "focus_profit_factor": 0.3951,
            },
            summary={"focus_bot": "mnq_futures_sage"},
        ),
    )
    monkeypatch.setattr(
        check,
        "_local_bot_evidence_audit",
        lambda bot_id: {
            "bot_id": bot_id,
            "total_rows": 1272,
            "by_data_source": {"historical_unverified": 1267, "live": 5},
            "rows_with_realized_pnl": 5,
            "rows_with_close_ts": 5,
            "rows_with_nonempty_extra": 5,
            "rows_with_fill_metadata": 5,
            "historical_unverified_rows": 1267,
            "historical_rows_with_fill_metadata": 0,
        },
    )
    monkeypatch.setattr(
        check,
        "_trade_close_source_audit",
        lambda bot_id: {
            "bot_id": bot_id,
            "canonical": {
                "path": str(state_root / "jarvis_intel" / "trade_closes.jsonl"),
                "exists": True,
                "line_count": 808,
                "bot_row_count": 5,
                "bot_rows_with_explicit_data_source": 5,
                "last_write_utc": "2026-05-15T18:55:25+00:00",
                "bot_latest_ts": "2026-05-15T18:55:24+00:00",
                "file_size_bytes": 379241,
            },
            "legacy": {
                "path": str(tmp_path / "eta_engine" / "state" / "jarvis_intel" / "trade_closes.jsonl"),
                "exists": True,
                "line_count": 43450,
                "bot_row_count": 1267,
                "bot_rows_with_explicit_data_source": 0,
                "last_write_utc": "2026-05-12T20:49:10+00:00",
                "bot_latest_ts": "2026-05-12T20:33:35+00:00",
                "file_size_bytes": 22810619,
            },
        },
    )

    report = check.build_diamond_retune_truth_report(
        state_root=state_root,
        public_url="https://ops.example.com/api/jarvis/diamond_retune_status",
        now=datetime(2026, 5, 15, 19, 10, tzinfo=UTC),
    )

    assert report["healthy"] is False
    assert report["status"] == "warning"
    assert report["diagnosis"] == "public_focus_provenance_gap"
    assert report["mismatch_count"] == 0
    assert report["public_focus_provenance_gap"]["status"] == "material_gap"
    assert report["public_focus_provenance_gap"]["gap_count"] == 136
    assert report["public_focus_provenance_gap"]["canonical_support_ratio"] == 0.0355
    assert any("materially exceeds the local canonical trade_closes sample" in item for item in report["warnings"])
    assert any("Refresh or repair the canonical trade_closes writer" in item for item in report["action_items"])


def test_write_report_persists_latest_snapshot(tmp_path) -> None:
    report = {
        "kind": "eta_diamond_retune_truth_check",
        "generated_at_utc": "2026-05-15T19:10:00+00:00",
        "healthy": True,
        "status": "healthy",
        "diagnosis": "public_local_focus_match",
    }

    output_path = tmp_path / "health" / "diamond_retune_truth_check_latest.json"
    written = check.write_diamond_retune_truth_report(report, output_path=output_path)

    assert written == output_path
    assert json.loads(output_path.read_text(encoding="utf-8"))["diagnosis"] == "public_local_focus_match"


def test_write_public_retune_truth_cache_persists_focus_summary(tmp_path) -> None:
    surface = {
        "label": "public_retune_truth",
        "source": "https://ops.example.com/api/jarvis/diamond_retune_status",
        "available": True,
        "readable": True,
        "status_code": 200,
        "normalized": {
            "focus_bot": "mnq_futures_sage",
            "focus_issue": "broker_pnl_negative",
            "focus_state": "COLLECT_MORE_SAMPLE",
        },
        "summary": {"broker_truth_focus_bot_id": "mnq_futures_sage"},
    }

    output_path = tmp_path / "health" / "public_diamond_retune_truth_latest.json"
    written = check.write_public_retune_truth_cache(surface, output_path=output_path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert written == output_path
    assert payload["kind"] == "eta_public_diamond_retune_truth_cache"
    assert payload["focus_bot"] == "mnq_futures_sage"
    assert payload["focus_issue"] == "broker_pnl_negative"
    assert payload["focus_state"] == "COLLECT_MORE_SAMPLE"


def test_write_public_retune_truth_cache_preserves_richer_existing_surface_when_incoming_is_skinny(tmp_path) -> None:
    rich_surface = {
        "label": "public_retune_truth",
        "source": "https://ops.example.com/api/jarvis/diamond_retune_status",
        "available": True,
        "readable": True,
        "status_code": 200,
        "normalized": {
            "focus_bot": "mnq_futures_sage",
            "focus_issue": "broker_pnl_negative",
            "focus_state": "COLLECT_MORE_SAMPLE",
        },
        "summary": {"broker_truth_focus_bot_id": "mnq_futures_sage"},
    }
    skinny_surface = {
        "available": True,
        "readable": True,
    }

    output_path = tmp_path / "health" / "public_diamond_retune_truth_latest.json"
    check.write_public_retune_truth_cache(rich_surface, output_path=output_path)
    written = check.write_public_retune_truth_cache(skinny_surface, output_path=output_path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert written == output_path
    assert payload["focus_bot"] == "mnq_futures_sage"
    assert payload["focus_issue"] == "broker_pnl_negative"
    assert payload["focus_state"] == "COLLECT_MORE_SAMPLE"
    assert payload["surface"]["available"] is True
    assert payload["surface"]["readable"] is True
    assert payload["surface"]["normalized"]["focus_bot"] == "mnq_futures_sage"


def test_write_public_broker_close_truth_cache_persists_focus_and_window_summary(tmp_path) -> None:
    surface = {
        "label": "public_broker_close_truth",
        "source": "https://ops.example.com/api/live/broker_state",
        "available": True,
        "readable": True,
        "status_code": 200,
        "normalized": {
            "focus_bot": "mnq_futures_sage",
            "focus_issue": "broker_pnl_negative",
            "focus_state": "COLLECT_MORE_SAMPLE",
            "focus_closed_trade_count": 141,
            "focus_total_realized_pnl": -1939.75,
            "focus_profit_factor": 0.3951,
            "broker_mtd_pnl": 24158.0,
            "today_realized_pnl": -1751.49,
            "total_unrealized_pnl": 3791.49,
            "open_position_count": 4,
            "reporting_timezone": "America/New_York",
            "close_windows": {"mtd": {"closed_outcome_count": 234}},
            "focus_recent_outcomes_mtd": [{"bot_id": "mnq_futures_sage", "realized_pnl": -279.0}],
            "mtd_pnl_map": {"limit": 5, "top_winners": [], "top_losers": []},
        },
        "summary": {"focus_bot": "mnq_futures_sage", "broker_mtd_pnl": 24158.0},
    }

    output_path = tmp_path / "health" / "public_broker_close_truth_latest.json"
    written = check.write_public_broker_close_truth_cache(surface, output_path=output_path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert written == output_path
    assert payload["kind"] == "eta_public_broker_close_truth_cache"
    assert payload["focus_bot"] == "mnq_futures_sage"
    assert payload["broker_mtd_pnl"] == 24158.0
    assert payload["close_windows"]["mtd"]["closed_outcome_count"] == 234
    assert payload["focus_recent_outcomes_mtd"][0]["realized_pnl"] == -279.0


def test_local_bot_evidence_audit_counts_fill_metadata(tmp_path, monkeypatch) -> None:
    rows = [
        {
            "bot_id": "mnq_futures_sage",
            "_data_source": "historical_unverified",
            "extra": {},
            "realized_pnl": None,
        },
        {
            "bot_id": "mnq_futures_sage",
            "_data_source": "live",
            "extra": {
                "symbol": "MNQ1",
                "fill_price": 29500.0,
                "realized_pnl": 80.5,
                "close_ts": "2026-05-15T19:00:00+00:00",
            },
            "realized_pnl": None,
        },
    ]
    monkeypatch.setattr(check, "load_close_records", None, raising=False)

    def _fake_load_close_records(*, bot_filter, data_sources):
        assert bot_filter == "mnq_futures_sage"
        assert data_sources is None
        return rows

    import eta_engine.scripts.closed_trade_ledger as ledger

    monkeypatch.setattr(ledger, "load_close_records", _fake_load_close_records)

    audit = check._local_bot_evidence_audit("mnq_futures_sage")

    assert audit["total_rows"] == 2
    assert audit["by_data_source"] == {"historical_unverified": 1, "live": 1}
    assert audit["rows_with_fill_metadata"] == 1
    assert audit["historical_unverified_rows"] == 1
    assert audit["historical_rows_with_fill_metadata"] == 0


def test_trade_close_source_audit_counts_canonical_vs_legacy_rows(tmp_path, monkeypatch) -> None:
    canonical = tmp_path / "var" / "eta_engine" / "state" / "jarvis_intel" / "trade_closes.jsonl"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(
        "\n".join(
            [
                json.dumps({"bot_id": "other_bot", "ts": "2026-05-15T18:00:00+00:00"}),
                json.dumps({"bot_id": "mnq_futures_sage", "ts": "2026-05-15T18:05:00+00:00", "data_source": "live"}),
            ],
        )
        + "\n",
        encoding="utf-8",
    )
    legacy = tmp_path / "eta_engine" / "state" / "jarvis_intel" / "trade_closes.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        "\n".join(
            [
                json.dumps({"bot_id": "mnq_futures_sage", "ts": "2026-05-12T18:00:00+00:00"}),
                json.dumps({"bot_id": "mnq_futures_sage", "ts": "2026-05-12T18:05:00+00:00"}),
                json.dumps({"bot_id": "mcl_sweep_reclaim", "ts": "2026-05-12T18:10:00+00:00"}),
            ],
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check, "CANONICAL_TRADE_CLOSES_PATH", canonical)
    monkeypatch.setattr(check, "LEGACY_TRADE_CLOSES_PATH", legacy)

    audit = check._trade_close_source_audit("mnq_futures_sage")

    assert audit["canonical"]["exists"] is True
    assert audit["canonical"]["line_count"] == 2
    assert audit["canonical"]["bot_row_count"] == 1
    assert audit["canonical"]["bot_rows_with_explicit_data_source"] == 1
    assert audit["legacy"]["exists"] is True
    assert audit["legacy"]["line_count"] == 3
    assert audit["legacy"]["bot_row_count"] == 2

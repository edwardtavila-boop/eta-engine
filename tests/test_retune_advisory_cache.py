from __future__ import annotations

import json
from pathlib import Path

import pytest

from eta_engine.scripts.retune_advisory_cache import build_retune_advisory


def test_build_retune_advisory_prefers_provenance_gap_warning_and_action(tmp_path: Path) -> None:
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    (health_dir / "public_diamond_retune_truth_latest.json").write_text(
        json.dumps(
            {
                "surface": {
                    "normalized": {
                        "focus_bot": "mnq_futures_sage",
                        "focus_issue": "broker_pnl_negative",
                        "focus_state": "COLLECT_MORE_SAMPLE",
                        "focus_closed_trade_count": 141,
                        "focus_total_realized_pnl": -1939.75,
                        "focus_profit_factor": 0.3951,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (health_dir / "public_broker_close_truth_latest.json").write_text(
        json.dumps(
            {
                "surface": {
                    "normalized": {
                        "focus_bot": "mnq_futures_sage",
                        "focus_closed_trade_count": 141,
                        "focus_total_realized_pnl": -1939.75,
                        "focus_profit_factor": 0.3951,
                        "broker_mtd_pnl": 20087.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (health_dir / "diamond_retune_truth_check_latest.json").write_text(
        json.dumps(
            {
                "diagnosis": "public_focus_provenance_gap",
                "warnings": [
                    "Public retune focus and local canonical retune receipt disagree.",
                    "Public broker-backed close sample materially exceeds the local canonical trade_closes sample for mnq_futures_sage (141 vs 5).",
                ],
                "action_items": [
                    "Refresh or repair the local closed-trade ledger and diamond_retune_status writers before using local retune receipts for operator decisions.",
                    "Refresh or repair the canonical trade_closes writer at C:\\EvolutionaryTradingAlgo\\var\\eta_engine\\state\\jarvis_intel\\trade_closes.jsonl from the authoritative VPS/public close source before trusting local broker-proof counts.",
                ],
            }
        ),
        encoding="utf-8",
    )

    advisory = build_retune_advisory(health_dir)

    assert advisory["diagnosis"] == "public_focus_provenance_gap"
    assert advisory["focus_bot"] == "mnq_futures_sage"
    assert advisory["preferred_warning"] == (
        "Public broker-backed close sample materially exceeds the local canonical trade_closes sample "
        "for mnq_futures_sage (141 vs 5)."
    )
    assert advisory["preferred_action"] == (
        "Refresh or repair the canonical trade_closes writer at "
        "C:\\EvolutionaryTradingAlgo\\var\\eta_engine\\state\\jarvis_intel\\trade_closes.jsonl "
        "from the authoritative VPS/public close source before trusting local broker-proof counts."
    )


def test_build_retune_advisory_preserves_focus_mismatch_primary_warning(tmp_path: Path) -> None:
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    (health_dir / "public_diamond_retune_truth_latest.json").write_text(
        json.dumps({"focus_bot": "mnq_futures_sage"}),
        encoding="utf-8",
    )
    (health_dir / "public_broker_close_truth_latest.json").write_text(
        json.dumps({}),
        encoding="utf-8",
    )
    (health_dir / "diamond_retune_truth_check_latest.json").write_text(
        json.dumps(
            {
                "diagnosis": "public_local_focus_mismatch",
                "warnings": [
                    "Public retune focus and local canonical retune receipt disagree.",
                    "Local canonical trade_closes source is thin.",
                ],
                "action_items": [
                    "Refresh or repair the local closed-trade ledger and diamond_retune_status writers before using local retune receipts for operator decisions."
                ],
            }
        ),
        encoding="utf-8",
    )

    advisory = build_retune_advisory(health_dir)

    assert advisory["diagnosis"] == "public_local_focus_mismatch"
    assert advisory["preferred_warning"] == "Public retune focus and local canonical retune receipt disagree."
    assert advisory["preferred_action"] == (
        "Refresh or repair the local closed-trade ledger and diamond_retune_status writers before using local retune receipts for operator decisions."
    )


def test_build_retune_advisory_includes_active_experiment_post_fix_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    (health_dir / "public_diamond_retune_truth_latest.json").write_text(
        json.dumps(
            {
                "surface": {
                    "normalized": {
                        "focus_bot": "mnq_futures_sage",
                        "focus_issue": "broker_pnl_negative",
                        "focus_state": "COLLECT_MORE_SAMPLE",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (health_dir / "public_broker_close_truth_latest.json").write_text(
        json.dumps(
            {
                "surface": {
                    "normalized": {
                        "focus_bot": "mnq_futures_sage",
                        "focus_closed_trade_count": 141,
                        "focus_total_realized_pnl": -1939.75,
                        "focus_profit_factor": 0.3951,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (health_dir / "diamond_retune_truth_check_latest.json").write_text(
        json.dumps({"diagnosis": "public_local_focus_match", "warnings": [], "action_items": []}),
        encoding="utf-8",
    )
    (health_dir / "strategy_experiment_markers.json").write_text(
        json.dumps(
            {
                "bots": {
                    "mnq_futures_sage": {
                        "experiment_id": "partial_profit_disabled",
                        "started_at": "2026-05-16T01:44:06+00:00",
                        "partial_profit_enabled": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    def _fake_load_close_records(**kwargs):  # noqa: ARG001
        return [
            {
                "bot_id": "mnq_futures_sage",
                "ts": "2026-05-16T01:50:00+00:00",
                "realized_r": 1.2,
                "realized_pnl": 120.0,
            },
            {
                "bot_id": "mnq_futures_sage",
                "ts": "2026-05-16T02:10:00+00:00",
                "realized_r": -0.8,
                "realized_pnl": -80.0,
            },
            {
                "bot_id": "mnq_futures_sage",
                "ts": "2026-05-15T23:00:00+00:00",
                "realized_r": -1.0,
                "realized_pnl": -100.0,
            },
        ]

    monkeypatch.setattr(
        "eta_engine.scripts.closed_trade_ledger.load_close_records",
        _fake_load_close_records,
    )

    advisory = build_retune_advisory(health_dir)

    experiment = advisory["active_experiment"]
    assert experiment["experiment_id"] == "partial_profit_disabled"
    assert experiment["partial_profit_enabled"] is False
    assert experiment["post_change_closed_trade_count"] == 2
    assert experiment["post_change_total_realized_pnl"] == 40.0
    assert experiment["post_change_profit_factor"] == 1.5


def test_build_retune_advisory_waits_for_first_post_fix_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    (health_dir / "public_diamond_retune_truth_latest.json").write_text(
        json.dumps(
            {
                "surface": {
                    "normalized": {
                        "focus_bot": "mnq_futures_sage",
                        "focus_issue": "broker_pnl_negative",
                        "focus_state": "COLLECT_MORE_SAMPLE",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (health_dir / "public_broker_close_truth_latest.json").write_text(
        json.dumps({"surface": {"normalized": {"focus_bot": "mnq_futures_sage"}}}),
        encoding="utf-8",
    )
    (health_dir / "diamond_retune_truth_check_latest.json").write_text(
        json.dumps({"diagnosis": "public_local_focus_match", "warnings": [], "action_items": []}),
        encoding="utf-8",
    )
    (health_dir / "strategy_experiment_markers.json").write_text(
        json.dumps(
            {
                "bots": {
                    "mnq_futures_sage": {
                        "experiment_id": "partial_profit_disabled",
                        "started_at": "2026-05-16T01:44:06+00:00",
                        "partial_profit_enabled": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    def _fake_load_close_records(**kwargs):  # noqa: ARG001
        return [
            {
                "bot_id": "mnq_futures_sage",
                "ts": "2026-05-15T20:59:35.998873+00:00",
                "realized_r": -0.5,
                "realized_pnl": -50.0,
            },
        ]

    monkeypatch.setattr(
        "eta_engine.scripts.closed_trade_ledger.load_close_records",
        _fake_load_close_records,
    )

    advisory = build_retune_advisory(health_dir)

    experiment = advisory["active_experiment"]
    assert experiment["post_change_closed_trade_count"] == 0
    assert experiment["awaiting_first_post_change_close"] is True
    assert experiment["latest_pre_change_close_ts"] == "2026-05-15T20:59:35.998873+00:00"
    assert advisory["preferred_warning"] == (
        "No broker-proof closed trades yet for mnq_futures_sage since the active partial_profit_disabled "
        "experiment started at 2026-05-16T01:44:06+00:00."
    )
    assert advisory["preferred_action"] == (
        "Await the first post-fix close for mnq_futures_sage; latest broker-proof close for this bot was "
        "2026-05-15T20:59:35.998873+00:00, before experiment start 2026-05-16T01:44:06+00:00."
    )

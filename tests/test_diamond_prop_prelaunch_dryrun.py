from __future__ import annotations

import json

from eta_engine.scripts import diamond_prop_prelaunch_dryrun as mod


def test_task_registration_no_go_when_core_tasks_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        mod,
        "_collect_task_registration",
        lambda: {
            "available": True,
            "missing": ["ETA-Diamond-LedgerEvery15Min", "ETA-Diamond-PropAllocatorHourly"],
            "nonready": [],
            "tasks": [],
        },
    )

    result = mod._check_task_registration()

    assert result.name == "task_registration"
    assert result.status == "NO_GO"
    assert result.detail["missing"] == [
        "ETA-Diamond-LedgerEvery15Min",
        "ETA-Diamond-PropAllocatorHourly",
    ]


def test_task_registration_hold_when_probe_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        mod,
        "_collect_task_registration",
        lambda: {"available": False, "error": "powershell_unavailable"},
    )

    result = mod._check_task_registration()

    assert result.name == "task_registration"
    assert result.status == "HOLD"
    assert result.detail["error"] == "powershell_unavailable"


def test_task_registration_catalog_covers_freshness_backing_tasks() -> None:
    assert "ETA-Diamond-OpsDashboardHourly" in mod.EXPECTED_SCHEDULED_TASKS
    assert "ETA-Diamond-WatchdogDaily" in mod.EXPECTED_SCHEDULED_TASKS
    assert "ETA-Diamond-PropAlertDispatcherEvery15Min" in mod.EXPECTED_SCHEDULED_TASKS


def test_retune_advisory_reads_public_caches(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path / "var" / "eta_engine" / "state")
    health_dir = mod.STATE_DIR / "health"
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
                        "focus_closed_trade_count": 141,
                        "focus_total_realized_pnl": -1939.75,
                        "focus_profit_factor": 0.3951,
                        "broker_mtd_pnl": 18131.0,
                        "broker_snapshot_source": "ibkr_probe_cache",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (health_dir / "diamond_retune_truth_check_latest.json").write_text(
        json.dumps(
            {
                "diagnosis": "public_local_focus_mismatch",
                "warnings": ["Public retune focus and local canonical retune receipt disagree."],
                "action_items": ["Refresh the canonical trade_closes writer before trusting local-only counts."],
            }
        ),
        encoding="utf-8",
    )

    advisory = mod._retune_advisory()

    assert advisory["available"] is True
    assert advisory["focus_bot"] == "mnq_futures_sage"
    assert advisory["focus_closed_trade_count"] == 141
    assert advisory["broker_mtd_pnl"] == 18131.0
    assert advisory["diagnosis"] == "public_local_focus_mismatch"


def test_run_includes_retune_advisory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mod, "_check_launch_readiness", lambda: mod.SectionResult("launch_readiness", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_drawdown_guard", lambda: mod.SectionResult("drawdown_guard", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_allocator", lambda: mod.SectionResult("allocator", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_freshness", lambda: mod.SectionResult("freshness", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_task_registration", lambda: mod.SectionResult("task_registration", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_supervisor_wiring", lambda: mod.SectionResult("supervisor_wiring", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_alert_channels", lambda: mod.SectionResult("alert_channels", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_wave25_lifecycle", lambda: mod.SectionResult("wave25_lifecycle", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_prop_ready_bots", lambda: mod.SectionResult("prop_ready", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_sizing", lambda prop_ready: mod.SectionResult("sizing", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_feed_sanity", lambda prop_ready: mod.SectionResult("feed_sanity", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_watchdog", lambda prop_ready: mod.SectionResult("watchdog", "GO", "ok"))
    monkeypatch.setattr(mod, "_retune_advisory", lambda: {"available": True, "focus_bot": "mnq_futures_sage"})
    monkeypatch.setattr(mod, "_load_json", lambda path: {"prop_ready_bots": ["bot_a"]})
    monkeypatch.setattr(mod, "OUT_LATEST", tmp_path / "diamond_prop_prelaunch_dryrun_latest.json")

    summary = mod.run()

    assert summary["overall_verdict"] == "GO"
    assert summary["retune_advisory"]["focus_bot"] == "mnq_futures_sage"


def test_print_renders_retune_advisory(capsys) -> None:
    mod._print(
        {
            "ts": "2026-05-15T20:20:00+00:00",
            "overall_verdict": "HOLD",
            "summary": "HOLD -- review",
            "sections": [{"name": "freshness", "status": "HOLD", "rationale": "stale receipt"}],
            "retune_advisory": {
                "available": True,
                "focus_bot": "mnq_futures_sage",
                "focus_issue": "broker_pnl_negative",
                "focus_state": "COLLECT_MORE_SAMPLE",
                "focus_closed_trade_count": 141,
                "focus_total_realized_pnl": -1939.75,
                "focus_profit_factor": 0.3951,
                "broker_mtd_pnl": 18131.0,
                "diagnosis": "public_local_focus_mismatch",
                "preferred_warning": "Public retune focus and local canonical retune receipt disagree.",
            },
        }
    )

    out = capsys.readouterr().out
    assert "broker-backed retune advisory" in out
    assert "focus=mnq_futures_sage state=COLLECT_MORE_SAMPLE issue=broker_pnl_negative" in out
    assert "local drift=public_local_focus_mismatch" in out

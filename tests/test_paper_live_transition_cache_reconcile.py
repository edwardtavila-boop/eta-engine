from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ETA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ETA_DASHBOARD_DISABLE_BROKER_PROBES", "1")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    from eta_engine.deploy.scripts.dashboard_api import app

    return TestClient(app)


def test_cached_paper_live_transition_clears_stale_gateway_only_blocker(app_client, tmp_path, monkeypatch):
    import eta_engine.deploy.scripts.dashboard_api as mod

    now = datetime.now(UTC)
    tws_watchdog_cmd = "python -m eta_engine.scripts.tws_watchdog --host 127.0.0.1 --port 4002"
    monkeypatch.setattr(
        mod,
        "_daily_loss_killswitch_snapshot",
        lambda: {
            "source": "daily_loss_killswitch",
            "status": "tripped",
            "tripped": True,
            "disabled": False,
            "today_pnl_usd": -925.50,
            "limit_usd": -900.0,
            "reason": "day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
            "timezone": "America/New_York",
            "reset_display": "2026-05-16 00:00 EDT",
        },
    )
    monkeypatch.setattr(
        mod,
        "_broker_gateway_snapshot",
        lambda: {
            "status": "connected",
            "detail": "serverVersion=176; clientId=9011",
            "ibkr": {"status": "connected", "healthy": True, "detail": "serverVersion=176; clientId=9011"},
        },
    )
    monkeypatch.setattr(mod, "_cached_live_broker_state_for_gateway_reconcile", lambda: {})
    (tmp_path / "state" / "paper_live_transition_check.json").write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "status": "blocked",
                "critical_ready": False,
                "operator_queue_blocked_count": 3,
                "operator_queue_warning_blocked_count": 2,
                "operator_queue_launch_blocked_count": 1,
                "operator_queue_effective_launch_blocked_count": 1,
                "operator_queue_first_blocker_op_id": "OP-19",
                "operator_queue_first_next_action": tws_watchdog_cmd,
                "operator_queue_first_launch_blocker_op_id": "OP-19",
                "operator_queue_first_launch_next_action": tws_watchdog_cmd,
                "paper_ready_bots": 9,
                "gates": [
                    {"name": "tws_api_4002", "critical": True, "passed": False},
                    {"name": "ibgateway_release_guard", "critical": True, "passed": False},
                    {"name": "op19_gateway_runtime", "critical": True, "passed": False},
                    {"name": "paper_ready_bots", "critical": True, "passed": True},
                ],
            },
        ),
        encoding="utf-8",
    )

    response = app_client.get("/api/jarvis/paper_live_transition")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "paper_live_transition_check_cache"
    assert payload["status"] == "ready_to_launch_paper_live"
    assert payload["critical_ready"] is True
    assert payload["operator_queue_launch_blocked_count"] == 0
    assert payload["operator_queue_effective_launch_blocked_count"] == 0
    assert payload["operator_queue_blocked_count"] == 2
    assert payload["operator_queue_first_blocker_op_id"] is None
    assert payload["operator_queue_first_launch_blocker_op_id"] is None
    assert payload["operator_queue_stale_op19_cleared"] is True
    assert payload["cache_gateway_reconciled"] is True
    assert payload["effective_status"] == "shadow_paper_active"
    assert payload["daily_loss_advisory_active"] is True
    assert "Shadow paper remains live" in payload["effective_detail"]


def test_bot_fleet_summary_uses_reconciled_cached_transition(app_client, tmp_path, monkeypatch):
    import time

    import eta_engine.deploy.scripts.dashboard_api as mod

    now = datetime.now(UTC)
    tws_watchdog_cmd = "python -m eta_engine.scripts.tws_watchdog --host 127.0.0.1 --port 4002"
    monkeypatch.setattr(
        mod,
        "_daily_loss_killswitch_snapshot",
        lambda: {
            "source": "daily_loss_killswitch",
            "status": "tripped",
            "tripped": True,
            "disabled": False,
            "today_pnl_usd": -925.50,
            "limit_usd": -900.0,
            "reason": "day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
            "timezone": "America/New_York",
            "reset_display": "2026-05-16 00:00 EDT",
        },
    )
    monkeypatch.setattr(
        mod,
        "_broker_gateway_snapshot",
        lambda: {
            "status": "connected",
            "detail": "serverVersion=176; clientId=9011",
            "ibkr": {"status": "connected", "healthy": True, "detail": "serverVersion=176; clientId=9011"},
        },
    )
    monkeypatch.setattr(mod, "_cached_live_broker_state_for_gateway_reconcile", lambda: {})
    monkeypatch.setattr(
        mod,
        "_cached_live_broker_state_for_diagnostics",
        lambda: {
            "ready": True,
            "source": "cached_live_broker_state_for_diagnostics",
            "probe_skipped": True,
            "broker_snapshot_source": "ibkr_probe_cache",
            "broker_snapshot_state": "warm",
            "broker_snapshot_age_s": 5.0,
            "today_actual_fills": 1,
            "today_realized_pnl": 0.0,
            "total_unrealized_pnl": 0.0,
            "open_position_count": 0,
            "focus_policy": mod._dashboard_focus_policy_payload(),
            "close_history": mod._close_history_windows([], now=now),
        },
    )
    monkeypatch.setattr(
        mod,
        "_broker_bracket_audit_payload",
        lambda **_: {
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
            "operator_action_required": False,
            "position_summary": {},
        },
    )
    monkeypatch.setattr(mod, "_recent_trade_closes", lambda limit=5000: [])
    with mod._IBKR_PROBE_LOCK:
        mod._IBKR_PROBE_CACHE["snapshot"] = {"ready": True, "open_position_count": 0, "open_positions": []}
        mod._IBKR_PROBE_CACHE["ts"] = time.time()
    (tmp_path / "state" / "paper_live_transition_check.json").write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "status": "blocked",
                "critical_ready": False,
                "operator_queue_blocked_count": 3,
                "operator_queue_warning_blocked_count": 2,
                "operator_queue_launch_blocked_count": 1,
                "operator_queue_effective_launch_blocked_count": 1,
                "operator_queue_first_blocker_op_id": "OP-19",
                "operator_queue_first_next_action": tws_watchdog_cmd,
                "operator_queue_first_launch_blocker_op_id": "OP-19",
                "operator_queue_first_launch_next_action": tws_watchdog_cmd,
                "paper_ready_bots": 9,
                "gates": [
                    {"name": "tws_api_4002", "critical": True, "passed": False},
                    {"name": "ibgateway_release_guard", "critical": True, "passed": False},
                    {"name": "op19_gateway_runtime", "critical": True, "passed": False},
                    {"name": "paper_ready_bots", "critical": True, "passed": True},
                ],
            },
        ),
        encoding="utf-8",
    )

    response = app_client.get("/api/bot-fleet")

    assert response.status_code == 200
    payload = response.json()
    assert payload["paper_live_transition"]["status"] == "ready_to_launch_paper_live"
    assert payload["paper_live_transition"]["effective_status"] == "shadow_paper_active"
    assert payload["paper_live_transition"]["cache_gateway_reconciled"] is True
    assert payload["summary"]["paper_live_status"] == "ready_to_launch_paper_live"
    assert payload["summary"]["paper_live_effective_status"] == "shadow_paper_active"
    assert payload["summary"]["paper_live_critical_ready"] is True
    assert payload["summary"]["paper_live_launch_blocked_count"] == 0
    assert payload["summary"]["paper_live_daily_loss_advisory_active"] is True

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _write_watchdog(path: Path, *, healthy: bool, checked_at: datetime) -> None:
    path.write_text(
        json.dumps(
            {
                "checked_at": checked_at.isoformat(),
                "healthy": healthy,
                "consecutive_failures": 0 if healthy else 8,
                "details": {
                    "host": "127.0.0.1",
                    "port": 4002,
                    "socket_ok": healthy,
                    "handshake_ok": healthy,
                    "handshake_detail": "serverVersion=176" if healthy else "ConnectionRefusedError",
                },
            },
        ),
        encoding="utf-8",
    )


def _write_hold(path: Path, *, active: bool, reason: str) -> None:
    path.write_text(
        json.dumps(
            {
                "active": active,
                "reason": reason,
                "operator": "codex",
            },
        ),
        encoding="utf-8",
    )


def test_unhealthy_watchdog_refuses_to_clear_order_hold(tmp_path: Path) -> None:
    from eta_engine.scripts import ibgateway_release_guard as guard

    now = datetime(2026, 5, 6, 9, 45, tzinfo=UTC)
    watchdog = tmp_path / "tws_watchdog.json"
    hold = tmp_path / "order_entry_hold.json"
    state = tmp_path / "ibgateway_reauth.json"
    _write_watchdog(watchdog, healthy=False, checked_at=now)
    _write_hold(hold, active=True, reason="ibgateway_waiting_for_manual_login_or_2fa")

    result = guard.run_guard(
        tws_status_path=watchdog,
        hold_path=hold,
        reauth_state_path=state,
        execute=True,
        now=now,
    )

    assert result["status"] == "blocked_watchdog_unhealthy"
    assert result["action"] == "none"
    assert result["operator_action_required"] is True
    assert json.loads(hold.read_text(encoding="utf-8"))["active"] is True


def test_execute_clears_ibkr_hold_and_restarts_release_tasks(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import ibgateway_release_guard as guard

    now = datetime(2026, 5, 6, 9, 45, tzinfo=UTC)
    watchdog = tmp_path / "tws_watchdog.json"
    hold = tmp_path / "order_entry_hold.json"
    state = tmp_path / "ibgateway_reauth.json"
    _write_watchdog(watchdog, healthy=True, checked_at=now - timedelta(seconds=10))
    _write_hold(hold, active=True, reason="ibgateway_waiting_for_manual_login_or_2fa")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(guard, "_run_task_command", lambda verb, task: calls.append((verb, task)) or 0)

    result = guard.run_guard(
        tws_status_path=watchdog,
        hold_path=hold,
        reauth_state_path=state,
        execute=True,
        now=now,
    )

    hold_payload = json.loads(hold.read_text(encoding="utf-8"))
    state_payload = json.loads(state.read_text(encoding="utf-8"))
    assert result["status"] == "released"
    assert result["operator_action_required"] is False
    assert hold_payload["active"] is False
    assert hold_payload["reason"] == "ibgateway_manual_login_verified_healthy"
    assert state_payload["status"] == "healthy_released"
    assert calls == [
        ("Enable-ScheduledTask", "ETA-IBGateway-Reauth"),
        ("Start-ScheduledTask", "ETA-Broker-Router"),
        ("Start-ScheduledTask", "ETA-Jarvis-Strategy-Supervisor"),
    ]


def test_dry_run_reports_already_released_when_hold_is_clear(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import ibgateway_release_guard as guard

    now = datetime(2026, 5, 6, 9, 45, tzinfo=UTC)
    watchdog = tmp_path / "tws_watchdog.json"
    hold = tmp_path / "order_entry_hold.json"
    state = tmp_path / "ibgateway_reauth.json"
    _write_watchdog(watchdog, healthy=True, checked_at=now - timedelta(seconds=10))
    _write_hold(hold, active=False, reason="ibgateway_manual_login_verified_healthy")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(guard, "_run_task_command", lambda verb, task: calls.append((verb, task)) or 0)

    result = guard.run_guard(
        tws_status_path=watchdog,
        hold_path=hold,
        reauth_state_path=state,
        execute=False,
        now=now,
    )

    assert result["status"] == "already_released"
    assert result["action"] == "none"
    assert result["operator_action_required"] is False
    assert result["hold"]["active"] is False
    assert result["task_results"] == []
    assert not state.exists()
    assert calls == []


def test_execute_refuses_to_clear_unrelated_operator_hold(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import ibgateway_release_guard as guard

    now = datetime(2026, 5, 6, 9, 45, tzinfo=UTC)
    watchdog = tmp_path / "tws_watchdog.json"
    hold = tmp_path / "order_entry_hold.json"
    state = tmp_path / "ibgateway_reauth.json"
    _write_watchdog(watchdog, healthy=True, checked_at=now)
    _write_hold(hold, active=True, reason="manual_flatten_in_progress")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(guard, "_run_task_command", lambda verb, task: calls.append((verb, task)) or 0)

    result = guard.run_guard(
        tws_status_path=watchdog,
        hold_path=hold,
        reauth_state_path=state,
        execute=True,
        now=now,
    )

    assert result["status"] == "blocked_operator_hold"
    assert "manual_flatten_in_progress" in result["reason"]
    assert json.loads(hold.read_text(encoding="utf-8"))["active"] is True
    assert not state.exists()
    assert calls == []

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _unhealthy_status(*, failures: int = 3, process_running: bool = True) -> dict:
    details: dict = {
        "host": "127.0.0.1",
        "port": 4002,
        "socket_ok": False,
        "handshake_ok": False,
        "handshake_detail": "skipped (socket down)",
    }
    if process_running:
        details["gateway_process"] = {
            "running": True,
            "pid": 8072,
            "name": "ibgateway.exe",
        }
    return {
        "checked_at": "2026-05-05T13:40:00+00:00",
        "healthy": False,
        "consecutive_failures": failures,
        "details": details,
    }


def test_healthy_gateway_does_not_restart() -> None:
    from eta_engine.scripts import ibgateway_reauth_controller as controller

    decision = controller.decide_reauth_action(
        {
            "healthy": True,
            "consecutive_failures": 0,
            "details": {"socket_ok": True, "handshake_ok": True},
        },
        {},
        now=datetime(2026, 5, 5, 13, 40, tzinfo=UTC),
    )

    assert decision["status"] == "healthy"
    assert decision["action"] == "none"
    assert decision["operator_action_required"] is False
    assert decision["restart_attempts"] == 0


def test_process_down_starts_existing_trader_owned_run_now_task(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import ibgateway_reauth_controller as controller

    tws_status = tmp_path / "tws_watchdog.json"
    state_path = tmp_path / "ibgateway_reauth.json"
    tws_status.write_text(json.dumps(_unhealthy_status(process_running=False)), encoding="utf-8")
    started: list[str] = []
    monkeypatch.setattr(controller, "_scheduled_task_exists", lambda _task_name: True)
    monkeypatch.setattr(controller, "_start_scheduled_task", lambda task_name: started.append(task_name) or 0)

    result = controller.run_controller(
        tws_status_path=tws_status,
        state_path=state_path,
        execute=True,
        now=datetime(2026, 5, 5, 13, 40, tzinfo=UTC),
    )

    assert result["status"] == "started_gateway"
    assert result["action"] == "start_gateway"
    assert result["recovery_lane"]["controller_task"] == "ETA-IBGateway-Reauth"
    assert result["recovery_lane"]["next_task"] == "ETA-IBGateway-RunNow"
    assert started == ["ETA-IBGateway-RunNow"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_task_name"] == "ETA-IBGateway-RunNow"
    assert state["operator_action_required"] is False
    assert state["recovery_lane"]["restart_task"] == "ETA-IBGateway-DailyRestart"


def test_process_down_falls_back_to_gateway_task_when_run_now_missing(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import ibgateway_reauth_controller as controller

    tws_status = tmp_path / "tws_watchdog.json"
    state_path = tmp_path / "ibgateway_reauth.json"
    tws_status.write_text(json.dumps(_unhealthy_status(process_running=False)), encoding="utf-8")
    started: list[str] = []
    monkeypatch.setattr(
        controller,
        "_scheduled_task_exists",
        lambda task_name: task_name == controller.GATEWAY_TASK_NAME,
    )
    monkeypatch.setattr(controller, "_start_scheduled_task", lambda task_name: started.append(task_name) or 0)

    result = controller.run_controller(
        tws_status_path=tws_status,
        state_path=state_path,
        execute=True,
        now=datetime(2026, 5, 5, 13, 40, tzinfo=UTC),
    )

    assert result["status"] == "started_gateway"
    assert result["action"] == "start_gateway"
    assert result["last_task_name"] == "ETA-IBGateway"
    assert result["recovery_lane"]["next_task"] == "ETA-IBGateway"
    assert result["recovery_lane"]["start_task_mode"] == "gateway_task_fallback"
    assert "Falling back to ETA-IBGateway" in result["reason"]
    assert started == ["ETA-IBGateway"]


def test_process_down_falls_back_to_gateway_task_when_run_now_disabled(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import ibgateway_reauth_controller as controller

    tws_status = tmp_path / "tws_watchdog.json"
    state_path = tmp_path / "ibgateway_reauth.json"
    tws_status.write_text(json.dumps(_unhealthy_status(process_running=False)), encoding="utf-8")
    started: list[str] = []
    monkeypatch.setattr(controller, "_scheduled_task_exists", lambda _task_name: True)
    monkeypatch.setattr(
        controller,
        "_scheduled_task_is_runnable",
        lambda task_name: task_name == controller.GATEWAY_TASK_NAME,
    )
    monkeypatch.setattr(controller, "_start_scheduled_task", lambda task_name: started.append(task_name) or 0)

    result = controller.run_controller(
        tws_status_path=tws_status,
        state_path=state_path,
        execute=True,
        now=datetime(2026, 5, 5, 13, 40, tzinfo=UTC),
    )

    assert result["status"] == "started_gateway"
    assert result["last_task_name"] == "ETA-IBGateway"
    assert result["recovery_lane"]["next_task"] == "ETA-IBGateway"
    assert result["recovery_lane"]["start_task_mode"] == "gateway_task_fallback"
    assert "missing or disabled" in result["reason"]
    assert started == ["ETA-IBGateway"]


def test_process_down_missing_run_now_task_requires_operator_action(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import ibgateway_reauth_controller as controller

    tws_status = tmp_path / "tws_watchdog.json"
    state_path = tmp_path / "ibgateway_reauth.json"
    tws_status.write_text(json.dumps(_unhealthy_status(process_running=False)), encoding="utf-8")
    monkeypatch.setattr(controller, "_scheduled_task_exists", lambda _task_name: False)

    result = controller.run_controller(
        tws_status_path=tws_status,
        state_path=state_path,
        execute=True,
        now=datetime(2026, 5, 5, 13, 40, tzinfo=UTC),
    )

    assert result["status"] == "missing_recovery_task"
    assert result["action"] == "none"
    assert result["operator_action_required"] is True
    assert "ETA-IBGateway-RunNow" in result["reason"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "missing_recovery_task"


def test_stuck_running_gateway_restarts_once_then_waits_for_ibkr_login(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import ibgateway_reauth_controller as controller

    tws_status = tmp_path / "tws_watchdog.json"
    state_path = tmp_path / "ibgateway_reauth.json"
    tws_status.write_text(json.dumps(_unhealthy_status(failures=4)), encoding="utf-8")
    restarted: list[str] = []
    monkeypatch.setattr(controller, "_scheduled_task_exists", lambda _task_name: True)
    monkeypatch.setattr(controller, "_restart_scheduled_task", lambda task_name: restarted.append(task_name) or 0)
    now = datetime(2026, 5, 5, 13, 40, tzinfo=UTC)

    first = controller.run_controller(tws_status_path=tws_status, state_path=state_path, execute=True, now=now)
    second = controller.run_controller(
        tws_status_path=tws_status,
        state_path=state_path,
        execute=True,
        now=now + timedelta(minutes=5),
    )

    assert first["status"] == "restart_requested"
    assert first["action"] == "restart_gateway"
    assert second["status"] == "auth_pending"
    assert second["action"] == "none"
    assert second["operator_action_required"] is True
    assert "IBKR Gateway login or two-factor" in second["operator_action"]
    assert restarted == ["ETA-IBGateway-DailyRestart"]


def test_wedged_api_listener_restarts_even_when_process_snapshot_missing(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import ibgateway_reauth_controller as controller

    tws_status = tmp_path / "tws_watchdog.json"
    state_path = tmp_path / "ibgateway_reauth.json"
    status = _unhealthy_status(process_running=False)
    status["details"]["socket_ok"] = True
    status["details"]["handshake_ok"] = False
    status["details"]["handshake_detail"] = "attempt 1 clientId=55: TimeoutError()"
    tws_status.write_text(json.dumps(status), encoding="utf-8")
    restarted: list[str] = []
    monkeypatch.setattr(controller, "_scheduled_task_exists", lambda _task_name: True)
    monkeypatch.setattr(controller, "_scheduled_task_is_runnable", lambda _task_name: True)
    monkeypatch.setattr(controller, "_restart_scheduled_task", lambda task_name: restarted.append(task_name) or 0)

    result = controller.run_controller(
        tws_status_path=tws_status,
        state_path=state_path,
        execute=True,
        now=datetime(2026, 5, 5, 13, 40, tzinfo=UTC),
    )

    assert result["status"] == "restart_requested"
    assert result["action"] == "restart_gateway"
    assert result["last_task_name"] == "ETA-IBGateway-DailyRestart"
    assert restarted == ["ETA-IBGateway-DailyRestart"]


def test_restart_scheduled_task_stops_stuck_task_before_starting(monkeypatch) -> None:
    from eta_engine.scripts import ibgateway_reauth_controller as controller

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(controller, "_run_task_command", lambda verb, task_name: calls.append((verb, task_name)) or 0)
    monkeypatch.setattr(controller.time, "sleep", lambda _seconds: None)

    rc = controller._restart_scheduled_task("ETA-IBGateway-DailyRestart")

    assert rc == 0
    assert calls == [
        ("Stop-ScheduledTask", "ETA-IBGateway-DailyRestart"),
        ("Start-ScheduledTask", "ETA-IBGateway-DailyRestart"),
    ]


def test_max_restarts_stops_and_requires_operator_action() -> None:
    from eta_engine.scripts import ibgateway_reauth_controller as controller

    decision = controller.decide_reauth_action(
        _unhealthy_status(failures=9),
        {
            "restart_attempts": 3,
            "last_restart_at": "2026-05-05T12:00:00+00:00",
        },
        now=datetime(2026, 5, 5, 13, 40, tzinfo=UTC),
        max_restart_attempts=3,
    )

    assert decision["status"] == "max_restarts_exceeded"
    assert decision["action"] == "none"
    assert decision["operator_action_required"] is True
    assert "manual IB Gateway recovery" in decision["operator_action"]


def test_reauth_controller_and_registrar_avoid_password_sso_and_legacy_paths() -> None:
    root = Path(__file__).resolve().parents[1]
    controller_text = (root / "scripts" / "ibgateway_reauth_controller.py").read_text(encoding="utf-8")
    registrar_text = (root / "deploy" / "scripts" / "register_ibgateway_reauth_task.ps1").read_text(
        encoding="utf-8",
    )
    combined = f"{controller_text}\n{registrar_text}"

    assert "IBKR_PASSWORD" not in combined
    assert "sso/Login" not in combined
    assert "ssovalidate" not in combined
    assert "C:\\EvolutionaryTradingAlgo" in registrar_text
    assert "OneDrive" not in combined
    assert "LOCALAPPDATA" not in combined
    assert "C:\\mnq_data" not in combined
    assert "C:\\crypto_data" not in combined
    assert "TheFirm" not in combined
    assert "The_Firm" not in combined


def test_bootstrap_registers_canonical_ibgateway_reauth_lane() -> None:
    root = Path(__file__).resolve().parents[1]
    bootstrap_text = (root / "deploy" / "vps_bootstrap.ps1").read_text(encoding="utf-8")
    registrar_text = (root / "deploy" / "scripts" / "register_ibgateway_reauth_task.ps1").read_text(
        encoding="utf-8",
    )

    assert "register_ibgateway_reauth_task.ps1" in bootstrap_text
    assert "ETA-IBGateway-Reauth" in bootstrap_text
    assert "-Start" in bootstrap_text
    assert "register_ibkr_gateway_watchdog_task.ps1" not in bootstrap_text
    assert "[switch]$Start" in registrar_text
    assert "Start-ScheduledTask -TaskName $TaskName" in registrar_text

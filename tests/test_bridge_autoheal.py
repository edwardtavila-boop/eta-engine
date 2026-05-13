"""Tests for bridge_autoheal — periodic self-healing watchdog."""

from __future__ import annotations

import json
import time


def test_autoheal_once_no_actions_when_all_healthy(monkeypatch, tmp_path) -> None:
    """All probes pass → autoheal_once() returns empty action list."""
    from eta_engine.scripts import bridge_autoheal

    # Force all health probes green
    monkeypatch.setattr(bridge_autoheal, "_hermes_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_status_server_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_audit_log_size", lambda: 1024)
    monkeypatch.setattr(bridge_autoheal, "_newest_backup_age_hours", lambda: 1.0)
    monkeypatch.setattr(bridge_autoheal, "AUTOHEAL_LOG", tmp_path / "autoheal.jsonl")

    actions = bridge_autoheal.autoheal_once()
    assert actions == []


def test_autoheal_restarts_hermes_when_down(monkeypatch, tmp_path) -> None:
    """Hermes /health unreachable → restart action recorded."""
    from eta_engine.scripts import bridge_autoheal

    monkeypatch.setattr(bridge_autoheal, "_hermes_healthy", lambda: False)
    monkeypatch.setattr(bridge_autoheal, "_status_server_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_audit_log_size", lambda: 0)
    monkeypatch.setattr(bridge_autoheal, "_newest_backup_age_hours", lambda: 1.0)
    monkeypatch.setattr(bridge_autoheal, "AUTOHEAL_LOG", tmp_path / "autoheal.jsonl")
    monkeypatch.setattr(bridge_autoheal, "_restart_scheduled_task", lambda name: (True, f"restarted {name}"))

    actions = bridge_autoheal.autoheal_once()
    assert len(actions) == 1
    assert actions[0].mode == "hermes_gateway_down"
    assert actions[0].status == "fixed"


def test_autoheal_restarts_status_server_when_down(monkeypatch, tmp_path) -> None:
    """Status server down → restart action recorded."""
    from eta_engine.scripts import bridge_autoheal

    monkeypatch.setattr(bridge_autoheal, "_hermes_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_status_server_healthy", lambda: False)
    monkeypatch.setattr(bridge_autoheal, "_audit_log_size", lambda: 0)
    monkeypatch.setattr(bridge_autoheal, "_newest_backup_age_hours", lambda: 1.0)
    monkeypatch.setattr(bridge_autoheal, "AUTOHEAL_LOG", tmp_path / "autoheal.jsonl")
    monkeypatch.setattr(bridge_autoheal, "_restart_scheduled_task", lambda name: (True, "ok"))

    actions = bridge_autoheal.autoheal_once()
    modes = {a.mode for a in actions}
    assert "status_server_down" in modes


def test_autoheal_rotates_oversize_audit_log(monkeypatch, tmp_path) -> None:
    """Audit log > threshold → rotation action recorded."""
    from eta_engine.scripts import bridge_autoheal

    monkeypatch.setattr(bridge_autoheal, "_hermes_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_status_server_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_audit_log_size", lambda: 100 * 1024 * 1024)  # 100MB
    monkeypatch.setattr(bridge_autoheal, "_newest_backup_age_hours", lambda: 1.0)
    monkeypatch.setattr(bridge_autoheal, "AUTOHEAL_LOG", tmp_path / "autoheal.jsonl")
    monkeypatch.setattr(bridge_autoheal, "_force_rotate_audit_log", lambda: (True, "rotated to .gz"))

    actions = bridge_autoheal.autoheal_once()
    rot = [a for a in actions if a.mode == "audit_log_oversize"]
    assert len(rot) == 1
    assert rot[0].status == "fixed"


def test_autoheal_fires_backup_when_stale(monkeypatch, tmp_path) -> None:
    """Newest backup older than 48h → backup action recorded."""
    from eta_engine.scripts import bridge_autoheal

    monkeypatch.setattr(bridge_autoheal, "_hermes_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_status_server_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_audit_log_size", lambda: 0)
    monkeypatch.setattr(bridge_autoheal, "_newest_backup_age_hours", lambda: 72.0)
    monkeypatch.setattr(bridge_autoheal, "AUTOHEAL_LOG", tmp_path / "autoheal.jsonl")
    monkeypatch.setattr(bridge_autoheal, "_run_memory_backup", lambda: (True, "backup ok"))

    actions = bridge_autoheal.autoheal_once()
    backups = [a for a in actions if a.mode == "memory_backup_stale"]
    assert len(backups) == 1


def test_autoheal_fires_backup_when_missing(monkeypatch, tmp_path) -> None:
    """No backup at all → backup_missing action recorded."""
    from eta_engine.scripts import bridge_autoheal

    monkeypatch.setattr(bridge_autoheal, "_hermes_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_status_server_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_audit_log_size", lambda: 0)
    monkeypatch.setattr(bridge_autoheal, "_newest_backup_age_hours", lambda: None)
    monkeypatch.setattr(bridge_autoheal, "AUTOHEAL_LOG", tmp_path / "autoheal.jsonl")
    monkeypatch.setattr(bridge_autoheal, "_run_memory_backup", lambda: (True, "first backup"))

    actions = bridge_autoheal.autoheal_once()
    missing = [a for a in actions if a.mode == "memory_backup_missing"]
    assert len(missing) == 1


def test_autoheal_records_failed_fix(monkeypatch, tmp_path) -> None:
    """Fix returns (False, ...) → action.status='failed', not exception."""
    from eta_engine.scripts import bridge_autoheal

    monkeypatch.setattr(bridge_autoheal, "_hermes_healthy", lambda: False)
    monkeypatch.setattr(bridge_autoheal, "_status_server_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_audit_log_size", lambda: 0)
    monkeypatch.setattr(bridge_autoheal, "_newest_backup_age_hours", lambda: 1.0)
    monkeypatch.setattr(bridge_autoheal, "AUTOHEAL_LOG", tmp_path / "autoheal.jsonl")
    monkeypatch.setattr(bridge_autoheal, "_restart_scheduled_task", lambda name: (False, "schtasks not available"))

    actions = bridge_autoheal.autoheal_once()
    assert len(actions) == 1
    assert actions[0].status == "failed"


def test_autoheal_writes_actions_to_log(monkeypatch, tmp_path) -> None:
    """Each action appends a JSONL line to the autoheal log."""
    from eta_engine.scripts import bridge_autoheal

    autoheal_log = tmp_path / "autoheal.jsonl"
    monkeypatch.setattr(bridge_autoheal, "_hermes_healthy", lambda: False)
    monkeypatch.setattr(bridge_autoheal, "_status_server_healthy", lambda: False)
    monkeypatch.setattr(bridge_autoheal, "_audit_log_size", lambda: 0)
    monkeypatch.setattr(bridge_autoheal, "_newest_backup_age_hours", lambda: 1.0)
    monkeypatch.setattr(bridge_autoheal, "AUTOHEAL_LOG", autoheal_log)
    monkeypatch.setattr(bridge_autoheal, "_restart_scheduled_task", lambda name: (True, "ok"))

    bridge_autoheal.autoheal_once()

    assert autoheal_log.exists()
    lines = autoheal_log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2  # one for each restart action
    for line in lines:
        rec = json.loads(line)
        assert "mode" in rec
        assert "status" in rec


def test_recent_actions_filters_by_window(monkeypatch, tmp_path) -> None:
    """recent_actions(since_hours=N) returns only entries newer than N hours."""
    from datetime import UTC, datetime, timedelta

    from eta_engine.scripts import bridge_autoheal

    autoheal_log = tmp_path / "autoheal.jsonl"
    now = datetime.now(UTC)
    with autoheal_log.open("w", encoding="utf-8") as fh:
        # 2 fresh (within last hour), 2 old (5 days ago)
        for hours_ago in (0.5, 0.2, 120, 121):
            ts = (now - timedelta(hours=hours_ago)).isoformat()
            fh.write(
                json.dumps(
                    {
                        "asof": ts,
                        "mode": "test",
                        "detection": "synthetic",
                        "action": "test",
                        "status": "fixed",
                    }
                )
                + "\n"
            )

    monkeypatch.setattr(bridge_autoheal, "AUTOHEAL_LOG", autoheal_log)
    recent = bridge_autoheal.recent_actions(since_hours=24)
    assert len(recent) == 2


def test_recent_actions_returns_empty_when_log_missing(monkeypatch, tmp_path) -> None:
    from eta_engine.scripts import bridge_autoheal

    monkeypatch.setattr(bridge_autoheal, "AUTOHEAL_LOG", tmp_path / "no_log.jsonl")
    assert bridge_autoheal.recent_actions() == []


def test_main_once_mode_returns_zero(monkeypatch, tmp_path, capsys) -> None:
    """main(['--once', '--json']) runs one cycle and prints JSON."""
    from eta_engine.scripts import bridge_autoheal

    monkeypatch.setattr(bridge_autoheal, "_hermes_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_status_server_healthy", lambda: True)
    monkeypatch.setattr(bridge_autoheal, "_audit_log_size", lambda: 0)
    monkeypatch.setattr(bridge_autoheal, "_newest_backup_age_hours", lambda: 1.0)
    monkeypatch.setattr(bridge_autoheal, "AUTOHEAL_LOG", tmp_path / "autoheal.jsonl")

    rc = bridge_autoheal.main(["--once", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    # JSON output is parseable
    actions = json.loads(captured.out)
    assert actions == []


def test_port_listening_helper_open_port(monkeypatch) -> None:
    """_port_listening returns True for an open port."""
    import http.server
    import socket
    import threading

    from eta_engine.scripts import bridge_autoheal

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    srv = http.server.HTTPServer(("127.0.0.1", port), http.server.BaseHTTPRequestHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.05)
        assert bridge_autoheal._port_listening("127.0.0.1", port) is True
    finally:
        srv.shutdown()


def test_port_listening_helper_closed_port() -> None:
    """_port_listening returns False for a closed port."""
    from eta_engine.scripts import bridge_autoheal

    # Port 1 is reserved on most systems; will refuse
    assert bridge_autoheal._port_listening("127.0.0.1", 1) is False

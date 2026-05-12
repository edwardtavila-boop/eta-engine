"""Tests for bridge_preflight — the live-cutover gate check."""
from __future__ import annotations

import json
from pathlib import Path


def test_check_audit_log_sane_passes_for_normal_log(tmp_path: Path, monkeypatch) -> None:
    """Normal-sized audit log under threshold → PASS."""
    from eta_engine.scripts import bridge_preflight

    log = tmp_path / "hermes_actions.jsonl"
    log.write_text('{"x":1}\n' * 100, encoding="utf-8")
    monkeypatch.setattr(bridge_preflight, "STATE_ROOT", tmp_path)
    status, detail, _ = bridge_preflight.check_audit_log_sane()
    assert status == "PASS"


def test_check_audit_log_sane_fails_when_huge(tmp_path: Path, monkeypatch) -> None:
    """Audit log over 50MB → FAIL (rotation broken)."""
    from eta_engine.scripts import bridge_preflight

    log = tmp_path / "hermes_actions.jsonl"
    # Write 60MB of junk to exceed AUDIT_LOG_MAX_OK_BYTES
    with log.open("wb") as fh:
        fh.write(b"x" * (60 * 1024 * 1024))
    monkeypatch.setattr(bridge_preflight, "STATE_ROOT", tmp_path)
    status, _, _ = bridge_preflight.check_audit_log_sane()
    assert status == "FAIL"


def test_check_memory_backup_warns_when_missing(tmp_path: Path, monkeypatch) -> None:
    """No backup dir → WARN (not yet ran), not FAIL."""
    from eta_engine.scripts import bridge_preflight

    monkeypatch.setattr(bridge_preflight, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(bridge_preflight.sys, "platform", "linux")
    status, _, _ = bridge_preflight.check_memory_backup_recent()
    assert status == "WARN"


def test_check_memory_backup_passes_when_recent(tmp_path: Path, monkeypatch) -> None:
    """Recent backup file → PASS."""
    from eta_engine.scripts import bridge_preflight

    backup_dir = tmp_path / "backups" / "hermes_memory"
    backup_dir.mkdir(parents=True)
    (backup_dir / "hermes_memory_20260512T040000Z.db").write_bytes(b"sqlite")
    monkeypatch.setattr(bridge_preflight, "STATE_ROOT", tmp_path)
    status, detail, extras = bridge_preflight.check_memory_backup_recent()
    assert status == "PASS"
    assert extras["count"] == 1


def test_verdict_ready_when_all_pass() -> None:
    """All PASS → READY."""
    from eta_engine.scripts import bridge_preflight

    results = [
        bridge_preflight.CheckResult(name="a", severity="critical", status="PASS", detail=""),
        bridge_preflight.CheckResult(name="b", severity="warning",  status="PASS", detail=""),
    ]
    assert bridge_preflight.verdict(results) == "READY"


def test_verdict_not_ready_when_critical_fails() -> None:
    """One critical FAIL → NOT_READY regardless of warnings."""
    from eta_engine.scripts import bridge_preflight

    results = [
        bridge_preflight.CheckResult(name="a", severity="critical", status="FAIL", detail=""),
        bridge_preflight.CheckResult(name="b", severity="warning",  status="PASS", detail=""),
    ]
    assert bridge_preflight.verdict(results) == "NOT_READY"


def test_verdict_concerns_when_only_warnings_fail() -> None:
    """Warnings only → READY_WITH_CONCERNS."""
    from eta_engine.scripts import bridge_preflight

    results = [
        bridge_preflight.CheckResult(name="a", severity="critical", status="PASS", detail=""),
        bridge_preflight.CheckResult(name="b", severity="warning",  status="WARN", detail=""),
    ]
    assert bridge_preflight.verdict(results) == "READY_WITH_CONCERNS"


def test_check_result_is_blocker_only_for_critical_failures() -> None:
    """is_blocker() returns True only for critical FAIL/WARN/SKIP."""
    from eta_engine.scripts import bridge_preflight

    crit_fail = bridge_preflight.CheckResult(name="x", severity="critical", status="FAIL", detail="")
    crit_pass = bridge_preflight.CheckResult(name="x", severity="critical", status="PASS", detail="")
    warn_fail = bridge_preflight.CheckResult(name="x", severity="warning",  status="FAIL", detail="")
    assert crit_fail.is_blocker()
    assert not crit_pass.is_blocker()
    assert not warn_fail.is_blocker()


def test_render_table_handles_empty_results() -> None:
    """Empty results list → no exception in render path."""
    from eta_engine.scripts import bridge_preflight

    text = bridge_preflight.render_table([])
    assert "BRIDGE PRE-FLIGHT" in text
    assert "VERDICT:" in text


def test_render_table_includes_verdict_line() -> None:
    from eta_engine.scripts import bridge_preflight

    results = [
        bridge_preflight.CheckResult(name="a", severity="critical", status="PASS", detail="ok"),
    ]
    text = bridge_preflight.render_table(results)
    assert "READY" in text
    assert "PASS" in text or "[ OK ]" in text


def test_run_all_catches_check_exceptions(monkeypatch) -> None:
    """A check that raises is wrapped as FAIL — never crashes the runner."""
    from eta_engine.scripts import bridge_preflight

    def explode(*a, **kw):
        raise RuntimeError("simulated check failure")

    monkeypatch.setattr(bridge_preflight, "check_tunnel", explode)
    monkeypatch.setattr(bridge_preflight, "check_gateway", explode)
    monkeypatch.setattr(bridge_preflight, "check_llm_latency", explode)
    monkeypatch.setattr(bridge_preflight, "check_write_back_round_trip", explode)
    monkeypatch.setattr(bridge_preflight, "check_credential_pool_is_literal", explode)
    monkeypatch.setattr(bridge_preflight, "check_tunnel_uptime", explode)
    monkeypatch.setattr(bridge_preflight, "check_scheduled_tasks_alive", explode)
    monkeypatch.setattr(bridge_preflight, "check_audit_log_sane", explode)
    monkeypatch.setattr(bridge_preflight, "check_memory_backup_recent", explode)
    monkeypatch.setattr(bridge_preflight, "check_disk_headroom", explode)
    monkeypatch.setattr(bridge_preflight, "check_kelly_recommendations_present", explode)
    monkeypatch.setattr(bridge_preflight, "check_status_server", explode)
    monkeypatch.setattr(bridge_preflight, "check_health_check_passes", explode)

    results = bridge_preflight.run_all()
    # Every result is FAIL, none raised
    assert all(r.status == "FAIL" for r in results)
    # Verdict is NOT_READY (critical failures)
    assert bridge_preflight.verdict(results) == "NOT_READY"


def test_main_exits_nonzero_when_not_ready(tmp_path: Path, monkeypatch, capsys) -> None:
    """End-to-end: main() returns 1 when verdict is NOT_READY."""
    from eta_engine.scripts import bridge_preflight

    # Sabotage every check so verdict is NOT_READY
    def force_fail(*a, **kw):
        return "FAIL", "simulated", {}

    for attr in (
        "check_tunnel", "check_gateway", "check_llm_latency",
        "check_credential_pool_is_literal", "check_write_back_round_trip",
        "check_health_check_passes",
    ):
        monkeypatch.setattr(bridge_preflight, attr, force_fail)

    rc = bridge_preflight.main([
        "--host", "127.0.0.1", "--port", "1",
        "--skip", (
            "tunnel_uptime,scheduled_tasks,audit_log,memory_backup,"
            "disk_headroom,kelly_ready,status_server"
        ),
    ])
    captured = capsys.readouterr()
    assert "NOT_READY" in captured.out
    assert rc == 1


def test_main_json_mode_emits_parseable_payload(monkeypatch, capsys) -> None:
    """--json emits a valid JSON document with checks + verdict."""
    from eta_engine.scripts import bridge_preflight

    monkeypatch.setattr(bridge_preflight, "_resolve_api_key", lambda: None)

    skip_checks = (
        "tunnel,gateway,llm_latency,credential_literal,write_back,scheduled_tasks,"
        "tunnel_uptime,audit_log,memory_backup,disk_headroom,kelly_ready,"
        "status_server,health_9_layers"
    )
    rc = bridge_preflight.main([
        "--host", "127.0.0.1", "--port", "1",
        "--skip", skip_checks,
        "--json",
    ])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "verdict" in payload
    assert "checks" in payload
    assert isinstance(payload["checks"], list)
    # With everything skipped, no checks ran → READY by default
    assert payload["verdict"] == "READY"
    assert rc == 0

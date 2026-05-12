from __future__ import annotations

from pathlib import Path

from eta_engine.scripts import jarvis_hermes_admin_audit as audit


def _write(path: Path, text: str = "marker\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _minimal_workspace(tmp_path: Path, *, task_count: int = 8) -> Path:
    workspace = tmp_path / "workspace"
    engine = workspace / "eta_engine"
    plan = workspace / "docs" / "superpowers" / "plans" / "2026-05-11-jarvis-hermes-bridge.md"
    _write(plan, "\n".join(f"### Task {idx}: thing" for idx in range(1, task_count + 1)))

    _write(
        engine / "mcp_servers" / "jarvis_mcp_server.py",
        "\n".join(
            [
                *audit.CORE_MCP_TOOLS,
                *audit.DESTRUCTIVE_MARKERS,
                "dispatch_tool_call",
            ],
        ),
    )
    _write(
        engine / "brain" / "jarvis_v3" / "hermes_client.py",
        "\n".join(audit.HERMES_CLIENT_MARKERS),
    )
    for relative_path, markers in audit.HOT_PATH_MARKERS.items():
        _write(engine / relative_path, "\n".join(markers))
    for relative_path in audit.REQUIRED_TESTS:
        _write(engine / relative_path)
    for relative_path in audit.REQUIRED_SKILL_FILES:
        _write(engine / relative_path)
    return workspace


def test_admin_audit_passes_minimal_safe_bridge(tmp_path: Path) -> None:
    workspace = _minimal_workspace(tmp_path)

    report = audit.run_audit(workspace, probe_port=False)

    assert report["status"] == "PASS"
    assert report["summary"]["admin_ai_ready"] is True
    assert report["summary"]["order_action_allowed"] is False
    assert report["summary"]["live_money_gate_bypassed"] is False


def test_admin_audit_blocks_missing_destructive_confirm_marker(tmp_path: Path) -> None:
    workspace = _minimal_workspace(tmp_path)
    server = workspace / "eta_engine" / "mcp_servers" / "jarvis_mcp_server.py"
    server.write_text(server.read_text(encoding="utf-8").replace("confirm_phrase_mismatch", ""), encoding="utf-8")

    report = audit.run_audit(workspace, probe_port=False)

    assert report["status"] == "BLOCKED"
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["mcp_destructive_safety"]["status"] == "BLOCKED"
    assert "confirm_phrase_mismatch" in checks["mcp_destructive_safety"]["evidence"]["missing"]


def test_admin_audit_warns_when_t17_wave_not_represented(tmp_path: Path) -> None:
    workspace = _minimal_workspace(tmp_path, task_count=8)

    report = audit.run_audit(workspace, expected_task_count=17, probe_port=False)

    assert report["status"] == "WARN"
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["bridge_plan_tasks"]["status"] == "WARN"
    assert checks["bridge_plan_tasks"]["evidence"]["actual_task_count"] == 8
    assert checks["bridge_plan_tasks"]["evidence"]["expected_task_count"] == 17

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from eta_engine.scripts import codex_overnight_operator as operator
from eta_engine.scripts.agent_coordinator import AgentCoordinator


def _write_pending_task(root: Path, task_id: str, *, preferred_agent: str = "codex") -> None:
    payload = {
        "id": task_id,
        "title": f"task {task_id}",
        "created": "2026-05-05T00:00:00Z",
        "created_by": "test",
        "priority": "P1",
        "preferred_agent": preferred_agent,
        "deliverables": ["report"],
        "constraints": ["canonical writes only"],
        "status": "pending",
        "agent": None,
        "claimed_at": None,
        "completed_at": None,
        "notes": [],
        "deliverable_refs": [],
    }
    path = root / "tasks" / "pending" / f"{task_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _age_heartbeat(root: Path, agent: str, age: timedelta) -> None:
    hb_path = root / "agents" / f"{agent}.heartbeat.json"
    payload = json.loads(hb_path.read_text(encoding="utf-8"))
    payload["ts"] = (datetime.now(UTC) - age).strftime("%Y-%m-%dT%H:%M:%SZ")
    hb_path.write_text(json.dumps(payload), encoding="utf-8")


def test_operator_cycle_emits_heartbeat_and_reports(tmp_path: Path) -> None:
    coordination = tmp_path / "coordination"
    reports = tmp_path / "reports"
    workspace = tmp_path / "workspace"
    eta_root = workspace / "eta_engine"
    eta_root.mkdir(parents=True)
    _write_pending_task(coordination, "T-CODEX-1")

    paths = operator.CodexOperatorPaths(
        workspace_root=workspace,
        eta_engine_root=eta_root,
        coordination_state_root=coordination,
        report_dir=reports,
    )

    report = operator.run_operator_cycle(paths=paths, run_health=False)

    assert (coordination / "agents" / "codex.heartbeat.json").exists()
    assert report.codex_pending_tasks[0]["id"] == "T-CODEX-1"
    latest = json.loads((reports / "codex_operator_latest.json").read_text(encoding="utf-8"))
    assert latest["cycle_id"] == report.cycle_id
    assert latest["safety"]["no_live_orders"] is True
    assert (reports / "codex_operator_history.jsonl").exists()


def test_operator_cycle_reclaims_stale_ai_tasks(tmp_path: Path) -> None:
    coordination = tmp_path / "coordination"
    reports = tmp_path / "reports"
    workspace = tmp_path / "workspace"
    eta_root = workspace / "eta_engine"
    eta_root.mkdir(parents=True)
    _write_pending_task(coordination, "T-STALE-1", preferred_agent="claude")

    claude = AgentCoordinator("claude", state_root=coordination)
    claude.claim("T-STALE-1")
    claude.emit_heartbeat()
    _age_heartbeat(coordination, "claude", timedelta(minutes=90))

    paths = operator.CodexOperatorPaths(
        workspace_root=workspace,
        eta_engine_root=eta_root,
        coordination_state_root=coordination,
        report_dir=reports,
    )
    report = operator.run_operator_cycle(paths=paths, run_health=False, max_stale_min=30)

    assert report.reclaimed_tasks == ["T-STALE-1"]
    assert (coordination / "tasks" / "pending" / "T-STALE-1.yaml").exists()


def test_git_surface_reports_dirty_count(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "x.txt").write_text("x\n", encoding="utf-8")

    surface = operator._git_surface(repo)

    assert surface.status == "dirty"
    assert surface.dirty_count == 1


def test_main_writes_report_and_returns_zero(tmp_path: Path, capsys) -> None:
    coordination = tmp_path / "coordination"
    reports = tmp_path / "reports"
    workspace = tmp_path / "workspace"
    eta_root = workspace / "eta_engine"
    eta_root.mkdir(parents=True)

    rc = operator.main(
        [
            "--workspace-root",
            str(workspace),
            "--eta-engine-root",
            str(eta_root),
            "--state-root",
            str(coordination),
            "--report-dir",
            str(reports),
            "--no-health",
        ]
    )

    assert rc == 0
    assert "health=skipped" in capsys.readouterr().out
    assert (reports / "codex_operator_latest.json").exists()

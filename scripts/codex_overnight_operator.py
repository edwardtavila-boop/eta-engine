"""Codex overnight operator loop for safe unattended ETA coordination.

This is intentionally conservative. It does not trade, mutate live routing,
or run git. The cycle makes Codex visible as an operator on the VPS by:

- emitting a Codex heartbeat into the shared three-AI coordination queue,
- reclaiming tasks from stale AI sessions,
- recording pending work visible to Codex,
- optionally running the existing VPS health check, and
- writing a canonical latest report plus append-only JSONL history.

All default writes stay under C:/EvolutionaryTradingAlgo/var/eta_engine/state.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ETA_ENGINE_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ETA_ENGINE_ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts.agent_coordinator import (  # noqa: E402
    DEFAULT_STATE_ROOT as DEFAULT_COORDINATION_STATE_ROOT,
)
from eta_engine.scripts.agent_coordinator import AgentCoordinator  # noqa: E402
from eta_engine.scripts.workspace_roots import ETA_RUNTIME_STATE_DIR  # noqa: E402

DEFAULT_REPORT_DIR = ETA_RUNTIME_STATE_DIR / "codex_operator"
GIT_LOCAL_ENV_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)


@dataclass(frozen=True)
class GitSurface:
    """Small, non-invasive git status summary for operator reports."""

    path: str
    branch: str = "unknown"
    dirty_count: int = 0
    status: str = "unknown"
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "branch": self.branch,
            "dirty_count": self.dirty_count,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CodexOperatorPaths:
    """Resolved filesystem roots used by one operator cycle."""

    workspace_root: Path = WORKSPACE_ROOT
    eta_engine_root: Path = ETA_ENGINE_ROOT
    coordination_state_root: Path = DEFAULT_COORDINATION_STATE_ROOT
    report_dir: Path = DEFAULT_REPORT_DIR


@dataclass
class CodexOperatorReport:
    """Serializable report emitted after each operator cycle."""

    cycle_id: str = field(default_factory=lambda: f"CODEX-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}")
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    mode: str = "observe_coordinate_verify"
    safety: dict[str, object] = field(
        default_factory=lambda: {
            "no_live_orders": True,
            "no_git_mutation": True,
            "canonical_write_root": "C:/EvolutionaryTradingAlgo",
        }
    )
    reclaimed_tasks: list[str] = field(default_factory=list)
    codex_pending_tasks: list[dict[str, object]] = field(default_factory=list)
    git_surfaces: list[GitSurface] = field(default_factory=list)
    health: dict[str, object] = field(default_factory=dict)
    next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "cycle_id": self.cycle_id,
            "ts": self.ts,
            "mode": self.mode,
            "safety": self.safety,
            "reclaimed_tasks": self.reclaimed_tasks,
            "codex_pending_tasks": self.codex_pending_tasks,
            "git_surfaces": [surface.to_dict() for surface in self.git_surfaces],
            "health": self.health,
            "next_actions": self.next_actions,
        }


def _git_surface(path: Path) -> GitSurface:
    """Return a bounded git status summary without mutating the repo."""

    git_env = os.environ.copy()
    for name in GIT_LOCAL_ENV_VARS:
        git_env.pop(name, None)

    try:
        result = subprocess.run(
            ["git", "status", "--branch", "--short"],
            cwd=path,
            env=git_env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitSurface(path=str(path), status="unknown", detail=str(exc))

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:500]
        return GitSurface(path=str(path), status="unknown", detail=detail)

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    branch = lines[0].removeprefix("## ").strip() if lines else "unknown"
    dirty = [line for line in lines[1:] if line.strip()]
    status = "clean" if not dirty else "dirty"
    return GitSurface(
        path=str(path),
        branch=branch,
        dirty_count=len(dirty),
        status=status,
        detail=f"{len(dirty)} uncommitted entries" if dirty else "clean",
    )


def _run_health_probe(report_dir: Path) -> dict[str, object]:
    """Run the existing VPS health check and normalize its result."""

    try:
        from eta_engine.scripts.health_check import run_health_check

        health_dir = report_dir / "health"
        report = run_health_check(output_dir=health_dir)
        return report.to_dict()
    except Exception as exc:  # pragma: no cover - defensive VPS safety net.
        return {
            "overall_status": "unknown",
            "exit_code": 0,
            "detail": f"health probe unavailable: {exc}",
        }


def _task_summary(task: dict[str, Any]) -> dict[str, object]:
    return {
        "id": str(task.get("id", "")),
        "title": str(task.get("title", "")),
        "priority": str(task.get("priority", "")),
        "preferred_agent": str(task.get("preferred_agent", "any")),
        "status": str(task.get("status", "")),
    }


def _next_actions(
    *,
    pending_count: int,
    reclaimed_count: int,
    git_surfaces: list[GitSurface],
    health: dict[str, object],
) -> list[str]:
    actions: list[str] = []
    if reclaimed_count:
        actions.append("Reclaimed stale AI tasks; next agent should review pending queue before claiming.")
    if pending_count:
        actions.append("Codex-visible tasks are pending; claim one highest-priority task in the next attended loop.")
    dirty = [surface for surface in git_surfaces if surface.status == "dirty"]
    if dirty:
        actions.append(
            "Workspace has dirty surfaces; keep future commits narrowly scoped and avoid sweeping user edits."
        )
    if health.get("overall_status") in {"warning", "critical"}:
        actions.append(
            "Health check reported degraded state; prioritize the listed action items before live promotion."
        )
    if not actions:
        actions.append("No immediate operator blockers detected; continue highest-value safe batch selection.")
    return actions


def write_operator_report(report: CodexOperatorReport, report_dir: Path) -> None:
    """Write latest JSON and append-only JSONL history."""

    report_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report.to_dict(), indent=2, default=str) + "\n"
    (report_dir / "codex_operator_latest.json").write_text(payload, encoding="utf-8")
    with (report_dir / "codex_operator_history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report.to_dict(), default=str) + "\n")


def run_operator_cycle(
    *,
    paths: CodexOperatorPaths | None = None,
    run_health: bool = True,
    max_stale_min: int | None = None,
) -> CodexOperatorReport:
    """Run one safe unattended Codex coordination cycle."""

    resolved = paths or CodexOperatorPaths()
    coordinator = AgentCoordinator("codex", state_root=resolved.coordination_state_root)
    coordinator.emit_heartbeat()
    reclaimed = coordinator.reclaim_stale(max_age_min=max_stale_min)
    pending = coordinator.list_pending(preferred_agent="codex")

    git_surfaces = [
        _git_surface(resolved.workspace_root),
        _git_surface(resolved.eta_engine_root),
    ]
    health = _run_health_probe(resolved.report_dir) if run_health else {"overall_status": "skipped", "exit_code": 0}

    report = CodexOperatorReport(
        reclaimed_tasks=reclaimed,
        codex_pending_tasks=[_task_summary(task) for task in pending],
        git_surfaces=git_surfaces,
        health=health,
    )
    report.next_actions = _next_actions(
        pending_count=len(pending),
        reclaimed_count=len(reclaimed),
        git_surfaces=git_surfaces,
        health=health,
    )
    write_operator_report(report, resolved.report_dir)
    return report


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one safe Codex overnight operator cycle.")
    parser.add_argument("--workspace-root", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument("--eta-engine-root", type=Path, default=ETA_ENGINE_ROOT)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_COORDINATION_STATE_ROOT)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--max-stale-min", type=int, default=None)
    parser.add_argument("--no-health", action="store_true", help="skip health_check.py integration")
    parser.add_argument("--json", action="store_true", help="print full JSON report")
    parser.add_argument(
        "--strict-exit",
        action="store_true",
        help="return the health-check exit code instead of always succeeding",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    paths = CodexOperatorPaths(
        workspace_root=args.workspace_root,
        eta_engine_root=args.eta_engine_root,
        coordination_state_root=args.state_root,
        report_dir=args.report_dir,
    )
    report = run_operator_cycle(
        paths=paths,
        run_health=not args.no_health,
        max_stale_min=args.max_stale_min,
    )
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(
            f"{report.cycle_id}: reclaimed={len(report.reclaimed_tasks)} "
            f"pending={len(report.codex_pending_tasks)} health={payload['health'].get('overall_status')}"
        )
    if args.strict_exit:
        return int(payload["health"].get("exit_code", 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only JARVIS/Hermes admin-AI integration audit.

This script is intentionally not a control surface. It never sends broker
orders, never toggles live-money gates, and never reads or prints secret
values. It checks whether the repo has the wiring, tests, and safety rails
needed for JARVIS + Hermes to operate as a VPS admin AI in a fail-closed way.
"""

from __future__ import annotations

import argparse
import json
import socket
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_WORKSPACE_ROOT = Path(r"C:\EvolutionaryTradingAlgo")
DEFAULT_HERMES_PORT = 8642

CORE_MCP_TOOLS = (
    "jarvis_fleet_status",
    "jarvis_trace_tail",
    "jarvis_wiring_audit",
    "jarvis_portfolio_assess",
    "jarvis_hot_weights",
    "jarvis_upcoming_events",
    "jarvis_kaizen_run",
    "jarvis_deploy_strategy",
    "jarvis_retire_strategy",
    "jarvis_kill_switch",
    "jarvis_explain_verdict",
)

DESTRUCTIVE_MARKERS = (
    "_KILL_PHRASE",
    "kill all",
    "confirm_phrase_mismatch",
    "_previous_retire_targets",
    "awaiting_confirmation",
    "_scrub_args",
    "_AUDIT_LOG_PATH",
    "_HERMES_STATE_PATH",
)

HERMES_CLIENT_MARKERS = (
    "HermesResult",
    "DEFAULT_TIMEOUT_S",
    "_BACKOFF_FAIL_THRESHOLD",
    "_BACKOFF_DURATION_S",
    "HERMES_TOKEN",
    "Authorization",
    "def narrative",
    "def web_search",
    "def memory_persist",
    "def memory_recall",
    "def health",
)

HOT_PATH_MARKERS = {
    "brain/jarvis_v3/jarvis_conductor.py": ("hermes_calls",),
    "brain/jarvis_v3/jarvis_full.py": ("hermes_client", "narrative"),
    "brain/jarvis_v3/context_enricher.py": ("hermes_client", "web_search", "news_snippets"),
    "brain/jarvis_v3/hot_learner.py": ("memory_persist", "memory_recall"),
    "brain/jarvis_v3/trace_emitter.py": ("hermes_calls",),
}

REQUIRED_TESTS = (
    "tests/test_jarvis_mcp_server.py",
    "tests/test_hermes_client.py",
    "tests/test_hermes_audit.py",
    "tests/test_hermes_wiring_sites.py",
)

REQUIRED_SKILL_FILES = (
    "hermes_skills/jarvis-trading/manifest.yaml",
    "hermes_skills/jarvis-trading/SKILL.md",
    "hermes_skills/jarvis-trading/README.md",
    "hermes_skills/jarvis-trading/deploy.ps1",
)


@dataclass(frozen=True)
class AuditCheck:
    """One read-only audit check result."""

    name: str
    status: str
    detail: str
    evidence: dict[str, Any]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8-sig")


def _check_markers(name: str, path: Path, markers: tuple[str, ...]) -> AuditCheck:
    if not path.exists():
        return AuditCheck(
            name=name,
            status="BLOCKED",
            detail=f"missing required file: {path}",
            evidence={"path": str(path), "missing": list(markers)},
        )
    text = _read_text(path)
    missing = [marker for marker in markers if marker not in text]
    status = "PASS" if not missing else "BLOCKED"
    detail = "required markers present" if not missing else f"missing markers: {', '.join(missing)}"
    return AuditCheck(
        name=name,
        status=status,
        detail=detail,
        evidence={"path": str(path), "missing": missing, "checked": list(markers)},
    )


def _check_plan_task_count(workspace_root: Path, expected_task_count: int) -> AuditCheck:
    path = workspace_root / "docs" / "superpowers" / "plans" / "2026-05-11-jarvis-hermes-bridge.md"
    if not path.exists():
        return AuditCheck(
            name="bridge_plan_tasks",
            status="WARN",
            detail="bridge plan file not present; skip task-count readiness",
            evidence={"path": str(path), "expected_task_count": expected_task_count, "actual_task_count": 0},
        )
    count = sum(1 for line in _read_text(path).splitlines() if line.startswith("### Task "))
    status = "PASS" if count >= expected_task_count else "WARN"
    detail = (
        f"bridge plan has {count} task(s), expected at least {expected_task_count}"
        if status == "PASS"
        else f"bridge plan has {count} task(s); T{expected_task_count} wave is not fully represented yet"
    )
    return AuditCheck(
        name="bridge_plan_tasks",
        status=status,
        detail=detail,
        evidence={"path": str(path), "expected_task_count": expected_task_count, "actual_task_count": count},
    )


def _check_file_set(name: str, root: Path, relative_paths: tuple[str, ...]) -> AuditCheck:
    missing = [rel for rel in relative_paths if not (root / rel).exists()]
    status = "PASS" if not missing else "BLOCKED"
    detail = "all required files present" if not missing else f"missing files: {', '.join(missing)}"
    return AuditCheck(
        name=name,
        status=status,
        detail=detail,
        evidence={"root": str(root), "missing": missing, "checked": list(relative_paths)},
    )


def _check_hot_path_markers(engine_root: Path) -> AuditCheck:
    missing: dict[str, list[str]] = {}
    for relative_path, markers in HOT_PATH_MARKERS.items():
        path = engine_root / relative_path
        if not path.exists():
            missing[relative_path] = list(markers)
            continue
        text = _read_text(path)
        missing_markers = [marker for marker in markers if marker not in text]
        if missing_markers:
            missing[relative_path] = missing_markers
    status = "PASS" if not missing else "BLOCKED"
    detail = "hot-path Hermes markers present" if not missing else "one or more hot-path markers are missing"
    return AuditCheck(
        name="jarvis_hot_path_wiring",
        status=status,
        detail=detail,
        evidence={"missing": missing},
    )


def _check_core_tools(engine_root: Path) -> AuditCheck:
    path = engine_root / "mcp_servers" / "jarvis_mcp_server.py"
    return _check_markers("jarvis_mcp_core_tools", path, CORE_MCP_TOOLS)


def _check_hermes_port(port: int, *, timeout_s: float = 0.25) -> AuditCheck:
    ok = False
    reason = "connection_failed"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_s)
        try:
            sock.connect(("127.0.0.1", port))
            ok = True
            reason = "listening"
        except OSError as exc:
            reason = type(exc).__name__
    status = "PASS" if ok else "WARN"
    detail = f"Hermes port {port} is {reason}"
    return AuditCheck(
        name="hermes_local_port",
        status=status,
        detail=detail,
        evidence={"host": "127.0.0.1", "port": port, "listening": ok, "reason": reason},
    )


def run_audit(
    workspace_root: Path = DEFAULT_WORKSPACE_ROOT,
    *,
    expected_task_count: int = 8,
    probe_port: bool = True,
    hermes_port: int = DEFAULT_HERMES_PORT,
) -> dict[str, Any]:
    """Return a machine-readable read-only audit report."""
    workspace_root = workspace_root.resolve()
    engine_root = workspace_root / "eta_engine"
    checks = [
        _check_plan_task_count(workspace_root, expected_task_count),
        _check_file_set("hermes_skill_package", engine_root, REQUIRED_SKILL_FILES),
        _check_file_set("hermes_regression_tests", engine_root, REQUIRED_TESTS),
        _check_core_tools(engine_root),
        _check_markers(
            "mcp_destructive_safety",
            engine_root / "mcp_servers" / "jarvis_mcp_server.py",
            DESTRUCTIVE_MARKERS,
        ),
        _check_markers(
            "jarvis_to_hermes_client_safety",
            engine_root / "brain" / "jarvis_v3" / "hermes_client.py",
            HERMES_CLIENT_MARKERS,
        ),
        _check_hot_path_markers(engine_root),
    ]
    if probe_port:
        checks.append(_check_hermes_port(hermes_port))

    blocked = [check for check in checks if check.status == "BLOCKED"]
    warnings = [check for check in checks if check.status == "WARN"]
    status = "BLOCKED" if blocked else "WARN" if warnings else "PASS"
    return {
        "kind": "jarvis_hermes_admin_audit",
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "workspace_root": str(workspace_root),
        "engine_root": str(engine_root),
        "status": status,
        "summary": {
            "checks": len(checks),
            "blocked": len(blocked),
            "warnings": len(warnings),
            "pass": sum(1 for check in checks if check.status == "PASS"),
            "admin_ai_ready": status == "PASS",
            "order_action_allowed": False,
            "live_money_gate_bypassed": False,
        },
        "checks": [asdict(check) for check in checks],
        "next_actions": _next_actions(blocked, warnings),
    }


def _next_actions(blocked: list[AuditCheck], warnings: list[AuditCheck]) -> list[str]:
    if blocked:
        return [f"Fix {check.name}: {check.detail}" for check in blocked[:5]]
    if warnings:
        return [f"Review {check.name}: {check.detail}" for check in warnings[:5]]
    return ["Jarvis/Hermes admin-AI bridge passes static safety readiness; keep live-money gates separate."]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", default=str(DEFAULT_WORKSPACE_ROOT))
    parser.add_argument("--expected-task-count", type=int, default=8)
    parser.add_argument("--no-port-probe", action="store_true")
    parser.add_argument("--hermes-port", type=int, default=DEFAULT_HERMES_PORT)
    parser.add_argument("--json", action="store_true", help="emit full JSON report")
    args = parser.parse_args(argv)

    report = run_audit(
        Path(args.workspace_root),
        expected_task_count=args.expected_task_count,
        probe_port=not args.no_port_probe,
        hermes_port=args.hermes_port,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"JARVIS/Hermes admin audit: {report['status']}")
        summary = report["summary"]
        print(
            "Checks: "
            f"{summary['pass']} pass, {summary['warnings']} warn, {summary['blocked']} blocked; "
            f"admin_ai_ready={summary['admin_ai_ready']}",
        )
        for action in report["next_actions"]:
            print(f"- {action}")
    return 0 if report["status"] in {"PASS", "WARN"} else 2


if __name__ == "__main__":
    raise SystemExit(main())

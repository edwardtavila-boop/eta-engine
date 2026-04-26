"""BTC support swarm for keeper, learning, shadow testing, and dashboard state.

This is the BTC equivalent of the MNQ helper machinery: the trading workers stay
in ``btc_broker_fleet.py`` while this script runs the surrounding processes that
keep them alive, inspect artifacts, run safe tests, and publish dashboard state.
All roles are read-only with respect to broker order flow.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from eta_engine.scripts import btc_broker_fleet  # noqa: E402

DEFAULT_OUT_DIR = ROOT / "docs" / "btc_live" / "ecosystem"
DEFAULT_FLEET_OUT_DIR = ROOT / "docs" / "btc_live" / "broker_fleet"
DEFAULT_INTERVAL_S = 30
ECOSYSTEM_MANIFEST = "btc_ecosystem_latest.json"

ROLE_DESCRIPTIONS = {
    "keeper": "watchdog that restarts or refreshes the four BTC broker-paper workers",
    "autolearn": "artifact learner that produces safe tuning recommendations",
    "shadow": "focused regression and shadow-health tester with no order placement",
    "risk": "fail-closed paper risk sentinel for worker liveness, cash, and routing posture",
    "audit": "artifact spine auditor for fleet, worker heartbeats, and support-state freshness",
    "optimizer": "promotion gate that combines autolearn, shadow, and risk signals",
    "broker_probe": "periodic read-only Tastytrade/IBKR readiness probe",
    "resource": "process/resource telemetry for BTC workers and helper swarm",
    "journal": "paper bankroll and lane-balance journal for BTC fleet state",
    "dashboard": "operator state exporter for Command Center style surfaces",
}


@dataclass(frozen=True)
class EcosystemRole:
    role: str
    interval_s: int = DEFAULT_INTERVAL_S


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def role_status_path(out_dir: Path, role: str) -> Path:
    return out_dir / f"btc_{role}_latest.json"


def role_log_path(out_dir: Path, role: str) -> Path:
    return out_dir / f"btc_{role}.jsonl"


def ecosystem_roles(interval_s: int = DEFAULT_INTERVAL_S) -> list[EcosystemRole]:
    return [
        EcosystemRole("keeper", interval_s),
        EcosystemRole("autolearn", interval_s),
        EcosystemRole("shadow", max(interval_s, 60)),
        EcosystemRole("risk", max(10, min(interval_s, 20))),
        EcosystemRole("audit", max(interval_s, 60)),
        EcosystemRole("optimizer", max(interval_s, 45)),
        EcosystemRole("broker_probe", max(interval_s, 60)),
        EcosystemRole("resource", max(10, min(interval_s, 20))),
        EcosystemRole("journal", max(interval_s, 60)),
        EcosystemRole("dashboard", max(5, min(interval_s, 15))),
    ]


def is_pid_running(pid: int) -> bool:
    return btc_broker_fleet.is_pid_running(pid)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def summarize_fleet(fleet: dict[str, Any]) -> dict[str, Any]:
    workers = fleet.get("workers", [])
    running = [worker for worker in workers if worker.get("status") == "RUNNING" and worker.get("process_running")]
    by_lane: dict[str, int] = {}
    by_broker: dict[str, int] = {}
    for worker in workers:
        by_lane[str(worker.get("lane") or "unknown")] = by_lane.get(str(worker.get("lane") or "unknown"), 0) + 1
        by_broker[str(worker.get("broker") or "unknown")] = by_broker.get(str(worker.get("broker") or "unknown"), 0) + 1
    return {
        "requested_workers": int(fleet.get("requested_workers") or 4),
        "running_workers": len(running),
        "paper_starting_cash_per_worker": float(fleet.get("paper_starting_cash_per_worker") or 0.0),
        "paper_starting_cash_total": float(fleet.get("paper_starting_cash_total") or 0.0),
        "order_routing": fleet.get("order_routing", btc_broker_fleet.PAPER_BROKER_ORDER_ROUTING),
        "live_money_orders": fleet.get("live_money_orders", "blocked"),
        "paper_broker_orders": fleet.get("paper_broker_orders", "allowed"),
        "by_lane": by_lane,
        "by_broker": by_broker,
    }


def heartbeat_age_s(worker: dict[str, Any], *, now: datetime | None = None) -> float | None:
    raw = worker.get("last_heartbeat_utc")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(0.0, (current - parsed).total_seconds())


def build_risk_report(fleet: dict[str, Any], *, expected_cash: float) -> dict[str, Any]:
    summary = summarize_fleet(fleet)
    findings: list[dict[str, Any]] = []
    workers = fleet.get("workers", [])
    if summary["running_workers"] < summary["requested_workers"]:
        findings.append(
            {
                "severity": "high",
                "code": "worker_count_below_target",
                "detail": f"{summary['running_workers']} of {summary['requested_workers']} workers are running",
            },
        )
    if summary["live_money_orders"] != "blocked":
        findings.append(
            {
                "severity": "critical",
                "code": "live_money_order_routing_enabled",
                "detail": "BTC framework must block live-money order placement",
            },
        )
    if summary["order_routing"] != btc_broker_fleet.PAPER_BROKER_ORDER_ROUTING:
        findings.append(
            {
                "severity": "medium",
                "code": "paper_broker_order_routing_not_enabled",
                "detail": "paper broker order routing should be enabled for live paper simulation",
            },
        )
    for worker in workers:
        cash = float(worker.get("paper_cash") or 0.0)
        age = heartbeat_age_s(worker)
        if abs(cash - expected_cash) > 0.01:
            findings.append(
                {
                    "severity": "medium",
                    "code": "paper_cash_drift",
                    "worker_id": worker.get("worker_id"),
                    "detail": f"paper_cash={cash:.2f} expected={expected_cash:.2f}",
                },
            )
        if age is None or age > 45:
            findings.append(
                {
                    "severity": "high",
                    "code": "heartbeat_stale",
                    "worker_id": worker.get("worker_id"),
                    "detail": "heartbeat missing or older than 45 seconds",
                },
            )
    severity_rank = {"critical": 3, "high": 2, "medium": 1}
    worst = max((severity_rank.get(item["severity"], 0) for item in findings), default=0)
    return {
        "health": "RED" if worst >= 3 else "YELLOW" if worst >= 1 else "GREEN",
        "findings": findings,
        "fleet_summary": summary,
        "expected_cash_per_bot": round(expected_cash, 2),
    }


def build_artifact_audit(*, out_dir: Path, fleet_out_dir: Path) -> dict[str, Any]:
    expected_paths = [
        fleet_out_dir / btc_broker_fleet.FLEET_MANIFEST,
        out_dir / ECOSYSTEM_MANIFEST,
    ]
    expected_paths.extend(
        btc_broker_fleet.worker_status_path(fleet_out_dir, spec.worker_id)
        for spec in btc_broker_fleet.fleet_workers()
    )
    expected_paths.extend(role_status_path(out_dir, role) for role in ROLE_DESCRIPTIONS if role != "audit")
    missing = [str(path) for path in expected_paths if not path.exists()]
    present = [str(path) for path in expected_paths if path.exists()]
    return {
        "health": "GREEN" if not missing else "YELLOW",
        "present_count": len(present),
        "missing_count": len(missing),
        "present": present,
        "missing": missing,
    }


def build_optimizer_gate(
    *,
    risk: dict[str, Any],
    shadow: dict[str, Any],
    autolearn: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    if risk.get("health") != "GREEN":
        reasons.append(f"risk health is {risk.get('health', 'UNKNOWN')}")
    if shadow.get("exit_code") not in (0, None):
        reasons.append(f"shadow exit_code={shadow.get('exit_code')}")
    if not autolearn.get("recommendations"):
        reasons.append("autolearn has no recommendations yet")
    return {
        "state": "PROMOTABLE" if not reasons else "HOLD",
        "reasons": reasons or ["risk green, shadow green, autolearn active"],
        "next_action": "continue paper soak" if not reasons else "hold promotion and keep observing",
    }


def build_resource_report(
    *,
    fleet: dict[str, Any],
    role_states: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    workers = fleet.get("workers", [])
    fleet_pids = [int(worker.get("pid") or 0) for worker in workers if int(worker.get("pid") or 0) > 0]
    role_pids = [
        int(state.get("pid") or 0)
        for state in role_states.values()
        if int(state.get("pid") or 0) > 0
    ]
    stale_workers = [
        worker.get("worker_id")
        for worker in workers
        if worker.get("status") != "RUNNING" or not worker.get("process_running")
    ]
    missing_roles = [
        role
        for role, state in role_states.items()
        if role != "resource" and not state.get("process_running")
    ]
    return {
        "health": "GREEN" if not stale_workers and not missing_roles else "YELLOW",
        "fleet_pid_count": len(fleet_pids),
        "support_pid_count": len(role_pids),
        "total_btc_pid_count": len(set(fleet_pids + role_pids)),
        "stale_workers": stale_workers,
        "missing_roles": missing_roles,
        "fleet_pids": fleet_pids,
        "support_pids": role_pids,
    }


def build_journal_snapshot(fleet: dict[str, Any]) -> dict[str, Any]:
    workers = fleet.get("workers", [])
    paper_cash = sum(float(worker.get("paper_cash") or 0.0) for worker in workers)
    paper_equity = sum(float(worker.get("paper_equity") or 0.0) for worker in workers)
    summary = summarize_fleet(fleet)
    return {
        "health": "GREEN" if summary["running_workers"] == summary["requested_workers"] else "YELLOW",
        "running_workers": summary["running_workers"],
        "requested_workers": summary["requested_workers"],
        "paper_cash_total": round(paper_cash, 2),
        "paper_equity_total": round(paper_equity, 2),
        "paper_pnl_total": round(paper_equity - paper_cash, 2),
        "by_lane": summary["by_lane"],
        "by_broker": summary["by_broker"],
    }


def build_autolearn_recommendations(fleet_summary: dict[str, Any]) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    if fleet_summary["running_workers"] < fleet_summary["requested_workers"]:
        recommendations.append(
            {
                "priority": "high",
                "action": "restore_btc_worker_count",
                "reason": "not all BTC broker-paper workers are running",
            },
        )
    if fleet_summary.get("live_money_orders", "blocked") != "blocked":
        recommendations.append(
            {
                "priority": "critical",
                "action": "halt_live_money_order_routing",
                "reason": "BTC ecosystem must keep live-money order placement blocked",
            },
        )
    if fleet_summary.get("order_routing") != btc_broker_fleet.PAPER_BROKER_ORDER_ROUTING:
        recommendations.append(
            {
                "priority": "high",
                "action": "enable_broker_paper_order_routing",
                "reason": "BTC live paper sims should place through paper broker adapters",
            },
        )
    if not recommendations:
        recommendations.append(
            {
                "priority": "normal",
                "action": "continue_observation",
                "reason": "all BTC paper workers are alive and safety posture is unchanged",
            },
        )
    return recommendations


def build_dashboard_state(
    *,
    fleet: dict[str, Any],
    role_states: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    summary = summarize_fleet(fleet)
    return {
        "generated_at_utc": utc_now(),
        "surface": "btc_eta_dashboard_state",
        "health": "GREEN" if summary["running_workers"] == summary["requested_workers"] else "YELLOW",
        "live_paper_trading_bots": summary["running_workers"],
        "requested_bots": summary["requested_workers"],
        "paper_starting_cash_per_bot": summary["paper_starting_cash_per_worker"],
        "paper_starting_cash_total": summary["paper_starting_cash_total"],
        "order_routing": summary["order_routing"],
        "paper_broker_orders": summary["paper_broker_orders"],
        "live_money_orders": summary["live_money_orders"],
        "fleet_summary": summary,
        "roles": role_states,
        "safety": "BTC helper swarm allows paper broker order routing and blocks live-money orders.",
    }


def run_keeper_once(*, out_dir: Path, fleet_out_dir: Path, starting_cash: float) -> dict[str, Any]:
    fleet = btc_broker_fleet.status_fleet(out_dir=fleet_out_dir, starting_cash=starting_cash)
    summary = summarize_fleet(fleet)
    action = "observe"
    if summary["running_workers"] < summary["requested_workers"]:
        fleet = __import__("asyncio").run(
            btc_broker_fleet.start_fleet(
                out_dir=fleet_out_dir,
                starting_cash=starting_cash,
                heartbeat_interval_s=5.0,
            ),
        )
        summary = summarize_fleet(fleet)
        action = "restart_fleet"
    payload = {
        "generated_at_utc": utc_now(),
        "role": "keeper",
        "pid": os.getpid(),
        "status": "RUNNING",
        "action": action,
        "fleet_summary": summary,
        "safety": "watchdog only; no broker order placement",
    }
    write_json(role_status_path(out_dir, "keeper"), payload)
    append_jsonl(role_log_path(out_dir, "keeper"), payload)
    return payload


def run_autolearn_once(*, out_dir: Path, fleet_out_dir: Path, starting_cash: float) -> dict[str, Any]:
    fleet = btc_broker_fleet.status_fleet(out_dir=fleet_out_dir, starting_cash=starting_cash)
    summary = summarize_fleet(fleet)
    payload = {
        "generated_at_utc": utc_now(),
        "role": "autolearn",
        "pid": os.getpid(),
        "status": "RUNNING",
        "fleet_summary": summary,
        "recommendations": build_autolearn_recommendations(summary),
        "champion_candidate": {
            "lane_balance": summary["by_lane"],
            "broker_balance": summary["by_broker"],
            "cash_per_bot": summary["paper_starting_cash_per_worker"],
        },
        "safety": "analysis only; recommendations do not mutate broker routing",
    }
    write_json(role_status_path(out_dir, "autolearn"), payload)
    append_jsonl(role_log_path(out_dir, "autolearn"), payload)
    return payload


def run_shadow_once(*, out_dir: Path) -> dict[str, Any]:
    tests = [
        "tests/test_btc_broker_fleet.py",
        "tests/test_btc_bootstrap.py",
        "tests/test_btc_live.py",
    ]
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *tests],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    payload = {
        "generated_at_utc": utc_now(),
        "role": "shadow",
        "pid": os.getpid(),
        "status": "RUNNING" if result.returncode == 0 else "DEGRADED",
        "exit_code": result.returncode,
        "duration_s": round(time.monotonic() - started, 3),
        "tests": tests,
        "stdout_tail": result.stdout[-1200:],
        "stderr_tail": result.stderr[-1200:],
        "safety": "pytest shadow checks only; no broker order placement",
    }
    write_json(role_status_path(out_dir, "shadow"), payload)
    append_jsonl(role_log_path(out_dir, "shadow"), payload)
    return payload


def run_risk_once(*, out_dir: Path, fleet_out_dir: Path, starting_cash: float) -> dict[str, Any]:
    fleet = btc_broker_fleet.status_fleet(out_dir=fleet_out_dir, starting_cash=starting_cash)
    report = build_risk_report(fleet, expected_cash=starting_cash)
    payload = {
        "generated_at_utc": utc_now(),
        "role": "risk",
        "pid": os.getpid(),
        "status": "RUNNING" if report["health"] == "GREEN" else "DEGRADED",
        "risk": report,
        "safety": "risk sentinel only; no broker order placement",
    }
    write_json(role_status_path(out_dir, "risk"), payload)
    append_jsonl(role_log_path(out_dir, "risk"), payload)
    return payload


def run_audit_once(*, out_dir: Path, fleet_out_dir: Path) -> dict[str, Any]:
    audit = build_artifact_audit(out_dir=out_dir, fleet_out_dir=fleet_out_dir)
    payload = {
        "generated_at_utc": utc_now(),
        "role": "audit",
        "pid": os.getpid(),
        "status": "RUNNING" if audit["health"] == "GREEN" else "DEGRADED",
        "artifact_audit": audit,
        "safety": "artifact audit only; no broker order placement",
    }
    write_json(role_status_path(out_dir, "audit"), payload)
    append_jsonl(role_log_path(out_dir, "audit"), payload)
    return payload


def run_optimizer_once(*, out_dir: Path) -> dict[str, Any]:
    risk_state = read_json(role_status_path(out_dir, "risk")).get("risk", {})
    shadow_state = read_json(role_status_path(out_dir, "shadow"))
    autolearn_state = read_json(role_status_path(out_dir, "autolearn"))
    gate = build_optimizer_gate(risk=risk_state, shadow=shadow_state, autolearn=autolearn_state)
    payload = {
        "generated_at_utc": utc_now(),
        "role": "optimizer",
        "pid": os.getpid(),
        "status": "RUNNING",
        "promotion_gate": gate,
        "safety": "optimizer writes recommendations only; no broker order placement",
    }
    write_json(role_status_path(out_dir, "optimizer"), payload)
    append_jsonl(role_log_path(out_dir, "optimizer"), payload)
    return payload


def run_broker_probe_once(*, out_dir: Path) -> dict[str, Any]:
    probe = __import__("asyncio").run(btc_broker_fleet.probe_required_brokers(["tastytrade", "ibkr"]))
    payload = {
        "generated_at_utc": utc_now(),
        "role": "broker_probe",
        "pid": os.getpid(),
        "status": "RUNNING" if not probe.get("missing_ready") else "DEGRADED",
        "broker_preflight": probe,
        "safety": "read-only broker readiness probe; no orders",
    }
    write_json(role_status_path(out_dir, "broker_probe"), payload)
    append_jsonl(role_log_path(out_dir, "broker_probe"), payload)
    return payload


def run_resource_once(*, out_dir: Path, fleet_out_dir: Path, starting_cash: float) -> dict[str, Any]:
    fleet = btc_broker_fleet.status_fleet(out_dir=fleet_out_dir, starting_cash=starting_cash)
    role_states = collect_role_states(out_dir)
    report = build_resource_report(fleet=fleet, role_states=role_states)
    payload = {
        "generated_at_utc": utc_now(),
        "role": "resource",
        "pid": os.getpid(),
        "status": "RUNNING" if report["health"] == "GREEN" else "DEGRADED",
        "resource": report,
        "safety": "process telemetry only; no broker order placement",
    }
    write_json(role_status_path(out_dir, "resource"), payload)
    append_jsonl(role_log_path(out_dir, "resource"), payload)
    return payload


def run_journal_once(*, out_dir: Path, fleet_out_dir: Path, starting_cash: float) -> dict[str, Any]:
    fleet = btc_broker_fleet.status_fleet(out_dir=fleet_out_dir, starting_cash=starting_cash)
    snapshot = build_journal_snapshot(fleet)
    payload = {
        "generated_at_utc": utc_now(),
        "role": "journal",
        "pid": os.getpid(),
        "status": "RUNNING" if snapshot["health"] == "GREEN" else "DEGRADED",
        "journal": snapshot,
        "safety": "paper ledger journal only; no broker order placement",
    }
    write_json(role_status_path(out_dir, "journal"), payload)
    append_jsonl(role_log_path(out_dir, "journal"), payload)
    return payload


def run_dashboard_once(*, out_dir: Path, fleet_out_dir: Path, starting_cash: float) -> dict[str, Any]:
    fleet = btc_broker_fleet.status_fleet(out_dir=fleet_out_dir, starting_cash=starting_cash)
    role_states = {
        role: read_json(role_status_path(out_dir, role))
        for role in ROLE_DESCRIPTIONS
        if role != "dashboard"
    }
    payload = build_dashboard_state(fleet=fleet, role_states=role_states)
    payload["role"] = "dashboard"
    payload["pid"] = os.getpid()
    payload["status"] = "RUNNING"
    write_json(role_status_path(out_dir, "dashboard"), payload)
    write_json(out_dir / "btc_dashboard_state.json", payload)
    append_jsonl(role_log_path(out_dir, "dashboard"), payload)
    return payload


def run_role_loop(role: str, *, out_dir: Path, fleet_out_dir: Path, starting_cash: float, interval_s: int) -> int:
    while True:
        if role == "keeper":
            run_keeper_once(out_dir=out_dir, fleet_out_dir=fleet_out_dir, starting_cash=starting_cash)
        elif role == "autolearn":
            run_autolearn_once(out_dir=out_dir, fleet_out_dir=fleet_out_dir, starting_cash=starting_cash)
        elif role == "shadow":
            run_shadow_once(out_dir=out_dir)
        elif role == "risk":
            run_risk_once(out_dir=out_dir, fleet_out_dir=fleet_out_dir, starting_cash=starting_cash)
        elif role == "audit":
            run_audit_once(out_dir=out_dir, fleet_out_dir=fleet_out_dir)
        elif role == "optimizer":
            run_optimizer_once(out_dir=out_dir)
        elif role == "broker_probe":
            run_broker_probe_once(out_dir=out_dir)
        elif role == "resource":
            run_resource_once(out_dir=out_dir, fleet_out_dir=fleet_out_dir, starting_cash=starting_cash)
        elif role == "journal":
            run_journal_once(out_dir=out_dir, fleet_out_dir=fleet_out_dir, starting_cash=starting_cash)
        elif role == "dashboard":
            run_dashboard_once(out_dir=out_dir, fleet_out_dir=fleet_out_dir, starting_cash=starting_cash)
        else:
            raise ValueError(f"unknown role: {role}")
        time.sleep(max(2, interval_s))


def start_role_process(
    role: EcosystemRole,
    *,
    out_dir: Path,
    fleet_out_dir: Path,
    starting_cash: float,
) -> subprocess.Popen[Any]:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--role-worker",
        role.role,
        "--out-dir",
        str(out_dir),
        "--fleet-out-dir",
        str(fleet_out_dir),
        "--starting-cash",
        str(starting_cash),
        "--interval-s",
        str(role.interval_s),
    ]
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )


def collect_role_states(out_dir: Path) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for role in ROLE_DESCRIPTIONS:
        state = read_json(role_status_path(out_dir, role))
        pid = int(state.get("pid") or 0)
        if state:
            state["process_running"] = is_pid_running(pid)
        states[role] = state
    return states


def build_ecosystem_manifest(*, out_dir: Path, role_states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    running = [role for role, state in role_states.items() if state.get("process_running")]
    return {
        "generated_at_utc": utc_now(),
        "ecosystem": "btc_apex_support_swarm",
        "requested_roles": sorted(ROLE_DESCRIPTIONS),
        "running_roles": sorted(running),
        "running_role_count": len(running),
        "role_descriptions": ROLE_DESCRIPTIONS,
        "roles": role_states,
        "safety": "support processes supervise, learn, test, and publish state without placing orders",
        "manifest_path": str(out_dir / ECOSYSTEM_MANIFEST),
    }


def start_ecosystem(*, out_dir: Path, fleet_out_dir: Path, starting_cash: float, interval_s: int) -> dict[str, Any]:
    for role in ecosystem_roles(interval_s):
        state = read_json(role_status_path(out_dir, role.role))
        pid = int(state.get("pid") or 0)
        if state.get("process_running") or (pid and is_pid_running(pid)):
            continue
        start_role_process(role, out_dir=out_dir, fleet_out_dir=fleet_out_dir, starting_cash=starting_cash)
    deadline = time.monotonic() + 90.0
    role_states = collect_role_states(out_dir)
    while time.monotonic() < deadline:
        role_states = collect_role_states(out_dir)
        if all(state.get("process_running") for state in role_states.values()):
            break
        time.sleep(2)
    manifest = build_ecosystem_manifest(out_dir=out_dir, role_states=role_states)
    write_json(out_dir / ECOSYSTEM_MANIFEST, manifest)
    return manifest


def status_ecosystem(*, out_dir: Path) -> dict[str, Any]:
    manifest = build_ecosystem_manifest(out_dir=out_dir, role_states=collect_role_states(out_dir))
    write_json(out_dir / ECOSYSTEM_MANIFEST, manifest)
    return manifest


def stop_ecosystem(*, out_dir: Path) -> dict[str, Any]:
    states = collect_role_states(out_dir)
    for state in states.values():
        pid = int(state.get("pid") or 0)
        if pid and is_pid_running(pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
            else:
                os.kill(pid, 15)
            state["process_running"] = False
            state["status"] = "STOPPED"
    manifest = build_ecosystem_manifest(out_dir=out_dir, role_states=states)
    write_json(out_dir / ECOSYSTEM_MANIFEST, manifest)
    return manifest


def format_summary(payload: dict[str, Any]) -> str:
    lines = [
        "BTC Apex support swarm",
        "=" * 72,
        f"running_roles: {payload.get('running_role_count')}/{len(ROLE_DESCRIPTIONS)}",
        "-" * 72,
    ]
    for role in sorted(ROLE_DESCRIPTIONS):
        state = payload.get("roles", {}).get(role, {})
        lines.append(f"{role:<10} {str(state.get('status', 'MISSING')):<9} pid={state.get('pid', 0)}")
    lines.append("=" * 72)
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run BTC Apex support machinery")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--stop", action="store_true")
    action.add_argument("--role-worker", choices=sorted(ROLE_DESCRIPTIONS))
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--fleet-out-dir", type=Path, default=DEFAULT_FLEET_OUT_DIR)
    parser.add_argument("--starting-cash", type=float, default=btc_broker_fleet.DEFAULT_STARTING_CASH)
    parser.add_argument("--interval-s", type=int, default=DEFAULT_INTERVAL_S)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.role_worker:
        return run_role_loop(
            args.role_worker,
            out_dir=args.out_dir,
            fleet_out_dir=args.fleet_out_dir,
            starting_cash=args.starting_cash,
            interval_s=args.interval_s,
        )
    if args.start:
        payload = start_ecosystem(
            out_dir=args.out_dir,
            fleet_out_dir=args.fleet_out_dir,
            starting_cash=args.starting_cash,
            interval_s=args.interval_s,
        )
    elif args.stop:
        payload = stop_ecosystem(out_dir=args.out_dir)
    else:
        payload = status_ecosystem(out_dir=args.out_dir)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_summary(payload))
        print(f"manifest -> {args.out_dir / ECOSYSTEM_MANIFEST}")
    return 0 if payload.get("running_role_count", 0) == len(ROLE_DESCRIPTIONS) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

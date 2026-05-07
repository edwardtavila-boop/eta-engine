"""VPS Health Check - self-diagnostic script for autonomous operation.

Checks:
  1. Disk space.
  2. Kaizen engine state.
  3. Quantum rebalance freshness.
  4. Hermes bridge queue health.
  5. Jarvis strategy supervisor heartbeat freshness.
  6. Git repo drift.

Run daily via: schtasks /Create /TN "ETA-VPS-HealthCheck" /TR ".../health_check.py" /SC DAILY /ST 08:00
Or invoke directly from any automation: python eta_engine/scripts/health_check.py

Exit codes: 0 = healthy, 1 = warning, 2 = critical
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.supervisor_heartbeat_check import build_supervisor_heartbeat_report  # noqa: E402
from eta_engine.scripts.workspace_roots import ETA_RUNTIME_STATE_DIR  # noqa: E402

_STATE_DIR = ETA_RUNTIME_STATE_DIR


@dataclass
class HealthComponent:
    name: str
    healthy: bool
    status: str = "unknown"
    detail: str = ""
    score: float = 1.0


@dataclass
class VpsHealthReport:
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    components: list[HealthComponent] = field(default_factory=list)
    overall_score: float = 0.0
    overall_status: str = "unknown"
    exit_code: int = 0
    action_items: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "overall_score": round(self.overall_score, 3),
            "overall_status": self.overall_status,
            "exit_code": self.exit_code,
            "components": [
                {"name": c.name, "healthy": c.healthy, "status": c.status, "detail": c.detail, "score": c.score}
                for c in self.components
            ],
            "action_items": self.action_items,
        }


def _check_disk_space() -> HealthComponent:
    try:
        import shutil

        usage = shutil.disk_usage(str(ROOT))
        free_pct = usage.free / usage.total * 100
        if free_pct < 5:
            return HealthComponent("disk_space", False, "critical", f"{free_pct:.1f}% free (< 5%)", 0.0)
        if free_pct < 10:
            return HealthComponent("disk_space", True, "warning", f"{free_pct:.1f}% free (< 10%)", 0.5)
        return HealthComponent("disk_space", True, "healthy", f"{free_pct:.1f}% free", 1.0)
    except Exception:
        return HealthComponent("disk_space", True, "unknown", "check unavailable", 0.5)


def _check_kaizen_state() -> HealthComponent:
    state_path = _STATE_DIR / "kaizen" / "kaizen_engine_state.json"
    guard_path = _STATE_DIR / "kaizen" / "guard_state.json"

    if not state_path.exists():
        return HealthComponent("kaizen_engine", True, "booting", "no state file yet - engine may not have run", 0.5)

    try:
        state = json.loads(state_path.read_text())
        cycle_count = state.get("cycle_count", 0)
        if cycle_count == 0:
            return HealthComponent("kaizen_engine", True, "booting", "engine initialized but no cycles run", 0.5)
    except (OSError, json.JSONDecodeError):
        return HealthComponent("kaizen_engine", False, "critical", "state file corrupted", 0.0)

    if guard_path.exists():
        try:
            guard = json.loads(guard_path.read_text())
            if guard.get("circuit_tripped"):
                detail = (
                    f"circuit breaker tripped: {guard.get('circuit_reason', 'unknown')} "
                    f"until {guard.get('circuit_until', '?')}"
                )
                return HealthComponent("kaizen_engine", False, "blocked", detail, 0.3)
        except (OSError, json.JSONDecodeError):
            pass

    return HealthComponent("kaizen_engine", True, "healthy", f"{cycle_count} cycles completed", 1.0)


def _check_quantum_freshness() -> HealthComponent:
    quantum_dir = _STATE_DIR / "quantum"
    if not quantum_dir.exists():
        return HealthComponent("quantum_rebalance", True, "booting", "no quantum state dir", 0.5)

    current = quantum_dir / "current_allocation.json"
    if not current.exists():
        return HealthComponent(
            "quantum_rebalance",
            True,
            "booting",
            "no current allocation - rebalance may not have run",
            0.5,
        )

    try:
        data = json.loads(current.read_text())
        ts_str = data.get("ts", "")
        if ts_str:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            age_hours = (datetime.now(UTC) - ts).total_seconds() / 3600
            if age_hours > 48:
                return HealthComponent(
                    "quantum_rebalance",
                    False,
                    "stale",
                    f"last rebalance {age_hours:.0f}h ago",
                    0.3,
                )
            if age_hours > 24:
                return HealthComponent(
                    "quantum_rebalance",
                    True,
                    "warning",
                    f"last rebalance {age_hours:.0f}h ago",
                    0.6,
                )
            return HealthComponent("quantum_rebalance", True, "healthy", f"last rebalance {age_hours:.1f}h ago", 1.0)
    except (OSError, json.JSONDecodeError, ValueError):
        return HealthComponent("quantum_rebalance", False, "warning", "unable to parse allocation file", 0.4)

    return HealthComponent("quantum_rebalance", True, "healthy", "allocation exists", 0.8)


def _check_hermes_connectivity() -> HealthComponent:
    saf_path = _STATE_DIR / "hermes" / "store_and_forward.jsonl"
    if saf_path.exists():
        try:
            count = sum(1 for line in saf_path.read_text(encoding="utf-8").splitlines() if line.strip())
            if count > 20:
                detail = f"{count} unsent messages queued - Telegram may be unreachable"
                return HealthComponent("hermes_bridge", False, "warning", detail, 0.4)
            if count > 0:
                return HealthComponent("hermes_bridge", True, "warning", f"{count} pending messages", 0.6)
        except OSError:
            pass
    return HealthComponent("hermes_bridge", True, "healthy", "store-and-forward queue clear", 1.0)


def _check_supervisor_heartbeat() -> HealthComponent:
    try:
        report = build_supervisor_heartbeat_report(state_root=_STATE_DIR)
    except Exception:
        return HealthComponent("supervisor_heartbeat", False, "critical", "heartbeat diagnostic failed", 0.0)

    age = report.get("canonical_age_seconds")
    age_text = "unknown" if age is None else f"{float(age):.1f}s"
    diagnosis = str(report.get("diagnosis", "unknown"))
    detail = f"{diagnosis}; canonical age {age_text}"
    if report.get("healthy"):
        return HealthComponent("supervisor_heartbeat", True, "healthy", detail, 1.0)
    status = str(report.get("status", "critical"))
    score = 0.4 if status == "wrong_write_path" else 0.1 if status in {"missing", "invalid"} else 0.2
    return HealthComponent("supervisor_heartbeat", False, status, detail, score)


def _check_repo_health() -> HealthComponent:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
        dirty = [line for line in result.stdout.splitlines() if line.strip()]
        if len(dirty) > 20:
            detail = f"{len(dirty)} uncommitted files - possible drift"
            return HealthComponent("repo_health", True, "warning", detail, 0.5)
        detail = f"{len(dirty)} uncommitted files" if dirty else "clean"
        return HealthComponent("repo_health", True, "healthy", detail, 1.0 if not dirty else 0.8)
    except Exception:
        return HealthComponent("repo_health", True, "unknown", "git check unavailable", 0.5)


def _apply_remote_supervisor_truth(component: HealthComponent) -> HealthComponent:
    if component.name != "supervisor_heartbeat" or component.healthy:
        return component
    return HealthComponent(
        name=component.name,
        healthy=True,
        status="remote_supervisor_truth",
        detail=f"{component.detail}; local heartbeat check satisfied by live ops probe",
        score=0.8,
    )


def run_health_check(
    *,
    output_dir: Path | None = None,
    allow_remote_supervisor_truth: bool = False,
) -> VpsHealthReport:
    components = [
        _check_disk_space(),
        _check_kaizen_state(),
        _check_quantum_freshness(),
        _check_hermes_connectivity(),
        _check_supervisor_heartbeat(),
        _check_repo_health(),
    ]
    if allow_remote_supervisor_truth:
        components = [_apply_remote_supervisor_truth(component) for component in components]

    scores = [c.score for c in components]
    overall = sum(scores) / len(scores) if scores else 0.0

    critical_count = sum(1 for c in components if c.status == "critical")
    warning_count = sum(1 for c in components if c.status == "warning" or not c.healthy)

    if critical_count >= 2 or overall < 0.3:
        status = "critical"
        exit_code = 2
    elif warning_count >= 2 or overall < 0.6:
        status = "warning"
        exit_code = 1
    else:
        status = "healthy"
        exit_code = 0

    action_items = [f"[{c.name}] {c.status}: {c.detail}" for c in components if not c.healthy]

    report = VpsHealthReport(
        components=components,
        overall_score=overall,
        overall_status=status,
        exit_code=exit_code,
        action_items=action_items,
    )

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"health_check_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
        current = output_dir / "current_health.json"
        current.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the ETA VPS health gate.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_STATE_DIR / "health",
        help="Directory where health snapshots should be written.",
    )
    parser.add_argument(
        "--allow-remote-supervisor-truth",
        action="store_true",
        help=(
            "Treat a missing local supervisor heartbeat as satisfied by a separate "
            "live ops probe. Intended for project_kaizen_closeout --live only."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_health_check(
        output_dir=args.output_dir,
        allow_remote_supervisor_truth=args.allow_remote_supervisor_truth,
    )
    print(json.dumps(report.to_dict(), indent=2))
    if report.action_items:
        print("\nACTION ITEMS:", file=sys.stderr)
        for item in report.action_items:
            print(f"  - {item}", file=sys.stderr)

    if report.exit_code > 0:
        try:
            from hermes_jarvis_telegram.hermes_bridge import get_bridge

            bridge = get_bridge()
            bridge.notify_system_health(
                health_score=report.overall_score,
                verdict=report.overall_status,
                issues=report.action_items,
            )
        except Exception:
            pass

    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())

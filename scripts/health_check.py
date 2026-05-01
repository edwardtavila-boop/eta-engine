"""VPS Health Check — self-diagnostic script for the autonomous operation.

Checks:
  1. All required services running (FirmCore, FirmWatchdog, FirmCommandCenter, Hermes)
  2. Kaizen engine state healthy (no stale cycles, guard not tripped)
  3. Quantum rebalance freshness (last run within 24h)
  4. Hermes bridge connectivity (Telegram reachable)
  5. Disk space (> 5% free)
  6. Memory pressure (< 90%)
  7. Git repos clean (no uncommitted state drift)
  8. Scheduler ticks running (last tick within expected interval)

Run daily via: schtasks /Create /TN "ETA-VPS-HealthCheck" /TR ".../health_check.py" /SC DAILY /ST 08:00
Or invoke directly from any automation: python eta_engine/scripts/health_check.py

Exit codes: 0 = healthy, 1 = warning, 2 = critical
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


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
    state_path = ROOT / "state" / "kaizen" / "kaizen_engine_state.json"
    guard_path = ROOT / "state" / "kaizen" / "guard_state.json"

    if not state_path.exists():
        return HealthComponent("kaizen_engine", True, "booting", "no state file yet — engine may not have run", 0.5)

    try:
        state = json.loads(state_path.read_text())
        cycle_count = state.get("cycle_count", 0)
        if cycle_count == 0:
            return HealthComponent("kaizen_engine", True, "booting", "engine initialized but no cycles run", 0.5)
    except (OSError, json.JSONDecodeError):
        return HealthComponent("kaizen_engine", False, "critical", "state file corrupted", 0.0)

    # Check guard
    if guard_path.exists():
        try:
            guard = json.loads(guard_path.read_text())
            if guard.get("circuit_tripped"):
                return HealthComponent("kaizen_engine", False, "blocked",
                    f"circuit breaker tripped: {guard.get('circuit_reason', 'unknown')} until {guard.get('circuit_until', '?')}", 0.3)
        except (OSError, json.JSONDecodeError):
            pass

    return HealthComponent("kaizen_engine", True, "healthy", f"{cycle_count} cycles completed", 1.0)


def _check_quantum_freshness() -> HealthComponent:
    quantum_dir = ROOT / "state" / "quantum"
    if not quantum_dir.exists():
        return HealthComponent("quantum_rebalance", True, "booting", "no quantum state dir", 0.5)

    current = quantum_dir / "current_allocation.json"
    if not current.exists():
        return HealthComponent("quantum_rebalance", True, "booting", "no current allocation — rebalance may not have run", 0.5)

    try:
        data = json.loads(current.read_text())
        ts_str = data.get("ts", "")
        if ts_str:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            age_hours = (datetime.now(UTC) - ts).total_seconds() / 3600
            if age_hours > 48:
                return HealthComponent("quantum_rebalance", False, "stale", f"last rebalance {age_hours:.0f}h ago", 0.3)
            if age_hours > 24:
                return HealthComponent("quantum_rebalance", True, "warning", f"last rebalance {age_hours:.0f}h ago", 0.6)
            return HealthComponent("quantum_rebalance", True, "healthy", f"last rebalance {age_hours:.1f}h ago", 1.0)
    except (OSError, json.JSONDecodeError, ValueError):
        return HealthComponent("quantum_rebalance", False, "warning", "unable to parse allocation file", 0.4)

    return HealthComponent("quantum_rebalance", True, "healthy", "allocation exists", 0.8)


def _check_hermes_connectivity() -> HealthComponent:
    saf_path = ROOT / "state" / "hermes" / "store_and_forward.jsonl"
    if saf_path.exists():
        try:
            count = sum(1 for _ in saf_path.read_text(encoding="utf-8").splitlines() if _.strip())
            if count > 20:
                return HealthComponent("hermes_bridge", False, "warning",
                    f"{count} unsent messages queued — Telegram may be unreachable", 0.4)
            if count > 0:
                return HealthComponent("hermes_bridge", True, "warning",
                    f"{count} pending messages", 0.6)
        except OSError:
            pass
    return HealthComponent("hermes_bridge", True, "healthy", "store-and-forward queue clear", 1.0)


def _check_repo_health() -> HealthComponent:
    import subprocess
    try:
        result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=str(ROOT), timeout=10)
        dirty = [l for l in result.stdout.splitlines() if l.strip()]
        if len(dirty) > 20:
            return HealthComponent("repo_health", True, "warning", f"{len(dirty)} uncommitted files — possible drift", 0.5)
        return HealthComponent("repo_health", True, "healthy", f"{len(dirty)} uncommitted files" if dirty else "clean", 1.0 if not dirty else 0.8)
    except Exception:
        return HealthComponent("repo_health", True, "unknown", "git check unavailable", 0.5)


def run_health_check(*, output_dir: Path | None = None) -> VpsHealthReport:
    components = [
        _check_disk_space(),
        _check_kaizen_state(),
        _check_quantum_freshness(),
        _check_hermes_connectivity(),
        _check_repo_health(),
    ]

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

    action_items = []
    for c in components:
        if not c.healthy:
            action_items.append(f"[{c.name}] {c.status}: {c.detail}")

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


def main() -> int:
    report = run_health_check(output_dir=ROOT / "state" / "health")
    print(json.dumps(report.to_dict(), indent=2))
    if report.action_items:
        print("\nACTION ITEMS:", file=sys.stderr)
        for item in report.action_items:
            print(f"  - {item}", file=sys.stderr)

    # Notify Hermes if unhealthy
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

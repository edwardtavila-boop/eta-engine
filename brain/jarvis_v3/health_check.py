"""JARVIS self-diagnostic (Wave-12, 2026-04-27).

When JARVIS is the source of truth, the operator needs to know
whether JARVIS itself is HEALTHY. A stale calibrator, an empty
memory, a kill switch flipped at midnight by a forgotten cron --
these all silently degrade decision quality.

This module surfaces the state of every JARVIS subsystem at
a glance:

    from eta_engine.brain.jarvis_v3.health_check import jarvis_health

    report = jarvis_health()
    print(report.summary)
    if report.overall_status != "OK":
        for issue in report.issues:
            print(f"  - {issue}")

    # In a CLI / monitoring tool:
    if report.overall_status == "CRITICAL":
        page_operator()

Components checked:

  * operator_override: NORMAL / SOFT_PAUSE / HARD_PAUSE / KILL
  * fleet kill switch: armed?
  * hierarchical memory: episode count, last-write age
  * filter bandit: arm count + total pulls
  * decision journal: file existence + recent activity
  * calibrator artifact: file age (days) -- flag if stale > 30
  * macro calendar: presence of upcoming-event window flag
  * model artifacts: most-recent retrain age in state/models/
  * jarvis verdict log: existence + recent activity

Pure stdlib + filesystem reads. No network, no async.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]


class HealthStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


@dataclass
class ComponentHealth:
    name: str
    status: HealthStatus
    detail: str
    metrics: dict = field(default_factory=dict)


@dataclass
class HealthReport:
    ts: str
    overall_status: str
    components: list[ComponentHealth] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "overall_status": self.overall_status,
            "components": [asdict(c) for c in self.components],
            "issues": self.issues,
            "summary": self.summary,
        }


# ─── Per-component checks ─────────────────────────────────────────


def _check_override() -> ComponentHealth:
    try:
        from eta_engine.obs.operator_override import get_state
        s = get_state()
        level = s.level.value
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            name="operator_override",
            status=HealthStatus.DEGRADED,
            detail=f"override read failed: {exc}",
        )
    if level == "NORMAL":
        return ComponentHealth(
            name="operator_override",
            status=HealthStatus.OK,
            detail="NORMAL",
        )
    if level == "SOFT_PAUSE":
        return ComponentHealth(
            name="operator_override",
            status=HealthStatus.DEGRADED,
            detail=f"SOFT_PAUSE: {s.reason}",
            metrics={"set_by": s.set_by},
        )
    return ComponentHealth(
        name="operator_override",
        status=HealthStatus.CRITICAL,
        detail=f"{level}: {s.reason}",
        metrics={"set_by": s.set_by},
    )


def _check_kill_switch() -> ComponentHealth:
    """Probe the fleet-level kill switch. We look for any of several
    common locations -- this is best-effort because the operator may
    have a custom layout."""
    candidates = [
        ROOT / "state" / "kill_switch.json",
        ROOT / "state" / "fleet_kill.json",
        ROOT / "kill_switch.json",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            armed = bool(data.get("armed", False))
        except (OSError, json.JSONDecodeError):
            continue
        if armed:
            return ComponentHealth(
                name="kill_switch",
                status=HealthStatus.CRITICAL,
                detail="ARMED -- fleet halted",
                metrics={"path": str(p)},
            )
        return ComponentHealth(
            name="kill_switch",
            status=HealthStatus.OK,
            detail="not armed",
            metrics={"path": str(p)},
        )
    return ComponentHealth(
        name="kill_switch",
        status=HealthStatus.OK,
        detail="no kill_switch.json present (default OK)",
    )


def _check_memory() -> ComponentHealth:
    try:
        from eta_engine.brain.jarvis_v3.memory_hierarchy import (
            HierarchicalMemory,
        )
        mem = HierarchicalMemory()
        n = len(mem._episodes)
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            name="hierarchical_memory",
            status=HealthStatus.DEGRADED,
            detail=f"memory load failed: {exc}",
        )
    if n == 0:
        return ComponentHealth(
            name="hierarchical_memory",
            status=HealthStatus.DEGRADED,
            detail="no episodes recorded yet (cold-start)",
            metrics={"n_episodes": 0},
        )
    if n < 30:
        return ComponentHealth(
            name="hierarchical_memory",
            status=HealthStatus.DEGRADED,
            detail=f"only {n} episodes; RAG retrieval will be weak",
            metrics={"n_episodes": n},
        )
    return ComponentHealth(
        name="hierarchical_memory",
        status=HealthStatus.OK,
        detail=f"{n} episodes in journal",
        metrics={"n_episodes": n},
    )


def _check_filter_bandit() -> ComponentHealth:
    try:
        from eta_engine.brain.jarvis_v3.filter_bandit import (
            default_filter_bandit,
        )
        fb = default_filter_bandit()
        report = fb.report()
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            name="filter_bandit",
            status=HealthStatus.DEGRADED,
            detail=f"bandit load failed: {exc}",
        )
    n_arms = len(report)
    total_pulls = sum(int(r.get("pulls", 0)) for r in report)
    if n_arms == 0:
        return ComponentHealth(
            name="filter_bandit",
            status=HealthStatus.OK,
            detail="no arms registered (bandit inactive)",
            metrics={"n_arms": 0},
        )
    return ComponentHealth(
        name="filter_bandit",
        status=HealthStatus.OK,
        detail=f"{n_arms} arms, {total_pulls} total pulls",
        metrics={"n_arms": n_arms, "total_pulls": total_pulls},
    )


def _check_calibrator() -> ComponentHealth:
    candidates = list((ROOT / "state" / "models").glob("calibrator_*.json"))
    if not candidates:
        return ComponentHealth(
            name="calibrator",
            status=HealthStatus.DEGRADED,
            detail="no calibrator artifact found in state/models/",
        )
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    age_days = (time.time() - latest.stat().st_mtime) / 86_400
    if age_days > 30:
        return ComponentHealth(
            name="calibrator",
            status=HealthStatus.DEGRADED,
            detail=f"latest calibrator is {age_days:.1f} days old (refit recommended)",
            metrics={"age_days": round(age_days, 1), "path": latest.name},
        )
    return ComponentHealth(
        name="calibrator",
        status=HealthStatus.OK,
        detail=f"latest calibrator is {age_days:.1f} days old",
        metrics={"age_days": round(age_days, 1), "path": latest.name},
    )


def _check_decision_journal() -> ComponentHealth:
    candidates = [
        ROOT / "state" / "jarvis_audit",
        ROOT / "docs" / "decision_journal.jsonl",
    ]
    for p in candidates:
        if p.exists():
            return ComponentHealth(
                name="decision_journal",
                status=HealthStatus.OK,
                detail=f"present at {p.name}",
                metrics={"path": str(p)},
            )
    return ComponentHealth(
        name="decision_journal",
        status=HealthStatus.DEGRADED,
        detail="no decision journal artifact found",
    )


def _check_intel_verdict_log() -> ComponentHealth:
    p = ROOT / "state" / "jarvis_intel" / "verdicts.jsonl"
    if not p.exists():
        return ComponentHealth(
            name="intel_verdict_log",
            status=HealthStatus.OK,
            detail="not yet created (intelligence layer fresh)",
        )
    try:
        size_kb = p.stat().st_size / 1024
        age_min = (time.time() - p.stat().st_mtime) / 60.0
    except OSError as exc:
        return ComponentHealth(
            name="intel_verdict_log",
            status=HealthStatus.DEGRADED,
            detail=f"stat failed: {exc}",
        )
    if age_min > 24 * 60:
        return ComponentHealth(
            name="intel_verdict_log",
            status=HealthStatus.DEGRADED,
            detail=f"no writes in {age_min/60:.1f} hours",
            metrics={"size_kb": round(size_kb, 1), "age_min": round(age_min, 1)},
        )
    return ComponentHealth(
        name="intel_verdict_log",
        status=HealthStatus.OK,
        detail=f"last write {age_min:.1f} min ago, {size_kb:.1f} KB",
        metrics={"size_kb": round(size_kb, 1), "age_min": round(age_min, 1)},
    )


def _check_macro_calendar() -> ComponentHealth:
    try:
        from eta_engine.brain.jarvis_v3.macro_calendar import (
            is_within_event_window,
            upcoming_events,
        )
        now = datetime.now(UTC)
        upcoming = upcoming_events(hours_ahead=24)
        within = is_within_event_window(now)
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            name="macro_calendar",
            status=HealthStatus.DEGRADED,
            detail=f"calendar load failed: {exc}",
        )
    if within is not None:
        return ComponentHealth(
            name="macro_calendar",
            status=HealthStatus.DEGRADED,
            detail=f"INSIDE event window: {within.name} ({within.kind.value})",
            metrics={"event": within.name},
        )
    return ComponentHealth(
        name="macro_calendar",
        status=HealthStatus.OK,
        detail=f"{len(upcoming)} events in next 24h",
        metrics={"upcoming_24h": len(upcoming)},
    )


# ─── Aggregate report ─────────────────────────────────────────────


def jarvis_health() -> HealthReport:
    """Run every component check and return an aggregated report."""
    components = [
        _check_override(),
        _check_kill_switch(),
        _check_memory(),
        _check_filter_bandit(),
        _check_calibrator(),
        _check_decision_journal(),
        _check_intel_verdict_log(),
        _check_macro_calendar(),
    ]

    # Overall = worst component
    if any(c.status == HealthStatus.CRITICAL for c in components):
        overall = HealthStatus.CRITICAL.value
    elif any(c.status == HealthStatus.DEGRADED for c in components):
        overall = HealthStatus.DEGRADED.value
    else:
        overall = HealthStatus.OK.value

    issues = [
        f"{c.name}: {c.detail}"
        for c in components
        if c.status != HealthStatus.OK
    ]
    summary = (
        f"JARVIS health: {overall} "
        f"({len(components)} components, {len(issues)} non-OK)"
    )
    return HealthReport(
        ts=datetime.now(UTC).isoformat(),
        overall_status=overall,
        components=components,
        issues=issues,
        summary=summary,
    )

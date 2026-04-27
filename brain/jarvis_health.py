"""JARVIS self-test."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class HealthCheckResult(BaseModel):
    name: str = Field(min_length=1)
    passed: bool
    detail: str = ""


class HealthVerdict(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS = _REPO_ROOT / "eta_engine" / "docs"

_CRITICAL_CHECKS: frozenset[str] = frozenset(
    {
        "session_state_module_imports",
        "session_state_snapshot_constructs",
        "model_policy_routes",
    }
)


def _check_session_state_imports() -> HealthCheckResult:
    try:
        from eta_engine.brain import jarvis_session_state  # noqa: F401

        return HealthCheckResult(
            name="session_state_module_imports",
            passed=True,
            detail="eta_engine.brain.jarvis_session_state imports cleanly",
        )
    except Exception as exc:  # noqa: BLE001
        return HealthCheckResult(
            name="session_state_module_imports",
            passed=False,
            detail=f"import failure: {exc}",
        )


def _check_session_state_snapshot() -> HealthCheckResult:
    try:
        from eta_engine.brain.jarvis_session_state import snapshot

        snap = snapshot()
        assert snap.iteration_phase is not None
        assert snap.slow_bleed_level is not None
        return HealthCheckResult(
            name="session_state_snapshot_constructs",
            passed=True,
            detail=(
                f"phase={snap.iteration_phase.value} bleed={snap.slow_bleed_level.value} "
                f"trials={snap.cumulative_trials}"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return HealthCheckResult(
            name="session_state_snapshot_constructs",
            passed=False,
            detail=f"snapshot() raised: {exc}",
        )


def _check_model_policy() -> HealthCheckResult:
    try:
        from eta_engine.brain.model_policy import TaskCategory, select_model

        s_search = select_model(TaskCategory.RED_TEAM_SCORING, iteration_phase="search")
        s_deploy = select_model(TaskCategory.RED_TEAM_SCORING, iteration_phase="deployment")
        if s_search.tier.value != "opus" or s_deploy.tier.value != "sonnet":
            return HealthCheckResult(
                name="model_policy_routes",
                passed=False,
                detail=f"phase-aware demotion broken: search={s_search.tier.value}, deploy={s_deploy.tier.value}",
            )
        return HealthCheckResult(
            name="model_policy_routes",
            passed=True,
            detail="phase-aware demotion working (search=opus, deploy=sonnet)",
        )
    except Exception as exc:  # noqa: BLE001
        return HealthCheckResult(
            name="model_policy_routes",
            passed=False,
            detail=f"select_model raised: {exc}",
        )


def _check_trial_log_readable() -> HealthCheckResult:
    p = _DOCS / "trial_log.json"
    if not p.exists():
        return HealthCheckResult(
            name="trial_log_readable",
            passed=True,
            detail=f"{p.name} does not exist (fresh repo OK)",
        )
    try:
        import json

        json.loads(p.read_text())
        return HealthCheckResult(name="trial_log_readable", passed=True, detail="parses cleanly")
    except Exception as exc:  # noqa: BLE001
        return HealthCheckResult(name="trial_log_readable", passed=False, detail=f"parse failed: {exc}")


def _check_gate_reports_readable() -> HealthCheckResult:
    if not _DOCS.exists():
        return HealthCheckResult(name="gate_reports_readable", passed=True, detail="docs/ missing (fresh)")
    candidates = list(_DOCS.glob("gate_report_*.json"))
    if not candidates:
        return HealthCheckResult(name="gate_reports_readable", passed=True, detail="no reports yet")
    bad: list[str] = []
    import json

    for p in candidates:
        try:
            json.loads(p.read_text())
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{p.name}: {exc}")
    if bad:
        return HealthCheckResult(
            name="gate_reports_readable", passed=False, detail=f"{len(bad)} corrupt: " + "; ".join(bad[:3])
        )
    return HealthCheckResult(
        name="gate_reports_readable", passed=True, detail=f"{len(candidates)} reports parse cleanly"
    )


def run_self_test() -> tuple[list[HealthCheckResult], HealthVerdict]:
    results = [
        _check_session_state_imports(),
        _check_session_state_snapshot(),
        _check_model_policy(),
        _check_trial_log_readable(),
        _check_gate_reports_readable(),
    ]
    failed_critical = [r for r in results if not r.passed and r.name in _CRITICAL_CHECKS]
    failed_noncrit = [r for r in results if not r.passed and r.name not in _CRITICAL_CHECKS]
    if failed_critical:
        verdict = HealthVerdict.UNHEALTHY
    elif failed_noncrit:
        verdict = HealthVerdict.DEGRADED
    else:
        verdict = HealthVerdict.HEALTHY
    return results, verdict

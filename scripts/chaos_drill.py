"""
EVOLUTIONARY TRADING ALGO  //  scripts.chaos_drill
======================================
Monthly chaos drill runner. Deliberately trips each safety component in
an isolated sandbox, verifies the downstream alerts fire, and reports
pass/fail.

Why this exists
---------------
The breaker, deadman, drift detector, and push bus are each supposed
to *trip loudly* when their underlying precondition fails. But they
only run in production -- they never get tested against their actual
wiring. When the breaker silently regresses (say, a refactor kills
the ``reason_code == 'jarvis_denied'`` branch) nobody notices until
a real outage, which is exactly when a silent breaker is most
expensive.

This module runs through a catalogue of "break it on purpose"
scenarios against temp-dir sandboxes and asserts:

* The component transitioned into its failure state.
* The shared-state file / sentinel / journal reflected that.
* A push alert made it to the notifier of record.

Run monthly (cron or CI) before the month's first live session.

Drills
------
1. ``breaker``  -- Trip a SharedCircuitBreaker and verify the state
                   propagates cross-process via ``~/.jarvis/breaker.json``
                   (simulated in a tempdir).
2. ``deadman``  -- Stamp an aged sentinel and verify the switch flips
                   to FROZEN.
3. ``push``     -- Fire an alert through a LocalFileNotifier and verify
                   it landed in the alerts journal with the right fields.
4. ``drift``    -- Feed a divergent live-returns series to the detector
                   and verify it recommends DEMOTE.

``chaos_drill all`` runs every drill.

Exit codes
----------
* 0 -- all drills passed
* 1 -- any drill failed
* 2 -- argument error

CLI
---
::

    python -m eta_engine.scripts.chaos_drill all
    python -m eta_engine.scripts.chaos_drill breaker
    python -m eta_engine.scripts.chaos_drill deadman push drift
    python -m eta_engine.scripts.chaos_drill all --json
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eta_engine.brain.avengers.base import PersonaId, TaskResult
from eta_engine.brain.avengers.circuit_breaker import BreakerState, BreakerTripped
from eta_engine.brain.avengers.deadman import DeadmanState, DeadmanSwitch
from eta_engine.brain.avengers.drift_detector import DriftDetector, DriftVerdict
from eta_engine.brain.avengers.promotion import PromotionAction
from eta_engine.brain.avengers.push import (
    AlertLevel,
    LocalFileNotifier,
    PushBus,
)
from eta_engine.brain.avengers.shared_breaker import (
    SharedCircuitBreaker,
    read_shared_status,
)
from eta_engine.scripts.chaos_drills import (
    drill_cftc_nfa_compliance,
    drill_firm_gate,
    drill_kill_switch_runtime,
    drill_live_shadow_guard,
    drill_oos_qualifier,
    drill_order_state_reconcile,
    drill_pnl_drift,
    drill_risk_engine,
    drill_runtime_allowlist,
    drill_shadow_paper_tracker,
    drill_smart_router,
    drill_two_factor,
)

ALL_DRILLS: tuple[str, ...] = (
    # Pre-v0.1.56 drills.
    "breaker",
    "deadman",
    "push",
    "drift",
    # v0.1.56 CHAOS DRILL CLOSURE: one drill per safety surface.
    "kill_switch_runtime",
    "risk_engine",
    "order_state_reconcile",
    "cftc_nfa_compliance",
    "two_factor",
    "smart_router",
    "firm_gate",
    "oos_qualifier",
    "shadow_paper_tracker",
    "live_shadow_guard",
    "pnl_drift",
    "runtime_allowlist",
)


# ---------------------------------------------------------------------------
# drill result type
# ---------------------------------------------------------------------------


def _result(
    name: str,
    *,
    passed: bool,
    details: str,
    observed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "drill": name,
        "passed": passed,
        "details": details,
        "observed": observed or {},
        "ts": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# individual drills
# ---------------------------------------------------------------------------


def drill_breaker(sandbox: Path) -> dict[str, Any]:
    """Trip a SharedCircuitBreaker; verify disk propagation."""
    breaker_path = sandbox / "breaker.json"
    a = SharedCircuitBreaker(
        path=breaker_path,
        max_consec_failures=2,
        cooldown_seconds=120,
    )
    b = SharedCircuitBreaker(
        path=breaker_path,
        max_consec_failures=2,
        cooldown_seconds=120,
    )

    # Feed 2 failures into A.
    for i in range(2):
        a.record(
            TaskResult(
                task_id=f"chaos_{i}",
                persona_id=PersonaId.ALFRED,
                tier_used=None,
                success=False,
                artifact="",
                reason_code="error",
                reason=f"chaos_failure_{i}",
                cost_multiplier=0.0,
                jarvis_verdict=None,
                ms_elapsed=1.0,
                ts=datetime.now(UTC),
            )
        )

    # A should be OPEN.
    if a.status().state is not BreakerState.OPEN:
        return _result(
            "breaker",
            passed=False,
            details=f"A failed to trip: state={a.status().state}",
        )

    # Disk should reflect OPEN.
    disk = read_shared_status(path=breaker_path)
    if disk is None or disk.get("state") != "OPEN":
        return _result(
            "breaker",
            passed=False,
            details=f"disk did not record OPEN: {disk}",
        )

    # B's pre_dispatch must raise BreakerTripped (adopted from disk).
    try:
        b.pre_dispatch()
    except BreakerTripped as exc:
        return _result(
            "breaker",
            passed=True,
            details="A tripped, disk propagated, B adopted and refused",
            observed={
                "a_state": a.status().state.value,
                "disk_state": disk.get("state"),
                "b_raised": str(exc),
                "tripped_at": disk.get("tripped_at"),
                "last_reason": disk.get("last_reason"),
            },
        )
    return _result(
        "breaker",
        passed=False,
        details="B did not raise BreakerTripped after disk OPEN",
    )


def drill_deadman(sandbox: Path) -> dict[str, Any]:
    """Stamp an aged sentinel; verify the switch flips to FROZEN."""
    sentinel = sandbox / "operator.sentinel"
    journal = sandbox / "operator_activity.jsonl"
    # Create sentinel with mtime 96h in the past (past the 72h FROZEN line).
    sentinel.write_text("chaos", encoding="utf-8")
    t96h_ago = (datetime.now(UTC) - timedelta(hours=96)).timestamp()
    import os as _os

    _os.utime(sentinel, (t96h_ago, t96h_ago))

    sw = DeadmanSwitch(sentinel_path=sentinel, journal_path=journal)
    status = sw.status()
    if status.state is not DeadmanState.FROZEN:
        return _result(
            "deadman",
            passed=False,
            details=(
                f"aged sentinel did not trigger FROZEN: state={status.state.value} hours_since={status.hours_since:.1f}"
            ),
        )
    return _result(
        "deadman",
        passed=True,
        details="aged sentinel correctly produced FROZEN state",
        observed={
            "state": status.state.value,
            "hours_since": round(status.hours_since, 2),
            "sentinel_age_target_hours": 96,
        },
    )


def drill_push(sandbox: Path) -> dict[str, Any]:
    """Fire a CRITICAL alert through LocalFileNotifier; verify the journal."""
    journal = sandbox / "alerts.jsonl"
    notifier = LocalFileNotifier(path=journal)
    bus = PushBus(notifiers=[notifier])
    bus.push(
        level=AlertLevel.CRITICAL,
        title="chaos_drill: push test",
        body="if you see this, the push bus is wired correctly",
        source="chaos_drill",
        tags=["chaos", "drill"],
    )
    if not journal.exists():
        return _result(
            "push",
            passed=False,
            details=f"alerts journal not created at {journal}",
        )
    lines = [ln for ln in journal.read_text().splitlines() if ln.strip()]
    if not lines:
        return _result(
            "push",
            passed=False,
            details="alerts journal empty after push",
        )
    try:
        rec = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return _result(
            "push",
            passed=False,
            details=f"last alert line is not JSON: {exc}",
        )
    if rec.get("level") != "CRITICAL":
        return _result(
            "push",
            passed=False,
            details=f"alert level wrong: {rec.get('level')!r}",
        )
    if "chaos" not in (rec.get("tags") or []):
        return _result(
            "push",
            passed=False,
            details=f"alert tags missing 'chaos': {rec.get('tags')!r}",
        )
    return _result(
        "push",
        passed=True,
        details="CRITICAL alert landed in journal with correct fields",
        observed={
            "records": len(lines),
            "level": rec.get("level"),
            "title": rec.get("title"),
            "tags": rec.get("tags"),
        },
    )


def drill_drift(sandbox: Path) -> dict[str, Any]:
    """Feed divergent returns to DriftDetector; verify AUTO_DEMOTE."""
    import random

    journal = sandbox / "drift.jsonl"
    det = DriftDetector(journal_path=journal)

    # Strong backtest (positive drift, tight vol) vs bleeding live (negative).
    random.seed(1337)
    bt = [0.003 + random.gauss(0, 0.005) for _ in range(252)]
    lv = [-0.003 + random.gauss(0, 0.010) for _ in range(80)]

    report = det.check("chaos_strat", bt, lv)
    if report.verdict is not DriftVerdict.AUTO_DEMOTE:
        return _result(
            "drift",
            passed=False,
            details=(
                f"opposite-drift live series did not trigger AUTO_DEMOTE: "
                f"verdict={report.verdict.value} "
                f"sharpe_delta={report.sharpe_delta_sigma:.2f}sigma "
                f"KL={report.kl_divergence:.3f}"
            ),
        )
    if report.recommendation is not PromotionAction.DEMOTE:
        return _result(
            "drift",
            passed=False,
            details=(f"AUTO_DEMOTE verdict not accompanied by DEMOTE recommendation: got {report.recommendation!r}"),
        )
    return _result(
        "drift",
        passed=True,
        details="opposite-drift live produced AUTO_DEMOTE + DEMOTE recommendation",
        observed={
            "verdict": report.verdict.value,
            "sharpe_bt": round(report.sharpe_bt, 2),
            "sharpe_live": round(report.sharpe_live, 2),
            "sharpe_delta_sigma": round(report.sharpe_delta_sigma, 2),
            "kl_divergence": round(report.kl_divergence, 3),
            "recommendation": report.recommendation.value,
        },
    )


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


DRILL_FUNCS: dict[str, Any] = {
    "breaker": drill_breaker,
    "deadman": drill_deadman,
    "push": drill_push,
    "drift": drill_drift,
    # v0.1.56 CHAOS DRILL CLOSURE: 12 surface-specific drills.
    "kill_switch_runtime": drill_kill_switch_runtime,
    "risk_engine": drill_risk_engine,
    "order_state_reconcile": drill_order_state_reconcile,
    "cftc_nfa_compliance": drill_cftc_nfa_compliance,
    "two_factor": drill_two_factor,
    "smart_router": drill_smart_router,
    "firm_gate": drill_firm_gate,
    "oos_qualifier": drill_oos_qualifier,
    "shadow_paper_tracker": drill_shadow_paper_tracker,
    "live_shadow_guard": drill_live_shadow_guard,
    "pnl_drift": drill_pnl_drift,
    "runtime_allowlist": drill_runtime_allowlist,
}


def run_drills(
    drills: list[str] | None = None,
    *,
    sandbox: Path | None = None,
) -> list[dict[str, Any]]:
    """Run the named drills in a sandbox. Returns list of result dicts.

    Parameters
    ----------
    drills
        List of drill names (``breaker``, ``deadman``, ``push``,
        ``drift``) to run. ``None`` or empty runs every drill.
    sandbox
        Temp directory to use. If ``None``, a fresh tempdir is created
        and cleaned up at exit.
    """
    names = list(drills) if drills else list(ALL_DRILLS)
    unknown = [n for n in names if n not in DRILL_FUNCS]
    if unknown:
        return [_result(n, passed=False, details=f"unknown drill: {n}") for n in unknown]

    own_sandbox = sandbox is None
    if own_sandbox:
        sandbox = Path(tempfile.mkdtemp(prefix="chaos_drill_"))
    else:
        sandbox.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    try:
        for name in names:
            # Isolate each drill in its own sub-sandbox so drills don't
            # collide on file paths (e.g., two drills writing breaker.json).
            sub = sandbox / name
            sub.mkdir(parents=True, exist_ok=True)
            try:
                results.append(DRILL_FUNCS[name](sub))
            except Exception as exc:  # noqa: BLE001 - report and move on
                results.append(
                    _result(
                        name,
                        passed=False,
                        details=f"drill raised {type(exc).__name__}: {exc}",
                    )
                )
    finally:
        if own_sandbox and sandbox is not None:
            shutil.rmtree(sandbox, ignore_errors=True)
    return results


def format_report(results: list[dict[str, Any]]) -> str:
    """Pretty-print results as a fixed-width table (for CLI usage)."""
    lines: list[str] = []
    lines.append("EVOLUTIONARY TRADING ALGO // CHAOS DRILL")
    lines.append("=" * 58)
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        lines.append(f"[{status}] {r['drill']:8s}  {r['details']}")
        if r.get("observed"):
            for k, v in r["observed"].items():
                lines.append(f"         {k} = {v}")
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    lines.append("-" * 58)
    lines.append(f"SUMMARY: {passed}/{total} drills passed")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(__doc__ or "")
        return 0

    # Split into "drill names" and "flags".
    drill_args: list[str] = []
    as_json = False
    for a in argv:
        if a == "--json":
            as_json = True
        elif a.startswith("-"):
            sys.stderr.write(f"[chaos_drill] unknown flag: {a}\n")
            return 2
        else:
            drill_args.append(a)

    # Expand 'all' to every drill.
    drills = list(ALL_DRILLS) if not drill_args or drill_args == ["all"] else drill_args

    results = run_drills(drills)
    failed = [r for r in results if not r["passed"]]

    if as_json:
        sys.stdout.write(
            json.dumps(
                {
                    "results": results,
                    "passed": len(results) - len(failed),
                    "failed": len(failed),
                    "total": len(results),
                },
                indent=2,
            )
            + "\n"
        )
    else:
        sys.stdout.write(format_report(results) + "\n")

    return 0 if not failed else 1


__all__ = [
    "ALL_DRILLS",
    "DRILL_FUNCS",
    "drill_breaker",
    "drill_cftc_nfa_compliance",
    "drill_deadman",
    "drill_drift",
    "drill_firm_gate",
    "drill_kill_switch_runtime",
    "drill_live_shadow_guard",
    "drill_oos_qualifier",
    "drill_order_state_reconcile",
    "drill_pnl_drift",
    "drill_push",
    "drill_risk_engine",
    "drill_runtime_allowlist",
    "drill_shadow_paper_tracker",
    "drill_smart_router",
    "drill_two_factor",
    "format_report",
    "main",
    "run_drills",
]


if __name__ == "__main__":
    raise SystemExit(main())

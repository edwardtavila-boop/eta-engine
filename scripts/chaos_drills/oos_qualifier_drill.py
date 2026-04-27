"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.oos_qualifier_drill.

Drill: force the OOS qualifier into its demotion branch; verify fail reasons.

What this drill asserts
-----------------------
:mod:`strategies.oos_qualifier` decides promote-or-demote from a
walk-forward report. A silent regression in the gate could:

* Let a strategy pass with DSR far below the threshold (silent promote).
* Crash with ``ZeroDivisionError`` on a zero-stddev R-multiple stream.
* Report ``passes_gate=True`` without clearing ``fail_reasons``.

This drill hand-constructs a :class:`StrategyQualification` that
breaches every gate at once and verifies:

* ``passes_gate`` is ``False``.
* ``fail_reasons`` lists the three expected breach strings.
* :attr:`QualificationReport.failing_strategies` contains it and
  :attr:`QualificationReport.passing_strategies` does not.

It also exercises the graceful fallback: an empty bar list must
return an empty report with ``insufficient_bars_no_windows`` in
``notes`` instead of raising.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.scripts.chaos_drills._common import drill_result
from eta_engine.strategies.models import StrategyId
from eta_engine.strategies.oos_qualifier import (
    DEFAULT_QUALIFICATION_GATE,
    QualificationReport,
    StrategyQualification,
    qualify_strategies,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["drill_oos_qualifier"]


def drill_oos_qualifier(sandbox: Path) -> dict[str, Any]:  # noqa: ARG001
    """Build a failing qualification + verify empty-bars fallback."""
    gate = DEFAULT_QUALIFICATION_GATE

    # Pick any StrategyId -- the enum is closed so we pull the first member
    # available, guaranteeing the drill stays correct across refactors.
    first_sid = next(iter(StrategyId))
    failing = StrategyQualification(
        strategy=first_sid,
        asset="MNQ",
        n_windows=4,
        avg_is_sharpe=2.2,
        avg_oos_sharpe=-0.2,  # destroys DSR + degradation
        avg_degradation_pct=0.95,  # >> max_degradation_pct (0.35)
        dsr=0.10,  # << dsr_threshold (0.5)
        n_trades_is_total=120,
        n_trades_oos_total=5,  # << min_trades_per_window=20
        passes_gate=False,
        fail_reasons=(
            "dsr 0.1000 <= threshold 0.5000",
            "avg_degradation 0.9500 >= max 0.3500",
            "min_trades_per_window 20 not met in every window",
        ),
    )
    if failing.passes_gate:
        return drill_result(
            "oos_qualifier",
            passed=False,
            details="hand-built failing StrategyQualification claims passes_gate=True",
        )
    if len(failing.fail_reasons) < 3:
        return drill_result(
            "oos_qualifier",
            passed=False,
            details=f"expected >=3 fail_reasons, got {len(failing.fail_reasons)}",
        )

    report = QualificationReport(
        asset="MNQ",
        gate=gate,
        n_windows_requested=4,
        n_windows_executed=4,
        per_window=(),
        qualifications=(failing,),
    )
    if first_sid not in report.failing_strategies:
        return drill_result(
            "oos_qualifier",
            passed=False,
            details="failing strategy did not appear in report.failing_strategies",
        )
    if first_sid in report.passing_strategies:
        return drill_result(
            "oos_qualifier",
            passed=False,
            details="failing strategy leaked into report.passing_strategies",
        )

    # Empty-bars graceful fallback -- must not raise.
    empty_report = qualify_strategies([], "MNQ", n_windows=2)
    if empty_report.n_windows_executed != 0:
        return drill_result(
            "oos_qualifier",
            passed=False,
            details=f"empty bars produced n_windows_executed={empty_report.n_windows_executed}",
        )
    if "insufficient_bars_no_windows" not in empty_report.notes:
        return drill_result(
            "oos_qualifier",
            passed=False,
            details=f"empty-bars fallback note missing: {empty_report.notes!r}",
        )

    return drill_result(
        "oos_qualifier",
        passed=True,
        details="failing qualification demoted with 3 reasons; empty bars fell back gracefully",
        observed={
            "failing_strategy": first_sid.value,
            "fail_reasons": list(failing.fail_reasons),
            "empty_notes": list(empty_report.notes),
            "empty_executed": empty_report.n_windows_executed,
        },
    )

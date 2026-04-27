"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.firm_gate_drill.

Drill: trip the Firm GO/KILL toggle; verify KILL blocks promotion.

What this drill asserts
-----------------------
:func:`brain.sweep_firm_gate.apply_firm_gate` is the typed seam between
tier-B parameter sweeps and the 6-agent Firm board. Its guarantees are:

* A ``GO`` verdict must set ``promotes == True`` so the sweep promotes
  the candidate.
* A ``KILL`` verdict must set ``promotes == False`` so the sweep drops
  it, even when the board-side runner technically ran to completion.
* A runner that raises must be absorbed into a deterministic ``HOLD``
  fallback (no exception leakage, no silent promote).
* A ``None`` runner (missing Firm package) must emit a ``HOLD`` so the
  sweep can decide whether to fall back to metric-only promotion.

A silent regression would either let a KILL'd candidate slip through
or crash the sweep on a bad board run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.brain.sweep_firm_gate import (
    FirmVerdict,
    FirmVerdictCode,
    SweepCandidate,
    apply_firm_gate,
    filter_go_verdicts,
)
from eta_engine.scripts.chaos_drills._common import drill_result

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["drill_firm_gate"]


def _candidate(strategy_id: str) -> SweepCandidate:
    return SweepCandidate(
        strategy_id=strategy_id,
        params={"risk_pct": 0.01},
        metrics={"sharpe": 1.6, "dsr": 0.62},
        seed=1337,
    )


def drill_firm_gate(sandbox: Path) -> dict[str, Any]:  # noqa: ARG001
    """Exercise GO / KILL / raise / None paths on apply_firm_gate."""
    go = apply_firm_gate(
        _candidate("strat_go"),
        board_runner=lambda c: FirmVerdict(
            code=FirmVerdictCode.GO,
            confidence=0.9,
            reasons=(f"{c.strategy_id} cleared all agents",),
        ),
    )
    if not go.promotes or go.code is not FirmVerdictCode.GO:
        return drill_result(
            "firm_gate",
            passed=False,
            details=f"GO runner did not promote (code={go.code}, promotes={go.promotes})",
        )

    kill = apply_firm_gate(
        _candidate("strat_kill"),
        board_runner=lambda c: FirmVerdict(  # noqa: ARG005
            code=FirmVerdictCode.KILL,
            confidence=0.95,
            reasons=("red team vetoed",),
        ),
    )
    if kill.promotes or kill.code is not FirmVerdictCode.KILL:
        return drill_result(
            "firm_gate",
            passed=False,
            details=f"KILL runner was treated as promotable (promotes={kill.promotes})",
        )

    def raising_runner(_c: SweepCandidate) -> FirmVerdict:
        raise RuntimeError("board crashed")

    raised = apply_firm_gate(_candidate("strat_raise"), board_runner=raising_runner)
    if raised.promotes or raised.code is not FirmVerdictCode.HOLD:
        return drill_result(
            "firm_gate",
            passed=False,
            details=(
                f"raising runner was not coerced to HOLD fallback (code={raised.code}, promotes={raised.promotes})"
            ),
        )

    missing = apply_firm_gate(_candidate("strat_no_runner"), board_runner=None)
    if missing.promotes or missing.code is not FirmVerdictCode.HOLD:
        return drill_result(
            "firm_gate",
            passed=False,
            details=f"None runner did not HOLD (code={missing.code}, promotes={missing.promotes})",
        )

    # filter_go_verdicts must return only the GO candidate.
    bundle = [
        (_candidate("strat_go"), go),
        (_candidate("strat_kill"), kill),
        (_candidate("strat_raise"), raised),
        (_candidate("strat_no_runner"), missing),
    ]
    promoted = filter_go_verdicts(bundle)
    if len(promoted) != 1 or promoted[0].strategy_id != "strat_go":
        return drill_result(
            "firm_gate",
            passed=False,
            details=f"filter_go_verdicts returned {[c.strategy_id for c in promoted]}",
        )

    return drill_result(
        "firm_gate",
        passed=True,
        details="GO promoted; KILL / raising runner / None runner all coerced to a non-promoting verdict",
        observed={
            "go": go.code.value,
            "kill": kill.code.value,
            "raised": raised.code.value,
            "missing": missing.code.value,
            "promoted_ids": [c.strategy_id for c in promoted],
        },
    )

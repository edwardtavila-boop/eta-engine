"""EVOLUTIONARY TRADING ALGO  //  brain.sweep_firm_gate.

Thin adapter that lets tier-B parameter sweeps feed each candidate through
the 6-agent Firm board before promotion.

Why this module exists
----------------------
:mod:`scripts.tier_b_param_sweep` produces many candidate param sets per
strategy. Today each candidate is scored on walk-forward metrics alone.
:mod:`scripts.engage_firm_board` runs the 6-agent adversarial board --
but as a one-shot per-strategy invocation, not per-candidate.

This module closes that gap with a pure-function wrapper that takes a
sweep candidate (as a dict) and returns a :class:`FirmVerdict` --
``GO / HOLD / MODIFY / KILL`` plus confidence and reasons. The actual
agent invocations live in the existing Firm package; this module is
the typed boundary the sweep loop calls.

Design
------
* **Pure at the boundary.** The adapter accepts a callable
  ``board_runner`` that encapsulates the live Firm-board invocation.
  Tests inject a fake runner; production wires the real one.
* **Deterministic fallback.** When no board runner is configured (e.g.
  in CI without the Firm package mounted), the adapter emits a
  ``HOLD`` verdict with a descriptive reason rather than blocking.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "FirmVerdictCode",
    "FirmVerdict",
    "SweepCandidate",
    "apply_firm_gate",
]


class FirmVerdictCode(StrEnum):
    GO = "GO"
    HOLD = "HOLD"
    MODIFY = "MODIFY"
    KILL = "KILL"


@dataclass(frozen=True)
class SweepCandidate:
    """One row of tier-B sweep output."""

    strategy_id: str
    params: dict
    metrics: dict
    seed: int = 0


@dataclass(frozen=True)
class FirmVerdict:
    code: FirmVerdictCode
    confidence: float
    reasons: tuple[str, ...] = field(default_factory=tuple)
    agent_summary: dict = field(default_factory=dict)

    @property
    def promotes(self) -> bool:
        return self.code == FirmVerdictCode.GO


BoardRunner = Callable[[SweepCandidate], FirmVerdict | None]


def _default_hold_verdict(reason: str) -> FirmVerdict:
    return FirmVerdict(
        code=FirmVerdictCode.HOLD,
        confidence=0.5,
        reasons=(reason,),
    )


def apply_firm_gate(
    candidate: SweepCandidate,
    *,
    board_runner: BoardRunner | None = None,
) -> FirmVerdict:
    """Run ``candidate`` through the Firm board and return a structured verdict.

    If ``board_runner`` is ``None`` or returns ``None``, emits a neutral
    HOLD so the sweep loop can fall back to metric-only promotion without
    crashing.
    """
    if board_runner is None:
        return _default_hold_verdict("no board_runner configured (fallback HOLD)")
    try:
        verdict = board_runner(candidate)
    except Exception as exc:  # noqa: BLE001
        return _default_hold_verdict(f"board_runner raised: {exc!r}")
    if verdict is None:
        return _default_hold_verdict("board_runner returned None")
    return verdict


def filter_go_verdicts(
    results: list[tuple[SweepCandidate, FirmVerdict]],
) -> list[SweepCandidate]:
    """Return only candidates whose verdict promotes."""
    return [c for c, v in results if v.promotes]

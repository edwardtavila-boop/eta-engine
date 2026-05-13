"""Candidate policy v18 -- tighten CONDITIONAL caps in HIGH stress (2026-04-27).

Hypothesis (kaizen ticket-derived)
----------------------------------
The current champion (``v17``) returns CONDITIONAL with ``size_cap_mult``
in the 0.50 range during regime transitions. Realized R-multiples on
trades approved during stress_composite > 0.7 windows have shown a long
left tail -- the cap is too generous when the tape is whippy.

v18 changes EXACTLY one thing: when the verdict is CONDITIONAL AND
stress_composite > 0.7, the cap is tightened to ``min(0.35, current_cap)``.
Everything else (DENIED, DEFERRED, APPROVED, CONDITIONAL in normal stress)
is unchanged from v17.

Promotion path
--------------
1. This module auto-registers v18 on import
2. Bandit harness (``brain.jarvis_v3.bandit_harness``) allocates 10% of
   decisions to v18 when ``ETA_BANDIT_ENABLED=true``
3. Promotion gate (``scripts/score_policy_candidate.py``) replays the
   last 30 days of audit records through v18 and scores vs v17
4. Operator approves promotion via Resend alert; the live JarvisAdmin
   instance gets ``policy_version=18`` and the next `evaluate_v17`
   call in the pipeline is replaced by `evaluate_v18`.

The single ``HIGH_STRESS_CAP`` constant is the entire surface area of
this candidate. Sweeping it (0.30, 0.35, 0.40) by registering siblings
v18a/v18b/v18c gives the bandit a multi-arm comparison.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eta_engine.brain.jarvis_admin import (
    ActionRequest,
    ActionResponse,
    Verdict,
    evaluate_request,
)
from eta_engine.brain.jarvis_v3.candidate_policy import register_candidate

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_context import JarvisContext

#: Stress composite threshold above which we tighten CONDITIONAL caps.
HIGH_STRESS_THRESHOLD: float = 0.70

#: Cap multiplier in HIGH-stress regime.
HIGH_STRESS_CAP: float = 0.35


def evaluate_v18(req: ActionRequest, ctx: JarvisContext) -> ActionResponse:
    """v18: tighter CONDITIONAL caps when stress_composite > 0.7."""
    resp = evaluate_request(req, ctx)
    if resp.verdict != Verdict.CONDITIONAL:
        return resp
    composite = resp.stress_composite or (ctx.stress_score.composite if ctx.stress_score else 0.0)
    if composite <= HIGH_STRESS_THRESHOLD:
        return resp
    # Tighten the cap. ``min`` with current cap so we never RELAX an
    # already-tighter v17 cap (defensive: only ever ratchet down).
    current_cap = resp.size_cap_mult if resp.size_cap_mult is not None else 0.5
    new_cap = min(current_cap, HIGH_STRESS_CAP)
    if new_cap >= current_cap:
        return resp
    return resp.model_copy(
        update={
            "size_cap_mult": new_cap,
            "reason": f"{resp.reason} [v18 high-stress tighten {current_cap:.2f}->{new_cap:.2f}]",
            "conditions": [*resp.conditions, f"v18_cap_tightened_to_{new_cap:.2f}"],
        }
    )


register_candidate(
    "v18",
    evaluate_v18,
    parent_version=17,
    rationale=(
        f"tighten CONDITIONAL cap to {HIGH_STRESS_CAP:.2f} when "
        f"stress_composite > {HIGH_STRESS_THRESHOLD:.2f}; addresses "
        f"observed long left tail on regime-transition trades"
    ),
    metadata={
        "high_stress_threshold": HIGH_STRESS_THRESHOLD,
        "high_stress_cap": HIGH_STRESS_CAP,
        "kaizen_ticket": "KZN-2026-04-27-tighten-high-stress-conditional-caps",
    },
    overwrite=True,  # tolerate re-import in test fixtures
)

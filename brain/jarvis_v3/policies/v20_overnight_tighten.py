"""Candidate policy v20 -- overnight session tightening (2026-04-27).

Hypothesis
----------
Overnight + premarket sessions have thinner books and more headline-driven
gaps than RTH. The champion (v17) doesn't differentiate session phase
when sizing; CONDITIONAL caps in OVERNIGHT are the same as in OPEN_DRIVE.
But our journal evidence shows overnight CONDITIONAL approvals had a
materially worse R-multiple distribution.

v20 adds a single session-phase rule on top of v17:

    If session_phase is OVERNIGHT or PREMARKET AND verdict is CONDITIONAL,
    tighten size_cap_mult to min(current, OVERNIGHT_CAP).

Doesn't touch APPROVED orders (which already imply normal market
conditions). Doesn't touch DENIED/DEFERRED (those are already blocked).
"""
from __future__ import annotations

from eta_engine.brain.jarvis_admin import (
    ActionRequest,
    ActionResponse,
    Verdict,
    evaluate_request,
)
from eta_engine.brain.jarvis_context import JarvisContext, SessionPhase
from eta_engine.brain.jarvis_v3.candidate_policy import register_candidate

#: Sessions where v20 tightens. PREMARKET is included because the gap
#: from overnight close to RTH open is the highest-variance window.
OVERNIGHT_SESSIONS: frozenset[SessionPhase] = frozenset({
    SessionPhase.OVERNIGHT,
    SessionPhase.PREMARKET,
})

#: Tighter cap for overnight CONDITIONAL approvals.
OVERNIGHT_CAP: float = 0.40


def evaluate_v20(req: ActionRequest, ctx: JarvisContext) -> ActionResponse:
    """v20: tighten CONDITIONAL caps in OVERNIGHT + PREMARKET."""
    resp = evaluate_request(req, ctx)
    if resp.verdict != Verdict.CONDITIONAL:
        return resp
    if resp.session_phase not in OVERNIGHT_SESSIONS:
        return resp
    current_cap = resp.size_cap_mult if resp.size_cap_mult is not None else 0.5
    new_cap = min(current_cap, OVERNIGHT_CAP)
    if new_cap >= current_cap:
        return resp
    return resp.model_copy(update={
        "size_cap_mult": new_cap,
        "reason": f"{resp.reason} [v20 overnight-tighten {current_cap:.2f}->{new_cap:.2f}]",
        "conditions": [*resp.conditions, f"v20_overnight_tightened_to_{new_cap:.2f}"],
    })


register_candidate(
    "v20",
    evaluate_v20,
    parent_version=17,
    rationale=(
        f"tighten CONDITIONAL cap to {OVERNIGHT_CAP:.2f} in OVERNIGHT/PREMARKET; "
        f"thin books + headline-gap risk"
    ),
    metadata={
        "overnight_sessions": [s.value for s in sorted(OVERNIGHT_SESSIONS)],
        "overnight_cap": OVERNIGHT_CAP,
        "kaizen_ticket": "KZN-2026-04-27-overnight-cap-tightening",
    },
    overwrite=True,
)

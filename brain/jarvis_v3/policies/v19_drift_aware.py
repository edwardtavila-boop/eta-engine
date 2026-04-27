"""Candidate policy v19 -- drift-aware tightening (2026-04-27).

Hypothesis
----------
When ALFRED's drift detector / stress binding_constraint flags "drift"
or "regime_shift", the order-flow distribution is changing under us.
The champion (v17) treats drift as one ingredient of stress_composite,
but doesn't single it out -- a CONDITIONAL with composite=0.55 driven
by 0.10 vol + 0.45 drift gets the same cap as one driven by 0.30 vol +
0.25 drift, even though the drift-heavy case is materially riskier.

v19 adds a single rule on top of v17:

    If verdict is APPROVED or CONDITIONAL AND
    binding_constraint contains "drift" / "regime" / "shift" (case-insensitive),
    tighten size_cap_mult to min(current, DRIFT_CAP).

Promotion path
--------------
Bandit harness allocates fraction of decisions to v19 alongside v17/v18.
Replay scores cap_tightened_count + outcome distribution. v19 promotes
when its drift-window decisions outperform v17's by N R-multiples over
a 30-day window (see scripts/score_policy_candidate.py).
"""
from __future__ import annotations

from eta_engine.brain.jarvis_admin import (
    ActionRequest,
    ActionResponse,
    Verdict,
    evaluate_request,
)
from eta_engine.brain.jarvis_context import JarvisContext
from eta_engine.brain.jarvis_v3.candidate_policy import register_candidate

#: When binding_constraint matches any of these substrings (case-insensitive),
#: v19 treats the regime as drift-positive and tightens.
DRIFT_KEYWORDS: frozenset[str] = frozenset({"drift", "regime", "shift"})

#: Tightened cap when drift is the binding constraint.
DRIFT_CAP: float = 0.40


def _binding_is_drift(resp: ActionResponse, ctx: JarvisContext) -> bool:
    binding = (resp.binding_constraint or "").lower()
    if any(kw in binding for kw in DRIFT_KEYWORDS):
        return True
    # Also check ctx.stress_score in case the response didn't denormalize
    if ctx.stress_score is not None:
        ctx_binding = (ctx.stress_score.binding_constraint or "").lower()
        if any(kw in ctx_binding for kw in DRIFT_KEYWORDS):
            return True
    return False


def evaluate_v19(req: ActionRequest, ctx: JarvisContext) -> ActionResponse:
    """v19: tighten cap when drift is the binding constraint."""
    resp = evaluate_request(req, ctx)
    # Only consider risk-adding verdicts
    if resp.verdict not in (Verdict.APPROVED, Verdict.CONDITIONAL):
        return resp
    if not _binding_is_drift(resp, ctx):
        return resp
    current_cap = resp.size_cap_mult if resp.size_cap_mult is not None else 1.0
    new_cap = min(current_cap, DRIFT_CAP)
    if new_cap >= current_cap:
        return resp
    # If we tighten APPROVED into a sub-1.0 cap, that's now CONDITIONAL.
    new_verdict = Verdict.CONDITIONAL if new_cap < 1.0 else resp.verdict
    return resp.model_copy(update={
        "size_cap_mult": new_cap,
        "verdict": new_verdict,
        "reason": f"{resp.reason} [v19 drift-aware {current_cap:.2f}->{new_cap:.2f}]",
        "conditions": [*resp.conditions, f"v19_drift_tightened_to_{new_cap:.2f}"],
    })


register_candidate(
    "v19",
    evaluate_v19,
    parent_version=17,
    rationale=(
        f"tighten cap to {DRIFT_CAP:.2f} when binding_constraint indicates "
        f"drift/regime-shift (keywords={sorted(DRIFT_KEYWORDS)})"
    ),
    metadata={
        "drift_keywords": sorted(DRIFT_KEYWORDS),
        "drift_cap": DRIFT_CAP,
        "kaizen_ticket": "KZN-2026-04-27-drift-aware-cap-tightening",
    },
    overwrite=True,
)

"""Candidate policy v21 -- drawdown-proximity hard tightening (2026-04-27).

Hypothesis
----------
The champion (v17) treats drawdown as one stress component. When the
account is within striking distance of the kill threshold (e.g. 80% of
the max-DD limit consumed), every additional risk-adding trade has
asymmetric consequences: a small loss can flip the kill switch and
end the session, but the marginal upside of one more trade is bounded.

v21 adds an asymmetric rule on top of v17:

    If binding_constraint indicates "drawdown" / "dd" / "kill" (signaling
    the dd component is dominant) AND verdict is APPROVED or CONDITIONAL,
    DEFER the action (don't widen the hole).

This is intentionally STRICTER than v18/v19/v20 -- those tighten size,
this DEFERS entry. Rationale: when you're 80% of the way to ruin, sizing
down isn't enough; you need to wait for confirmation that the drawdown
isn't deepening before adding fresh risk.
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

#: Substrings (lowercase) in binding_constraint that indicate the
#: drawdown component is binding.
DD_KEYWORDS: frozenset[str] = frozenset({"drawdown", "dd", "kill", "max_dd"})


def _binding_is_drawdown(resp: ActionResponse, ctx: JarvisContext) -> bool:
    binding = (resp.binding_constraint or "").lower()
    if any(kw in binding for kw in DD_KEYWORDS):
        return True
    if ctx.stress_score is not None:
        ctx_binding = (ctx.stress_score.binding_constraint or "").lower()
        if any(kw in ctx_binding for kw in DD_KEYWORDS):
            return True
    return False


def evaluate_v21(req: ActionRequest, ctx: JarvisContext) -> ActionResponse:
    """v21: DEFER when drawdown is the binding constraint."""
    resp = evaluate_request(req, ctx)
    if resp.verdict not in (Verdict.APPROVED, Verdict.CONDITIONAL):
        return resp
    if not _binding_is_drawdown(resp, ctx):
        return resp
    return resp.model_copy(update={
        "verdict": Verdict.DEFERRED,
        "size_cap_mult": 0.0,
        "reason_code": "v21_dd_proximity_defer",
        "reason": (
            f"v21: drawdown binding ({resp.binding_constraint}) -- "
            f"deferring entry until DD pressure releases (was {resp.verdict.value})"
        ),
        "conditions": [*resp.conditions, "v21_dd_proximity_defer"],
    })


register_candidate(
    "v21",
    evaluate_v21,
    parent_version=17,
    rationale=(
        "DEFER risk-adding entries when binding_constraint indicates "
        "drawdown/kill proximity; asymmetric downside near kill threshold"
    ),
    metadata={
        "dd_keywords": sorted(DD_KEYWORDS),
        "kaizen_ticket": "KZN-2026-04-27-dd-proximity-defer",
    },
    overwrite=True,
)

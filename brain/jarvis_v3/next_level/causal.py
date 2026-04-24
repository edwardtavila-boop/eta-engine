"""
JARVIS v3 // next_level.causal
==============================
Causal inference / do-calculus engine.

Precedent gives us correlations. Causal inference gives us:
  * "what WOULD have happened if we had not denied request X?"
  * "does stand-aside on FOMC+1h actually reduce drawdown, or would we
     have recovered anyway?"

This module provides a lightweight DAG-based causal framework:

  * ``CausalNode``        -- variable in the DAG (regime, verdict, outcome)
  * ``CausalDAG``         -- adjacency + intervention API
  * ``Intervention``      -- do(X=x): force a node to a value
  * ``propensity_match``  -- nearest-neighbor matching for confounders
  * ``ate``               -- average treatment effect on outcome

Pure stdlib + pydantic. Not a full-featured causal library -- just
enough to answer "which direction is this gate moving P&L?"
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CausalNode(BaseModel):
    """One variable in the DAG."""
    model_config = ConfigDict(frozen=True)

    name:   str = Field(min_length=1)
    kind:   str = Field(pattern="^(binary|categorical|continuous)$")
    parents: list[str] = Field(default_factory=list)


class CausalDAG:
    """Minimal DAG with parent lists and sample observations."""

    def __init__(self) -> None:
        self._nodes: dict[str, CausalNode] = {}
        self._observations: list[dict[str, float]] = []

    def add_node(self, node: CausalNode) -> None:
        self._nodes[node.name] = node

    def add_observation(self, obs: dict[str, float]) -> None:
        self._observations.append(dict(obs))

    def nodes(self) -> list[CausalNode]:
        return list(self._nodes.values())

    def observations(self) -> list[dict[str, float]]:
        return list(self._observations)


class InterventionResult(BaseModel):
    """Result of a do(X=x) operation on the DAG."""
    model_config = ConfigDict(frozen=True)

    treatment_variable: str
    treatment_value:    float
    outcome_variable:   str
    sample_n:           int = Field(ge=0)
    treated_n:          int = Field(ge=0)
    control_n:          int = Field(ge=0)
    mean_treated:       float
    mean_control:       float
    ate:                float  # average treatment effect = E[Y|do(X=x)] - E[Y|do(X=x')]
    note:               str


def propensity_match(
    dag: CausalDAG, *, treatment: str, outcome: str, treatment_value: float,
    confounders: list[str] | None = None,
    tolerance: float = 0.1,
) -> InterventionResult:
    """Nearest-neighbor propensity matching to estimate the ATE.

    For each TREATED row, find the closest CONTROL row by confounder
    values (L2 distance within tolerance) and compare outcomes.
    """
    confounders = confounders or []
    treated: list[dict[str, float]] = []
    control: list[dict[str, float]] = []
    for obs in dag.observations():
        if treatment not in obs or outcome not in obs:
            continue
        if abs(obs[treatment] - treatment_value) < 1e-9:
            treated.append(obs)
        else:
            control.append(obs)

    # For each treated obs, find a control match
    matched_pairs: list[tuple[float, float]] = []
    for t in treated:
        best: dict[str, float] | None = None
        best_d = float("inf")
        for c in control:
            d = 0.0
            ok = True
            for feat in confounders:
                if feat not in t or feat not in c:
                    ok = False
                    break
                d += (t[feat] - c[feat]) ** 2
            if not ok:
                continue
            d = d ** 0.5
            if d < best_d:
                best_d = d
                best = c
        if best is not None and best_d <= tolerance * max(1.0, len(confounders)):
            matched_pairs.append((t[outcome], best[outcome]))

    if not matched_pairs:
        return InterventionResult(
            treatment_variable=treatment,
            treatment_value=treatment_value,
            outcome_variable=outcome,
            sample_n=len(dag.observations()),
            treated_n=len(treated),
            control_n=len(control),
            mean_treated=0.0,
            mean_control=0.0,
            ate=0.0,
            note="no matched pairs -- insufficient overlap",
        )

    mean_t = sum(p[0] for p in matched_pairs) / len(matched_pairs)
    mean_c = sum(p[1] for p in matched_pairs) / len(matched_pairs)
    ate = mean_t - mean_c
    return InterventionResult(
        treatment_variable=treatment,
        treatment_value=treatment_value,
        outcome_variable=outcome,
        sample_n=len(dag.observations()),
        treated_n=len(treated),
        control_n=len(control),
        mean_treated=round(mean_t, 4),
        mean_control=round(mean_c, 4),
        ate=round(ate, 4),
        note=f"matched {len(matched_pairs)} pairs on {confounders}",
    )


def counterfactual_denied(
    dag: CausalDAG,
    *, confounders: list[str] | None = None,
) -> InterventionResult:
    """Convenience: what's the P&L ATE of DENIED verdicts?

    Assumes ``verdict_denied`` is a binary (1 = denied) and ``realized_r``
    is the outcome. The answer tells us: does denying trades protect
    P&L, or does it just leave alpha on the table?
    """
    return propensity_match(
        dag,
        treatment="verdict_denied",
        treatment_value=1.0,
        outcome="realized_r",
        confounders=confounders or ["stress_composite", "regime_code"],
    )

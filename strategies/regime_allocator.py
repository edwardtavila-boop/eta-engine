"""EVOLUTIONARY TRADING ALGO  //  strategies.regime_allocator.

Top-level portfolio allocator for strategy #5 (Regime-Adaptive
Portfolio Allocation + Apex Entries).

This module produces **weights across layers** -- it is the companion
to :mod:`eta_engine.funnel.waterfall`, which owns the flow of
*realized* profits across the same four layers. Where ``waterfall``
asks "how should harvested profit move downstream?", this module asks
"how should *new risk* be split at the top of the waterfall?".

Inputs
------
  * A ``VolRegime`` label per layer (taken from the existing enum
    imported by the waterfall planner).
  * A simple pairwise correlation dict, e.g. ``{("BTC","ETH"): 0.82}``.
  * A global kill flag from :class:`FunnelSnapshot`.

Outputs
-------
  * :class:`AllocationPlan` with normalized weights that sum to 1.0
    across the non-staking layers (staking is a terminal sink with a
    fixed floor weight, set by ``sink_weight``).

Design
------
Pure function. No floats of doom. All clamps exposed. Written so the
``Command Center`` panel can display "weights" + "regime override"
straight from the dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from eta_engine.funnel.waterfall import LayerId, VolRegime


@dataclass(frozen=True, slots=True)
class LayerAllocInput:
    """Per-layer input for the allocator."""

    layer: LayerId
    vol_regime: VolRegime = VolRegime.NORMAL
    base_weight: float = 0.0
    realized_edge: float = 0.0  # per-layer realised edge signal, 0..1


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    """Output of :func:`plan_allocation`."""

    weights: dict[LayerId, float]
    notes: tuple[str, ...] = field(default_factory=tuple)
    corr_penalty_applied: bool = False
    global_kill_applied: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "weights": {layer.value: w for layer, w in self.weights.items()},
            "notes": list(self.notes),
            "corr_penalty_applied": self.corr_penalty_applied,
            "global_kill_applied": self.global_kill_applied,
        }


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


DEFAULT_BASE: dict[LayerId, float] = {
    LayerId.LAYER_1_MNQ: 0.40,
    LayerId.LAYER_2_BTC: 0.30,
    LayerId.LAYER_3_PERPS: 0.20,
    LayerId.LAYER_4_STAKING: 0.10,
}
"""Starting weights -- matches the user's 40/40/20 -> 40/30/20/10 brief
with staking as the terminal sink."""


VOL_MULT: dict[VolRegime, float] = {
    VolRegime.LOW: 0.70,
    VolRegime.NORMAL: 1.00,
    VolRegime.HIGH: 0.55,
}
"""Per-regime weight multiplier. ``VolRegime`` in
:mod:`eta_engine.funnel.waterfall` only defines LOW/NORMAL/HIGH
(the extreme branch is modelled in the waterfall-level risk actions
rather than here)."""


def plan_allocation(
    inputs: list[LayerAllocInput],
    *,
    correlations: dict[tuple[LayerId, LayerId], float] | None = None,
    corr_threshold: float = 0.75,
    corr_penalty_mult: float = 0.70,
    global_kill: bool = False,
    sink_weight: float = 0.10,
    base: dict[LayerId, float] | None = None,
) -> AllocationPlan:
    """Compute layer weights.

    Steps:
      1. Start from ``base`` (default: ``DEFAULT_BASE``).
      2. Multiply each risky layer by ``VOL_MULT[regime]``.
      3. If any risky-layer pair has correlation > ``corr_threshold``,
         shrink the smaller-weight member by ``corr_penalty_mult``.
      4. Apply realized-edge boost (each layer's edge in 0..1 adds up to
         +50% weight).
      5. If ``global_kill``, zero everything except the staking sink.
      6. Re-normalize so risky layers sum to (1 - sink_weight); staking
         gets ``sink_weight``.
    """
    base_map = dict(base or DEFAULT_BASE)
    corr_map = correlations or {}
    notes: list[str] = []

    weights: dict[LayerId, float] = {inp.layer: base_map.get(inp.layer, 0.0) for inp in inputs}

    # 2. Vol adjustment for risky layers
    for inp in inputs:
        if inp.layer is LayerId.LAYER_4_STAKING:
            continue
        mult = VOL_MULT.get(inp.vol_regime, 1.0)
        weights[inp.layer] *= mult
        if mult != 1.0:
            notes.append(f"{inp.layer.value}:vol_{inp.vol_regime.value}x{mult:.2f}")

    # 3. Correlation penalty
    corr_penalty_applied = False
    for (a, b), rho in corr_map.items():
        if a is LayerId.LAYER_4_STAKING or b is LayerId.LAYER_4_STAKING:
            continue
        if rho > corr_threshold:
            if weights.get(a, 0.0) < weights.get(b, 0.0):
                weights[a] = weights.get(a, 0.0) * corr_penalty_mult
            else:
                weights[b] = weights.get(b, 0.0) * corr_penalty_mult
            notes.append(
                f"corr_penalty:{a.value}<->{b.value}={rho:.2f}>{corr_threshold:.2f}",
            )
            corr_penalty_applied = True

    # 4. Edge boost
    for inp in inputs:
        if inp.layer is LayerId.LAYER_4_STAKING or inp.realized_edge <= 0.0:
            continue
        boost = 1.0 + min(0.5, max(0.0, inp.realized_edge) * 0.5)
        weights[inp.layer] *= boost

    # 5. Global kill
    if global_kill:
        risky = {k: 0.0 for k in weights if k is not LayerId.LAYER_4_STAKING}
        weights.update(risky)
        notes.append("global_kill_risky_zeroed")

    # 6. Normalise
    risky_sum = sum(v for k, v in weights.items() if k is not LayerId.LAYER_4_STAKING)
    if risky_sum > 0.0:
        scale = (1.0 - sink_weight) / risky_sum
        for layer in list(weights.keys()):
            if layer is not LayerId.LAYER_4_STAKING:
                weights[layer] *= scale
    else:
        notes.append("risky_sum_zero_sink_only")
    weights[LayerId.LAYER_4_STAKING] = sink_weight

    return AllocationPlan(
        weights=weights,
        notes=tuple(notes),
        corr_penalty_applied=corr_penalty_applied,
        global_kill_applied=global_kill,
    )

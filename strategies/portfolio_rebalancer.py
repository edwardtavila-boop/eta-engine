"""EVOLUTIONARY TRADING ALGO  //  strategies.portfolio_rebalancer.

Bridge between :mod:`eta_engine.strategies.regime_allocator` (which
produces per-layer target weights) and :mod:`eta_engine.funnel.waterfall`
(which produces profit sweeps + risk directives).

Why this module exists
----------------------
The regime allocator answers *"what weight should each layer carry?"*
The waterfall answers *"how should realized profit move downstream?"*
Neither answers *"the layers have drifted away from target weights --
what transfers put us back on plan?"* That's what this module does.

Design
------
* **Pure function** -- :func:`plan_rebalance` takes a
  :class:`FunnelSnapshot` (current per-layer equity) plus an
  :class:`AllocationPlan` (target weights) and returns a
  :class:`RebalancePlan` (proposed transfers). No I/O, no mutation,
  deterministic.
* **Reuses** :class:`ProposedSweep` from :mod:`waterfall` so downstream
  consumers (the funnel orchestrator, the transfer manager, dashboards)
  accept the output with zero code change.
* **Drift threshold** in percent of total equity. Below threshold the
  layer is considered "on plan" and no transfer is emitted. Above
  threshold the overweighted layer sources a transfer sized to close
  the gap, subject to ``min_transfer_usd``.
* **Greedy matching** -- we pair the largest overweight source with
  the largest underweight destination and keep going until either
  side is drained. This produces a minimal-transfer plan even when
  several layers have drifted.

Kill-switch interaction
-----------------------
If ``AllocationPlan.global_kill_applied`` is True, the risky layers'
weights are already zero -- meaning "pull everything out of risk".
In that case we do not emit any rebalance transfers; the kill-switch
unwind is handled by the waterfall's HALT directives, not by this
module. This is intentional: two competing transfer streams during a
kill event would race each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from eta_engine.funnel.transfer import TransferRequest
from eta_engine.funnel.waterfall import LayerId, ProposedSweep

if TYPE_CHECKING:
    from eta_engine.funnel.waterfall import FunnelSnapshot
    from eta_engine.strategies.regime_allocator import AllocationPlan


__all__ = [
    "DEFAULT_DRIFT_THRESHOLD_PCT",
    "DEFAULT_MIN_TRANSFER_USD",
    "RebalancePlan",
    "plan_rebalance",
    "rebalance_plan_to_transfers",
]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

#: Minimum drift (percent of total equity) before a layer is considered
#: off-target. 5% avoids churn during ordinary intraday moves.
DEFAULT_DRIFT_THRESHOLD_PCT: float = 0.05

#: Minimum transfer size in USD. Below this the bridge cost dominates.
DEFAULT_MIN_TRANSFER_USD: float = 100.0


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RebalancePlan:
    """Output of :func:`plan_rebalance`."""

    ts_utc: str
    total_equity_usd: float
    sweeps: tuple[ProposedSweep, ...] = field(default_factory=tuple)
    drift_pct_by_layer: dict[LayerId, float] = field(default_factory=dict)
    target_usd_by_layer: dict[LayerId, float] = field(default_factory=dict)
    notes: tuple[str, ...] = field(default_factory=tuple)
    global_kill_skipped: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "ts_utc": self.ts_utc,
            "total_equity_usd": round(self.total_equity_usd, 2),
            "sweeps": [
                {
                    "src": s.src.value,
                    "dst": s.dst.value,
                    "amount_usd": round(s.amount_usd, 2),
                    "reason": s.reason,
                }
                for s in self.sweeps
            ],
            "drift_pct_by_layer": {layer.value: round(pct, 6) for layer, pct in self.drift_pct_by_layer.items()},
            "target_usd_by_layer": {layer.value: round(usd, 2) for layer, usd in self.target_usd_by_layer.items()},
            "notes": list(self.notes),
            "global_kill_skipped": self.global_kill_skipped,
        }


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def plan_rebalance(
    snapshot: FunnelSnapshot,
    allocation: AllocationPlan,
    *,
    drift_threshold_pct: float = DEFAULT_DRIFT_THRESHOLD_PCT,
    min_transfer_usd: float = DEFAULT_MIN_TRANSFER_USD,
) -> RebalancePlan:
    """Produce rebalance transfers from current equity toward target weights.

    Parameters
    ----------
    snapshot:
        Current per-layer financial state. Only ``current_equity`` is
        read; drawdown / vol / pnl are ignored here (those feed the
        waterfall planner, not this bridge).
    allocation:
        Target weights from :func:`regime_allocator.plan_allocation`.
        Weights should already sum to 1.0 across all layers; this
        function normalizes defensively in case they don't.
    drift_threshold_pct:
        Minimum drift (absolute) in percent-of-total-equity before a
        layer is eligible to source or receive a rebalance transfer.
        Defaults to 5%.
    min_transfer_usd:
        Minimum USD amount per transfer. Smaller transfers are
        skipped with a note. Defaults to $100.

    Returns
    -------
    RebalancePlan -- contains the proposed sweeps, drift map, target
    USD map, and explanatory notes. Empty sweeps tuple means
    everything is within threshold (or kill-switch vetoed).
    """
    total_equity = snapshot.total_equity
    notes: list[str] = []

    # Guard: zero equity -> nothing to rebalance.
    if total_equity <= 0.0:
        return RebalancePlan(
            ts_utc=snapshot.ts_utc,
            total_equity_usd=0.0,
            notes=("zero_total_equity",),
        )

    # Guard: kill-switch in allocator means the risky layers are
    # already zero -- waterfall HALT directives handle the unwind;
    # we do not duplicate that path.
    if allocation.global_kill_applied:
        return RebalancePlan(
            ts_utc=snapshot.ts_utc,
            total_equity_usd=total_equity,
            notes=("global_kill_active_rebalance_skipped",),
            global_kill_skipped=True,
        )

    # Normalize target weights defensively (allocator guarantees this
    # already but a hand-built AllocationPlan might not).
    weight_sum = sum(allocation.weights.values())
    if weight_sum <= 0.0:
        return RebalancePlan(
            ts_utc=snapshot.ts_utc,
            total_equity_usd=total_equity,
            notes=("all_target_weights_zero",),
        )

    target_usd_by_layer: dict[LayerId, float] = {
        layer: (weight / weight_sum) * total_equity for layer, weight in allocation.weights.items()
    }

    drift_usd_by_layer: dict[LayerId, float] = {}
    drift_pct_by_layer: dict[LayerId, float] = {}
    for layer, target_usd in target_usd_by_layer.items():
        current = snapshot.layers.get(layer)
        current_usd = current.current_equity if current is not None else 0.0
        delta_usd = current_usd - target_usd
        drift_usd_by_layer[layer] = delta_usd
        drift_pct_by_layer[layer] = delta_usd / total_equity

    # Layers strictly above the threshold source transfers; strictly
    # below receive them. Exactly at threshold is treated as on plan.
    over = {
        layer: delta for layer, delta in drift_usd_by_layer.items() if drift_pct_by_layer[layer] > drift_threshold_pct
    }
    under = {
        layer: -delta for layer, delta in drift_usd_by_layer.items() if drift_pct_by_layer[layer] < -drift_threshold_pct
    }

    if not over or not under:
        if not over and not under:
            notes.append("all_layers_within_drift_threshold")
        elif not over:
            notes.append("no_overweight_layer_above_threshold")
        else:
            notes.append("no_underweight_layer_above_threshold")
        return RebalancePlan(
            ts_utc=snapshot.ts_utc,
            total_equity_usd=total_equity,
            drift_pct_by_layer=drift_pct_by_layer,
            target_usd_by_layer=target_usd_by_layer,
            notes=tuple(notes),
        )

    # Greedy pairing: largest overweight source -> largest underweight
    # destination. Keep taking until either side is drained.
    sweeps: list[ProposedSweep] = []
    over_queue = sorted(over.items(), key=lambda kv: -kv[1])
    under_queue: dict[LayerId, float] = dict(
        sorted(under.items(), key=lambda kv: -kv[1]),
    )

    for src_layer, src_excess in over_queue:
        remaining = src_excess
        for dst_layer in list(under_queue.keys()):
            dst_need = under_queue[dst_layer]
            if dst_need <= 0.0 or remaining <= 0.0:
                continue
            move = min(remaining, dst_need)
            if move < min_transfer_usd:
                notes.append(
                    f"skip {src_layer.value}->{dst_layer.value}: ${move:.2f} < min ${min_transfer_usd:.2f}",
                )
                continue
            sweeps.append(
                ProposedSweep(
                    src=src_layer,
                    dst=dst_layer,
                    amount_usd=move,
                    reason=(f"rebalance:{src_layer.value}_overweight_to_{dst_layer.value}_underweight"),
                ),
            )
            remaining -= move
            under_queue[dst_layer] = dst_need - move

    if not sweeps:
        notes.append("no_transfers_above_min_transfer_usd")

    return RebalancePlan(
        ts_utc=snapshot.ts_utc,
        total_equity_usd=total_equity,
        sweeps=tuple(sweeps),
        drift_pct_by_layer=drift_pct_by_layer,
        target_usd_by_layer=target_usd_by_layer,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Execution bridge -- convert RebalancePlan into TransferRequest rows
# ---------------------------------------------------------------------------


def rebalance_plan_to_transfers(
    plan: RebalancePlan,
    layer_to_bot: dict[LayerId, str],
) -> list[TransferRequest]:
    """Convert a :class:`RebalancePlan` into concrete :class:`TransferRequest` rows.

    This is the glue between the pure-function rebalancer (which speaks
    in :class:`LayerId` + :class:`ProposedSweep`) and the funnel
    orchestrator (which speaks in bot-name strings + :class:`TransferRequest`).

    Parameters
    ----------
    plan:
        Output of :func:`plan_rebalance`. Only ``plan.sweeps`` is read;
        the drift / target maps and notes are carried on the plan for
        observability but play no role in the conversion.
    layer_to_bot:
        Map from :class:`LayerId` to the bot-name string used by the
        funnel. Any sweep whose source *or* destination layer is missing
        from the map is silently skipped -- the caller can compare
        ``len(plan.sweeps)`` against ``len(result)`` to detect gaps and
        log/alert accordingly.

    Returns
    -------
    list[TransferRequest] ordered the same way as ``plan.sweeps``, ready
    to route through :meth:`TransferManager.execute` or a raw executor.
    Amounts are rounded to two decimal places. ``requires_approval`` is
    left False on the request -- the downstream manager will enforce
    the approval threshold if one applies.
    """
    out: list[TransferRequest] = []
    for sweep in plan.sweeps:
        from_bot = layer_to_bot.get(sweep.src)
        to_bot = layer_to_bot.get(sweep.dst)
        if from_bot is None or to_bot is None:
            continue
        out.append(
            TransferRequest(
                from_bot=from_bot,
                to_bot=to_bot,
                amount_usd=round(sweep.amount_usd, 2),
                reason=sweep.reason,
            ),
        )
    return out

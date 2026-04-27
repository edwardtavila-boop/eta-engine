"""
EVOLUTIONARY TRADING ALGO  //  funnel.waterfall
===================================
Four-layer profit waterfall: MNQ -> BTC -> ETH/BTC/SOL perps -> staking.

Every layer runs its own engine with its own risk tier. At the end of each
sweep window (daily by default) excess realized profits flow downstream on
hard rules. Correlation guard, global kill switch, and vol-scaling adjust
per-layer risk mid-run.

Design
------
The waterfall is a pure value-in / plan-out transform. It takes a
``FunnelSnapshot`` describing the current state of all four layers plus the
market-vol context, and returns a ``WaterfallPlan`` of sweeps + risk actions.
Execution is left to the existing funnel.orchestrator (which owns
transfers/transfer failure handling). Nothing here makes network calls.

Layer math
----------
  * sweep_pct applies to realized_pnl_since_last_sweep, not equity.
  * A layer only contributes to a sweep if its realized pnl is > 0 AND the
    resulting transfer >= the dest layer's min_incoming_usd (so we don't
    spam L3 with $3 rounds).
  * Global kill: if the sum of (peak_equity - current_equity) across layers
    divided by sum of peak_equity >= kill_pct, every layer is told to HALT.
  * Correlation guard: if >=2 risky layers are simultaneously in HIGH vol,
    each gets a reduce_size action with mult=0.6.
  * Vol scaling: each layer's size_mult is 1 / (1 + vol_z * sensitivity).
    Clamped to [0.25, 1.0].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LayerId(StrEnum):
    LAYER_1_MNQ = "LAYER_1_MNQ"
    LAYER_2_BTC = "LAYER_2_BTC"
    LAYER_3_PERPS = "LAYER_3_PERPS"
    LAYER_4_STAKING = "LAYER_4_STAKING"


class RiskAction(StrEnum):
    NORMAL = "NORMAL"
    REDUCE_SIZE = "REDUCE_SIZE"
    HALT = "HALT"
    RESUME = "RESUME"


class VolRegime(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


# ---------------------------------------------------------------------------
# Per-layer risk tier (user's hard rules from the brief)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerRiskTier:
    """Hard per-trade / per-day / global DD rules for one layer."""

    layer: LayerId
    max_position_pct_per_trade: float
    daily_loss_cap_pct: float
    drawdown_kill_pct: float
    leverage_cap: float
    sweep_out_pct: float  # fraction of realized pnl to pass downstream
    min_outgoing_usd: float
    min_incoming_usd: float = 0.0


# The tier presets lifted straight from the user's brief.
TIER_L1_MNQ = LayerRiskTier(
    layer=LayerId.LAYER_1_MNQ,
    max_position_pct_per_trade=0.05,
    daily_loss_cap_pct=0.06,
    drawdown_kill_pct=0.12,
    leverage_cap=10.0,
    sweep_out_pct=0.65,
    min_outgoing_usd=25.0,
)
TIER_L2_BTC = LayerRiskTier(
    layer=LayerId.LAYER_2_BTC,
    max_position_pct_per_trade=0.03,
    daily_loss_cap_pct=0.04,
    drawdown_kill_pct=0.09,
    leverage_cap=5.0,
    sweep_out_pct=0.65,
    min_outgoing_usd=25.0,
)
TIER_L3_PERPS = LayerRiskTier(
    layer=LayerId.LAYER_3_PERPS,
    max_position_pct_per_trade=0.015,
    daily_loss_cap_pct=0.025,
    drawdown_kill_pct=0.06,
    leverage_cap=3.0,
    sweep_out_pct=0.75,
    min_outgoing_usd=50.0,
    min_incoming_usd=100.0,
)
TIER_L4_STAKING = LayerRiskTier(
    layer=LayerId.LAYER_4_STAKING,
    max_position_pct_per_trade=0.0,  # no trading
    daily_loss_cap_pct=0.0,
    drawdown_kill_pct=0.0,
    leverage_cap=1.0,
    sweep_out_pct=0.0,  # staking is a sink
    min_outgoing_usd=0.0,
    min_incoming_usd=50.0,
)

DEFAULT_TIERS: dict[LayerId, LayerRiskTier] = {
    LayerId.LAYER_1_MNQ: TIER_L1_MNQ,
    LayerId.LAYER_2_BTC: TIER_L2_BTC,
    LayerId.LAYER_3_PERPS: TIER_L3_PERPS,
    LayerId.LAYER_4_STAKING: TIER_L4_STAKING,
}


# ---------------------------------------------------------------------------
# Snapshot + plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerSnapshot:
    """Current financial state of one layer at the sweep cutoff."""

    layer: LayerId
    current_equity: float
    peak_equity: float
    realized_pnl_since_last_sweep: float
    vol_regime: VolRegime = VolRegime.NORMAL
    vol_z: float = 0.0  # z-score of 14-day ATR vs 90-day baseline

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, 1.0 - self.current_equity / self.peak_equity)


@dataclass(frozen=True)
class FunnelSnapshot:
    layers: dict[LayerId, LayerSnapshot]
    ts_utc: str  # ISO timestamp owned by the caller

    @property
    def total_equity(self) -> float:
        return sum(layer.current_equity for layer in self.layers.values())

    @property
    def total_peak(self) -> float:
        return sum(layer.peak_equity for layer in self.layers.values())

    @property
    def global_drawdown_pct(self) -> float:
        peak = self.total_peak
        if peak <= 0:
            return 0.0
        return max(0.0, 1.0 - self.total_equity / peak)


@dataclass(frozen=True)
class ProposedSweep:
    src: LayerId
    dst: LayerId
    amount_usd: float
    reason: str


@dataclass(frozen=True)
class RiskDirective:
    layer: LayerId
    action: RiskAction
    size_mult: float
    reason: str


@dataclass(frozen=True)
class WaterfallPlan:
    ts_utc: str
    sweeps: list[ProposedSweep] = field(default_factory=list)
    directives: list[RiskDirective] = field(default_factory=list)
    global_kill: bool = False
    global_dd_pct: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts_utc": self.ts_utc,
            "sweeps": [
                {
                    "src": s.src.value,
                    "dst": s.dst.value,
                    "amount_usd": round(s.amount_usd, 2),
                    "reason": s.reason,
                }
                for s in self.sweeps
            ],
            "directives": [
                {
                    "layer": d.layer.value,
                    "action": d.action.value,
                    "size_mult": round(d.size_mult, 4),
                    "reason": d.reason,
                }
                for d in self.directives
            ],
            "global_kill": self.global_kill,
            "global_dd_pct": round(self.global_dd_pct, 4),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Waterfall
# ---------------------------------------------------------------------------


# Default waterfall map: (src, dst, reason_tag)
DEFAULT_WATERFALL = (
    (LayerId.LAYER_1_MNQ, LayerId.LAYER_3_PERPS, "L1_profit_sweep_to_L3"),
    (LayerId.LAYER_2_BTC, LayerId.LAYER_3_PERPS, "L2_profit_sweep_to_L3"),
    (LayerId.LAYER_3_PERPS, LayerId.LAYER_4_STAKING, "L3_profit_sweep_to_staking"),
)


class FunnelWaterfall:
    """Pure planner -- snapshot in, sweeps + directives out.

    Parameters
    ----------
    tiers:
        Per-layer risk tier config. Defaults to the four presets at module top.
    global_kill_pct:
        If total funnel drawdown >= this, issue HALT directives everywhere.
    correlation_vol_mult:
        Multiplier applied to size when the correlation guard fires.
    vol_sensitivity:
        Bigger -> sharper inverse-vol size scaling.
    """

    def __init__(
        self,
        *,
        tiers: dict[LayerId, LayerRiskTier] | None = None,
        global_kill_pct: float = 0.08,
        correlation_vol_mult: float = 0.6,
        vol_sensitivity: float = 0.5,
        waterfall: tuple[tuple[LayerId, LayerId, str], ...] = DEFAULT_WATERFALL,
    ) -> None:
        self.tiers = tiers if tiers is not None else DEFAULT_TIERS
        self.global_kill_pct = global_kill_pct
        self.correlation_vol_mult = correlation_vol_mult
        self.vol_sensitivity = vol_sensitivity
        self.waterfall = waterfall

    # -- planning API -------------------------------------------------------

    def plan(self, snapshot: FunnelSnapshot) -> WaterfallPlan:
        notes: list[str] = []
        sweeps: list[ProposedSweep] = []
        directives: list[RiskDirective] = []

        # 1. Global kill check first -- blocks everything else.
        dd_pct = snapshot.global_drawdown_pct
        if dd_pct >= self.global_kill_pct:
            for layer_id in self.tiers:
                directives.append(
                    RiskDirective(
                        layer=layer_id,
                        action=RiskAction.HALT,
                        size_mult=0.0,
                        reason=(f"global funnel DD {dd_pct:.2%} >= kill {self.global_kill_pct:.2%}"),
                    ),
                )
            return WaterfallPlan(
                ts_utc=snapshot.ts_utc,
                sweeps=[],
                directives=directives,
                global_kill=True,
                global_dd_pct=dd_pct,
                notes=["global_kill_triggered -- all layers HALT; no sweeps"],
            )

        # 2. Per-layer DD kill.
        for layer_id, layer in snapshot.layers.items():
            tier = self.tiers.get(layer_id)
            if tier is None:
                continue
            if tier.drawdown_kill_pct > 0 and layer.drawdown_pct >= tier.drawdown_kill_pct:
                directives.append(
                    RiskDirective(
                        layer=layer_id,
                        action=RiskAction.HALT,
                        size_mult=0.0,
                        reason=(f"layer DD {layer.drawdown_pct:.2%} >= kill {tier.drawdown_kill_pct:.2%}"),
                    ),
                )

        halted = {d.layer for d in directives if d.action == RiskAction.HALT}

        # 3. Correlation guard -- if >=2 risky layers are in HIGH vol,
        #    cut their size.
        risky_layers = (LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, LayerId.LAYER_3_PERPS)
        high_vol_hits = [
            lid for lid in risky_layers if lid in snapshot.layers and snapshot.layers[lid].vol_regime == VolRegime.HIGH
        ]
        if len(high_vol_hits) >= 2:
            for lid in high_vol_hits:
                if lid in halted:
                    continue
                directives.append(
                    RiskDirective(
                        layer=lid,
                        action=RiskAction.REDUCE_SIZE,
                        size_mult=self.correlation_vol_mult,
                        reason=(f"correlation_guard: {len(high_vol_hits)} risky layers in HIGH vol simultaneously"),
                    ),
                )
            notes.append(
                f"correlation_guard fired on {[lid.value for lid in high_vol_hits]}",
            )

        # 4. Inverse-vol size scaling for any layer with a positive z that
        #    wasn't already halted / correlation-cut.
        size_cut_layers = {d.layer for d in directives if d.action == RiskAction.REDUCE_SIZE}
        for layer_id, layer in snapshot.layers.items():
            if layer_id in halted or layer_id in size_cut_layers:
                continue
            if layer.vol_z <= 0:
                continue
            mult = 1.0 / (1.0 + layer.vol_z * self.vol_sensitivity)
            mult = max(0.25, min(1.0, mult))
            if mult < 0.95:
                directives.append(
                    RiskDirective(
                        layer=layer_id,
                        action=RiskAction.REDUCE_SIZE,
                        size_mult=mult,
                        reason=f"vol_scale: z={layer.vol_z:.2f} -> mult={mult:.2f}",
                    ),
                )

        # 5. Profit sweeps.
        for src_id, dst_id, reason_tag in self.waterfall:
            if src_id in halted:
                notes.append(f"sweep {src_id.value}->{dst_id.value} skipped: src halted")
                continue
            src_tier = self.tiers.get(src_id)
            dst_tier = self.tiers.get(dst_id)
            src_layer = snapshot.layers.get(src_id)
            if src_tier is None or dst_tier is None or src_layer is None:
                continue
            pnl = src_layer.realized_pnl_since_last_sweep
            if pnl <= 0:
                continue
            amount = pnl * src_tier.sweep_out_pct
            if amount < src_tier.min_outgoing_usd:
                notes.append(
                    f"sweep {src_id.value}->{dst_id.value} skipped: "
                    f"amount ${amount:.2f} < min_outgoing ${src_tier.min_outgoing_usd}",
                )
                continue
            if amount < dst_tier.min_incoming_usd:
                notes.append(
                    f"sweep {src_id.value}->{dst_id.value} skipped: "
                    f"amount ${amount:.2f} < min_incoming ${dst_tier.min_incoming_usd}",
                )
                continue
            sweeps.append(
                ProposedSweep(
                    src=src_id,
                    dst=dst_id,
                    amount_usd=amount,
                    reason=(f"{reason_tag}: {src_tier.sweep_out_pct:.0%} of ${pnl:.2f} realized pnl"),
                ),
            )

        return WaterfallPlan(
            ts_utc=snapshot.ts_utc,
            sweeps=sweeps,
            directives=directives,
            global_kill=False,
            global_dd_pct=dd_pct,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Daily digest -- tiny markdown formatter for the nightly Discord ping
# ---------------------------------------------------------------------------


def format_digest(snapshot: FunnelSnapshot, plan: WaterfallPlan) -> str:
    lines: list[str] = []
    lines.append(f"**APEX Funnel Digest -- {snapshot.ts_utc}**")
    lines.append("")
    lines.append(f"Total equity: ${snapshot.total_equity:,.2f}")
    lines.append(f"Peak equity : ${snapshot.total_peak:,.2f}")
    lines.append(f"Funnel DD   : {snapshot.global_drawdown_pct:.2%}")
    lines.append("")
    lines.append("Per-layer state:")
    for layer_id, layer in snapshot.layers.items():
        lines.append(
            f"  {layer_id.value:<20} equity=${layer.current_equity:,.2f} "
            f"dd={layer.drawdown_pct:.2%} vol={layer.vol_regime.value} "
            f"pnl_since_sweep=${layer.realized_pnl_since_last_sweep:+,.2f}",
        )
    lines.append("")
    if plan.global_kill:
        lines.append("*** GLOBAL KILL TRIGGERED -- all layers HALTED ***")
    else:
        if plan.sweeps:
            lines.append("Sweeps queued:")
            for s in plan.sweeps:
                lines.append(
                    f"  {s.src.value} -> {s.dst.value}  ${s.amount_usd:,.2f}   ({s.reason})",
                )
        else:
            lines.append("Sweeps queued: none")
        if plan.directives:
            lines.append("")
            lines.append("Risk directives:")
            for d in plan.directives:
                lines.append(
                    f"  {d.layer.value:<20} {d.action.value:<12} mult={d.size_mult:.2f}  ({d.reason})",
                )
    if plan.notes:
        lines.append("")
        lines.append("Notes:")
        for n in plan.notes:
            lines.append(f"  - {n}")
    return "\n".join(lines)

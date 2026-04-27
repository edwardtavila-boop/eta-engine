"""
EVOLUTIONARY TRADING ALGO  //  core.master_tweaks
=====================================
Apply "winning" parameters from a sweep back onto a bot config -- with
guardrails so an unstable cell can't silently override a production setup.

Flow:
    sweep cells -> propose_tweaks() -> [Tweak, ...]
                |
                v
    bot_configs  ---->  apply_tweaks(cfg, tweaks, policy)  ---->  new config

Every tweak carries:
  * source     -- which sweep produced it
  * reason     -- why this cell won
  * risk_tag   -- {SAFE, MODERATE, AGGRESSIVE} based on delta from baseline
  * proposal   -- the new parameter dict

Policy gates (TweakPolicy) let the operator refuse AGGRESSIVE tweaks in
production, cap the % change per parameter, etc.

This is a config-plane module, NOT the trading path. No positions are
changed by this module. It just edits JSON.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from eta_engine.core.parameter_sweep import SweepCell

# ---------------------------------------------------------------------------
# Tweak + risk classification
# ---------------------------------------------------------------------------


class RiskTag(StrEnum):
    SAFE = "SAFE"
    MODERATE = "MODERATE"
    AGGRESSIVE = "AGGRESSIVE"


class Tweak(BaseModel):
    """A proposed parameter change for a single bot."""

    bot: str
    source: str = "sweep"
    reason: str = ""
    risk_tag: RiskTag = RiskTag.SAFE
    proposal: dict[str, Any]
    expected_expectancy_r: float = 0.0
    expected_dd_pct: float = 0.0
    gate_pass: bool = False


class TweakPolicy(BaseModel):
    """Operator-configurable guardrails for applying tweaks."""

    allow_aggressive: bool = Field(
        default=False,
        description="If False, AGGRESSIVE tweaks are rejected.",
    )
    max_relative_change: float = Field(
        default=0.50,
        gt=0,
        description=(
            "Max allowed |new - old| / max(|old|, 1e-9) for numeric params. Per-parameter, not portfolio-wide."
        ),
    )
    require_gate_pass: bool = Field(
        default=True,
        description="If True, reject any tweak whose source cell did not pass the sweep gate.",
    )


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------


def classify_risk(
    baseline: dict[str, Any],
    proposal: dict[str, Any],
) -> RiskTag:
    """Classify how aggressive a proposal is vs the current baseline.

    Decision rules (applied to numeric params, max across keys):
      * max relative delta <= 0.10  -> SAFE
      * max relative delta <= 0.35  -> MODERATE
      * otherwise                   -> AGGRESSIVE

    Non-numeric / new keys are treated as MODERATE (structural change).
    """
    if not proposal:
        return RiskTag.SAFE

    max_rel = 0.0
    has_structural = False
    for k, new in proposal.items():
        old = baseline.get(k)
        if old is None:
            has_structural = True
            continue
        if not isinstance(new, (int, float)) or not isinstance(old, (int, float)):
            if new != old:
                has_structural = True
            continue
        rel = abs(new - old) / max(abs(old), 1e-9)
        max_rel = max(max_rel, rel)

    if max_rel > 0.35:
        return RiskTag.AGGRESSIVE
    if max_rel > 0.10 or has_structural:
        return RiskTag.MODERATE
    return RiskTag.SAFE


# ---------------------------------------------------------------------------
# Proposal generation
# ---------------------------------------------------------------------------


def propose_tweaks(
    winners: dict[str, SweepCell],
    baselines: dict[str, dict[str, Any]],
    source: str = "sweep",
) -> list[Tweak]:
    """Turn per-bot winning cells into Tweak proposals.

    Args:
        winners:   {bot_name: SweepCell} -- typically pick_winner() output.
        baselines: {bot_name: {param: value}} -- current bot configs.
        source:    tag carried on each Tweak for audit.

    Returns:
        List of Tweak, one per bot in ``winners``. Bots missing from
        ``baselines`` get an empty baseline dict (treats every param as new).
    """
    tweaks: list[Tweak] = []
    for bot, cell in winners.items():
        baseline = baselines.get(bot, {})
        risk = classify_risk(baseline, cell.params)
        tweaks.append(
            Tweak(
                bot=bot,
                source=source,
                reason=_reason_for(cell),
                risk_tag=risk,
                proposal=dict(cell.params),
                expected_expectancy_r=cell.score.expectancy_r,
                expected_dd_pct=cell.score.max_dd_pct,
                gate_pass=cell.gate_pass,
            ),
        )
    return tweaks


def _reason_for(cell: SweepCell) -> str:
    if cell.gate_pass:
        return (
            f"gate-pass: exp={cell.score.expectancy_r:+.3f}R "
            f"dd={cell.score.max_dd_pct:.2f}% "
            f"stability={cell.stability:.3f}"
        )
    return (
        f"closest-to-passing: exp={cell.score.expectancy_r:+.3f}R "
        f"dd={cell.score.max_dd_pct:.2f}% "
        f"stability={cell.stability:.3f}"
    )


# ---------------------------------------------------------------------------
# Application + rejection
# ---------------------------------------------------------------------------


class TweakApplyResult(BaseModel):
    """Outcome of attempting to apply a single tweak."""

    bot: str
    applied: bool
    reason: str
    new_config: dict[str, Any] = Field(default_factory=dict)
    rejected_params: list[str] = Field(default_factory=list)


def apply_tweak(
    baseline: dict[str, Any],
    tweak: Tweak,
    policy: TweakPolicy | None = None,
) -> TweakApplyResult:
    """Apply ``tweak`` to ``baseline`` subject to ``policy`` gates.

    Returns a TweakApplyResult describing whether the apply succeeded, the
    resulting config, and any per-param rejections.

    Gates (in order):
      1. require_gate_pass  -> whole tweak rejected.
      2. allow_aggressive   -> whole tweak rejected if risk_tag == AGGRESSIVE.
      3. max_relative_change -> per-parameter rejection (keep baseline value).
    """
    p = policy or TweakPolicy()

    if p.require_gate_pass and not tweak.gate_pass:
        return TweakApplyResult(
            bot=tweak.bot,
            applied=False,
            reason="rejected: source cell did not pass gate",
            new_config=dict(baseline),
        )
    if not p.allow_aggressive and tweak.risk_tag == RiskTag.AGGRESSIVE:
        return TweakApplyResult(
            bot=tweak.bot,
            applied=False,
            reason="rejected: AGGRESSIVE risk tag not allowed by policy",
            new_config=dict(baseline),
        )

    new_cfg = dict(baseline)
    rejected: list[str] = []
    for k, new in tweak.proposal.items():
        old = baseline.get(k)
        if old is not None and isinstance(new, (int, float)) and isinstance(old, (int, float)):
            rel = abs(new - old) / max(abs(old), 1e-9)
            if rel > p.max_relative_change:
                rejected.append(k)
                continue
        new_cfg[k] = new

    if rejected and not tweak.proposal.keys() - set(rejected):
        # Every proposed param was rejected -- no-op apply
        return TweakApplyResult(
            bot=tweak.bot,
            applied=False,
            reason=f"rejected: all params exceed max_relative_change ({p.max_relative_change:.0%})",
            new_config=dict(baseline),
            rejected_params=rejected,
        )

    return TweakApplyResult(
        bot=tweak.bot,
        applied=True,
        reason=f"applied ({tweak.risk_tag.value}): {tweak.reason}",
        new_config=new_cfg,
        rejected_params=rejected,
    )


def apply_tweaks_bulk(
    baselines: dict[str, dict[str, Any]],
    tweaks: list[Tweak],
    policy: TweakPolicy | None = None,
) -> dict[str, TweakApplyResult]:
    """Apply many tweaks at once. Returns {bot: result}."""
    out: dict[str, TweakApplyResult] = {}
    for t in tweaks:
        base = baselines.get(t.bot, {})
        out[t.bot] = apply_tweak(base, t, policy)
    return out

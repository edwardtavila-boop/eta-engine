"""Guarded multi-bot portfolio rebalancer (Tier-4 #14, 2026-04-27).

This module turns rolling realized Sharpe + correlation telemetry into an
explicit rebalance plan. The plan is advisory by default: callers can inspect
the recommended equity ceilings first, then opt into ``apply_rebalance_plan``
when they want to push ceilings through ``BaseBot.set_equity_ceiling``.

Policy shape:

  1. Compute rolling Sharpe per bot.
  2. Scale baselines by Sharpe rank, capped at [0.5x, 2.0x].
  3. Dampen highly correlated winners so one crowded theme cannot consume the
     shared IBKR margin pool by itself.
  4. Preserve the total baseline budget unless the fleet drawdown brake fires,
     in which case the total target budget is cut in half.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)


@dataclass
class BotPerformance:
    bot_name: str
    rolling_returns: Sequence[float]   # daily returns over the rolling window
    baseline_usd: float


@dataclass(frozen=True)
class AllocationDecision:
    """Single-bot rebalance recommendation with audit reasons."""

    bot_name: str
    baseline_usd: float
    target_usd: float
    multiplier: float
    sharpe: float
    correlation_group: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PortfolioRebalancePlan:
    """Auditable output of the rebalancer."""

    allocations: dict[str, float]
    decisions: tuple[AllocationDecision, ...]
    drawdown_brake_active: bool
    total_baseline_usd: float
    total_target_usd: float

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready representation for status surfaces."""
        return {
            "allocations": self.allocations,
            "drawdown_brake_active": self.drawdown_brake_active,
            "total_baseline_usd": self.total_baseline_usd,
            "total_target_usd": self.total_target_usd,
            "decisions": [
                {
                    "bot_name": decision.bot_name,
                    "baseline_usd": decision.baseline_usd,
                    "target_usd": decision.target_usd,
                    "multiplier": decision.multiplier,
                    "sharpe": decision.sharpe,
                    "correlation_group": list(decision.correlation_group),
                    "reasons": list(decision.reasons),
                }
                for decision in self.decisions
            ],
        }


def realized_sharpe(returns: Sequence[float], *, ann_factor: float = 252.0) -> float:
    """Annualized Sharpe of a daily-returns series. 0.0 if insufficient samples."""
    if len(returns) < 5:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return 0.0
    return (mean / sd) * math.sqrt(ann_factor)


def build_rebalance_plan(
    perf: Sequence[BotPerformance],
    *,
    correlations: Mapping[tuple[str, str] | str, float] | None = None,
    correlation_threshold: float = 0.85,
    cap_low: float = 0.5,
    cap_high: float = 2.0,
    fleet_drawdown_pct: float = 0.0,
    drawdown_brake_threshold_pct: float = 0.05,
) -> PortfolioRebalancePlan:
    """Compute an auditable advisory rebalance plan.

    ``fleet_drawdown_pct`` is a fraction, so ``0.05`` means a 5% fleet
    drawdown. Correlation keys may be ``("BotA", "BotB")`` tuples or strings
    such as ``"BotA|BotB"`` / ``"BotA:BotB"``.
    """
    if not perf:
        return PortfolioRebalancePlan(
            allocations={},
            decisions=(),
            drawdown_brake_active=False,
            total_baseline_usd=0.0,
            total_target_usd=0.0,
        )

    sharpes: dict[str, float] = {p.bot_name: realized_sharpe(p.rolling_returns) for p in perf}
    baselines = {p.bot_name: p.baseline_usd for p in perf}

    # Rank-based scaling: best Sharpe gets cap_high, worst gets cap_low
    sorted_by_sharpe = sorted(sharpes.items(), key=lambda kv: kv[1])
    n = len(sorted_by_sharpe)
    if n == 1:
        ranks = {sorted_by_sharpe[0][0]: 1.0}
    else:
        ranks = {}
        for i, (name, _) in enumerate(sorted_by_sharpe):
            # i=0 -> cap_low, i=n-1 -> cap_high, linear
            mult = cap_low + (cap_high - cap_low) * (i / (n - 1))
            ranks[name] = mult

    group_by_name = _correlation_group_by_name(tuple(baselines), correlations, threshold=correlation_threshold)
    damped_ranks = _dampen_correlated_winners(ranks, group_by_name)

    total_baseline = sum(baselines.values())
    raw_targets = {name: baselines[name] * damped_ranks.get(name, 1.0) for name in baselines}
    raw_total = sum(raw_targets.values())
    drawdown_brake_active = fleet_drawdown_pct > drawdown_brake_threshold_pct
    target_total = total_baseline * (0.5 if drawdown_brake_active else 1.0)

    if drawdown_brake_active:
        logger.warning(
            "fleet DD %.2f%% > %.2f%% threshold -- halving total allocation",
            fleet_drawdown_pct * 100,
            drawdown_brake_threshold_pct * 100,
        )

    normalization = target_total / raw_total if raw_total > 0 else 0.0
    allocations = {name: round(raw_targets[name] * normalization, 2) for name in baselines}
    decisions = []
    for name in baselines:
        group = group_by_name.get(name, (name,))
        reasons = ["rank_scaled"]
        if len(group) > 1 and ranks.get(name, 1.0) > damped_ranks.get(name, 1.0):
            reasons.append("correlation_group_damped")
        if drawdown_brake_active:
            reasons.append("fleet_drawdown_brake")
        decisions.append(
            AllocationDecision(
                bot_name=name,
                baseline_usd=round(baselines[name], 2),
                target_usd=allocations[name],
                multiplier=round(allocations[name] / baselines[name], 6) if baselines[name] > 0 else 0.0,
                sharpe=round(sharpes[name], 6),
                correlation_group=group,
                reasons=tuple(reasons),
            )
        )

    return PortfolioRebalancePlan(
        allocations=allocations,
        decisions=tuple(decisions),
        drawdown_brake_active=drawdown_brake_active,
        total_baseline_usd=round(total_baseline, 2),
        total_target_usd=round(sum(allocations.values()), 2),
    )


def rebalance_allocations(
    perf: Sequence[BotPerformance],
    *,
    correlations: Mapping[tuple[str, str] | str, float] | None = None,
    correlation_threshold: float = 0.85,
    cap_low: float = 0.5,
    cap_high: float = 2.0,
    fleet_drawdown_pct: float = 0.0,
    drawdown_brake_threshold_pct: float = 0.05,
) -> dict[str, float]:
    """Compatibility wrapper returning bot_name -> recommended_allocation_usd."""
    return build_rebalance_plan(
        perf,
        correlations=correlations,
        correlation_threshold=correlation_threshold,
        cap_low=cap_low,
        cap_high=cap_high,
        fleet_drawdown_pct=fleet_drawdown_pct,
        drawdown_brake_threshold_pct=drawdown_brake_threshold_pct,
    ).allocations


def apply_rebalance_plan(
    bots: Mapping[str, object],
    plan: PortfolioRebalancePlan,
    *,
    dry_run: bool = True,
) -> list[dict[str, object]]:
    """Apply a plan through BaseBot-compatible ``set_equity_ceiling`` hooks.

    Dry runs are the default so operators and schedulers can publish the plan
    without mutating live bot sizing. Returned rows are JSON-ready audit events.
    """
    results: list[dict[str, object]] = []
    for decision in plan.decisions:
        bot = bots.get(decision.bot_name)
        if bot is None:
            results.append(_apply_result(decision, status="missing_bot", dry_run=dry_run))
            continue

        setter = getattr(bot, "set_equity_ceiling", None)
        if not callable(setter):
            results.append(_apply_result(decision, status="skipped_no_equity_ceiling_hook", dry_run=dry_run))
            continue

        if dry_run:
            results.append(_apply_result(decision, status="dry_run", dry_run=True))
            continue

        try:
            setter(decision.target_usd)
        except (TypeError, ValueError, RuntimeError) as exc:
            results.append(_apply_result(decision, status="error", dry_run=False, error=str(exc)))
        else:
            results.append(_apply_result(decision, status="applied", dry_run=False))
    return results


def _apply_result(
    decision: AllocationDecision,
    *,
    status: str,
    dry_run: bool,
    error: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "bot_name": decision.bot_name,
        "target_usd": decision.target_usd,
        "status": status,
        "dry_run": dry_run,
    }
    if error is not None:
        row["error"] = error
    return row


def _dampen_correlated_winners(
    ranks: dict[str, float],
    group_by_name: dict[str, tuple[str, ...]],
) -> dict[str, float]:
    damped = dict(ranks)
    for name, group in group_by_name.items():
        if len(group) <= 1:
            continue
        mult = ranks.get(name, 1.0)
        if mult > 1.0:
            damped[name] = 1.0 + ((mult - 1.0) / len(group))
    return damped


def _correlation_group_by_name(
    names: Sequence[str],
    correlations: Mapping[tuple[str, str] | str, float] | None,
    *,
    threshold: float,
) -> dict[str, tuple[str, ...]]:
    parent = {name: name for name in names}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    if correlations:
        known = set(names)
        for key, value in correlations.items():
            pair = _parse_correlation_pair(key)
            if pair is None:
                continue
            left, right = pair
            if left in known and right in known and abs(value) >= threshold:
                union(left, right)

    grouped: dict[str, list[str]] = {}
    for name in names:
        grouped.setdefault(find(name), []).append(name)
    canonical_groups = {root: tuple(sorted(group)) for root, group in grouped.items()}
    return {name: canonical_groups[find(name)] for name in names}


def _parse_correlation_pair(key: tuple[str, str] | str) -> tuple[str, str] | None:
    if isinstance(key, tuple) and len(key) == 2:
        return key
    if isinstance(key, str):
        for separator in ("|", ":", ","):
            if separator in key:
                left, right = key.split(separator, 1)
                left = left.strip()
                right = right.strip()
                if left and right:
                    return left, right
    return None

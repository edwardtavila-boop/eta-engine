"""
EVOLUTIONARY TRADING ALGO  //  core.kill_switch_runtime
===========================================
Runtime kill-switch evaluator.

Reads configs/kill_switch.yaml and evaluates every tick against live bot state.
Returns a KillVerdict describing what action (if any) the runtime must take.

Decisions source: docs/decisions_v1.json (#4, #12, #13, #16).

The contract is explicit:
  - per_bucket.max_loss_usd           → flatten bot + pause until next session
  - per_bucket.consecutive_losses     → same
  - apex_eval_preemptive              → flatten tier_a + CRITICAL alert
  - tier_b.correlation_kill           → flatten tier_b only, leave tier_a
  - tier_b.funding_veto               → soft = halve size, hard = flatten + 6h pause
  - global.daily_loss_cap_pct_of_port → flatten everything, pause until manual

This module has no venue IO. It is pure policy. The runtime calls
evaluate() each tick and acts on the returned KillVerdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from pathlib import Path


class KillAction(StrEnum):
    CONTINUE = "CONTINUE"
    HALVE_SIZE = "HALVE_SIZE"
    PAUSE_NEW_ENTRIES = "PAUSE_NEW_ENTRIES"
    FLATTEN_BOT = "FLATTEN_BOT"
    FLATTEN_TIER_B = "FLATTEN_TIER_B"
    FLATTEN_ALL = "FLATTEN_ALL"
    FLATTEN_TIER_A_PREEMPTIVE = "FLATTEN_TIER_A_PREEMPTIVE"


class KillSeverity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


@dataclass
class KillVerdict:
    action: KillAction
    severity: KillSeverity
    reason: str
    scope: str  # "bot:<name>" | "tier_a" | "tier_b" | "global"
    evidence: dict[str, Any] = field(default_factory=dict)


class BotSnapshot(BaseModel):
    """Minimal live state for one bot — fed into KillSwitch.evaluate."""

    name: str
    tier: str  # "A" | "B"
    equity_usd: float
    peak_equity_usd: float
    session_realized_pnl_usd: float = 0.0
    consecutive_losses: int = 0
    open_position_count: int = 0
    market_context_summary: dict[str, Any] | None = None
    market_context_summary_text: str | None = None


class PortfolioSnapshot(BaseModel):
    """Aggregate state across all bots."""

    total_equity_usd: float
    peak_equity_usd: float
    daily_realized_pnl_usd: float = 0.0


class CorrelationSnapshot(BaseModel):
    """Rolling correlation summary for Tier-B kill."""

    window_minutes: int = 60
    pair_abs_corr: dict[str, float] = Field(default_factory=dict)
    # key example: "BTC-ETH", value in [0, 1]


class FundingSnapshot(BaseModel):
    """Per-symbol funding-rate in bps."""

    symbol_to_bps: dict[str, float] = Field(default_factory=dict)


class ApexEvalSnapshot(BaseModel):
    """Apex account cushion vs trailing DD."""

    trailing_dd_limit_usd: float = 2500.0
    distance_to_limit_usd: float = 2500.0  # how close we are (smaller = worse)


# ---------------------------------------------------------------------------
# R2 closure -- tick-cadence validator
# ---------------------------------------------------------------------------
# Apex's trailing-DD enforcement is sub-second. Our runtime polls every
# ``tick_interval_s`` seconds. Between ticks, equity can move further than
# the cushion -- i.e. we can silently cross Apex's floor mid-tick. The
# validator enforces a simple worst-case guard:
#
#   tick_interval_s * max_usd_move_per_sec <= cushion_usd / safety_factor
#
# i.e. even under a pathological one-tick price burst, the market is still
# expected to print *inside* the cushion at the NEXT tick, giving the
# KillSwitch at least one chance to fire FLATTEN_TIER_A_PREEMPTIVE before
# Apex's real floor is crossed.
#
# Defaults come from empirical MNQ tick behavior:
#   * Typical 1s MNQ move: 1-2 points = $2-4 on 1x, $10-20 on 5x sizing
#   * Pathological (news spike): up to ~50 points/sec = $100 on 1x, $500 on 5x
# We bake in ``DEFAULT_MAX_USD_MOVE_PER_SEC = 300`` as a Tier-A conservative
# worst-case assuming max Apex sizing.


DEFAULT_MAX_USD_MOVE_PER_SEC: float = 300.0
DEFAULT_TICK_CADENCE_SAFETY_FACTOR: float = 2.0


class ApexTickCadenceError(RuntimeError):
    """Raised when tick_interval_s is too slow for the configured cushion.

    R2 closure: an Apex eval with a 5-second tick and a $500 cushion can be
    busted in a single tick during a fast move. Refusing to start is the
    correct behavior -- better to halt the runtime than to silently fail
    the eval on a latency gap.
    """


def validate_apex_tick_cadence(
    *,
    tick_interval_s: float,
    cushion_usd: float,
    max_usd_move_per_sec: float = DEFAULT_MAX_USD_MOVE_PER_SEC,
    safety_factor: float = DEFAULT_TICK_CADENCE_SAFETY_FACTOR,
    live: bool = False,
) -> None:
    """Enforce that tick cadence is fast enough for the configured cushion.

    Parameters
    ----------
    tick_interval_s:
        Seconds between runtime ticks. From ``RuntimeConfig.tick_interval_s``.
    cushion_usd:
        Apex-preemptive cushion in USD. From ``kill_switch.yaml``:
        ``tier_a.apex_eval_preemptive.cushion_usd``.
    max_usd_move_per_sec:
        Conservative worst-case one-second equity move. Default tuned for
        MNQ at max Apex sizing (see module comment).
    safety_factor:
        How many worst-case ticks must fit inside the cushion before it
        is considered "safe". Default 2.0 means we require the cushion to
        be at least 2x the worst-case one-tick move.
    live:
        If False, the validator no-ops (paper / backtest runs tolerate
        arbitrary tick cadences). If True, any violation raises.

    Raises
    ------
    ApexTickCadenceError
        When ``tick_interval_s * max_usd_move_per_sec * safety_factor``
        exceeds ``cushion_usd``.
    ValueError
        On non-positive inputs.
    """
    if tick_interval_s <= 0:
        msg = f"tick_interval_s must be > 0 (got {tick_interval_s})"
        raise ValueError(msg)
    if cushion_usd <= 0:
        msg = f"cushion_usd must be > 0 (got {cushion_usd})"
        raise ValueError(msg)
    if max_usd_move_per_sec <= 0:
        msg = f"max_usd_move_per_sec must be > 0 (got {max_usd_move_per_sec})"
        raise ValueError(msg)
    if safety_factor <= 0:
        msg = f"safety_factor must be > 0 (got {safety_factor})"
        raise ValueError(msg)
    if not live:
        return
    required = tick_interval_s * max_usd_move_per_sec * safety_factor
    if required > cushion_usd:
        msg = (
            f"Apex tick cadence too slow for cushion: "
            f"tick_interval_s={tick_interval_s} * max_usd_move_per_sec="
            f"{max_usd_move_per_sec} * safety={safety_factor} = "
            f"${required:.0f} required, but cushion_usd=${cushion_usd:.0f}. "
            f"Either (a) drop tick_interval_s, or (b) raise cushion_usd in "
            f"configs/kill_switch.yaml tier_a.apex_eval_preemptive."
        )
        raise ApexTickCadenceError(msg)


class KillSwitch:
    """
    Holds the parsed configs/kill_switch.yaml and evaluates bot state each tick.

    Usage
    -----
    ks = KillSwitch.from_yaml(Path("configs/kill_switch.yaml"))
    verdicts = ks.evaluate(
        bots=[BotSnapshot(...), ...],
        portfolio=PortfolioSnapshot(...),
        correlations=CorrelationSnapshot(...),
        funding=FundingSnapshot(...),
        apex_eval=ApexEvalSnapshot(...),
    )
    for v in verdicts:
        act_on(v)
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self._g = cfg.get("global", {}) or {}
        self._ta = cfg.get("tier_a", {}) or {}
        self._tb = cfg.get("tier_b", {}) or {}

    @classmethod
    def from_yaml(cls, path: Path) -> KillSwitch:
        with path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        return cls(cfg or {})

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        bots: list[BotSnapshot],
        portfolio: PortfolioSnapshot,
        correlations: CorrelationSnapshot | None = None,
        funding: FundingSnapshot | None = None,
        apex_eval: ApexEvalSnapshot | None = None,
    ) -> list[KillVerdict]:
        verdicts: list[KillVerdict] = []

        # 1. Global trip — takes precedence
        g = self._check_global(portfolio)
        if g:
            verdicts.append(g)
            return verdicts  # global flatten supersedes everything

        # 2. Apex eval preemptive (Tier-A only)
        if apex_eval is not None:
            ae = self._check_apex_preemptive(apex_eval)
            if ae:
                verdicts.append(ae)

        # 3. Tier-B correlation kill
        if correlations is not None:
            cv = self._check_correlation(correlations)
            if cv:
                verdicts.append(cv)

        # 4. Tier-B funding veto (per-symbol)
        if funding is not None:
            for fv in self._check_funding(funding):
                verdicts.append(fv)

        # 5. Per-bucket bot checks
        for bot in bots:
            bv = self._check_bot(bot)
            if bv:
                verdicts.append(bv)

        if not verdicts:
            verdicts.append(
                KillVerdict(
                    action=KillAction.CONTINUE,
                    severity=KillSeverity.INFO,
                    reason="no trip",
                    scope="global",
                )
            )
        return verdicts

    # ------------------------------------------------------------------ #
    # Internal checks
    # ------------------------------------------------------------------ #
    def _check_global(self, p: PortfolioSnapshot) -> KillVerdict | None:
        if p.total_equity_usd <= 0 or p.peak_equity_usd <= 0:
            return None
        dd_pct = (p.peak_equity_usd - p.total_equity_usd) / p.peak_equity_usd * 100.0
        daily_loss_pct = abs(min(0.0, p.daily_realized_pnl_usd)) / p.peak_equity_usd * 100.0
        dd_cap = float(self._g.get("max_drawdown_kill_pct_of_portfolio", 100.0))
        daily_cap = float(self._g.get("daily_loss_cap_pct_of_portfolio", 100.0))
        if dd_pct >= dd_cap:
            return KillVerdict(
                action=KillAction.FLATTEN_ALL,
                severity=KillSeverity.CRITICAL,
                reason=f"portfolio DD {dd_pct:.2f}% >= cap {dd_cap}%",
                scope="global",
                evidence={"dd_pct": dd_pct, "cap_pct": dd_cap},
            )
        if daily_loss_pct >= daily_cap:
            return KillVerdict(
                action=KillAction.FLATTEN_ALL,
                severity=KillSeverity.CRITICAL,
                reason=f"daily loss {daily_loss_pct:.2f}% >= cap {daily_cap}%",
                scope="global",
                evidence={"daily_loss_pct": daily_loss_pct, "cap_pct": daily_cap},
            )
        return None

    def _check_apex_preemptive(self, ae: ApexEvalSnapshot) -> KillVerdict | None:
        spec = self._ta.get("apex_eval_preemptive", {}) or {}
        cushion = float(spec.get("cushion_usd", 500))
        if ae.distance_to_limit_usd <= cushion:
            return KillVerdict(
                action=KillAction.FLATTEN_TIER_A_PREEMPTIVE,
                severity=KillSeverity.CRITICAL,
                reason=(f"apex cushion {ae.distance_to_limit_usd:.0f} <= preempt {cushion:.0f}"),
                scope="tier_a",
                evidence={
                    "distance_to_limit_usd": ae.distance_to_limit_usd,
                    "cushion_usd": cushion,
                    "trailing_dd_limit_usd": ae.trailing_dd_limit_usd,
                },
            )
        return None

    def _check_correlation(self, c: CorrelationSnapshot) -> KillVerdict | None:
        spec = self._tb.get("correlation_kill", {}) or {}
        if not bool(spec.get("enabled", False)):
            return None
        thr = float(spec.get("threshold_abs_corr", 0.85))
        need_pairs = int(spec.get("pairs_required", 4))
        high = [k for k, v in c.pair_abs_corr.items() if abs(v) >= thr]
        if len(high) >= need_pairs:
            return KillVerdict(
                action=KillAction.FLATTEN_TIER_B,
                severity=KillSeverity.WARN,
                reason=(f"{len(high)}/{need_pairs} pairs >= |corr|={thr}: {high}"),
                scope="tier_b",
                evidence={"high_corr_pairs": high, "threshold": thr},
            )
        return None

    def _check_funding(self, f: FundingSnapshot) -> list[KillVerdict]:
        spec = self._tb.get("funding_veto", {}) or {}
        soft = float(spec.get("soft_threshold_bps", 20))
        hard = float(spec.get("hard_threshold_bps", 50))
        out: list[KillVerdict] = []
        for sym, bps in f.symbol_to_bps.items():
            bps_abs = abs(bps)
            if bps_abs >= hard:
                out.append(
                    KillVerdict(
                        action=KillAction.FLATTEN_BOT,
                        severity=KillSeverity.WARN,
                        reason=f"{sym} funding |{bps:.1f}bps| >= hard {hard}",
                        scope=f"bot:{sym}",
                        evidence={"symbol": sym, "bps": bps, "hard_bps": hard},
                    )
                )
            elif bps_abs >= soft:
                out.append(
                    KillVerdict(
                        action=KillAction.HALVE_SIZE,
                        severity=KillSeverity.INFO,
                        reason=f"{sym} funding |{bps:.1f}bps| >= soft {soft}",
                        scope=f"bot:{sym}",
                        evidence={"symbol": sym, "bps": bps, "soft_bps": soft},
                    )
                )
        return out

    def _check_bot(self, bot: BotSnapshot) -> KillVerdict | None:
        tier_spec = self._ta if bot.tier == "A" else self._tb
        per_bucket = (tier_spec.get("per_bucket", {}) or {}).get(bot.name, {}) or {}

        # Tier A uses max_loss_usd absolute; Tier B uses max_loss_pct of bucket.
        if bot.tier == "A":
            max_loss_usd = float(per_bucket.get("max_loss_usd", 1e12))
            if bot.session_realized_pnl_usd <= -max_loss_usd:
                return KillVerdict(
                    action=KillAction.FLATTEN_BOT,
                    severity=KillSeverity.WARN,
                    reason=(
                        f"{bot.name} session pnl {bot.session_realized_pnl_usd:.0f} <= -${max_loss_usd:.0f} trip-wire"
                    ),
                    scope=f"bot:{bot.name}",
                    evidence={
                        "session_realized_pnl_usd": bot.session_realized_pnl_usd,
                        "max_loss_usd": max_loss_usd,
                    },
                )
        else:
            max_loss_pct = float(per_bucket.get("max_loss_pct", 100.0))
            if bot.peak_equity_usd > 0:
                loss_pct = (bot.peak_equity_usd - bot.equity_usd) / bot.peak_equity_usd * 100.0
                if loss_pct >= max_loss_pct:
                    return KillVerdict(
                        action=KillAction.FLATTEN_BOT,
                        severity=KillSeverity.WARN,
                        reason=(f"{bot.name} equity drawdown {loss_pct:.2f}% >= {max_loss_pct}%"),
                        scope=f"bot:{bot.name}",
                        evidence={"loss_pct": loss_pct, "cap_pct": max_loss_pct},
                    )

        # Both tiers respect consecutive-loss trip
        consec_cap = int(per_bucket.get("consecutive_losses", 0) or 0)
        if consec_cap > 0 and bot.consecutive_losses >= consec_cap:
            return KillVerdict(
                action=KillAction.FLATTEN_BOT,
                severity=KillSeverity.WARN,
                reason=f"{bot.name} {bot.consecutive_losses} consecutive losses >= {consec_cap}",
                scope=f"bot:{bot.name}",
                evidence={
                    "consecutive_losses": bot.consecutive_losses,
                    "cap": consec_cap,
                },
            )
        return None

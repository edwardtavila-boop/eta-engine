"""
EVOLUTIONARY TRADING ALGO  //  strategies.ensemble_voting_strategy
===================================================================
Multi-strategy ensemble voting: only fires when ≥ N independent
sub-strategies propose the SAME side at roughly the same bar.

Rationale
---------
Each sub-strategy in the catalog captures a different edge:
* `crypto_regime_trend`              — pullback to fast EMA
* `crypto_macro_confluence` (+ETF)   — pullback + ETF flow filter
* `crypto_orb` (UTC anchor)          — UTC midnight breakout
* `htf_routed` (mean-revert mode)    — fade extremes in range

These are independently-edge'd signals on different mechanics. When
two or more agree on the same side at the same time, conviction is
materially higher than any single strategy alone — that's the
information theoretic value of independent confirmation.

The voter:
1. On each bar, calls all sub-strategies (their states advance
   regardless of voting outcome).
2. Collects their proposals (side + confidence — uses the strategy's
   inherent risk_usd as a proxy for confidence).
3. Counts votes per side.
4. Fires when total agreeing votes >= ``min_agreement_count``.
5. Position size is the AVERAGE of the agreeing strategies' sizes.

Trade count stays high because individual strategies still fire when
alone, but only ``min_agreement_count`` proposals get past the voter.
For ``min_agreement_count=2``, roughly half of all single-strategy
fires get a vote of confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from statistics import mean
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


# Type-alias for any sub-strategy that exposes maybe_enter()
if TYPE_CHECKING:
    from typing import Protocol

    class _SubStrategy(Protocol):
        """Protocol for any object with the engine's maybe_enter contract."""

        def maybe_enter(
            self,
            bar: BarData,
            hist: list[BarData],
            equity: float,
            config: BacktestConfig,
        ) -> _Open | None:
            ...


@dataclass(frozen=True)
class EnsembleVotingConfig:
    """Voter knobs."""

    # Minimum number of sub-strategies that must propose the same
    # side for an entry to fire. 2 = light agreement; 3+ = high
    # confidence (rare).
    min_agreement_count: int = 2

    # When agreement count exceeds min, optionally scale position size
    # by (count / min). Default OFF — agreement gates the trade but
    # doesn't amplify size.
    size_by_agreement: bool = False
    max_size_multiplier: float = 1.5

    # Tag to write into _Open.regime so the audit trail shows which
    # strategies voted.
    regime_prefix: str = "ensemble"

    # Turn on context-aware routing: strategy votes are down/up-weighted
    # by the inferred tape regime (trend/chop/high-vol) before selecting
    # the winner.
    use_regime_router: bool = True

    # Confidence-weighted aggregation (legacy mode):
    # combine proposals via a conviction weight derived from confluence,
    # implied R multiple and risk commitment.  Default OFF in favor of
    # elect_one composition (see below).
    use_confidence_weighting: bool = False

    # Composition mode.  ``elect_one`` (default) picks ONE proposal as
    # the bracket source — the highest-confluence agreeing sub sets
    # entry/stop/target verbatim, agreement count is the gate only.
    # This avoids the geometric incoherence of averaging brackets that
    # no individual sub designed (sub A enters at 100/stops at 95, sub B
    # enters at 105/stops at 100 → average bracket is "100 entry / 97.5
    # stop" which is too tight for A AND too loose for B).
    # ``average`` falls back to the legacy averaging path.
    composition_mode: str = "elect_one"

    # Adversarial safety rail: abstain in toxic market micro-conditions.
    enable_fail_safe: bool = True
    max_wick_to_range_ratio: float = 0.75
    max_short_term_range_to_long_term_range: float = 2.6
    max_gap_to_atr: float = 2.5


class EnsembleVotingStrategy:
    """Aggregates multiple strategies via majority vote."""

    def __init__(
        self,
        sub_strategies: list[tuple[str, _SubStrategy]],
        config: EnsembleVotingConfig | None = None,
    ) -> None:
        if not sub_strategies:
            raise ValueError("ensemble requires at least one sub-strategy")
        self._subs = list(sub_strategies)
        self.cfg = config or EnsembleVotingConfig()
        if self.cfg.min_agreement_count < 1:
            raise ValueError("min_agreement_count must be >= 1")
        if self.cfg.min_agreement_count > len(self._subs):
            raise ValueError(
                f"min_agreement_count {self.cfg.min_agreement_count} "
                f"exceeds number of sub-strategies {len(self._subs)}"
            )

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        if self.cfg.enable_fail_safe and self._should_abstain(bar, hist):
            return None

        # Always call ALL sub-strategies (even if vote will fail)
        # so their states (EMAs, cooldowns) advance every bar.
        proposals: list[tuple[str, _Open]] = []
        for name, strat in self._subs:
            try:
                out = strat.maybe_enter(bar, hist, equity, config)
            except Exception:  # noqa: BLE001 - sub isolation
                continue
            if out is not None:
                if self.cfg.use_regime_router:
                    regime_weight = self._regime_weight_for_strategy(name, bar, hist)
                    if regime_weight <= 0.0:
                        continue
                    out = self._scale_open(out, regime_weight)
                proposals.append((name, out))

        if len(proposals) < self.cfg.min_agreement_count:
            return None

        # Tally votes by side
        long_votes = [p for n, p in proposals if p.side == "BUY"]
        short_votes = [p for n, p in proposals if p.side == "SELL"]

        if len(long_votes) >= self.cfg.min_agreement_count:
            chosen_side = "BUY"
            chosen_proposals = long_votes
        elif len(short_votes) >= self.cfg.min_agreement_count:
            chosen_side = "SELL"
            chosen_proposals = short_votes
        else:
            # Sub-strategies disagree on side — no consensus
            return None

        # Aggregate chosen-side proposals.
        n = len(chosen_proposals)
        if self.cfg.composition_mode == "elect_one":
            # Winner-takes-bracket: the highest-confluence agreeing
            # proposal sets entry/stop/target verbatim. Agreement count
            # is the gate only.  Avoids averaging brackets to a geometry
            # no individual sub designed.
            winner = max(
                chosen_proposals, key=lambda p: self._proposal_weight(p),
            )
            avg_entry = winner.entry_price
            avg_stop = winner.stop
            avg_target = winner.target
            avg_qty = winner.qty
            avg_risk = winner.risk_usd
            agg_conf = float(getattr(winner, "confluence", 5.0))
        elif self.cfg.use_confidence_weighting:
            weights = [self._proposal_weight(p) for p in chosen_proposals]
            wsum = sum(weights) or float(n)
            avg_entry = sum(p.entry_price * w for p, w in zip(chosen_proposals, weights, strict=False)) / wsum
            avg_stop = sum(p.stop * w for p, w in zip(chosen_proposals, weights, strict=False)) / wsum
            avg_target = sum(p.target * w for p, w in zip(chosen_proposals, weights, strict=False)) / wsum
            avg_qty = sum(p.qty * w for p, w in zip(chosen_proposals, weights, strict=False)) / wsum
            avg_risk = sum(p.risk_usd * w for p, w in zip(chosen_proposals, weights, strict=False)) / wsum
            agg_conf = min(10.0, max(0.0, mean(weights)))
        else:
            avg_entry = sum(p.entry_price for p in chosen_proposals) / n
            avg_stop = sum(p.stop for p in chosen_proposals) / n
            avg_target = sum(p.target for p in chosen_proposals) / n
            avg_qty = sum(p.qty for p in chosen_proposals) / n
            avg_risk = sum(p.risk_usd for p in chosen_proposals) / n
            agg_conf = mean(p.confluence for p in chosen_proposals)

        # Optional size scaling by vote count
        if self.cfg.size_by_agreement:
            mult = min(
                self.cfg.max_size_multiplier,
                n / self.cfg.min_agreement_count,
            )
            avg_qty *= mult
            avg_risk *= mult

        # GUARD: averaged geometry must still be valid.  Sub-strategies'
        # individually-correct stops can average to wrong-side configurations
        # when sub-strategies disagree wildly on entry price.  _Open's
        # __post_init__ would also catch this, but we abstain cleanly here
        # rather than raise.
        if chosen_side == "BUY":
            if avg_stop >= avg_entry or avg_target <= avg_entry:
                return None
        else:
            if avg_stop <= avg_entry or avg_target >= avg_entry:
                return None

        agreement_names = "+".join(name for name, _ in proposals if name in {
            n_ for n_, p in self._subs_for_proposals(chosen_proposals)
        })
        if not agreement_names:
            agreement_names = ",".join(name for name, p in proposals if p.side == chosen_side)

        regime_tag = (
            f"{self.cfg.regime_prefix}_{chosen_side.lower()}_"
            f"{n}of{len(self._subs)}_{agreement_names}"
        )

        # Use the FIRST agreeing proposal as the base (for entry_bar
        # + leverage etc.), then overlay the averaged values.
        base = chosen_proposals[0]
        return replace(
            base,
            side=chosen_side,
            entry_price=avg_entry,
            stop=avg_stop,
            target=avg_target,
            qty=avg_qty,
            risk_usd=avg_risk,
            confluence=agg_conf,
            regime=regime_tag,
        )

    def _proposal_weight(self, o: _Open) -> float:
        stop_dist = abs(o.entry_price - o.stop)
        tgt_dist = abs(o.target - o.entry_price)
        rr = tgt_dist / stop_dist if stop_dist > 0 else 1.0
        # Blend three dimensions:
        # - confluence (quality),
        # - rr (asymmetric payoff),
        # - risk_usd (skin in the game).
        conf_term = max(0.1, min(10.0, o.confluence)) / 10.0
        rr_term = max(0.5, min(2.5, rr)) / 2.5
        risk_term = max(1.0, o.risk_usd) ** 0.2
        return conf_term * rr_term * risk_term

    def _scale_open(self, o: _Open, mult: float) -> _Open:
        m = max(0.0, min(1.5, mult))
        return replace(
            o,
            qty=o.qty * m,
            risk_usd=o.risk_usd * m,
            confluence=max(0.0, min(10.0, o.confluence * m)),
        )

    def _infer_regime(self, bar: BarData, hist: list[BarData]) -> str:
        window = hist[-24:] if len(hist) >= 24 else hist
        if len(window) < 6:
            return "neutral"
        avg_range = mean(max(1e-9, b.high - b.low) for b in window)
        short = window[-6:]
        short_range = mean(max(1e-9, b.high - b.low) for b in short)
        closes = [b.close for b in window]
        drift = closes[-1] - closes[0]
        trend_strength = abs(drift) / max(1e-9, avg_range * len(window))
        if short_range / max(1e-9, avg_range) > 2.0:
            return "high_vol"
        if trend_strength > 0.75:
            return "trend"
        return "chop"

    def _regime_weight_for_strategy(self, name: str, bar: BarData, hist: list[BarData]) -> float:
        regime = self._infer_regime(bar, hist)
        lname = name.lower()
        if regime == "high_vol":
            return 0.6 if ("trend" in lname or "orb" in lname) else 0.25
        if regime == "trend":
            return 1.2 if ("trend" in lname or "orb" in lname) else 0.7
        if regime == "chop":
            return 1.15 if ("meanrev" in lname or "grid" in lname or "sweep" in lname) else 0.8
        return 1.0

    def _should_abstain(self, bar: BarData, hist: list[BarData]) -> bool:
        if len(hist) < 8:
            return False
        last = bar
        prev = hist[-1]
        body = abs(last.close - last.open)
        rng = max(1e-9, last.high - last.low)
        upper_wick = max(0.0, last.high - max(last.open, last.close))
        lower_wick = max(0.0, min(last.open, last.close) - last.low)
        wick_ratio = (upper_wick + lower_wick) / rng

        short = hist[-6:]
        long = hist[-24:] if len(hist) >= 24 else hist
        short_range = mean(max(1e-9, b.high - b.low) for b in short)
        long_range = mean(max(1e-9, b.high - b.low) for b in long)
        vol_shock = short_range / max(1e-9, long_range)

        # Approximate gap by close-to-open jump relative to rolling ATR proxy.
        atr = mean(max(1e-9, b.high - b.low) for b in long)
        gap_to_atr = abs(last.open - prev.close) / max(1e-9, atr)

        if wick_ratio > self.cfg.max_wick_to_range_ratio and body / rng < 0.35:
            return True
        if vol_shock > self.cfg.max_short_term_range_to_long_term_range:
            return True
        return gap_to_atr > self.cfg.max_gap_to_atr

    # -- helper for audit-trail name tagging ---------------------------------

    def _subs_for_proposals(
        self, proposals: list[_Open],  # noqa: ARG002 - reserved for future ref-matching
    ) -> list[tuple[str, _SubStrategy]]:
        # Reverse-map proposals back to (name, sub) pairs by matching
        # references where possible. Best-effort; tagging is informational.
        return [(name, sub) for name, sub in self._subs if sub is not None]

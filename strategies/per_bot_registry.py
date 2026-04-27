"""
EVOLUTIONARY TRADING ALGO  //  strategies.per_bot_registry
===========================================================
Per-bot strategy assignments — the canonical answer to "which
strategy should this bot run as its baseline?"

Why this exists
---------------
What moves the price differs across instruments:

  * **MNQ / NQ futures**: macro events, ES correlation, RTH structure,
    EoD rebalance, regime (trending vs choppy)
  * **BTC perps**: funding rate, on-chain activity (whale transfers,
    exchange netflow), Asian session timing, sentiment
  * **ETH / XRP / SOL perps**: same as BTC + token-specific
    catalysts (upgrades, ETF flows for ETH, regulation)
  * **Long-haul (daily / weekly)**: trend persistence, weekly options
    gamma, macro regime

Until now every bot in ``bots/`` shared one FeaturePipeline.default()
and one global scorer. That's wrong: a strategy that works on
choppy MNQ 5m will not work on BTC perps where funding is the
dominant signal.

This module is the registry that says, per bot:

  * which dataset (symbol + timeframe) to evaluate against
  * which scorer to use (global / MNQ-tuned / future BTC-tuned)
  * which regimes to block
  * what threshold to clear
  * the baseline metrics the strategy was promoted at, if any

The registry is **read-only** — every assignment is a frozen
dataclass — so no caller can mutate state at runtime. Updating a
bot's assignment is a code change reviewed via PR, not a
configuration drift.

Adoption
--------
* ``research_grid`` (``scripts.run_research_grid``) reads from this
  to run every bot's assigned strategy in one sweep.
* ``drift_check_all`` reads baselines from here when
  ``strategy_baselines.json`` doesn't have an entry for a bot.
* New bots get added in ``ASSIGNMENTS`` below and immediately get
  smoke-tested in the next research-grid run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.obs.drift_monitor import BaselineSnapshot


@dataclass(frozen=True)
class StrategyAssignment:
    """Canonical strategy-for-this-bot record."""

    bot_id: str  # e.g. "mnq_futures", "btc_perp"
    strategy_id: str  # e.g. "mnq_v3_regime_gated"

    # Data binding
    symbol: str
    timeframe: str

    # Scoring
    scorer_name: str  # "global" or "mnq" (future: "btc", "long_haul")
    confluence_threshold: float

    # Regime gate
    block_regimes: frozenset[str]

    # Walk-forward / promotion config
    window_days: int
    step_days: int
    min_trades_per_window: int

    # Why this combination — short rationale, not a docstring novel
    rationale: str

    # Promotion-time baseline (may be None if not yet promoted)
    baseline: "BaselineSnapshot | None" = None

    # Free-form extras (e.g. EoD-flatten on/off, leverage caps).
    # Reserved for future engine knobs without breaking serialisation.
    extras: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-bot assignments
# ---------------------------------------------------------------------------
# Each bot here gets the best-known strategy for its instrument,
# based on the regime-gate findings and data-availability scan from
# 2026-04-27. These are *baselines to improve upon*, not finalised
# production picks.

_BASE_BLOCK = frozenset({"trending_up", "trending_down"})


ASSIGNMENTS: tuple[StrategyAssignment, ...] = (
    # MNQ futures — micro E-mini Nasdaq
    StrategyAssignment(
        bot_id="mnq_futures",
        strategy_id="mnq_v3_regime_gated",
        symbol="MNQ1",
        timeframe="4h",
        scorer_name="mnq",
        confluence_threshold=5.0,
        block_regimes=_BASE_BLOCK,
        window_days=180,
        step_days=60,
        min_trades_per_window=10,
        rationale=(
            "MNQ price moves are dominated by ES correlation + RTH "
            "structure + macro events. The MNQ-tuned scorer drops "
            "the crypto-only features (funding/onchain/sentiment) "
            "that were artificially inflating composite scores. The "
            "regime gate blocks trending bars where the strategy "
            "(mean-reversion) bleeds — Window 0 deep-dive on 5m "
            "showed +6R in choppy regimes, -2.5R in trending_up. "
            "4h timeframe gave the best DSR pass fraction (45%) in "
            "the 2026-04-27 research grid. v2 optimization stack "
            "(classify_regime_v2 + session gate + ES correlation) "
            "is wired and tested in strategies.mnq_optimizations "
            "but is opt-in via env vars — adding all three gates on "
            "top of regime block dropped sample-per-window below "
            "the strict gate's min_trades floor in the 2026-04-27 "
            "ablation. Real edge requires new features (CME-spot "
            "basis, options gamma exposure, ES decoupling), not "
            "more gates on the current feature set."
        ),
    ),
    # NQ futures — full E-mini Nasdaq, longer-haul lens
    StrategyAssignment(
        bot_id="nq_futures",
        strategy_id="nq_daily_regime_gated",
        symbol="NQ1",
        timeframe="D",
        scorer_name="mnq",
        confluence_threshold=5.0,
        block_regimes=_BASE_BLOCK,
        window_days=365,
        step_days=180,
        min_trades_per_window=10,
        rationale=(
            "NQ daily is the only configuration that produced "
            "POSITIVE aggregate OOS Sharpe (+0.157) across a 27-year "
            "history (1999-2026). Trade frequency is low (most "
            "windows have 0-3 trades) so promotion bar is high — "
            "this is a bias-test more than an active-trading config. "
            "Use as a sanity baseline; a real edge claim needs a "
            "regime+macro feature set that fires more often."
        ),
    ),
    # BTC hybrid — futures + perp blended bot
    StrategyAssignment(
        bot_id="btc_hybrid",
        strategy_id="btc_global_funding_skew",
        symbol="MNQ1",  # placeholder until we have BTC bars in the library
        timeframe="1h",
        scorer_name="global",  # use global until a BTC-tuned scorer exists
        confluence_threshold=7.0,
        block_regimes=frozenset(),  # no gate — funding/onchain ARE the signal
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        rationale=(
            "Operator directive 2026-04-27: crypto bots trade CME "
            "crypto futures (cash-settled, no native funding/onchain) "
            "rather than spot perps. The BTC-tuned scorer "
            "(score_confluence_btc) equal-weights all 5 features so "
            "spot-driven signals (funding/onchain/sentiment) still "
            "contribute when paired feeds are available, and the "
            "scorer degrades gracefully to bar-derived signals only "
            "when they're not. Threshold 6.0 — between MNQ's 5.0 "
            "(2 features active) and global's 7.0 (5 features "
            "weighted unequally). Regime gate disabled — trending "
            "regimes in crypto are often the trade, not the danger. "
            "Symbol/timeframe placeholder until CME crypto bars "
            "land in the data library (see data.requirements for "
            "the full feed list)."
        ),
    ),
    # ETH perp — same family as BTC but with smart-contract catalysts
    StrategyAssignment(
        bot_id="eth_perp",
        strategy_id="eth_global_default",
        symbol="MNQ1",  # placeholder
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=6.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        rationale=(
            "ETH shares price drivers with BTC (funding, on-chain) "
            "but adds smart-contract / staking catalysts that aren't "
            "in our feature set yet. Until ETH-specific features "
            "(staking yield delta, gas fee regime, gas-price "
            "trending) are wired, ETH inherits the BTC global-scorer "
            "approach. Symbol placeholder same as btc_hybrid."
        ),
    ),
    # XRP perp — DEACTIVATED until news/sentiment feed lands.
    StrategyAssignment(
        bot_id="xrp_perp",
        strategy_id="xrp_DEACTIVATED",
        symbol="MNQ1",  # placeholder; not used while bot is muted
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=10.0,  # impossible to reach — bot is muted
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        rationale=(
            "DEACTIVATED 2026-04-27. XRP price is dominated by "
            "regulatory news (SEC headlines, lawsuit outcomes, ETF "
            "approval cycles), none of which the current feature "
            "set captures. Operating XRP without that signal is "
            "noise-chasing. Threshold raised to 10.0 (mathematically "
            "unreachable since the scorer caps at 10.0 only with "
            "every feature at 1.0 normalized) so the bot fires zero "
            "trades — explicitly muted, not silently broken. "
            "Reactivate once: (1) a news/regulatory feed is wired "
            "into the data library (see BotRequirements:xrp_perp), "
            "and (2) a feature class consumes it (e.g. SECHeadline"
            "Feature returning a time-decay signal around recent "
            "rulings)."
        ),
        extras={"deactivated": True, "deactivation_reason": "no news feed"},
    ),
    # SOL perp — high-beta crypto, behaves like BTC * 2-3x
    StrategyAssignment(
        bot_id="sol_perp",
        strategy_id="sol_btc_default",
        symbol="MNQ1",  # placeholder
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=6.5,  # slight bump for higher noise
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        rationale=(
            "SOL behaves as a BTC-amplified beta. Same global "
            "scorer; threshold raised from 7.0 to 7.5 to dampen "
            "false fires from SOL's higher noise floor. Real upgrade: "
            "an explicit BTC-correlation feature so SOL only fires "
            "when BTC is also confirming. Placeholder symbol/timeframe."
        ),
    ),
    # Crypto seed — long-only DCA-style accumulator
    StrategyAssignment(
        bot_id="crypto_seed",
        strategy_id="crypto_seed_dca",
        symbol="MNQ1",  # placeholder
        timeframe="D",
        scorer_name="global",
        confluence_threshold=4.0,  # very low — DCA fires often by design
        block_regimes=frozenset(),
        window_days=365,
        step_days=180,
        min_trades_per_window=5,
        rationale=(
            "DCA accumulator — the strategy is to buy steadily at "
            "any non-distressed score. Threshold 4.0 (very low) "
            "ensures regular fires. Daily timeframe matches the "
            "accumulation cadence. Distinct from all other bots "
            "because the goal is *exposure*, not edge."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Lookup API
# ---------------------------------------------------------------------------


def get_for_bot(bot_id: str) -> StrategyAssignment | None:
    """Return the assignment for ``bot_id`` or None."""
    for a in ASSIGNMENTS:
        if a.bot_id == bot_id:
            return a
    return None


def all_assignments() -> list[StrategyAssignment]:
    """Stable-ordered list of every registered assignment."""
    return list(ASSIGNMENTS)


def bots() -> list[str]:
    """Stable-ordered list of every registered bot_id."""
    return [a.bot_id for a in ASSIGNMENTS]


def summary_markdown() -> str:
    """One-table dump of the registry, suitable for status pages."""
    lines = [
        "# Per-bot strategy assignments",
        "",
        "| Bot | Strategy | Sym/TF | Scorer | Thr | Gate | Win/Step (d) | Min trades |",
        "|---|---|---|---|---:|---|---|---:|",
    ]
    for a in ASSIGNMENTS:
        gate_str = "/".join(sorted(a.block_regimes)) if a.block_regimes else "—"
        lines.append(
            f"| {a.bot_id} | {a.strategy_id} | {a.symbol}/{a.timeframe} | "
            f"{a.scorer_name} | {a.confluence_threshold:.1f} | {gate_str} | "
            f"{a.window_days}/{a.step_days} | {a.min_trades_per_window} |"
        )
    return "\n".join(lines)

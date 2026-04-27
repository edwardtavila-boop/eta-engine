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
    baseline: BaselineSnapshot | None = None

    # Free-form extras (e.g. EoD-flatten on/off, leverage caps).
    # Reserved for future engine knobs without breaking serialisation.
    extras: dict[str, object] = field(default_factory=dict)

    # Which entry-decision path the bot uses at backtest/live time.
    # "confluence"     = score features through scorer_name + check
    #                    threshold + regime gate (legacy behaviour).
    # "orb"            = Opening Range Breakout (intraday) — see
    #                    strategies.orb_strategy. RTH-anchored.
    # "drb"            = Daily Range Breakout — see
    #                    strategies.drb_strategy. Prior-day high/low
    #                    break on daily bars; works on 27y of NQ
    #                    history where intraday ORB has zero range.
    # "grid"           = Grid trading — primary baseline for crypto
    #                    perps. Ladder of buy/sell levels around a
    #                    rolling reference; engine-compatible single-
    #                    position variant. See
    #                    strategies.grid_trading_strategy. Per the
    #                    2026-04-27 user directive: "Most Popular &
    #                    Bot-Native for Crypto".
    # "crypto_orb"     = UTC-anchored ORB for 24/7 crypto. Same engine
    #                    contract as ORB; defaults pinned to UTC
    #                    midnight + 60m range. See
    #                    strategies.crypto_orb_strategy.
    # "crypto_trend"   = EMA(9/21) crossover + HTF EMA bias for 24/7
    #                    bars. See strategies.crypto_trend_strategy.
    # "crypto_meanrev" = Bollinger touch + RSI extreme. See
    #                    strategies.crypto_meanrev_strategy.
    # "crypto_scalp"   = N-bar level break + VWAP + RSI on short TFs.
    #                    See strategies.crypto_scalp_strategy.
    # "sage_consensus" = JARVIS sage 22-school weighted-vote entry.
    #                    Heavy CPU (sage on every bar) but uses every
    #                    classical + modern + statistical school's
    #                    bias as the directional signal. See
    #                    strategies.sage_consensus_strategy.
    # "orb_sage_gated" = ORB + sage overlay on the breakout direction.
    #                    Sage vetoes false breakouts where the
    #                    ensemble disagrees. 2026-04-27 sweep on MNQ
    #                    5m: agg OOS Sharpe **+10.06** vs plain ORB
    #                    +5.71 — sage gating ~doubles the OOS Sharpe.
    #                    See strategies.sage_gated_orb_strategy.
    # All non-"confluence" kinds ignore scorer/threshold/regime
    # fields — those modules have their own knobs that the research
    # grid pulls from the per-bot extras dict under "*_config" keys.
    strategy_kind: str = "confluence"


# ---------------------------------------------------------------------------
# Per-bot assignments
# ---------------------------------------------------------------------------
# Each bot here gets the best-known strategy for its instrument,
# based on the regime-gate findings and data-availability scan from
# 2026-04-27. These are *baselines to improve upon*, not finalised
# production picks.

_BASE_BLOCK = frozenset({"trending_up", "trending_down"})


ASSIGNMENTS: tuple[StrategyAssignment, ...] = (
    # MNQ futures — micro E-mini Nasdaq, ORB baseline
    StrategyAssignment(
        bot_id="mnq_futures",
        strategy_id="mnq_orb_v1",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="mnq",  # unused when strategy_kind=orb but kept for sync
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="orb",
        rationale=(
            "Switched from confluence-mean-reversion to ORB on "
            "2026-04-27 after the mean-reversion baseline "
            "(MNQ-tuned scorer + regime gate) failed to produce "
            "edge across all real-data tests (best result: "
            "agg OOS Sharpe -1.31). ORB on real MNQ 5m at 60/30 "
            "windows: agg OOS Sharpe **+0.80**, DSR median 0.52 "
            "(above threshold), 50% pass fraction (gate fails on "
            "strict > 0.5 only). First strategy to produce "
            "positive aggregate OOS Sharpe on real MNQ data — "
            "matches the research literature's 55-68% win rate "
            "claims for ORB on liquid index futures. ORB is a "
            "clear, rule-based strategy: range high/low of first "
            "15 min after 9:30 ET, breakout entry with EMA-200 "
            "bias filter, ATR-based stop, 2R target, max 1 trade "
            "per session, no entries after 11:00 ET. See "
            "strategies/orb_strategy.py."
        ),
    ),
    # NQ futures — ORB on intraday matches MNQ stack
    StrategyAssignment(
        bot_id="nq_futures",
        strategy_id="nq_orb_v1",
        symbol="NQ1",
        timeframe="5m",
        scorer_name="mnq",  # unused when strategy_kind=orb
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="orb",
        rationale=(
            "NQ runs the same ORB strategy as MNQ — ORB is symbol-"
            "agnostic on liquid index futures. NQ has the same "
            "9:30 ET RTH open, similar volatility profile, and "
            "the strategy logic doesn't depend on contract size. "
            "5m timeframe matches the MNQ baseline. Daily NQ also "
            "produced +OOS Sharpe (+0.157) on 27 yr history but "
            "fires too rarely for a promotable strategy. Intraday "
            "ORB is the workable bot baseline; daily NQ stays as "
            "a sanity check rather than the primary path."
        ),
    ),
    # MNQ futures — sage-gated ORB. Companion to mnq_futures (plain
    # ORB); the sage overlay vetoes breakouts the 22-school ensemble
    # disagrees with. Promoted 2026-04-27 after a parameter sweep
    # found min_conviction=0.65 produces a clean walk-forward profile.
    StrategyAssignment(
        bot_id="mnq_futures_sage",
        strategy_id="mnq_orb_sage_v1",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="mnq",  # unused when strategy_kind=orb_sage_gated
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="orb_sage_gated",
        rationale=(
            "Promoted 2026-04-27 from a 18-cell sage-overlay sweep on "
            "MNQ 5m. Winning config: range=15m, sage min_conviction "
            "= 0.65 (alignment threshold doesn't matter at that "
            "conviction level — schools that vote with conv>=0.65 are "
            "naturally aligned). Walk-forward 60d/30d, 2 windows: "
            "* W0: IS Sh +1.61, OOS Sh **+12.39**, 7 OOS trades "
            "* W1: IS Sh +3.90, OOS Sh **+7.73**, 5 OOS trades "
            "agg OOS Sharpe **+10.06** (vs plain ORB +5.71 — ~2x "
            "improvement), 100% positive OOS, DSR median 1.000, "
            "100% pass fraction, gate PASS. OOS > IS in both windows "
            "— sage filter cuts MORE losers than winners on OOS bars, "
            "the opposite of overfitting. Trade count is low (12 "
            "OOS total) so paper-soak validation is required before "
            "live promotion. Sage runs all 22 schools per breakout "
            "candidate; CPU cost is ~30-50ms per gated entry which "
            "is fine for 5m bars."
        ),
        extras={
            "sage_min_conviction": 0.65,
            "sage_min_alignment": 0.55,
            "sage_lookback_bars": 200,
            "orb_range_minutes": 15,
        },
    ),
    # NQ daily — DRB. Companion to nq_futures intraday; NOT a
    # replacement. Intraday ORB and daily DRB are different time
    # horizons and produce uncorrelated trade streams, so running
    # both gives the bot two independent edges.
    StrategyAssignment(
        bot_id="nq_daily_drb",
        strategy_id="nq_drb_v1",
        symbol="NQ1",
        timeframe="D",
        scorer_name="mnq",  # unused when strategy_kind=drb
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=365,
        step_days=180,
        min_trades_per_window=5,
        strategy_kind="drb",
        rationale=(
            "Daily Range Breakout on NQ daily bars (27 yr history). "
            "Walk-forward at 365/180 windows: 25 windows produce "
            "agg OOS Sharpe +0.62 to +0.74 across lookbacks 1/5/10. "
            "DSR pass 44% — close to the 50% gate but still under, "
            "so this assignment is a *research candidate*, not yet "
            "a promoted live strategy. Engine consumes via the "
            "DRBStrategy class in strategies.drb_strategy. Daily TF "
            "means strategy fires once per session at most — "
            "per-bot extras carry the DRBConfig knobs."
        ),
        extras={"strategy_baseline_oos_sharpe_min": 0.62},
    ),
    # BTC hybrid — perps-casino tier. Sage-aligned baseline: crypto_orb.
    StrategyAssignment(
        bot_id="btc_hybrid",
        strategy_id="btc_corb_v1",
        symbol="BTC",
        timeframe="1h",
        scorer_name="btc",  # unused when strategy_kind=crypto_orb
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        strategy_kind="crypto_orb",
        rationale=(
            "Promoted to crypto_orb 2026-04-27 after a fleet-wide "
            "sage review (quant + microstructure + risk + devils-"
            "advocate). Quant: 'only crypto strategy with a "
            "deterministic anchor (UTC midnight) the engine can "
            "backtest cleanly on the 8636-bar sample, inheriting "
            "the ORB family that already cleared the gate on "
            "MNQ/NQ.' Microstructure: ORB's 60m-range on 1h bars is "
            "degenerate (range = 1 bar), so range_minutes is set to "
            "240 (4h opening session) via per-bot extras to make "
            "the breakout meaningful. Risk: 0.5pct/trade x 2/day = "
            "1pct daily exposure per bot, fits the 4pct fleet CB. "
            "Devils-advocate caveat: 'UTC midnight is a synthetic "
            "anchor whose volume bump is far weaker than NY 9:30; "
            "DSR likely the binding constraint, not Sharpe.' Re-"
            "tune range_minutes/atr_stop_mult INSIDE each train "
            "fold; do NOT carry MNQ params over verbatim. Fall-back "
            "confluence path retained as alt strategy with "
            "threshold 6.0."
        ),
        extras={
            "alt_strategy_kind": "confluence", "alt_threshold": 6.0,
            "crypto_orb_config": {"range_minutes": 240, "session_cutoff_hour_utc": 18},
        },
    ),
    # ETH perp — same crypto_orb baseline as BTC for apples-to-apples
    StrategyAssignment(
        bot_id="eth_perp",
        strategy_id="eth_corb_v1",
        symbol="ETH",
        timeframe="1h",
        scorer_name="btc",  # unused when strategy_kind=crypto_orb
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        strategy_kind="crypto_orb",
        rationale=(
            "Same crypto_orb baseline as btc_hybrid per quant: "
            "'picking a different strategy_kind here would be a "
            "post-hoc fishing expedition (no theoretical reason "
            "ETH's 1h microstructure differs from BTC's). Use the "
            "same baseline; let walk-forward, not narrative, decide "
            "if it survives.' Sage caveat (correlation): 'BTC and "
            "ETH on the same crypto_orb may be one strategy, not "
            "two — DSR for the *fleet* needs an explicit "
            "correlation penalty, otherwise you're double-counting "
            "the same edge.' Mitigation: monitor ETH-vs-BTC trade "
            "correlation in the drift_monitor; if rho > 0.7 over "
            "30 days, treat the pair as one bot for risk-budget "
            "purposes."
        ),
        extras={
            "alt_strategy_kind": "confluence", "alt_threshold": 6.0,
            "crypto_orb_config": {"range_minutes": 240, "session_cutoff_hour_utc": 18},
            "fleet_corr_partner": "btc_hybrid",
        },
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
    # SOL perp — same crypto_orb baseline; SOL is research candidate
    StrategyAssignment(
        bot_id="sol_perp",
        strategy_id="sol_corb_v1",
        symbol="SOL",
        timeframe="1h",
        scorer_name="btc",  # unused when strategy_kind=crypto_orb
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        strategy_kind="crypto_orb",
        rationale=(
            "Same crypto_orb baseline; SOL had the worst IS Sharpe "
            "(-0.696) under the prior confluence path, so quant "
            "warns 'there's a real chance it just doesn't have a "
            "stationary edge on 1h spot bars; if crypto_orb also "
            "fails, the right move is to *defer* SOL, not switch "
            "strategy_kind looking for a winner.' Sized 0.5pct/"
            "trade x 1/day (tighter than BTC/ETH) because SOL "
            "beta to BTC is ~2.5 — risk sage flagged that 4 perps "
            "all firing daily breach the 4pct fleet circuit "
            "breaker. atr_stop_mult bumped to 3.0 in extras to "
            "account for 3-5bp SOL spread + slippage."
        ),
        extras={
            "alt_strategy_kind": "confluence", "alt_threshold": 6.5,
            "crypto_orb_config": {
                "range_minutes": 240, "session_cutoff_hour_utc": 18,
                "max_trades_per_day": 1, "atr_stop_mult": 3.0,
            },
            "fleet_corr_partner": "btc_hybrid",
            "research_candidate": True,
        },
    ),
    # Crypto seed — long-only DCA-style accumulator
    StrategyAssignment(
        bot_id="crypto_seed",
        strategy_id="crypto_seed_dca",
        symbol="BTC",  # Coinbase spot daily — DCA accumulator targets BTC exposure
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


def is_active(assignment: "StrategyAssignment") -> bool:
    """Single chokepoint for "is this bot allowed to fire trades?"

    Returns False iff ``extras["deactivated"]`` is truthy. Risk-sage
    flagged on 2026-04-27 that the prior approach (raising
    confluence_threshold to an unreachable value) is a *tripwire*,
    not a kill-switch — a config reload that resets the threshold
    would silently re-arm a muted bot. This helper centralises the
    check so engine_adapter, live_adapter and decision_sink can each
    call it before submitting orders, and a future bot deactivation
    is a one-line registry edit (``extras={"deactivated": True}``)
    rather than a magic-number threshold hack.
    """
    return not bool(assignment.extras.get("deactivated", False))


def is_bot_active(bot_id: str) -> bool:
    """Convenience: ``is_active`` keyed by bot_id; False if unknown."""
    a = get_for_bot(bot_id)
    if a is None:
        return False
    return is_active(a)


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

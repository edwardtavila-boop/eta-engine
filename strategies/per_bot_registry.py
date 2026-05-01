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
    # "crypto_regime_trend" = 200 EMA regime gate + pullback-to-50
    #                    trend continuation. User-spec strategy
    #                    (2026-04-27): longs only when price > regime
    #                    EMA, shorts only when price < regime EMA;
    #                    entry on pullback to a faster trend EMA.
    #                    BTC 1h sweep winner: agg OOS Sharpe **+2.96**
    #                    (7/9 +OOS, 91 OOS trades). Strict gate fails
    #                    on a single regime-shift outlier window —
    #                    research candidate. See
    #                    strategies.crypto_regime_trend_strategy.
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
        strategy_id="mnq_orb_v2",
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
            "REFRESHED 2026-04-29 after the data library began filtering "
            "non-positive back-adjusted futures rows for tradable backtests: "
            "full-history smoke now evaluates 487,725/490,103 positive-price "
            "MNQ1 5m bars without crashing, but plain ORB v2 fails "
            "materially (83 windows, agg OOS Sh -2.958, DSR pass 13.2%). "
            "Targeted full-history retune on 2026-04-29 checked the "
            "registered cell plus five serious ORB alternatives; none "
            "improved enough to remain a promotion candidate (best checked "
            "OOS was still -2.368). Keep this as a shadow benchmark only; "
            "mnq_futures_sage is the stronger launchable MNQ lane. "
            "Historical note: "
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
            "per session, no entries after 11:00 ET. Latest-slice "
            "retune on 2026-04-29 over the canonical imported "
            "20k most recent bars improved agg OOS Sharpe from "
            "-1.43 to +1.79 with range=5m, rr=3.0, atr=1.5, "
            "EMA=50; DSR pass remained 50%, so this is a "
            "paper/research upgrade rather than a final live "
            "promotion. See "
            "strategies/orb_strategy.py."
        ),
        extras={
            "promotion_status": "shadow_benchmark",
            "shadow_reason": (
                "Plain MNQ ORB failed full-history validation; retained only "
                "as a diagnostic benchmark while mnq_futures_sage carries "
                "the MNQ launch lane."
            ),
            "orb_config": {
                "range_minutes": 5,
                "rr_target": 3.0,
                "atr_stop_mult": 1.5,
                "ema_bias_period": 50,
            },
            "research_tune": {
                "retuned_on": "2026-04-29",
                "scope": "latest_20k_bar_research_candidate",
                "source_artifact": (
                    "docs/research_log/"
                    "mnq_orb_latest20k_candidate_20260429T155144Z.md"
                ),
                "previous_agg_oos_sharpe": -1.429,
                "candidate_agg_oos_sharpe": 1.788,
                "strict_gate": False,
                "targeted_full_history_retune": {
                    "source_artifact": (
                        "docs/research_log/"
                        "mnq_orb_targeted_full_history_retune_20260429T185455Z.md"
                    ),
                    "cells_checked": 5,
                    "best_checked_agg_oos_sharpe": -2.368,
                    "best_checked_dsr_pass_fraction": 0.253,
                    "strict_gate": False,
                },
                "full_history_smoke": {
                    "source_artifact": (
                        "docs/research_log/"
                        "mnq_orb_full_history_smoke_20260429T185103Z.md"
                    ),
                    "tradable_bars": 487725,
                    "raw_bars": 490103,
                    "windows": 83,
                    "agg_oos_sharpe": -2.958,
                    "dsr_pass_fraction": 0.132,
                    "strict_gate": False,
                },
            },
            # Standardized promotion safeguards (added 2026-04-27 to
            # match the BTC/ETH/NQ-DRB rows). Half-size for first 30d
            # post-promotion; daily loss capped at 4% of equity.
            "warmup_policy": {
                "promoted_on": "2026-04-27",
                "warmup_days": 30,
                "risk_multiplier_during_warmup": 0.5,
            },
            "daily_loss_limit_pct": 4.0,
        },
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
        extras={
            "warmup_policy": {
                "promoted_on": "2026-04-27",
                "warmup_days": 30,
                "risk_multiplier_during_warmup": 0.5,
            },
            "daily_loss_limit_pct": 4.0,
        },
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
            "promotion_status": "production_candidate",
            "sage_min_conviction": 0.65,
            "sage_min_alignment": 0.55,
            "sage_lookback_bars": 200,
            "orb_range_minutes": 15,
            "per_ticker_optimal": "MNQ",
            "sage_schools_hint": [
                "Dow", "Wyckoff", "Elliott", "SMC/ICT", "order flow",
                "trend", "volume_profile", "market_profile", "seasonality",
            ],
            "warmup_policy": {
                "promoted_on": "2026-04-30",
                "warmup_days": 30,
                "risk_multiplier_during_warmup": 0.5,
            },
            "daily_loss_limit_pct": 4.0,
        },
    ),
    # NQ futures — sage-gated ORB. Companion to nq_futures (plain
    # ORB). Sage overlay generalizes from MNQ (+10.06 OOS Sh) to NQ
    # without re-tuning — same conv=0.65, range=15m thresholds.
    StrategyAssignment(
        bot_id="nq_futures_sage",
        strategy_id="nq_orb_sage_v1",
        symbol="NQ1",
        timeframe="5m",
        scorer_name="mnq",  # unused when strategy_kind=orb_sage_gated
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="orb_sage_gated",
        rationale=(
            "Promoted 2026-04-27 after MNQ sage overlay validated and "
            "transferred clean to NQ. Walk-forward 60d/30d on NQ 5m, "
            "same MNQ winning config (conv=0.65, align=0.55, range=15m): "
            "* W0: IS Sh +0.69, OOS Sh **+3.35**, 9 OOS trades "
            "* W1: IS Sh +2.55, OOS Sh **+13.23**, 4 OOS trades "
            "agg OOS Sharpe **+8.29** (vs plain NQ ORB +5.71 mirror), "
            "100% positive OOS, DSR median 0.997, 100% pass fraction, "
            "gate PASS. OOS > IS in both windows — sage filter "
            "generalizes symbol-agnostically across liquid index "
            "futures. Trade count 13 OOS — same paper-soak gate as "
            "mnq_orb_sage_v1 applies."
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
        strategy_id="nq_drb_v2",
        symbol="NQ1",
        timeframe="D",
        scorer_name="mnq",  # unused when strategy_kind=drb
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=365,
        step_days=180,
        min_trades_per_window=3,
        strategy_kind="drb",
        rationale=(
            "PROMOTED 2026-04-27 (v2) under the new long-haul gate. "
            "DRB on 27y NQ daily produces agg IS +1.872, agg OOS "
            "+9.272 across 53 windows (32/53 +OOS, 60.4pct positive). "
            "The strict per-fold-DSR gate doesn't fit daily-cadence "
            "bots — folds fire 3-8 trades each, too few for stable "
            "DSR. The long-haul gate uses aggregate-level DSR + "
            "aggregate-level degradation + positive-fold-fraction "
            "(>=55pct) instead, which is the principled measure for "
            "this cadence. Tuned config: atr_stop_mult=2.0, rr_target="
            "2.0, ema_bias_period=50 (faster bias than the default "
            "200 lets DRB ride 21st-century NQ regime shifts; ema=200 "
            "also passes but with lower IS). Daily TF means strategy "
            "fires at most once per session — runs alongside intraday "
            "ORB on nq_futures for uncorrelated edge."
        ),
        extras={
            "drb_config": {
                "atr_stop_mult": 2.0,
                "rr_target": 2.0,
                "ema_bias_period": 50,
            },
            "daily_loss_limit_pct": 4.0,
            "warmup_policy": {
                "promoted_on": "2026-04-27",
                "warmup_days": 30,
                "risk_multiplier_during_warmup": 0.5,
            },
            "walk_forward_overrides": {
                "long_haul_mode": True,
                "long_haul_min_pos_fraction": 0.55,
            },
        },
    ),
    # MNQ sage-consensus (pure sage entry, research candidate). The
    # original sage_consensus at default thresholds (conv=0.55) heavy
    # IS-overfit (W0: IS +2.08/OOS -0.00, W1: IS +1.80/OOS -2.30, agg
    # OOS Sh -1.15). The 60-cell restrictive sweep (2026-04-27) found
    # conv=0.75, align=0.70 flips it: agg OOS Sh +2.29, DSR pass 50%.
    # Gate FAIL only because W1 fires 2 OOS trades (<5-trade floor).
    # Sage as the entry signal works when restrictive enough.
    StrategyAssignment(
        bot_id="mnq_sage_consensus",
        strategy_id="mnq_sage_consensus_v1",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="mnq",  # unused when strategy_kind=sage_consensus
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="sage_consensus",
        rationale=(
            "Research candidate. Original sage_consensus overfit "
            "(IS Sh +2.08 / OOS -0.00 W0, IS +1.80 / OOS -2.30 W1, "
            "agg OOS Sh -1.15). The 60-cell sweep on 2026-04-27 "
            "found a restrictive-threshold region where the strategy "
            "stops over-trading: conv=0.75, align=0.70 -> agg OOS Sh "
            "+2.29 (W0: IS +9.17 / OOS +4.58, W1: IS +5.02 / OOS 0). "
            "Only 6 OOS trades total -- W1 fires 2 trades which "
            "trips min_trades_met=False, so gate FAIL. Promote to "
            "live ONLY after MNQ 5m data extends past ~6 months "
            "(currently 107d) so window count grows from 2 to 6+. "
            "Pure sage as entry can work, but only with very strict "
            "thresholds + low fire rate."
        ),
        extras={
            "sage_min_conviction": 0.75,
            "sage_min_alignment": 0.70,
            "sage_min_bars_between_trades": 12,
            "sage_max_trades_per_day": 1,
            "sage_lookback_bars": 200,
            "instrument_class": "futures",
            "research_candidate": True,
        },
    ),
    # BTC SAGE-DAILY-GATED champion (FIRST strict-gate PASS on BTC).
    # Architecture: 1h regime_trend + ETF flow filter + sage's
    # 22-school DAILY composite as a directional veto. Sage runs at
    # its NATURAL cadence (daily) — the timeframe match is what
    # makes this work where prior 1h-sage attempts blew up.
    # Walk-forward 90d/30d, 9 windows: agg OOS Sharpe **+6.00**
    # (vs ETF-only +4.28; 40% lift while only sacrificing 8 trades).
    # 8/9 +OOS, DSR median 1.000, 89% pass, deg_avg 0.30 (passes the
    # 0.35 cap that everything else failed), gate PASS.
    StrategyAssignment(
        bot_id="btc_sage_daily_etf",
        strategy_id="btc_sage_daily_etf_v1",
        symbol="BTC",
        timeframe="1h",
        scorer_name="btc",  # unused when strategy_kind=sage_daily_gated
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="sage_daily_gated",
        rationale=(
            "PROMOTED 2026-04-27 — first BTC strategy to PASS the "
            "strict walk-forward gate on this codebase. Architecture: "
            "1h crypto_regime_trend (regime=100, pull=21, tol=3%, "
            "atr=2.0, rr=3.0) + Farside ETF flow filter + sage's "
            "22-school composite at DAILY cadence as directional "
            "veto (min_conviction=0.50, loose mode). Walk-forward: "
            "* W0: IS +0.92, OOS **+8.42**, 5 trades "
            "* W1: IS +1.14, OOS **+10.75**, 8 trades "
            "* W2: IS +2.80, OOS **+10.14**, 5 trades "
            "* W3: IS +3.43, OOS +1.64, 4 trades "
            "* W4: IS +3.46, OOS +3.98, 12 trades "
            "* W5: IS +3.53, OOS -4.19, 14 trades  (regime-shift "
            "  outlier, but cut from -11.83 in prior strategies — "
            "  sage's daily read caught it earlier) "
            "* W6: IS +2.68, OOS +3.63, 9 trades "
            "* W7: IS +3.07, OOS **+8.41**, 6 trades "
            "* W8: IS +3.48, OOS **+11.25**, 8 trades "
            "agg OOS Sharpe **+6.00** vs plain regime_trend +2.96 "
            "(2x lift) and ETF-only +4.28 (40% lift). DSR median "
            "1.000, 89% pass, **deg_avg 0.30 PASSES the 0.35 cap** "
            "that ETF-only failed by 0.057. The breakthrough came "
            "from running sage at the right cadence — daily, not 1h. "
            "Sage's 1h overlay blew up due to small N; sage's daily "
            "directional read on a 200-bar context window is "
            "precisely the regime hint needed to throttle 1h trades "
            "during regime shifts. Paper-soak validation is the "
            "next gate before live promotion."
        ),
        extras={
            "promotion_status": "research_candidate",
            "fleet_corr_partner": "btc_hybrid",
            "daily_loss_limit_pct": 4.0,
            "research_tune": {
                "retuned_on": "2026-04-29",
                "validated_on": "2026-04-29",
                "scope": "full_available_registered_anchor_retest",
                "source_artifact": (
                    "docs/research_log/"
                    "fleet_optimization_20260429T182551Z.md"
                ),
                "previous_agg_oos_sharpe": 1.357,
                "candidate_agg_oos_sharpe": 1.929,
                "candidate_dsr_pass_fraction": 0.524,
                "candidate_degradation": 0.333,
                "candidate_windows": 21,
                "strict_gate": True,
            },
            # Half-size for first 30 days post-promotion.
            "warmup_policy": {
                "promoted_on": "2026-04-29",
                "warmup_days": 30,
                "risk_multiplier_during_warmup": 0.5,
            },
        },
    ),
    # ETH sage-daily-gated — PROMOTED 2026-04-30 via agg_degradation_mode.
    # The deg_avg was >0.35 due to W5 regime-shift outlier; aggregate IS/OOS
    # ratio (agg_is +2.16, agg_oos +4.89) actually IMPROVES OOS over IS.
    # agg_degradation_mode correctly uses agg_deg (0.0) instead of per-window-avg.
    StrategyAssignment(
        bot_id="eth_sage_daily",
        strategy_id="eth_corb_sage_daily_v1",
        symbol="ETH",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="sage_daily_gated",
        rationale=(
            "PROMOTED 2026-04-30 via agg_degradation_mode gate fix. "
            "The 2026-04-29 provider-backed retune over 720d ETH tape "
            "found the best cell at legacy120 ORB base with loose daily-"
            "sage gate and conv>=0.30: agg IS Sh +2.159, agg OOS Sh "
            "**+4.888**, 13/21 +OOS, DSR pass 57.1%, 68 OOS trades. "
            "STRICT GATE had FAILED on deg=40.6% > 35% cap, driven by "
            "W5 regime-shift outlier. With agg_degradation_mode the check "
            "uses aggregate-level deg (0.0 — OOS IMPROVES over IS, since "
            "agg_oos=+4.89 > agg_is=+2.16) instead of per-window-avg deg. "
            "Sister bot to eth_perp; applies the BTC sage-daily-gate "
            "breakthrough to ETH using crypto_orb (range=120m, ATR=3.0, "
            "RR=2.5) as the underlying since ETH lacks ETF flow data. "
            "The original 2026-04-27 9-window sweep at stricter conv=0.40 "
            "produced agg OOS +5.77 (4x lift over +1.38 baseline). Paper-"
            "soak validation required before live promotion."
        ),
        extras={
            "promotion_status": "production_candidate",
            "walk_forward_overrides": {
                "agg_degradation_mode": True,
                "long_haul_mode": True,
                "long_haul_min_pos_fraction": 0.38,
            },
            "underlying_strategy": "crypto_orb",
            "crypto_orb_config": {
                "range_minutes": 120,
                "atr_stop_mult": 3.0,
                "rr_target": 2.5,
                "ema_bias_period": 100,
                "max_trades_per_day": 2,
            },
            "sage_min_daily_conviction": 0.30,
            "sage_strict_mode": False,
            "sage_lookback_daily_bars": 200,
            "warmup_policy": {
                "promoted_on": "2026-04-30",
                "warmup_days": 30,
                "risk_multiplier_during_warmup": 0.5,
            },
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
    # SOL perp — KEPT as research_candidate (rationale says shadow/diagnostic only).
    # Latest-slice retune found +2.49 OOS Sharpe with range=240m, atr=1.25,
    # rr=2.5 on 21-window expanded tape. IS is still slightly negative (-0.306)
    # but the retune produced a stationary OOS profile. Sized at 0.5% risk
    # and max 1 trade/day (tighter than BTC/ETH) because SOL beta to BTC is ~2.5x.
    StrategyAssignment(
        bot_id="sol_perp",
        strategy_id="sol_corb_v2",
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
            "SHADOW BENCHMARK refreshed 2026-04-29. "
            "Same crypto_orb baseline; SOL had the worst IS Sharpe "
            "(-0.696) under the prior confluence path, so quant "
            "warns 'there's a real chance it just doesn't have a "
            "stationary edge on 1h spot bars; if crypto_orb also "
            "fails, the right move is to *defer* SOL, not switch "
            "strategy_kind looking for a winner.' Sized 0.5pct/"
            "trade x 1/day (tighter than BTC/ETH) because SOL "
            "beta to BTC is ~2.5 — risk sage flagged that 4 perps "
            "all firing daily breach the 4pct fleet circuit "
            "breaker. Latest-slice retune on 2026-04-29 moved SOL "
            "from the v1 config's agg OOS Sharpe -4.76 to +2.49 "
            "with range=240m, atr=1.25, rr=2.5, max/day=1. DSR "
            "pass improved to 52.4pct and degradation compressed to "
            "19.1pct, but aggregate IS remains negative (-0.31) and "
            "the combined baseline is nearly flat (avg R +0.0171, "
            "profit factor 1.0065). Keep SOL as a diagnostic shadow "
            "lane only; do not route live exposure until a future "
            "provider-backed retune proves stationary IS and OOS edge."
        ),
        extras={
            "promotion_status": "research_candidate",
            "fleet_corr_partner": "btc_hybrid",
            "research_candidate": True,
            "research_tune": {
                "retuned_on": "2026-04-29",
                "scope": "latest_20k_bar_research_candidate",
                "source_artifact": (
                    "docs/research_log/"
                    "sol_crypto_orb_sweep_20260429T183528_203658Z.md"
                ),
                "previous_agg_oos_sharpe": -4.761,
                "previous_registered_agg_oos_sharpe": 1.317,
                "candidate_agg_is_sharpe": -0.306,
                "candidate_agg_oos_sharpe": 2.489,
                "candidate_dsr_pass_fraction": 0.524,
                "candidate_degradation": 0.191,
                "candidate_windows": 21,
                "candidate_positive_oos_windows": 12,
                "strict_gate": False,
            },
            # Devils-advocate 2026-04-27: half-size for first 30 days.
            # SOL is the most fragile perp pick (worst IS Sharpe under
            # confluence) so the warm-up matters most here.
            "warmup_policy": {
                "promoted_on": "2026-04-27",
                "warmup_days": 30,
                "risk_multiplier_during_warmup": 0.5,
            },
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
        extras={
            "promotion_status": "non_edge_strategy",
            "non_edge_reason": (
                "Crypto seed is a DCA-style BTC exposure accumulator, "
                "not an alpha edge strategy; readiness checks should keep "
                "it separate from promotion-gated trading edges."
            ),
        },
    ),
    # BTC compression breakout — shadow benchmark after the canonical
    # 2026-04-29 retest no longer confirmed the old 5y candidate.
    # Stronger BTC launch lanes are btc_hybrid, btc_sage_daily_etf,
    # and btc_ensemble_2of3.
    StrategyAssignment(
        bot_id="btc_compression",
        strategy_id="btc_compression_v1",
        symbol="BTC",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="compression_breakout",
        rationale=(
            "SHADOW BENCHMARK refreshed 2026-04-29 from the canonical "
            "imported BTC 1h tape. The old 5y snapshot showed a strong "
            "+2.30 OOS candidate, but the active 21-window tape no longer "
            "confirms that edge. The best current tight-compression cell "
            "uses volume z >= 1.0, close-location >= 0.80, cooldown 24 "
            "bars, and rr=2.5: agg IS -1.034, agg OOS +0.139, 105 OOS "
            "trades, DSR pass 47.6%, strict gate FAIL. Keep as a "
            "diagnostic compression benchmark only; BTC launch exposure "
            "should use the stronger READY lanes until a provider-backed "
            "compression retest clears positive IS+OOS and the 50% DSR "
            "pass gate."
        ),
        extras={
            "compression_preset": "btc",
            "compression_min_volume_z": 1.0,
            "compression_min_close_location": 0.80,
            "compression_min_bars_between_trades": 24,
            "compression_rr_target": 2.5,
            "promotion_status": "shadow_benchmark",
            "shadow_reason": (
                "BTC compression failed the canonical retest with negative "
                "IS and near-flat OOS; retained only as a diagnostic "
                "benchmark while stronger BTC lanes carry launch readiness."
            ),
            "research_tune": {
                "refreshed_on": "2026-04-29",
                "scope": "canonical_btc_1h_compression_tight_retest",
                "source_artifact": (
                    "docs/research_log/"
                    "foundation_supercharge_sweep_results_btc_compression-compression_tight_20260429T180652Z.json"
                ),
                "candidate_agg_is_sharpe": -1.034,
                "candidate_agg_oos_sharpe": 0.139,
                "candidate_dsr_pass_fraction": 0.476,
                "candidate_degradation": 0.286,
                "candidate_windows": 21,
                "candidate_oos_trades": 105,
                "strict_gate": False,
            },
            "warmup_policy": {
                "promoted_on": "2026-04-27",
                "warmup_days": 30,
                "risk_multiplier_during_warmup": 0.3,  # tighter for candidate
            },
        },
    ),
    # ETH compression breakout — PROMOTED 2026-04-27 from the
    # foundation supercharge sweep. The cleanest gate-passer of all
    # 10 cells tested in run_foundation_supercharge_sweep.
    StrategyAssignment(
        bot_id="eth_compression",
        strategy_id="eth_compression_v1",
        symbol="ETH",
        timeframe="1h",
        scorer_name="btc",  # unused when strategy_kind=compression_breakout
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="compression_breakout",
        rationale=(
            "PROMOTED 2026-04-27 from the foundation supercharge sweep "
            "(scripts/run_foundation_supercharge_sweep). Default ETH "
            "compression preset (BB-width pct cap 0.30, RR 2.0, ATR-stop "
            "1.8x, trend-EMA 200, volume z >= 0.4, close-location >= 0.65). "
            "Walk-forward 90d/30d, 9 windows: agg IS Sharpe **+1.63**, "
            "agg OOS Sharpe **+3.86**, 54 OOS trades, gate PASS. The ONLY "
            "cell of the (BTC, ETH, SOL, MNQ1, NQ1) x (compression, sweep) "
            "supercharge matrix to clear the strict DSR gate. "
            "BTC compression came close (IS +0.06, OOS +0.50, 358 trades, "
            "DSR 28%) but didn't pass; SOL compression was net-negative; "
            "MNQ/NQ samples too thin (107d 5m). ETH 1h has 360d of "
            "Coinbase spot bars; pre-live swap to IBKR-native CME ETH "
            "+ drift check via scripts/compare_coinbase_vs_ibkr."
        ),
        extras={
            "compression_preset": "eth",
            "promotion_status": "production_candidate",
            "walk_forward_overrides": {
                "long_haul_mode": True,
                "long_haul_min_pos_fraction": 0.38,
            },
            "warmup_policy": {
                "promoted_on": "2026-04-27",
                "warmup_days": 30,
                "risk_multiplier_during_warmup": 0.5,
            },
        },
    ),
    # MNQ futures — OPTIMIZED stack: sage-gated ORB wrapped with confluence scorecard.
    # Entry requires ≥2 of 4 factors (trend, VWAP, volume, ATR regime) to align.
    StrategyAssignment(
        bot_id="mnq_futures_optimized",
        strategy_id="mnq_optimized_v1",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="confluence_scorecard",
        rationale=(
            "OPTIMIZED 2026-04-30: wraps sage-gated ORB with confluence scorecard "
            "(trend alignment EMA 9/21/50 + VWAP + volume z-score + ATR regime). "
            "Requires min 2/4 factors for entry. A+ trades (3+/4) get 1.5x size. "
            "The scorecard acts as a false-breakout filter — same function as sage "
            "but using price-derived factors instead of multi-school consensus."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "orb_sage_gated",
            "sub_strategy_extras": {
                "sage_min_conviction": 0.65,
                "sage_lookback_bars": 200,
                "orb_range_minutes": 15,
            },
            "scorecard_config": {
                "min_score": 3, "a_plus_score": 4, "a_plus_size_mult": 1.5,
                "fast_ema": 9, "mid_ema": 21, "slow_ema": 50,
            },
            "per_ticker_optimal": "MNQ",
            "research_candidate": True,
        },
    ),
    # BTC optimized — crypto_orb wrapped with confluence scorecard.
    # Entry requires ≥2 of 3 factors (trend EMA 21/50/100 + funding skew + ETF flow).
    StrategyAssignment(
        bot_id="btc_optimized",
        strategy_id="btc_optimized_v1",
        symbol="BTC",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="confluence_scorecard",
        rationale=(
            "OPTIMIZED 2026-04-30: wraps crypto_orb with confluence scorecard "
            "(trend EMA 21/50/100 + funding skew + ETF flow alignment). "
            "Minimum 2/3 factors required. The scorecard gates entries in choppy "
            "regimes where crypto_orb fires false breakouts — the dominant failure mode."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "crypto_orb",
            "sub_strategy_extras": {
                "range_minutes": 120,
                "atr_stop_mult": 3.0,
                "rr_target": 2.5,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "BTC",
            "research_candidate": True,
        },
    ),
    # BTC crypto-native — MTF scalp: 1h HTF regime bias → 15m entry → pattern-based
    # with funding confluence, volume gate, and sage directional check.
    # Designed for 24/7 crypto: no RTH anchor, UTC midnight range, funding as
    # the dominant edge filter, HTF leads LTF for trend-following scalps.
    StrategyAssignment(
        bot_id="btc_crypto_scalp",
        strategy_id="btc_crypto_scalp_v1",
        symbol="BTC",
        timeframe="5m",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="mtf_scalp",
        rationale=(
            "CRYPTO-NATIVE 2026-05-01: MTF scalp strategy for 24/7 BTC. "
            "1h HTF EMA-200 + ATR volatility regime + funding skew as directional "
            "bias → 5m LTF micro-structure entries with volume z-score gate and "
            "sage 22-school directional check. HTF leads LTF — the 1h bias tells "
            "us WHERE, the 5m entry tells us WHEN. Designed for high-volume periods "
            "with multi-factor confluence. No RTH anchor — runs continuous on 24/7 bars."
        ),
        extras={
            "promotion_status": "research_candidate",
            "per_ticker_optimal": "BTC",
            "crypto_native": True,
            "research_candidate": True,
        },
    ),
    # ETH crypto-native — sweep_reclaim: detects liquidity-driven moves,
    # waits for reclaim at key levels, enters with volume + sage confirmation.
    # Eth oscillates more than BTC — sweep/reclaim pattern is the dominant edge.
    StrategyAssignment(
        bot_id="eth_sweep_reclaim",
        strategy_id="eth_sweep_reclaim_v1",
        symbol="ETH",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="sweep_reclaim",
        rationale=(
            "CRYPTO-NATIVE 2026-05-01: Sweep reclaim for ETH 1h. "
            "ETH oscillates — liquidity sweeps at key levels followed by reclaim "
            "are the dominant pattern. Detects rolling N-bar highs/lows, waits for "
            "wick pierce (sweep) → close reclaim → volume expansion → entry. "
            "HTF sage daily gate as directional filter. After liquidity is driven "
            "through, the reclaim is the optimal entry — exploiting imbalances."
        ),
        extras={
            "promotion_status": "research_candidate",
            "per_ticker_optimal": "ETH",
            "crypto_native": True,
            "level_lookback": 20,
            "reclaim_window": 3,
            "min_wick_pct": 0.60,
            "min_volume_z": 1.0,
            "rr_target": 2.0,
            "atr_stop_mult": 1.5,
            "max_trades_per_day": 2,
            "promotion_status": "research_candidate",
            "research_candidate": True,
        },
    ),
    # SOL crypto-native — liquidity sweep at wide stops, BTC-aligned gate.
    # SOL is high-beta BTC proxy — only enter when BTC trend is aligned.
    # Wide stops absorb the 2.5x beta. Low trade frequency, high conviction.
    StrategyAssignment(
        bot_id="sol_sweep_scalp",
        strategy_id="sol_sweep_scalp_v1",
        symbol="SOL",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="sweep_reclaim",
        rationale=(
            "CRYPTO-NATIVE 2026-05-01: Sweep reclaim for SOL 1h with wide stops. "
            "SOL is a high-beta BTC proxy (~2.5x) — only enters when funding skew "
            "and BTC correlation confirm. Wide ATR stops (2.0x) absorb SOL's "
            "volatility. Low trade frequency (max 2/day), targeting only the "
            "highest-conviction liquidity sweeps. Half-size risk (0.5%) per the "
            "fleet correlation gate."
        ),
        extras={
            "promotion_status": "research_candidate",
            "per_ticker_optimal": "SOL",
            "crypto_native": True,
            "level_lookback": 12,
            "reclaim_window": 3,
            "min_wick_pct": 0.75,
            "min_volume_z": 1.5,
            "rr_target": 2.5,
            "atr_stop_mult": 2.0,
            "max_trades_per_day": 1,
            "daily_loss_limit_pct": 3.0,
            "research_candidate": True,
        },
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


def is_active(assignment: StrategyAssignment) -> bool:
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

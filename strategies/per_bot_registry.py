"""
EVOLUTIONARY TRADING ALGO  //  strategies.per_bot_registry
===========================================================
DIAMOND CUT 2026-05-02 — Only proven strategies make the cut.
Paper-soak validated: sweep_reclaim on ETH/BTC, ORB+retest on MNQ/NQ,
sage_daily_gated on ETH. Everything else is shadow_benchmark or dead.

What moves price:
  MNQ/NQ: ES correlation, RTH structure, opening range breakouts
  BTC: sweep/reclaim at daily levels, confluence scorecard
  ETH: sweep/reclaim (oscillation pattern), daily sage gate
  SOL: shadow — 20% WR, no standalone edge on 1h
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from eta_engine.scripts import workspace_roots

if TYPE_CHECKING:
    from eta_engine.obs.drift_monitor import BaselineSnapshot

# Use the canonical workspace_roots helper (M1/M4 path-cleanup mandate)
# instead of relative Path math. See test_workspace_path_cleanup.
_ETF_FLOWS_PATH = str(workspace_roots.MNQ_HISTORY_ROOT / "BTC_ETF_FLOWS.csv")


@dataclass(frozen=True)
class StrategyAssignment:
    bot_id: str
    strategy_id: str
    symbol: str
    timeframe: str
    scorer_name: str
    confluence_threshold: float
    block_regimes: frozenset[str]
    window_days: int
    step_days: int
    min_trades_per_window: int
    rationale: str
    baseline: BaselineSnapshot | None = None
    extras: dict[str, object] = field(default_factory=dict)
    strategy_kind: str = "confluence"


_BASE_BLOCK = frozenset({"trending_up", "trending_down"})


ASSIGNMENTS: tuple[StrategyAssignment, ...] = (

    # ═══════════════════════════════════════════════════════════════════
    # DIAMOND TIER — paper-soak proven profitable (ETH)
    # ═══════════════════════════════════════════════════════════════════

    # eth_sweep_reclaim — THE CHAMPION. 62.5% WR, +$17k on 160 trades.
    # Sweep/reclaim on ETH 1h: detects liquidity sweeps at prior N-bar
    # extremes, waits for reclaim + close confirmation, enters with
    # volume and ATR-based structural stops.
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
            "DIAMOND #1: 62.5% WR, +$17k, 160 trades. Sweep/reclaim is the "
            "dominant ETH edge — ETH oscillates more than BTC, producing "
            "clean liquidity sweeps at prior extremes followed by reclaims. "
            "Tight filters (wick_pct=0.60, vol_z=1.0) keep quality high."
        ),
        extras={
            "promotion_status": "research_candidate",
            "per_ticker_optimal": "ETH",
            "crypto_native": True,
            "sweep_preset": "eth",
            "research_candidate": True,
        },
    ),

    # eth_perp — SAVED. 40% WR, +$8.8k on 120 trades.
    # Was crypto_orb with RR=1.0 (0% WR). Switched to sage_daily_gated
    # with crypto_orb base at RR=3.0. Sage gate filters directional false
    # positives on ETH 1h.
    StrategyAssignment(
        bot_id="eth_perp",
        strategy_id="eth_corb_v4",
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
            "DIAMOND #2: 40% WR, +$8.8k, 120 trades. Rescued from 0% WR by "
            "fixing RR 1.0→3.0 and adding sage daily gate. Sage filters the "
            "worst directional mismatches; wide stops (2.5 ATR) survive "
            "ETH's 2-5%% per-bar volatility."
        ),
        extras={
            "promotion_status": "research_candidate",
            "walk_forward_overrides": {"long_haul_mode": True, "long_haul_min_pos_fraction": 0.38},
            "min_daily_conviction": 0.30,
            "strict_mode": False,
            "instrument_class": "crypto",
            "sage_lookback_daily_bars": 200,
            "underlying_strategy": "crypto_orb",
            "crypto_orb_config": {
                "range_minutes": 120,
                "atr_stop_mult": 2.5,
                "rr_target": 3.0,
                "ema_bias_period": 100,
                "max_trades_per_day": 2,
            },
            "fleet_corr_partner": "btc_hybrid",
            "daily_loss_limit_pct": 4.0,
            "warmup_policy": {"promoted_on": "2026-05-01", "warmup_days": 30, "risk_multiplier_during_warmup": 0.5},
            # Elite scoreboard 2026-05-05: PF=0.43, Sharpe=-1.67,
            # expectancy_R=-0.0352 over 112 closes — clearly losing.
            # Sage's dow_theory dissented at 0.75 SHORT (primary
            # downtrend) on every consultation; bot configured long-only
            # could not adapt. Replaced by eth_sage_daily (ELITE: PF=2.53
            # Sharpe=2.48 +$12.01). Retired per elite framework.
            "deactivated": True,
            "deactivated_on": "2026-05-05",
            "deactivated_reason": (
                "elite_scoreboard 2026-05-05: PF=0.43 Sharpe=-1.67 "
                "expR=-0.0352 n=112 — fighting dow_theory short trend; "
                "replaced by eth_sage_daily ELITE"
            ),
        },
    ),

    # eth_sage_daily — consistent. 40% WR, +$3.8k on 80 trades.
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
            "DIAMOND #3: 40% WR, +$3.8k, 80 trades. Consistent ETH performer. "
            "Sage daily gate on crypto_orb base. The daily sage verdict "
            "provides directional filtering that crypto_orb alone lacks."
        ),
        extras={
            "promotion_status": "research_candidate",
            "walk_forward_overrides": {"agg_degradation_mode": True, "long_haul_mode": True, "long_haul_min_pos_fraction": 0.38},
            "underlying_strategy": "crypto_orb",
            "crypto_orb_config": {
                "range_minutes": 120,
                # FLEET PRESSURE TEST 2026-05-04: lab heatmap shows
                # sharpe=0.99 at stop_atr=2.5x vs 0.00 at current 3.0x —
                # 16.7% tighter stop, ~+1.0 sharpe lift in heatmap.
                # Current 3.0x is over-wide for ETH 1h's actual range.
                "atr_stop_mult": 2.5,
                "rr_target": 2.5,
                "ema_bias_period": 100,
                "max_trades_per_day": 2,
            },
            "sage_min_daily_conviction": 0.30,
            "sage_strict_mode": False,
            "sage_lookback_daily_bars": 200,
            "warmup_policy": {"promoted_on": "2026-04-30", "warmup_days": 30, "risk_multiplier_during_warmup": 0.5},
        },
    ),

    # ═══════════════════════════════════════════════════════════════════
    # DIAMOND TIER — paper-soak proven profitable (BTC)
    # ═══════════════════════════════════════════════════════════════════

    # btc_optimized — PROVEN BTC ARCHITECTURE. 50% WR, +$35k on 32 trades.
    # Sweep_reclaim + confluence scorecard. THIS is the BTC diamond —
    # no other BTC architecture is positive in paper soak.
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
            "DIAMOND #4 (BTC): 50% WR, +$35k, 32 trades. Sweep_reclaim + "
            "confluence scorecard (min 2/5 factors: trend EMA 21/50/100, "
            "VWAP, ATR regime, volume z, HTF). The ONLY BTC architecture "
            "with positive PnL. A+ trades (3+/5) get 1.3x size. "
            "Replaces broken crypto_orb scorecard that lost -$32k."
        ),
        extras={
            "promotion_status": "production_candidate",
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "sweep_preset": "btc",
                "sweep_config": {
                    "rr_target": 3.0,
                    "atr_stop_mult": 2.0,
                },
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "BTC",
            "research_candidate": True,
        },
    ),

    # btc_hybrid — migrated to proven BTC architecture (was sage_daily_gated 0% WR).
    StrategyAssignment(
        bot_id="btc_hybrid",
        strategy_id="btc_corb_v3",
        symbol="BTC",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=365,
        step_days=90,
        min_trades_per_window=10,
        strategy_kind="confluence_scorecard",
        rationale=(
            "DIAMOND CUT: Migrated to proven sweep_reclaim+scorecard from "
            "broken sage_daily_gated (0% WR, -$36k). Uses btc_optimized "
            "architecture verified at 50% WR, +$35k."
        ),
        extras={
            "promotion_status": "production",
            "walk_forward_overrides": {"long_haul_mode": True, "long_haul_min_pos_fraction": 0.38},
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 48, "reclaim_window": 3,
                "min_wick_pct": 0.30, "min_volume_z": 0.3,
                "rr_target": 3.0, "atr_stop_mult": 2.0,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "BTC",
            "daily_loss_limit_pct": 4.0,
            "warmup_policy": {"promoted_on": "2026-04-27", "warmup_days": 30, "risk_multiplier_during_warmup": 0.5},
            # Elite scoreboard 2026-05-05: PF=0.82, Sharpe=-0.40,
            # expectancy_R=-0.0128 over 125 closes on real-data feed.
            # Negative expectancy + already replaced by btc_hybrid_sage
            # (ELITE: PF=2.42 Sharpe=2.06 +$24.56). Retired per the
            # elite framework's "evolve or replace decaying strategies"
            # principle. Live data invalidated the legacy DIAMOND tag.
            "deactivated": True,
            "deactivated_on": "2026-05-05",
            "deactivated_reason": (
                "elite_scoreboard 2026-05-05: PF=0.82 Sharpe=-0.40 "
                "expR=-0.0128 over n=125 — replaced by btc_hybrid_sage ELITE"
            ),
        },
    ),

    # btc_regime_trend_etf — TIGHTER variant of the BTC sweep_reclaim
    # base.  Reactivated 2026-05-05 with deliberately differentiated
    # parameters (vs btc_hybrid's wider profile) so the three BTC slots
    # actually explore parameter space instead of triplicating one edge.
    # CHANGES from btc_hybrid baseline:
    #   level_lookback 48 → 24 (shorter — fresher liquidity pools)
    #   min_wick_pct 0.30 → 0.50 (stricter — only deeper sweeps)
    #   rr_target 3.0 → 2.0 (tighter target = higher WR, lower R)
    #   atr_stop_mult 2.0 → 1.5 (tighter stop)
    #   scorecard min_score 2 → 3 (stricter confluence gate)
    StrategyAssignment(
        bot_id="btc_regime_trend_etf",
        strategy_id="btc_regime_trend_etf_v2_tight",
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
            "DIAMOND CUT v2: Tighter variant of btc_hybrid (shorter "
            "lookback + stricter wick + tighter RR + stricter scorecard). "
            "Differentiated from the baseline so the 3 BTC slots explore "
            "parameter space instead of triplicating one edge."
        ),
        extras={
            "promotion_status": "research_candidate",
            "fleet_corr_partner": "btc_hybrid",
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 24, "reclaim_window": 3,
                "min_wick_pct": 0.50, "min_volume_z": 0.3,
                "rr_target": 2.0, "atr_stop_mult": 1.5,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 3, "a_plus_score": 4, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "BTC",
            "etf_csv_path": _ETF_FLOWS_PATH,
        },
    ),

    # btc_sage_daily_etf — WIDER variant of the BTC sweep_reclaim base.
    # Reactivated 2026-05-05 with deliberately differentiated parameters
    # (vs btc_hybrid's baseline AND vs the tight variant above).
    # CHANGES from btc_hybrid baseline:
    #   level_lookback 48 → 96 (longer — major liquidity pools only)
    #   reclaim_window 3 → 5 (more patient — slower reversion ok)
    #   rr_target 3.0 → 4.0 (wider target = lower WR, higher R)
    #   max_trades_per_day 2 → 1 (more selective)
    #   scorecard slow_ema 100 → 200 (longer-term trend filter)
    StrategyAssignment(
        bot_id="btc_sage_daily_etf",
        strategy_id="btc_sage_daily_etf_v2_wide",
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
            "DIAMOND CUT v2: Wider variant of btc_hybrid (longer "
            "lookback + slower reclaim + wider RR + more selective + "
            "longer trend filter).  Targets the bigger swings and "
            "complements the tight variant + the baseline."
        ),
        extras={
            "promotion_status": "research_candidate",
            "fleet_corr_partner": "btc_hybrid",
            "daily_loss_limit_pct": 4.0,
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 96, "reclaim_window": 5,
                "min_wick_pct": 0.30, "min_volume_z": 0.3,
                "rr_target": 4.0, "atr_stop_mult": 2.0,
                "max_trades_per_day": 1, "min_bars_between_trades": 24,
                "warmup_bars": 200,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 200,
            },
            "per_ticker_optimal": "BTC",
            "etf_csv_path": _ETF_FLOWS_PATH,
        },
    ),

    # ═══════════════════════════════════════════════════════════════════
    # DIAMOND TIER — paper-soak proven workable (MNQ/NQ futures)
    # ═══════════════════════════════════════════════════════════════════

    # mnq_futures_sage — ORB with retest + sage gate. 30.1% WR, close to breakeven.
    # RETUNED: lowered sage conv 0.50→0.40 (fewer missed breakouts), volume filter
    # 1.0→1.5 (require real volume on breakout), retest window 5→3 (faster confirm).
    StrategyAssignment(
        bot_id="mnq_futures_sage",
        strategy_id="mnq_orb_sage_v1",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="orb_sage_gated",
        rationale=(
            "DIAMOND #5: ORB+retest+sage on MNQ 5m. 30.1% WR paper soak, close "
            "to profitable. Tuned: sage conv 0.40 (was 0.50), volume filter 1.5x "
            "(was 1.0x), retest max_bars 3 (was 5). Retest mode filters false "
            "breakouts that reverse immediately; sage gate adds 22-school "
            "directional filter. RR=3.0 gives positive expectancy at WR>25%. "
            "Original: +10.06 OOS Sharpe."
        ),
        extras={
            "promotion_status": "production_candidate",
            "sage_min_conviction": 0.55,
            "sage_min_alignment": 0.50,
            "sage_lookback_bars": 200,
            "enabled_schools": frozenset({
                "dow_theory", "wyckoff", "trend_following", "vpa", "market_profile",
                "smc_ict", "order_flow", "support_resistance",
                "volatility_regime", "risk_management",
            }),
            "orb_range_minutes": 15,
            "orb_config": {
                "range_minutes": 15,
                "require_retest": True,
                "retest_atr_band": 1.0,
                "retest_max_bars": 3,
                "retest_require_close_bounce": True,
                "runaway_atr_mult": 2.5,
                "rr_target": 3.5,
                "atr_stop_mult": 2.5,
                "ema_bias_period": 200,
                "max_trades_per_day": 1,
                "volume_mult": 1.5,
            },
            "per_ticker_optimal": "MNQ",
            "warmup_policy": {"promoted_on": "2026-04-30", "warmup_days": 30, "risk_multiplier_during_warmup": 0.5},
            "daily_loss_limit_pct": 4.0,
        },
    ),

    # nq_futures_sage — same ORB+retest+sage on NQ. 35.4% WR, RR=3.0.
    StrategyAssignment(
        bot_id="nq_futures_sage",
        strategy_id="nq_orb_sage_v1",
        symbol="NQ1",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="orb_sage_gated",
        rationale=(
            "DIAMOND #6: ORB+retest+sage on NQ 5m. 36.8% WR, -$13.7k over "
            "304 trades (NQ is $20/pt vs MNQ $0.50/pt, so losses scale). "
            "Better WR than MNQ but higher per-contract loss. RR→3.0 should "
            "push this into profit. Original: +8.29 OOS Sharpe."
        ),
        extras={
            "promotion_status": "production_candidate",
            "sage_min_conviction": 0.55,
            "sage_min_alignment": 0.50,
            "sage_lookback_bars": 200,
            "enabled_schools": frozenset({
                "dow_theory", "wyckoff", "trend_following", "vpa", "market_profile",
                "smc_ict", "order_flow", "support_resistance",
                "volatility_regime", "risk_management",
            }),
            "orb_range_minutes": 15,
            "orb_config": {
                "range_minutes": 15,
                "require_retest": True,
                "retest_atr_band": 1.0,
                "retest_max_bars": 5,
                "retest_require_close_bounce": True,
                "runaway_atr_mult": 2.5,
                "rr_target": 3.0,
                "atr_stop_mult": 2.0,
                "ema_bias_period": 200,
                "max_trades_per_day": 1,
            },
            "per_ticker_optimal": "NQ",
            "warmup_policy": {"promoted_on": "2026-05-03", "warmup_days": 30, "risk_multiplier_during_warmup": 0.5},
            "daily_loss_limit_pct": 4.0,
        },
    ),

    # mnq_sweep_reclaim — NEW DIAMOND. Same btc_optimized formula on MNQ 5m intraday.
    # Sweep_reclaim finds liquidity grabs at prior N-bar extremes; confluence
    # scorecard requires 2/5 quality factors. MNQ-specific: tighter ATR stop
    # (1.0x), shorter lookback (20 bars = ~100 min), reclaim within 3 bars.
    StrategyAssignment(
        bot_id="mnq_sweep_reclaim",
        strategy_id="mnq_sweep_diamond_v1",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "DIAMOND #7: btc_optimized formula applied to MNQ 5m. Sweep_reclaim "
            "(detect liquidity grabs at prior extremes -> reclaim -> enter) + "
            "confluence scorecard (2/5 factors: trend EMA 9/21/50, VWAP, "
            "ATR regime, volume z, HTF agreement). MNQ-calibrated: 20-bar "
            "lookback (vs 48 for BTC), ATR stop 1.0x (vs 2.0 for BTC), "
            "RR 2.0 (vs 3.0 for BTC). Expected: 35-45% WR, positive PnL. "
            "The btc_optimized formula works because sweep_reclaim finds real "
            "liquidity-driven moves and the scorecard filters noise."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "sweep_preset": "mnq",
                "sweep_config": {
                    "rr_target": 3.0,
                    "atr_stop_mult": 1.0,
                    "max_trades_per_day": 4,
                    "min_bars_between_trades": 6,
                },
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 9, "mid_ema": 21, "slow_ema": 50,
            },
            "per_ticker_optimal": "MNQ",
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
            "warmup_policy": {"promoted_on": "2026-05-02", "warmup_days": 30, "risk_multiplier_during_warmup": 0.5},
        },
    ),

    # ═══════════════════════════════════════════════════════════════════
    # DIAMOND TIER — new supercharged extensions (2026-05-02)
    # Fresh builds. Paper-soak pending. Architecture proven via
    # btc_optimized pattern (sub-strategy → confluence scorecard → A+
    # size boost). Each fills a gap the existing diamond tier misses.
    # ═══════════════════════════════════════════════════════════════════

    # rsi_mr_mnq — RSI/BB Mean Reversion on MNQ 5m.
    StrategyAssignment(
        bot_id="rsi_mr_mnq",
        strategy_id="rsi_mr_mnq_v1",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "DIAMOND #8: RSI/BB mean-reversion on MNQ 5m. Counter-trend — "
            "fires when RSI < 25 (oversold) or > 75 (overbought) AND price "
            "touches BB 2σ bands AND rejection candle confirms AND volume "
            "confirms. Fills the biggest gap: every diamond strategy is "
            "trend-following. This thrives in range-bound regimes where "
            "existing strategies bleed. Afternoon session bias (13:30-15:30 "
            "ET) where mean-reversion edge concentrates."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "rsi_mean_reversion",
            "sub_strategy_extras": {
                "rsi_period": 14, "oversold_threshold": 25.0,
                "overbought_threshold": 75.0,
                "bb_window": 20, "bb_std_mult": 2.0,
                "min_volume_z": 0.3, "require_rejection": True,
                # PRESSURE TEST 2026-05-04: lab heatmap shows sharpe=1.81
                # at stop_atr=1.0x vs 0.78 at 1.5x — tighter stop more
                # than doubles risk-adjusted return. Counter-trend bots
                # need fast invalidation; 1.0x lets the bot eat losses
                # quickly and re-arm rather than ride a 1.5x drawdown.
                "rr_target": 1.5, "atr_stop_mult": 1.0,
                "max_trades_per_day": 3, "min_bars_between_trades": 12,
                "warmup_bars": 50,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 9, "mid_ema": 21, "slow_ema": 50,
            },
            "per_ticker_optimal": "MNQ",
            "paper_soak_result": "28.6% WR, +$66 on 7 trades",
            "walk_forward_overrides": {
                "long_haul_mode": True, "long_haul_min_pos_fraction": 0.33,
                "min_trades_per_window": 2,
            },
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
        },
    ),

    # rsi_mr_btc — DEACTIVATED. BTC 1h trends too strongly for mean-reversion.
    # RSI/BB mean-reversion works on MNQ 5m (28.6% WR, +$66) but BTC on 1h
    # has persistent directional moves that break the mean-reversion premise.
    # Re-activate when a higher-timeframe directional bias filter is added.
    StrategyAssignment(
        bot_id="rsi_mr_btc",
        strategy_id="rsi_mr_btc_DEACTIVATED",
        symbol="BTC",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=10.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        rationale=(
            "DEACTIVATED — BTC 1h trends too strongly for pure RSI/BB "
            "mean-reversion. 21.4% WR on 14 trades over 3000 bars. "
            "Same mechanic works on MNQ (28.6% WR). Re-activate with "
            "higher-timeframe directional bias filter."
        ),
        extras={"deactivated": True, "deactivation_reason": "BTC trends too strongly for mean-reversion on 1h"},
    ),

    # vwap_mr_mnq — VWAP Reversion on MNQ 5m.
    StrategyAssignment(
        bot_id="vwap_mr_mnq",
        strategy_id="vwap_mr_mnq_v1",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "DIAMOND #10: VWAP reversion on MNQ 5m. Fades deviations > 2σ "
            "from session VWAP, targeting VWAP. 64.4% WR paper sim (45 trades, "
            "+$1,641 on 30 days). Thrives on range/choppy days where ORB "
            "bleeds. Run alongside ORB sage for regime diversification."
        ),
        extras={
            "promotion_status": "production_candidate",
            "sub_strategy_kind": "vwap_reversion",
            "sub_strategy_extras": {
                "vwap_std_band": 2.0, "min_dev_std_mult": 1.8,
                "min_volume_z": 0.3,
                "rr_target": 2.0, "atr_stop_mult": 1.0,
                "max_trades_per_day": 3, "min_bars_between_trades": 12,
                "warmup_bars": 50,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 9, "mid_ema": 21, "slow_ema": 50,
            },
            "per_ticker_optimal": "MNQ",
            "paper_soak_result": "58.8% WR, +$171 on 3000 bars",
            "walk_forward_overrides": {"long_haul_mode": True, "long_haul_min_pos_fraction": 0.33},
            "daily_loss_limit_pct": 4.0,
            "warmup_policy": {"promoted_on": "2026-05-02", "warmup_days": 30, "risk_multiplier_during_warmup": 0.5},
        },
    ),

    # vwap_mr_nq — VWAP Reversion on NQ 5m.
    StrategyAssignment(
        bot_id="vwap_mr_nq",
        strategy_id="vwap_mr_nq_v1",
        symbol="NQ1",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "DIAMOND #10b: VWAP reversion on NQ 5m. 58% WR paper sim "
            "(50 trades, +$894 on 30 days). Same mechanic as MNQ — NQ "
            "deviations > 2σ mean-revert to session VWAP. Complements "
            "nq_futures_sage (ORB trend) as portfolio hedge."
        ),
        extras={
            "promotion_status": "production_candidate",
            "sub_strategy_kind": "vwap_reversion",
            "sub_strategy_extras": {
                "vwap_std_band": 2.0, "min_dev_std_mult": 1.8,
                "min_volume_z": 0.3,
                "rr_target": 2.0, "atr_stop_mult": 1.0,
                "max_trades_per_day": 3, "min_bars_between_trades": 12,
                "warmup_bars": 50,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 9, "mid_ema": 21, "slow_ema": 50,
            },
            "per_ticker_optimal": "NQ",
            "daily_loss_limit_pct": 4.0,
            "warmup_policy": {"promoted_on": "2026-05-03", "warmup_days": 30, "risk_multiplier_during_warmup": 0.5},
        },
    ),

    # vwap_mr_btc — VWAP Reversion on BTC 1h.
    StrategyAssignment(
        bot_id="vwap_mr_btc",
        strategy_id="vwap_mr_btc_v1",
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
            "DIAMOND #11: VWAP reversion on BTC 1h. UTC-anchored VWAP, "
            "fades deviations > 2σ. London open session bias (07:00-09:00 "
            "UTC) where VWAP reversion edge is strongest for crypto. "
            "Complements btc_optimized (trend) with uncorrelated VWAP "
            "mean-reversion edge."
        ),
        extras={
            "promotion_status": "production_candidate",
            "sub_strategy_kind": "vwap_reversion",
            "sub_strategy_extras": {
                "vwap_std_band": 2.0, "min_dev_std_mult": 1.5,
                "min_volume_z": 0.0,
                "rr_target": 2.0, "atr_stop_mult": 1.5,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 1, "a_plus_score": 2, "a_plus_size_mult": 1.5,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "BTC",
            "paper_soak_result": "85.7% WR, +$1,947 on 3000 bars",
            "walk_forward_overrides": {"long_haul_mode": True, "long_haul_min_pos_fraction": 0.33},
            "warmup_policy": {"promoted_on": "2026-05-02", "warmup_days": 30, "risk_multiplier_during_warmup": 0.5},
        },
    ),

    # volume_profile_mnq — Volume Profile / Value Area on MNQ 5m.
    StrategyAssignment(
        bot_id="volume_profile_mnq",
        strategy_id="vol_prof_mnq_v1",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "DIAMOND #12: Volume profile / value area mean-reversion on "
            "MNQ 5m. Computes POC/VAH/VAL from rolling 200-bar volume "
            "profile. Enters toward POC when price escapes value area with "
            "rejection candle confirmation. Auction-market theory: ~70% of "
            "volume clusters in value area; price gravitates to POC when "
            "it escapes. Different mechanic from any existing strategy."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "volume_profile",
            "sub_strategy_extras": {
                "profile_lookback": 1000, "bucket_size": 2.0,
                "min_va_spread_atr_mult": 2.0, "min_extreme_distance_atr_mult": 1.5,
                "max_qty_equity_pct": 0.005,
                "require_rejection": True, "min_rejection_wick_pct": 0.25,
                "min_volume_z": 0.3,
                # PRESSURE TEST 2026-05-04: lab heatmap shows sharpe=0.73
                # at stop_atr=2.5x vs 0.32 at current 1.0x — wider stop
                # MORE than doubles sharpe. Volume-profile bot trades
                # value-area extremes which often poke past with noise
                # before reverting; tight 1.0x stop was getting hit on
                # noise. 2.5x lets the auction-revert thesis breathe.
                "rr_target": 1.5, "atr_stop_mult": 2.5,
                "max_trades_per_day": 2, "min_bars_between_trades": 24,
                "warmup_bars": 1000,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 9, "mid_ema": 21, "slow_ema": 50,
            },
            "per_ticker_optimal": "MNQ",
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
        },
    ),

    # volume_profile_btc — Volume Profile / Value Area on BTC 1h.
    StrategyAssignment(
        bot_id="volume_profile_btc",
        strategy_id="vol_prof_btc_v1",
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
            "DIAMOND #13: Volume profile / value area on BTC 1h. 168-bar "
            "rolling profile (~7 days). POC magnetic effect is stronger on "
            "longer timeframes. Complements sweep_reclaim by providing a "
            "separate auction-theory edge from a completely different "
            "mechanic (gravitational vs momentum)."
        ),
        extras={
            "promotion_status": "production_candidate",
            "sub_strategy_kind": "volume_profile",
            "sub_strategy_extras": {
                "profile_lookback": 500, "bucket_size": 50.0,
                "min_va_spread_atr_mult": 2.0, "min_extreme_distance_atr_mult": 1.5,
                "max_qty_equity_pct": 0.005,
                "require_rejection": True, "min_rejection_wick_pct": 0.20,
                "min_volume_z": 0.2,
                "rr_target": 2.0, "atr_stop_mult": 1.5,
                "max_trades_per_day": 2, "min_bars_between_trades": 24,
                "warmup_bars": 500,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "BTC",
            "research_candidate": True,
        },
    ),

    # gap_fill_mnq — DEACTIVATED. Session gaps not reliably detected
    # in 5m data without RTH session markers. DeepSeek analysis: structural.
    StrategyAssignment(
        bot_id="gap_fill_mnq",
        strategy_id="gap_fill_mnq_DEACTIVATED",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=10.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=10,
        rationale="DEACTIVATED — session gaps not reliably detected without RTH markers.",
        extras={"deactivated": True, "deactivation_reason": "session gap detection unreliable"},
    ),

    # gap_fill_btc — DEACTIVATED. BTC is 24/7, no meaningful overnight gaps.
    StrategyAssignment(
        bot_id="gap_fill_btc",
        strategy_id="gap_fill_btc_DEACTIVATED",
        symbol="BTC",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=10.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        rationale="DEACTIVATED — BTC is 24/7 with continuous trading; no meaningful overnight gaps exist in crypto markets unlike traditional equities and futures.",
        extras={"deactivated": True, "deactivation_reason": "24/7 crypto, no overnight gap edge"},
    ),

    # cross_asset_mnq — NQ/ES ratio divergence on MNQ 5m.
    StrategyAssignment(
        bot_id="cross_asset_mnq",
        strategy_id="xasset_mnq_v1",
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
            "DIAMOND #16: Cross-asset NQ/ES ratio divergence on MNQ 5m. "
            "Tracks MNQ/ES ratio z-score. When NQ overperforms ES by >2σ, "
            "short MNQ (bet on ratio mean-reversion). When NQ underperforms, "
            "long MNQ. Genuine diversification — the signal source (asset "
            "ratio) is orthogonal to single-instrument price action. "
            "Requires ES reference data provider."
        ),
        extras={
            "promotion_status": "production_candidate",
            "sub_strategy_kind": "cross_asset_divergence",
            "sub_strategy_extras": {
                "z_lookback": 100, "entry_z_threshold": 2.0,
                "min_z_threshold": 1.5,
                "min_volume_z": 0.3,
                "rr_target": 2.5, "atr_stop_mult": 2.5,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 100,
                "reference_asset": "ES1",
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 9, "mid_ema": 21, "slow_ema": 50,
            },
            "per_ticker_optimal": "MNQ",
            "paper_soak_result": "56.2% WR, +$1,084 on 16 trades",
            "walk_forward_overrides": {
                "grid_mode": True, "grid_min_profit_factor": 1.0,
                "grid_max_dd_pct": 50.0, "grid_min_pos_fraction": 0.33,
                "min_trades_per_window": 2,
            },
            "daily_loss_limit_pct": 4.0,
            "warmup_policy": {"promoted_on": "2026-05-02", "warmup_days": 30, "risk_multiplier_during_warmup": 0.5},
        },
    ),

    # cross_asset_btc — BTC/ETH ratio divergence on BTC 1h.
    StrategyAssignment(
        bot_id="cross_asset_btc",
        strategy_id="xasset_btc_v1",
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
            "DEACTIVATED 2026-05-03: BTC/ETH ratio divergence at 20% WR on "
            "10 trades over 30-day paper sim. The ratio doesn't mean-revert "
            "cleanly enough on 1h bars. Re-activate with higher-TF filter."
        ),
        extras={
            "promotion_status": "shadow_benchmark",
            "deactivated": True,
            "deactivation_reason": "20% WR on paper sim — BTC/ETH ratio not mean-reverting on 1h",
            "sub_strategy_kind": "cross_asset_divergence",
            "sub_strategy_extras": {
                "z_lookback": 168, "entry_z_threshold": 1.5,
                "min_z_threshold": 1.0,
                "min_volume_z": 0.2,
                "rr_target": 2.5, "atr_stop_mult": 1.5,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 168,
                "reference_asset": "ETH",
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "BTC",
            "paper_soak_result": "30.4% WR, +$7,624 on 46 trades",
            "walk_forward_overrides": {
                "grid_mode": True, "grid_min_profit_factor": 1.0,
                "grid_max_dd_pct": 50.0, "grid_min_pos_fraction": 0.5,
            },
            "warmup_policy": {"promoted_on": "2026-05-02", "warmup_days": 30, "risk_multiplier_during_warmup": 0.5},
        },
    ),

    # funding_rate_btc — Funding rate momentum on BTC 1h.
    StrategyAssignment(
        bot_id="funding_rate_btc",
        strategy_id="fund_rate_btc_v1",
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
            "DIAMOND #18: Funding rate momentum on BTC 1h. This is the "
            "COMPANION to funding_divergence_strategy. Where "
            "funding_divergence fades extreme funding (contrarian "
            "mean-reversion at |funding| > 0.075%), THIS strategy follows "
            "PERSISTENT funding (when funding has been positive for 4+/6 "
            "cycles, signaling sustained bullish positioning). Different "
            "thresholds, different direction, different edge. Requires "
            "BTCFUND_8h data provider."
        ),
        extras={
            "promotion_status": "production_candidate",
            "sub_strategy_kind": "funding_rate",
            "sub_strategy_extras": {
                "persistence_lookback": 6, "persistence_threshold": 0.50,
                "ema_period": 21, "require_pullback": True,
                "min_volume_z": 0.2,
                "rr_target": 2.0, "atr_stop_mult": 1.5,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "BTC",
            "paper_soak_result": "52.6% WR, +$6,383 on 19 trades",
            "walk_forward_overrides": {
                "grid_mode": True, "grid_min_profit_factor": 1.0,
                "grid_max_dd_pct": 50.0, "grid_min_pos_fraction": 0.33,
            },
            "warmup_policy": {"promoted_on": "2026-05-02", "warmup_days": 30, "risk_multiplier_during_warmup": 0.5},
        },
    ),

    # ═══════════════════════════════════════════════════════════════════
    # MICRO FUTURES — leveraged MBT/MET (1/10th BTC/ETH on CME)
    # ═══════════════════════════════════════════════════════════════════

    # MBT/MET micro futures — de-duplicated. See correct entries at end of
    # file with symbol="MBT"/"MET". These stubs were duplicates.

    # ═══════════════════════════════════════════════════════════════════
    # SHADOW BENCHMARK — diagnostic only, not for live exposure
    # ═══════════════════════════════════════════════════════════════════

    StrategyAssignment(
        bot_id="btc_hybrid_sage",
        strategy_id="btc_corb_sage_v1",
        symbol="BTC",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=5,
        strategy_kind="orb_sage_gated",
        rationale="SHADOW: orb_sage_gated on BTC 1h. 25% WR paper soak.",
        extras={
            "promotion_status": "shadow_benchmark",
            "shadow_reason": "25% WR paper soak. Keep as diagnostic while proven sweep_reclaim+scorecard carries BTC exposure.",
            "sage_min_conviction": 0.40,
            "sage_lookback_bars": 200,
            "orb_range_minutes": 60,
            "orb_config": {"range_minutes": 60, "rr_target": 2.5, "atr_stop_mult": 2.5, "ema_bias_period": 100, "max_trades_per_day": 2},
            "instrument_class": "crypto",
        },
    ),

    StrategyAssignment(
        bot_id="btc_ensemble_2of3",
        strategy_id="btc_ensemble_2of3_v1",
        symbol="BTC",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="ensemble_voting",
        rationale="SHADOW: ensemble voting on BTC 1h — 25% WR paper soak, losing. Keep as diagnostic benchmark while sweep_reclaim+scorecard carries BTC exposure.",
        extras={
            "promotion_status": "shadow_benchmark",
            "shadow_reason": "25% WR paper soak, losing. Keep as diagnostic while sweep_reclaim+scorecard carries BTC exposure.",
            "min_agreement_count": 3,
            "voters": ["regime_trend", "regime_trend_etf", "sage_daily_gated"],
            "etf_csv_path": _ETF_FLOWS_PATH,
        },
    ),

    StrategyAssignment(
        bot_id="sol_perp",
        strategy_id="sol_corb_v2",
        symbol="SOL",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        strategy_kind="sweep_reclaim",
        rationale="SHADOW: SOL sweep_reclaim on 1h — 20% WR paper soak, no verifiable standalone edge yet.",
        extras={
            "promotion_status": "shadow_benchmark",
            "shadow_reason": "SOL 1h produces 20% WR regardless of strategy. SOL has no standalone edge on 1h — it's a pure BTC beta proxy.",
            "research_candidate": True,
            "level_lookback": 48, "reclaim_window": 3, "min_wick_pct": 0.25,
            "min_volume_z": 0.3, "rr_target": 3.0, "atr_stop_mult": 2.5,
            "max_trades_per_day": 2, "min_bars_between_trades": 12, "warmup_bars": 72,
            "risk_per_trade_pct": 0.005,
            "deactivated": True,
            "deactivated_on": "2026-05-04",
            "deactivated_reason": "lab_sweep_2026_05_04: sol_perp failed gates (sharpe=-0.96, exp_R=-0.077, wr=0.346)",
        },
    ),

    StrategyAssignment(
        bot_id="eth_compression",
        strategy_id="eth_compression_v1",
        symbol="ETH",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="compression_breakout",
        rationale="SHADOW: compression breakout on ETH 1h. 12.5% WR paper soak.",
        extras={
            "compression_preset": "eth",
            "promotion_status": "shadow_benchmark",
            "shadow_reason": "12.5% WR paper soak. Compression breakout doesn't produce edge on ETH 1h.",
            "compression_config": {"compression_recency_window": 12, "min_close_location": 0.50, "min_volume_z": 0.2, "atr_stop_mult": 2.0, "rr_target": 2.5},
            # Elite scoreboard 2026-05-05: PF=0.68 Sharpe=-0.93
            # expR=-0.0245 over 124 closes — confirmed shadow_reason.
            # Compression-breakout on ETH 1h has no edge regardless of
            # tuning. Retired per elite framework.
            "deactivated": True,
            "deactivated_on": "2026-05-05",
            "deactivated_reason": (
                "elite_scoreboard 2026-05-05: PF=0.68 Sharpe=-0.93 "
                "expR=-0.0245 n=124 — compression on ETH 1h has no edge"
            ),
        },
    ),

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
        rationale="SHADOW: scorecard wrapping orb_sage. 33% WR paper soak.",
        extras={
            "promotion_status": "shadow_benchmark",
            "shadow_reason": "33% WR, losing. mnq_futures_sage is the stronger MNQ launch lane.",
            "sub_strategy_kind": "orb_sage_gated",
            "sub_strategy_extras": {"sage_min_conviction": 0.65, "sage_lookback_bars": 200, "orb_range_minutes": 15},
            "scorecard_config": {"min_score": 3, "a_plus_score": 4, "a_plus_size_mult": 1.5, "fast_ema": 9, "mid_ema": 21, "slow_ema": 50},
            "per_ticker_optimal": "MNQ",
        },
    ),

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
        rationale="SHADOW: MTF scalp on BTC 5m — 0% WR paper soak; wrong timeframe for BTC, 1h is the correct cadence.",
        extras={
            "promotion_status": "shadow_benchmark",
            "shadow_reason": "0% WR on BTC 5m. Wrong timeframe for BTC — 1h is the right cadence.",
            "per_ticker_optimal": "BTC",
            "crypto_native": True,
            "mtf_scalp_config": {"htf_bars_per_aggregate": 12, "ltf_rr_target": 2.5, "ltf_atr_stop_mult": 1.5, "max_trades_per_day": 6},
        },
    ),

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
        rationale="SHADOW: SOL sweep_reclaim on 1h — 20% WR, no standalone edge; SOL trades as pure BTC beta proxy and lacks independent alpha.",
        extras={
            "promotion_status": "shadow_benchmark",
            "shadow_reason": "SOL 1h sweep_reclaim at 20% WR. Same as sol_perp — SOL has no standalone edge.",
            "per_ticker_optimal": "SOL",
            "crypto_native": True,
            "level_lookback": 48, "reclaim_window": 3, "min_wick_pct": 0.30,
            "min_volume_z": 0.5, "rr_target": 3.0, "atr_stop_mult": 2.5,
            "max_trades_per_day": 2, "min_bars_between_trades": 12, "warmup_bars": 72,
            "risk_per_trade_pct": 0.005,
            "deactivated": True,
            "deactivated_on": "2026-05-04",
            "deactivated_reason": "lab_sweep_2026_05_04: sol_sweep_scalp failed gates (sharpe=-0.91, exp_R=-0.072, wr=0.348)",
        },
    ),

    # ═══════════════════════════════════════════════════════════════════
    # DEACTIVATED / NON-EDGE — excluded from paper-soak
    # ═══════════════════════════════════════════════════════════════════

    StrategyAssignment(
        bot_id="crypto_seed",
        strategy_id="crypto_seed_dca",
        symbol="BTC",
        timeframe="D",
        scorer_name="global",
        confluence_threshold=4.0,
        block_regimes=frozenset(),
        window_days=365,
        step_days=180,
        min_trades_per_window=5,
        rationale="DCA accumulator — non-edge strategy for exposure, not alpha.",
        extras={"promotion_status": "non_edge_strategy", "non_edge_reason": "DCA accumulator, not alpha edge."},
    ),

    StrategyAssignment(
        bot_id="xrp_perp",
        strategy_id="xrp_DEACTIVATED",
        symbol="XRP",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=10.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        rationale="DEACTIVATED — no news or regulatory event feed available for XRP; cannot validate fundamental edge drivers.",
        extras={"deactivated": True, "deactivation_reason": "no news feed"},
    ),

    # ═══════════════════════════════════════════════════════════════════
    # Deactivated/historical stubs — kept in registry only to satisfy
    # registry/requirements bidirectional-sync audit. Their data
    # requirements remain in data/requirements.py because tests and
    # readiness checks still reference these IDs by name. Reactivate by
    # flipping deactivated=False in extras and updating strategy_id.
    # ═══════════════════════════════════════════════════════════════════

    StrategyAssignment(
        bot_id="mnq_futures",
        strategy_id="mnq_futures_DEPRECATED",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="btc",
        confluence_threshold=10.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        rationale="DEPRECATED — superseded by mnq_futures_sage (sage-overlay variant).",
        extras={"deactivated": True, "deactivation_reason": "superseded by mnq_futures_sage"},
    ),

    StrategyAssignment(
        bot_id="nq_futures",
        strategy_id="nq_futures_DEPRECATED",
        symbol="NQ1",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=10.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        rationale="DEPRECATED — superseded by nq_futures_sage (sage-overlay variant).",
        extras={"deactivated": True, "deactivation_reason": "superseded by nq_futures_sage"},
    ),

    StrategyAssignment(
        bot_id="mnq_sage_consensus",
        strategy_id="mnq_sage_consensus_DEPRECATED",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="btc",
        confluence_threshold=10.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        rationale="DEPRECATED — folded into mnq_futures_sage; kept as historical stub for registry sync.",
        extras={"deactivated": True, "deactivation_reason": "folded into mnq_futures_sage"},
    ),

    StrategyAssignment(
        bot_id="nq_daily_drb",
        strategy_id="nq_daily_drb_DEPRECATED",
        symbol="NQ1",
        timeframe="1d",
        scorer_name="btc",
        confluence_threshold=10.0,
        block_regimes=frozenset(),
        window_days=365,
        step_days=30,
        min_trades_per_window=10,
        rationale="DEPRECATED — daily DRB variant superseded by nq_futures_sage; kept as historical stub.",
        extras={"deactivated": True, "deactivation_reason": "superseded by nq_futures_sage"},
    ),

    StrategyAssignment(
        bot_id="btc_regime_trend",
        strategy_id="btc_regime_trend_DEPRECATED",
        symbol="BTC",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=10.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        rationale="DEPRECATED — superseded by btc_regime_trend_etf (ETF-routed execution).",
        extras={"deactivated": True, "deactivation_reason": "superseded by btc_regime_trend_etf"},
    ),

    # ═══════════════════════════════════════════════════════════════════
    # MBT/MET — CME micro crypto futures (US-person compliant)
    # ═══════════════════════════════════════════════════════════════════
    # Uses the same proven sweep_reclaim+scorecard architecture as BTC
    # but on CME micro futures with RTH session constraints. MBT tracks
    # BTCUSDT through M2 translation, MET tracks ETHUSDT.

    StrategyAssignment(
        bot_id="mbt_sweep_reclaim",
        strategy_id="mbt_sweep_reclaim_v1",
        symbol="MBT",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=365,
        step_days=90,
        min_trades_per_window=10,
        strategy_kind="confluence_scorecard",
        rationale=(
            "NEW: CME Micro Bitcoin futures (0.1 BTC) using proven "
            "sweep_reclaim+scorecard architecture from BTC. "
            "US-person compliant, RTH-only CME session gating."
        ),
        extras={
            "promotion_status": "production_candidate",
            "walk_forward_overrides": {"long_haul_mode": True, "long_haul_min_pos_fraction": 0.38},
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 48, "reclaim_window": 3,
                "min_wick_pct": 0.30, "min_volume_z": 0.3,
                "rr_target": 3.0, "atr_stop_mult": 2.0,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "MBT",
            "daily_loss_limit_pct": 4.0,
            "fleet_corr_partner": "btc_hybrid",
            "deactivated": True,
            "deactivated_on": "2026-05-04",
            "deactivated_reason": "lab_sweep_2026_05_04: mbt_sweep_reclaim failed gates (sharpe=-0.71, exp_R=-0.057)",
        },
    ),

    StrategyAssignment(
        bot_id="met_sweep_reclaim",
        strategy_id="met_sweep_reclaim_v1",
        symbol="MET",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=365,
        step_days=90,
        min_trades_per_window=10,
        strategy_kind="confluence_scorecard",
        rationale=(
            "NEW: CME Micro Ether futures (0.1 ETH) using proven "
            "sweep_reclaim+scorecard architecture from ETH. "
            "US-person compliant, RTH-only CME session gating."
        ),
        extras={
            "promotion_status": "research_candidate",
            "walk_forward_overrides": {"long_haul_mode": True, "long_haul_min_pos_fraction": 0.38},
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 48, "reclaim_window": 3,
                "min_wick_pct": 0.30, "min_volume_z": 0.3,
                "rr_target": 3.0, "atr_stop_mult": 2.0,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "MET",
            "daily_loss_limit_pct": 4.0,
            "fleet_corr_partner": "eth_perp",
            "deactivated": True,
            "deactivated_on": "2026-05-04",
            "deactivated_reason": "lab_sweep_2026_05_04: met_sweep_reclaim failed gates (sharpe=-0.47, exp_R=-0.038)",
        },
    ),

    # ═══════════════════════════════════════════════════════════════════
    # ANCHOR-SWEEP TIER (2026-05-04)
    # Named-anchor variant of sweep_reclaim for US index futures.
    # The base sweep_reclaim uses a 20-bar lookback (~100 min on 5m)
    # to identify liquidity pools — wrong abstraction for MNQ/NQ where
    # institutions stop-hunt at FIXED, named levels (PDH/PDL/PMH/PML/
    # ONH/ONL). This variant anchors detection to those levels.
    # ═══════════════════════════════════════════════════════════════════

    StrategyAssignment(
        bot_id="mnq_anchor_sweep",
        strategy_id="mnq_anchor_sweep_v1",
        symbol="MNQ1",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="anchor_sweep",
        rationale=(
            "Named-anchor variant of sweep_reclaim for MNQ 5m. Tracks "
            "PDH/PDL (RTH 09:30-16:00 ET), PMH/PML (premarket 04:00-"
            "09:30 ET), ONH/ONL (overnight 18:00-04:00 ET) and fires "
            "on a wick-pierce + close-reclaim of any active anchor. "
            "Direction: sweep-of-high → SHORT, sweep-of-low → LONG. "
            "Wick-aware structural stops; opposite anchor as natural "
            "target with 2R fallback."
        ),
        extras={
            "promotion_status": "research_candidate",
            "anchor_preset": "mnq",
            "per_ticker_optimal": "MNQ",
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
        },
    ),

    StrategyAssignment(
        bot_id="nq_anchor_sweep",
        strategy_id="nq_anchor_sweep_v1",
        symbol="NQ1",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="anchor_sweep",
        rationale=(
            "Named-anchor variant of sweep_reclaim for NQ 5m. Same "
            "Nasdaq-100 underlying as MNQ; identical mechanic. NQ is "
            "$20/point vs MNQ $2/point but qty sizing absorbs that via "
            "risk_per_trade_pct * equity / stop_distance."
        ),
        extras={
            "promotion_status": "research_candidate",
            "anchor_preset": "nq",
            "per_ticker_optimal": "NQ",
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
        },
    ),

    # ═══════════════════════════════════════════════════════════════════
    # COMMODITY + FX TIER (2026-05-04)
    # User directive: "sage needs to read real data for all tickers
    # including commodities". Pre-this-session the registry was crypto +
    # equity-index futures only. With YF_MAP + _FUTURES_ROOTS extended
    # to cover energies (CL/NG), metals (GC), rates (ZN), and FX (6E),
    # Sage now has real bars for these symbols. Adding bots so the live
    # supervisor actually consults Sage on them. All start with the
    # proven sweep_reclaim + confluence_scorecard architecture (the
    # btc_optimized template that produced the post-fix top earners) at
    # conservative parameters — atr_stop_mult=2.0, rr_target=2.5,
    # max_trades_per_day=2 — and `research_candidate` promotion status
    # so they live in paper-soak until lab evidence promotes them.
    # ═══════════════════════════════════════════════════════════════════
    StrategyAssignment(
        bot_id="gc_sweep_reclaim",
        strategy_id="gc_sweep_reclaim_v1",
        symbol="GC",
        timeframe="1h",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=180,
        step_days=60,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "COMMODITY: Gold (GC) sweep_reclaim+scorecard on 1h. Real "
            "yfinance data via composite feed. Gold's safe-haven flow "
            "produces clean liquidity sweeps at prior daily extremes; "
            "Wyckoff distribution + accumulation visible at session "
            "boundaries. Conservative atr_stop_mult=2.0 — gold ATR is "
            "$25-40/h so stop ~ $50-80 per contract = ~$5-8k notional."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 48, "reclaim_window": 3,
                "min_wick_pct": 0.30, "min_volume_z": 0.3,
                "rr_target": 2.5, "atr_stop_mult": 2.0,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "GC",
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
        },
    ),

    StrategyAssignment(
        bot_id="cl_sweep_reclaim",
        strategy_id="cl_sweep_reclaim_v1",
        symbol="CL",
        timeframe="1h",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=180,
        step_days=60,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "COMMODITY: WTI Crude (CL) sweep_reclaim+scorecard on 1h. "
            "Real yfinance data. Energy markets are reflexive — supply/"
            "demand shocks plus inventory flows produce strong intraday "
            "trends and equally strong mean reversions at level "
            "extremes. Sage's market_profile + vpa schools are well-"
            "suited; expect dow_theory/wyckoff to drive bias."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 48, "reclaim_window": 3,
                "min_wick_pct": 0.30, "min_volume_z": 0.3,
                "rr_target": 2.5, "atr_stop_mult": 2.0,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "CL",
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
        },
    ),

    StrategyAssignment(
        bot_id="ng_sweep_reclaim",
        strategy_id="ng_sweep_reclaim_v1",
        symbol="NG",
        timeframe="1h",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=180,
        step_days=60,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "COMMODITY: Natural Gas (NG) sweep_reclaim+scorecard on 1h. "
            "NG is the most volatile mainstream futures contract — daily "
            "ranges of 5-10% are routine. Wider stop multiplier may be "
            "needed; start conservative and let lab evidence promote a "
            "wider variant. Vol_regime school will frequently flag this."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 48, "reclaim_window": 3,
                "min_wick_pct": 0.30, "min_volume_z": 0.3,
                "rr_target": 2.5, "atr_stop_mult": 2.0,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "NG",
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
        },
    ),

    StrategyAssignment(
        bot_id="zn_sweep_reclaim",
        strategy_id="zn_sweep_reclaim_v1",
        symbol="ZN",
        timeframe="1h",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=180,
        step_days=60,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "COMMODITY: 10Y Note (ZN) sweep_reclaim+scorecard on 1h. "
            "Rates flow primarily on FOMC + macro releases; intraday "
            "structure tends to respect 32nd ticks. Tight ATR (~$200-"
            "$400) — point_value=$1000 means 1 contract risk = "
            "stop_dist × 1000."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 48, "reclaim_window": 3,
                "min_wick_pct": 0.30, "min_volume_z": 0.3,
                "rr_target": 2.5, "atr_stop_mult": 2.0,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "ZN",
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
        },
    ),

    StrategyAssignment(
        bot_id="eur_sweep_reclaim",
        strategy_id="eur_sweep_reclaim_v1",
        symbol="6E",
        timeframe="1h",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=180,
        step_days=60,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "COMMODITY: EUR/USD (6E) sweep_reclaim+scorecard on 1h. "
            "FX session structure (London/NY overlap, Asian quiet) "
            "produces predictable seasonality the school can pick up. "
            "Tight stops; point_value=$125k makes any move material."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 48, "reclaim_window": 3,
                "min_wick_pct": 0.30, "min_volume_z": 0.3,
                "rr_target": 2.5, "atr_stop_mult": 2.0,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "6E",
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
        },
    ),

    # ═══════════════════════════════════════════════════════════════════
    # EQUITY-INDEX MICROS TIER (2026-05-04)
    # MES (S&P), M2K (Russell), YM (Dow), MYM (micro Dow). The full-size
    # NQ + ES + RTY + YM bots aren't in the active registry; the micros
    # let small accounts trade the same setups with 5-10x less notional
    # exposure per contract, and the futures floor in bracket_sizing
    # ensures the per-bot cap doesn't round qty to 0.
    # ═══════════════════════════════════════════════════════════════════
    StrategyAssignment(
        bot_id="mes_sweep_reclaim",
        strategy_id="mes_sweep_reclaim_v1",
        symbol="MES",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=120,
        step_days=30,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "EQUITY-INDEX MICRO: S&P 500 (MES) sweep_reclaim+scorecard "
            "on 5m. point_value=$5/pt vs ES $50/pt — 10x less notional "
            "per contract. Same RTH-session opening-range / sweep "
            "structure as MNQ; expect dow_theory + market_profile + "
            "trend_following to dominate Sage's read."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 48, "reclaim_window": 3,
                "min_wick_pct": 0.30, "min_volume_z": 0.3,
                "rr_target": 2.5, "atr_stop_mult": 2.0,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "MES",
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
        },
    ),

    StrategyAssignment(
        bot_id="m2k_sweep_reclaim",
        strategy_id="m2k_sweep_reclaim_v1",
        symbol="M2K",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=120,
        step_days=30,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "EQUITY-INDEX MICRO: Russell 2000 (M2K) sweep_reclaim+"
            "scorecard on 5m. Small caps lead at risk-on inflection "
            "points — historically beats S&P at cycle turns. Same "
            "structure as MES; cross_asset_correlation school can "
            "tell when M2K is leading or lagging the broader index."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 48, "reclaim_window": 3,
                "min_wick_pct": 0.30, "min_volume_z": 0.3,
                "rr_target": 2.5, "atr_stop_mult": 2.0,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "M2K",
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
        },
    ),

    StrategyAssignment(
        bot_id="ym_sweep_reclaim",
        strategy_id="ym_sweep_reclaim_v1",
        symbol="YM",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=120,
        step_days=30,
        min_trades_per_window=5,
        strategy_kind="confluence_scorecard",
        rationale=(
            "EQUITY-INDEX: Dow Jones (YM) sweep_reclaim+scorecard on "
            "5m. point_value=$5/pt; 30-stock blue-chip index moves "
            "differently from cap-weighted S&P. Useful diversifier in "
            "the equity-index basket; cross_asset_correlation tracks "
            "YM vs ES vs NQ vs M2K disagreement."
        ),
        extras={
            "promotion_status": "research_candidate",
            "sub_strategy_kind": "sweep_reclaim",
            "sub_strategy_extras": {
                "level_lookback": 48, "reclaim_window": 3,
                "min_wick_pct": 0.30, "min_volume_z": 0.3,
                "rr_target": 2.5, "atr_stop_mult": 2.0,
                "max_trades_per_day": 2, "min_bars_between_trades": 12,
                "warmup_bars": 72,
            },
            "scorecard_config": {
                "min_score": 2, "a_plus_score": 3, "a_plus_size_mult": 1.3,
                "fast_ema": 21, "mid_ema": 50, "slow_ema": 100,
            },
            "per_ticker_optimal": "YM",
            "research_candidate": True,
            "daily_loss_limit_pct": 4.0,
        },
    ),

)


# ---------------------------------------------------------------------------
# Lookup API
# ---------------------------------------------------------------------------

def get_for_bot(bot_id: str) -> StrategyAssignment | None:
    for a in ASSIGNMENTS:
        if a.bot_id == bot_id:
            return a
    return None


_KAIZEN_OVERRIDES_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "kaizen_overrides.json"


def _load_kaizen_overrides() -> dict[str, dict]:
    """Read the kaizen-loop deactivation sidecar.

    Empty / missing / malformed file → no overrides (safe default).
    Re-read every call: file is tiny (~1KB) and auto-RETIRE only
    appends when the 2-run gate confirms — fewer than ~10 entries
    in normal operation. is_active() is called once per bot at
    supervisor startup, not in a hot loop.
    """
    try:
        if not _KAIZEN_OVERRIDES_PATH.exists():
            return {}
        import json as _json
        data = _json.loads(_KAIZEN_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    deact = data.get("deactivated") if isinstance(data, dict) else None
    return deact if isinstance(deact, dict) else {}


def is_active(assignment: StrategyAssignment) -> bool:
    if bool(assignment.extras.get("deactivated", False)):
        return False
    # Kaizen-loop sidecar override: a bot listed under
    # var/eta_engine/state/kaizen_overrides.json -> deactivated -> {bot_id: ...}
    # has been auto-deactivated by the daily kaizen pass after the
    # 2-run confirmation gate. Operator can re-enable by removing
    # the entry (or running `python -m eta_engine.scripts.kaizen_reactivate
    # <bot_id>`).
    return assignment.bot_id not in _load_kaizen_overrides()


def is_bot_active(bot_id: str) -> bool:
    a = get_for_bot(bot_id)
    return a is not None and is_active(a)


def all_assignments() -> list[StrategyAssignment]:
    return list(ASSIGNMENTS)


def bots() -> list[str]:
    return [a.bot_id for a in ASSIGNMENTS]


def _config_signature(a: StrategyAssignment) -> tuple:
    """Return a hashable signature representing the bot's tradeable config.

    Two bots with the same signature will produce identical trades on the
    same data — which means deploying both wastes a risk slot on the same
    edge.  Used by ``find_duplicate_active_bots`` to surface the duplicates
    so the operator can deactivate or differentiate before live capital
    flows.

    Signature includes:
    - strategy_kind (e.g., confluence_scorecard, sweep_reclaim)
    - sub_strategy_kind (when wrapping)
    - sub_strategy_extras (the actual sub-strategy parameters)
    - scorecard_config (when confluence_scorecard wraps)
    - confluence_threshold

    Excluded (these don't change trade behavior):
    - bot_id, strategy_id, rationale
    - promotion_status, fleet_corr_partner annotations
    - per_ticker_optimal, etf_csv_path, daily_loss_limit_pct
    """
    extras = a.extras or {}
    sub_extras = extras.get("sub_strategy_extras")
    scorecard = extras.get("scorecard_config")

    def _freeze(o: object) -> object:
        if isinstance(o, dict):
            return tuple(sorted((k, _freeze(v)) for k, v in o.items()))
        if isinstance(o, list):
            return tuple(_freeze(x) for x in o)
        if isinstance(o, set | frozenset):
            return tuple(sorted(_freeze(x) for x in o))
        return o

    return (
        a.strategy_kind,
        extras.get("sub_strategy_kind"),
        _freeze(sub_extras),
        _freeze(scorecard),
        round(float(a.confluence_threshold or 0.0), 6),
    )


def find_duplicate_active_bots(
    assignments: list[StrategyAssignment] | None = None,
) -> list[tuple[str, str, list[str]]]:
    """Surface (symbol, timeframe, [bot_ids]) groups whose active bots share
    a bit-for-bit identical tradeable config.

    Returns a list of duplicate groups; empty list = no duplicates.

    Why: deploying two bots with identical config wastes a risk slot —
    they make the same trades on the same data, so the operator pays for
    diversification they don't actually get.  The 2026-05-05 audit found
    btc_hybrid + btc_regime_trend_etf + btc_sage_daily_etf were all
    bit-for-bit identical and would have routed 3x risk on a single edge
    if all three reached live.  This guard catches the next instance
    before it ships.
    """
    if assignments is None:
        assignments = all_assignments()

    # Group by (symbol, timeframe, signature)
    groups: dict[tuple, list[str]] = {}
    for a in assignments:
        if not is_active(a):
            continue
        key = (a.symbol, a.timeframe, _config_signature(a))
        groups.setdefault(key, []).append(a.bot_id)

    duplicates: list[tuple[str, str, list[str]]] = []
    for (symbol, timeframe, _sig), bot_ids in groups.items():
        if len(bot_ids) >= 2:
            duplicates.append((symbol, timeframe, sorted(bot_ids)))
    return duplicates


def validate_registry_no_duplicates(
    assignments: list[StrategyAssignment] | None = None,
    *,
    raise_on_duplicate: bool = False,
) -> list[str]:
    """Validate the active fleet has no bot_id pairs with identical config.

    Returns a list of human-readable warning messages (empty = clean fleet).
    When ``raise_on_duplicate=True``, raises ``RuntimeError`` instead of
    returning warnings — use this from startup wiring to fail-closed.

    Two duplicate-active bots would route the same trades to the broker
    twice, doubling risk on one edge — a critical risk-budget violation.
    """
    duplicates = find_duplicate_active_bots(assignments)
    if not duplicates:
        return []

    warnings: list[str] = []
    for symbol, timeframe, bot_ids in duplicates:
        msg = (
            f"DUPLICATE ACTIVE BOTS on {symbol}/{timeframe}: "
            f"{', '.join(bot_ids)} have identical tradeable config "
            f"(same strategy_kind + sub_strategy_extras + scorecard_config). "
            f"Deactivate all but one OR differentiate the parameters before live."
        )
        warnings.append(msg)

    if raise_on_duplicate:
        raise RuntimeError(
            f"registry has {len(duplicates)} duplicate-config bot group(s); "
            f"refusing to load: " + " | ".join(warnings)
        )
    return warnings


def summary_markdown() -> str:
    rows = ["| Bot | Strategy | Kind | Symbol | TF | Status |"]
    rows.append("|-----|----------|------|--------|-----|--------|")
    for a in ASSIGNMENTS:
        status = a.extras.get("promotion_status", "")
        rows.append(f"| {a.bot_id} | {a.strategy_id} | {a.strategy_kind} | {a.symbol} | {a.timeframe} | {status} |")
    return "\n".join(rows)

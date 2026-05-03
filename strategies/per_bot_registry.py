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

if TYPE_CHECKING:
    from eta_engine.obs.drift_monitor import BaselineSnapshot


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
            "promotion_status": "production_candidate",
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
            "promotion_status": "production_candidate",
            "walk_forward_overrides": {"agg_degradation_mode": True, "long_haul_mode": True, "long_haul_min_pos_fraction": 0.38},
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
            "promotion_status": "research_candidate",
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
        },
    ),

    # btc_regime_trend_etf — migrated to proven BTC architecture.
    StrategyAssignment(
        bot_id="btc_regime_trend_etf",
        strategy_id="btc_regime_trend_etf_v1",
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
            "DIAMOND CUT: Migrated from sage_daily_gated (33% WR, -$6.9k) "
            "to proven BTC sweep_reclaim+scorecard architecture."
        ),
        extras={
            "promotion_status": "research_candidate",
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
        },
    ),

    # btc_sage_daily_etf — migrated to proven BTC architecture.
    StrategyAssignment(
        bot_id="btc_sage_daily_etf",
        strategy_id="btc_sage_daily_etf_v1",
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
            "DIAMOND CUT: Migrated from sage_daily_gated (15.4% WR, -$26k) "
            "to proven BTC sweep_reclaim+scorecard. Original OOS +6.00 "
            "Sharpe was real but factory ignores registry config in paper mode."
        ),
        extras={
            "promotion_status": "research_candidate",
            "fleet_corr_partner": "btc_hybrid",
            "daily_loss_limit_pct": 4.0,
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
            "sage_min_conviction": 0.35,
            "sage_min_alignment": 0.35,
            "sage_lookback_bars": 200,
            "orb_range_minutes": 15,
            "orb_config": {
                "range_minutes": 15,
                "require_retest": True,
                "retest_atr_band": 1.0,
                "retest_max_bars": 3,
                "retest_require_close_bounce": True,
                "runaway_atr_mult": 2.5,
                "rr_target": 3.0,
                "atr_stop_mult": 2.0,
                "ema_bias_period": 200,
                "max_trades_per_day": 1,
                "volume_mult": 1.5,
            },
            "per_ticker_optimal": "MNQ",
            "sage_schools_hint": ["Dow", "Wyckoff", "Elliott", "SMC/ICT", "order flow", "trend", "volume_profile", "market_profile", "seasonality"],
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
            "sage_min_conviction": 0.50,
            "sage_min_alignment": 0.45,
            "sage_lookback_bars": 200,
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
                "rr_target": 1.5, "atr_stop_mult": 1.5,
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
            "from session VWAP, targeting VWAP. Afternoon session bias "
            "(13:30-15:30 ET). ORB thrives on trend days; this thrives on "
            "range days. Natural portfolio hedge — when one wins the other "
            "loses, smoothing equity curve."
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
                "rr_target": 1.5, "atr_stop_mult": 1.0,
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
            "promotion_status": "research_candidate",
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
        rationale="DEACTIVATED — BTC is 24/7, no overnight gaps.",
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
                "rr_target": 2.0, "atr_stop_mult": 1.0,
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
            "DIAMOND #17: Cross-asset BTC/ETH ratio divergence on BTC 1h. "
            "Tracks BTC/ETH ratio z-score. The ratio mean-reverts faster "
            "than single-instrument price because both assets share crypto "
            "beta. The alpha is in the spread. Requires ETH reference "
            "data provider."
        ),
        extras={
            "promotion_status": "production_candidate",
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
        rationale="SHADOW: ensemble voting. 25% WR paper soak.",
        extras={
            "promotion_status": "shadow_benchmark",
            "shadow_reason": "25% WR paper soak, losing. Keep as diagnostic while sweep_reclaim+scorecard carries BTC exposure.",
            "min_agreement_count": 3,
            "voters": ["regime_trend", "regime_trend_etf", "sage_daily_gated"],
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
        rationale="SHADOW: SOL sweep_reclaim. 20% WR paper soak.",
        extras={
            "promotion_status": "shadow_benchmark",
            "shadow_reason": "SOL 1h produces 20% WR regardless of strategy. SOL has no standalone edge on 1h — it's a pure BTC beta proxy.",
            "research_candidate": True,
            "level_lookback": 48, "reclaim_window": 3, "min_wick_pct": 0.25,
            "min_volume_z": 0.3, "rr_target": 3.0, "atr_stop_mult": 2.5,
            "max_trades_per_day": 2, "min_bars_between_trades": 12, "warmup_bars": 72,
            "risk_per_trade_pct": 0.005,
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
        rationale="SHADOW: MTF scalp on BTC 5m. 0% WR paper soak.",
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
        rationale="SHADOW: SOL sweep_reclaim. 20% WR paper soak.",
        extras={
            "promotion_status": "shadow_benchmark",
            "shadow_reason": "SOL 1h sweep_reclaim at 20% WR. Same as sol_perp — SOL has no standalone edge.",
            "per_ticker_optimal": "SOL",
            "crypto_native": True,
            "level_lookback": 48, "reclaim_window": 3, "min_wick_pct": 0.30,
            "min_volume_z": 0.5, "rr_target": 3.0, "atr_stop_mult": 2.5,
            "max_trades_per_day": 2, "min_bars_between_trades": 12, "warmup_bars": 72,
            "risk_per_trade_pct": 0.005,
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
        symbol="MNQ1",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=10.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=10,
        rationale="DEACTIVATED — no news/regulatory feed for XRP.",
        extras={"deactivated": True, "deactivation_reason": "no news feed"},
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
            "per_ticker_optimal": "MBT",
            "daily_loss_limit_pct": 4.0,
            "fleet_corr_partner": "btc_hybrid",
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


def is_active(assignment: StrategyAssignment) -> bool:
    return not bool(assignment.extras.get("deactivated", False))


def is_bot_active(bot_id: str) -> bool:
    a = get_for_bot(bot_id)
    return a is not None and is_active(a)


def all_assignments() -> list[StrategyAssignment]:
    return list(ASSIGNMENTS)


def bots() -> list[str]:
    return [a.bot_id for a in ASSIGNMENTS]


def summary_markdown() -> str:
    rows = ["| Bot | Kind | Symbol | TF | Status |"]
    rows.append("|-----|------|--------|-----|--------|")
    for a in ASSIGNMENTS:
        status = a.extras.get("promotion_status", "")
        rows.append(f"| {a.bot_id} | {a.strategy_kind} | {a.symbol} | {a.timeframe} | {status} |")
    return "\n".join(rows)

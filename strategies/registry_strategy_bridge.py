"""Bridge: per_bot_registry strategy assignments → policy_router dispatch.

The DEFAULT_ELIGIBILITY in policy_router.py dispatches the 6 legacy SMC/ICT
strategies. The per_bot_registry.py promotes ORB, sage-gated ORB, DRB,
crypto_orb, sage_daily_gated, ensemble_voting, etc. — strategies with
proven +6 to +10 OOS Sharpes. Until now these were NEVER called at runtime.

This module connects the two worlds:
1. Maps registry strategy_kind → StrategyId enum value
2. Builds a dispatch-ready callable (bars, ctx) → StrategySignal for each kind
3. Returns (eligibility_map, registry_map) that RouterAdapter.push_bar can use

Usage (in RouterAdapter.push_bar):
    from eta_engine.strategies.registry_strategy_bridge import build_registry_dispatch
    eligibility, reg = build_registry_dispatch(self.bot_id)
    decision = dispatch(self.asset, bars, ctx, eligibility=eligibility, registry=reg)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.strategies.models import Bar, Side, StrategyId, StrategySignal

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.strategies.eta_policy import StrategyContext
    from eta_engine.strategies.per_bot_registry import StrategyAssignment

_STRATEGY_CACHE: dict[str, object] = {}
"""Per-bot_id strategy cache. Strategies are expensive to construct
(ORB needs _DayState, sage-gated needs 22-school consensus engine, etc.)
and are called on every bar in the paper-trade loop. Cache by bot_id
so paper_trade_sim over thousands of bars doesn't reconstruct on every tick."""


def _clear_strategy_cache() -> None:
    _STRATEGY_CACHE.clear()
    try:
        from eta_engine.scripts.run_research_grid import _CROSS_ASSET_REF_CACHE, _FUNDING_RATE_PROVIDER_CACHE
        _CROSS_ASSET_REF_CACHE.clear()
        _FUNDING_RATE_PROVIDER_CACHE.clear()
    except ImportError:
        pass


# Public API for invalidation
clear_strategy_cache = _clear_strategy_cache

_KIND_TO_SID: dict[str, StrategyId] = {
    "orb": StrategyId.REGISTRY_ORB,
    "drb": StrategyId.REGISTRY_DRB,
    "orb_sage_gated": StrategyId.REGISTRY_ORB_SAGE_GATED,
    "sage_consensus": StrategyId.REGISTRY_SAGE_CONSENSUS,
    "crypto_orb": StrategyId.REGISTRY_CRYPTO_ORB,
    "crypto_trend": StrategyId.REGISTRY_CRYPTO_TREND,
    "crypto_regime_trend": StrategyId.REGISTRY_CRYPTO_REGRESSION,
    "sage_daily_gated": StrategyId.REGISTRY_SAGE_DAILY_GATED,
    "ensemble_voting": StrategyId.REGISTRY_ENSEMBLE_VOTING,
    "crypto_macro_confluence": StrategyId.REGISTRY_CRYPTO_MACRO_CONFLUENCE,
    "compression_breakout": StrategyId.REGISTRY_COMPRESSION_BREAKOUT,
    "crypto_meanrev": StrategyId.REGISTRY_CRYPTO_MEANREV,
    "confluence": StrategyId.REGISTRY_CONFLUENCE,
    "htf_routed": StrategyId.REGISTRY_HTF_ROUTED,
    "confluence_scorecard": StrategyId.REGISTRY_CONFLUENCE_SCORECARD,
    "sweep_reclaim": StrategyId.REGISTRY_SWEEP_RECLAIM,
    "regime_gated": StrategyId.REGISTRY_REGIME_GATED,
    "mtf_scalp": StrategyId.REGISTRY_MTF_SCALP,
    "anchor_sweep": StrategyId.REGISTRY_ANCHOR_SWEEP,
}


def _strategy_id_for(assignment: StrategyAssignment) -> StrategyId | None:
    return _KIND_TO_SID.get(assignment.strategy_kind)


def _build_callable_for_assignment(
    assignment: StrategyAssignment,
) -> Callable[..., StrategySignal] | None:
    kind = assignment.strategy_kind
    extras = dict(assignment.extras)

    # Use the canonical strategy factory from run_research_grid — it
    # already handles every strategy_kind with the correct config
    # construction. Avoid duplicating per-kind logic here.
    strategy = None
    try:
        from eta_engine.scripts.run_research_grid import _build_strategy_factory

        # Bug fix 2026-05-05: confluence_scorecard is handled at the
        # CELL level in run_research_grid (not _build_strategy_factory)
        # because it wraps a sub-strategy.  Bridge dispatch needs to
        # mirror that behavior — extract the sub_strategy_kind from
        # extras, build the sub-strategy factory, then wrap with
        # ConfluenceScorecardStrategy.  Was blocking vwap_mr_btc and
        # volume_profile_btc from running through the harness.
        if kind == "confluence_scorecard":
            sub_kind = str(extras.get("sub_strategy_kind") or "")
            sub_extras = extras.get("sub_strategy_extras") or {}
            sc_raw = extras.get("scorecard_config") or {}
            if "per_ticker_optimal" in extras and isinstance(sub_extras, dict):
                sub_extras.setdefault("per_ticker_optimal", extras["per_ticker_optimal"])
            if not sub_kind:
                raise ValueError("confluence_scorecard requires sub_strategy_kind in extras")

            from eta_engine.strategies.confluence_scorecard import (
                ConfluenceScorecardConfig,
                ConfluenceScorecardStrategy,
            )
            sc_cfg = ConfluenceScorecardConfig(
                min_score=int(sc_raw.get("min_score", 2)),
                a_plus_score=int(sc_raw.get("a_plus_score", 3)),
                a_plus_size_mult=float(sc_raw.get("a_plus_size_mult", 1.3)),
                fast_ema=int(sc_raw.get("fast_ema", 21)),
                mid_ema=int(sc_raw.get("mid_ema", 50)),
                slow_ema=int(sc_raw.get("slow_ema", 100)),
            )
            sub_factory = _build_strategy_factory(sub_kind, dict(sub_extras))
            sub_strategy = sub_factory()
            strategy = ConfluenceScorecardStrategy(sub_strategy, sc_cfg)
        else:
            factory = _build_strategy_factory(kind, extras)

            # Attach daily sage verdicts for sage_daily_gated strategies.
            # The research_grid factory builds the strategy but doesn't
            # attach the verdict provider — wire it here so bridge dispatch
            # has REAL sage gating, not passthrough.
            if kind == "sage_daily_gated":
                symbol = str(assignment.symbol) if assignment else "BTC"
                try:
                    from eta_engine.scripts.run_research_grid import (
                        _with_daily_sage_provider,
                    )

                    inst_class = extras.get("instrument_class", "crypto")
                    factory = _with_daily_sage_provider(
                        factory,
                        symbol=symbol,
                        instrument_class=inst_class,
                    )
                except (ValueError, ImportError):
                    pass

            strategy = factory()
    except (ValueError, ImportError):
        pass

    # Fallback: some kinds need providers (sage daily verdicts,
    # ensemble voter wiring, macro ETF data). Build them per-kind.
    if strategy is None:
        strategy = _build_strategy_fallback(kind, extras)

    if strategy is None:
        return None

    # Wrap with EdgeAmplifier when an assignment carries an explicit edge
    # config. Bots without edge_config stay on their raw strategy.
    edge_raw = extras.get("edge_config")
    if edge_raw is not None:
        try:
            from eta_engine.strategies.edge_layers import EdgeAmplifier, EdgeAmplifierConfig

            if isinstance(edge_raw, dict):
                defaults = EdgeAmplifierConfig()
                ec = EdgeAmplifierConfig(
                    enable_session_gate=bool(edge_raw.get("enable_session_gate", defaults.enable_session_gate)),
                    timezone_name=str(edge_raw.get("timezone_name", defaults.timezone_name)),
                    is_crypto=bool(edge_raw.get("is_crypto", defaults.is_crypto)),
                    strategy_mode=str(edge_raw.get("strategy_mode", defaults.strategy_mode)),
                    enable_exhaustion_gate=bool(
                        edge_raw.get("enable_exhaustion_gate", defaults.enable_exhaustion_gate),
                    ),
                    exhaustion_max_trend=int(edge_raw.get("exhaustion_max_trend", defaults.exhaustion_max_trend)),
                    exhaustion_veto=int(edge_raw.get("exhaustion_veto", defaults.exhaustion_veto)),
                    exhaustion_counter=int(edge_raw.get("exhaustion_counter", defaults.exhaustion_counter)),
                    enable_absorption_gate=bool(
                        edge_raw.get("enable_absorption_gate", defaults.enable_absorption_gate),
                    ),
                    absorption_vol_z_min=float(edge_raw.get("absorption_vol_z_min", defaults.absorption_vol_z_min)),
                    absorption_range_z_max=float(
                        edge_raw.get("absorption_range_z_max", defaults.absorption_range_z_max),
                    ),
                    enable_drift_boost=bool(edge_raw.get("enable_drift_boost", defaults.enable_drift_boost)),
                    drift_vol_z_min=float(edge_raw.get("drift_vol_z_min", defaults.drift_vol_z_min)),
                    drift_clv_min=float(edge_raw.get("drift_clv_min", defaults.drift_clv_min)),
                    drift_recency_bars=int(edge_raw.get("drift_recency_bars", defaults.drift_recency_bars)),
                    enable_structural_stops=bool(
                        edge_raw.get("enable_structural_stops", defaults.enable_structural_stops),
                    ),
                    structural_lookback=int(edge_raw.get("structural_lookback", defaults.structural_lookback)),
                    structural_buffer_mult=float(
                        edge_raw.get("structural_buffer_mult", defaults.structural_buffer_mult),
                    ),
                    enable_vol_sizing=bool(edge_raw.get("enable_vol_sizing", defaults.enable_vol_sizing)),
                    vol_regime_lookback=int(edge_raw.get("vol_regime_lookback", defaults.vol_regime_lookback)),
                    vol_atr_period=int(edge_raw.get("vol_atr_period", defaults.vol_atr_period)),
                )
            elif isinstance(edge_raw, str) and edge_raw == "mnq_futures":
                from eta_engine.strategies.edge_layers import mnq_futures_preset
                ec = mnq_futures_preset()
            elif isinstance(edge_raw, str) and edge_raw == "btc_crypto":
                from eta_engine.strategies.edge_layers import btc_crypto_preset
                ec = btc_crypto_preset()
            elif isinstance(edge_raw, str) and edge_raw == "eth_crypto":
                from eta_engine.strategies.edge_layers import eth_crypto_preset
                ec = eth_crypto_preset()
            elif isinstance(edge_raw, str) and edge_raw == "sol_crypto":
                from eta_engine.strategies.edge_layers import sol_crypto_preset
                ec = sol_crypto_preset()
            else:
                ec = EdgeAmplifierConfig()

            strategy = EdgeAmplifier(strategy, ec)
        except Exception:
            pass

    # Wrap with AlphaSniper if cross-symbol/tape-reading config is present.
    # DISABLED during integration — re-enable with extras["alpha_enabled"] = True.
    # Wrap with AlphaSniper — always on for tape reading (default=True unless
    # extras explicitly sets "alpha_sniper": False). Tape reading is zero-cost
    # same-symbol bar structure analysis. Intermarket confirmation is opt-in
    # via provider attachment. A/B testing: set "alpha_sniper": False to disable.
    alpha_raw = extras.get("alpha_sniper")
    if alpha_raw is not False:  # defaults to True — only False disables
        try:
            from eta_engine.strategies.alpha_sniper import AlphaSniper, AlphaSniperConfig

            if isinstance(alpha_raw, dict):
                ac = AlphaSniperConfig(
                    enable_tape_reading=bool(alpha_raw.get("enable_tape_reading", True)),
                    enable_intermarket=bool(alpha_raw.get("enable_intermarket", True)),
                    enable_spread_check=bool(alpha_raw.get("enable_spread_check", False)),
                    max_spread_pct=float(alpha_raw.get("max_spread_pct", 0.20)),
                    enable_divergence_check=bool(alpha_raw.get("enable_divergence_check", True)),
                    divergence_atr_mult=float(alpha_raw.get("divergence_atr_mult", 0.5)),
                    divergence_lookback=int(alpha_raw.get("divergence_lookback", 5)),
                    min_peer_confirmation=float(alpha_raw.get("min_peer_confirmation", 0.5)),
                )
            else:
                ac = AlphaSniperConfig()

            sniper = AlphaSniper(strategy, ac)
            strategy = sniper
        except Exception:
            pass

    return _wrap_strategy(strategy)


def _build_strategy_fallback(kind: str, extras: dict) -> object | None:
    """Per-kind strategy construction when _build_strategy_factory isn't available.
    Returns the RAW strategy object (not wrapped with _wrap_strategy)."""
    if kind == "sage_daily_gated":
        from eta_engine.strategies.crypto_macro_confluence_strategy import (
            CryptoMacroConfluenceConfig,
        )
        from eta_engine.strategies.crypto_regime_trend_strategy import (
            CryptoRegimeTrendConfig,
        )
        from eta_engine.strategies.sage_daily_gated_strategy import (
            SageDailyGatedConfig,
            SageDailyGatedStrategy,
        )

        # Read the base strategy config from extras — same keys as crypto_regime_trend
        trend_raw = extras.get("crypto_regime_trend_config", {})
        base_trend = CryptoRegimeTrendConfig(
            regime_ema=trend_raw.get("regime_ema", 100),
            pullback_ema=trend_raw.get("pullback_ema", 21),
            pullback_tolerance_pct=trend_raw.get("pullback_tolerance_pct", 3.0),
            atr_stop_mult=trend_raw.get("atr_stop_mult", 2.0),
            rr_target=trend_raw.get("rr_target", 2.5),
            risk_per_trade_pct=trend_raw.get("risk_per_trade_pct", 0.01),
            min_bars_between_trades=trend_raw.get("min_bars_between_trades", 12),
            max_trades_per_day=trend_raw.get("max_trades_per_day", 3),
            warmup_bars=trend_raw.get("warmup_bars", 220),
        )
        macro_cfg = CryptoMacroConfluenceConfig(base=base_trend)
        min_conv = float(extras.get("min_daily_conviction", 0.30))
        strict = bool(extras.get("strict_mode", False))
        cfg = SageDailyGatedConfig(base=macro_cfg, min_daily_conviction=min_conv, strict_mode=strict)
        return SageDailyGatedStrategy(cfg)

    if kind == "crypto_regime_trend":
        from eta_engine.strategies.crypto_regime_trend_strategy import (
            CryptoRegimeTrendConfig,
            CryptoRegimeTrendStrategy,
        )

        cfg_raw = extras.get("crypto_regime_trend_config", {})
        cfg = CryptoRegimeTrendConfig(
            regime_ema=cfg_raw.get("regime_ema", 100),
            pullback_ema=cfg_raw.get("pullback_ema", 21),
            pullback_tolerance_pct=cfg_raw.get("pullback_tolerance_pct", 3.0),
            atr_stop_mult=cfg_raw.get("atr_stop_mult", 2.0),
            rr_target=cfg_raw.get("rr_target", 3.0),
        )
        return CryptoRegimeTrendStrategy(cfg)

    if kind == "crypto_macro_confluence":
        from eta_engine.strategies.crypto_macro_confluence_strategy import (
            CryptoMacroConfluenceConfig,
            CryptoMacroConfluenceStrategy,
        )
        from eta_engine.strategies.crypto_regime_trend_strategy import (
            CryptoRegimeTrendConfig,
        )

        trend_raw = extras.get("crypto_regime_trend_config", {})
        base_cfg = CryptoRegimeTrendConfig(
            regime_ema=trend_raw.get("regime_ema", 100),
            pullback_ema=trend_raw.get("pullback_ema", 21),
            pullback_tolerance_pct=trend_raw.get("pullback_tolerance_pct", 3.0),
            atr_stop_mult=trend_raw.get("atr_stop_mult", 2.0),
            rr_target=trend_raw.get("rr_target", 3.0),
            risk_per_trade_pct=trend_raw.get("risk_per_trade_pct", 0.01),
            min_bars_between_trades=trend_raw.get("min_bars_between_trades", 12),
            max_trades_per_day=trend_raw.get("max_trades_per_day", 3),
            warmup_bars=trend_raw.get("warmup_bars", 220),
        )
        cfg = CryptoMacroConfluenceConfig(base=base_cfg)
        return CryptoMacroConfluenceStrategy(cfg)

    if kind == "compression_breakout":
        from eta_engine.strategies.compression_breakout_strategy import (
            CompressionBreakoutConfig,
            CompressionBreakoutStrategy,
        )

        preset_name = extras.get("compression_preset", "default")
        if preset_name == "eth":
            cfg = CompressionBreakoutConfig(
                bb_period=30, bb_width_max_percentile=0.60,
                min_close_location=0.40, min_volume_z=0.2,
                breakout_lookback=12, min_bars_between_trades=12,
                rr_target=2.0, atr_stop_mult=1.5,
            )
        elif preset_name == "btc":
            cfg = CompressionBreakoutConfig(
                bb_period=20, bb_width_max_percentile=0.50,
                min_close_location=0.50, min_volume_z=0.3,
                breakout_lookback=24, min_bars_between_trades=24,
                rr_target=2.5, atr_stop_mult=2.0,
            )
        else:
            cfg = CompressionBreakoutConfig()
        return CompressionBreakoutStrategy(cfg)

    if kind == "crypto_trend":
        from eta_engine.strategies.crypto_trend_strategy import (
            CryptoTrendConfig,
            CryptoTrendStrategy,
        )
        return CryptoTrendStrategy(CryptoTrendConfig())

    if kind == "crypto_meanrev":
        from eta_engine.strategies.crypto_meanrev_strategy import (
            CryptoMeanRevConfig,
            CryptoMeanRevStrategy,
        )
        return CryptoMeanRevStrategy(CryptoMeanRevConfig())

    if kind == "ensemble_voting":
        from eta_engine.strategies.ensemble_voting_strategy import (
            EnsembleVotingConfig,
            EnsembleVotingStrategy,
        )

        voter_names = extras.get("voters", [])
        sub_strategies: list = []
        voter_kind_map = {
            "regime_trend": "crypto_regime_trend",
            "regime_trend_etf": "crypto_macro_confluence",
            "sage_daily_gated": "sage_daily_gated",
        }
        for name in voter_names:
            v_kind = voter_kind_map.get(name, name)
            try:
                from eta_engine.scripts.run_research_grid import _build_strategy_factory
                factory = _build_strategy_factory(v_kind, extras)
                sub_strategies.append((name, factory()))
            except (ValueError, ImportError):
                pass
        if not sub_strategies:
            return None

        cfg = EnsembleVotingConfig(
            min_agreement_count=int(extras.get("min_agreement_count", 2)),
        )
        return EnsembleVotingStrategy(sub_strategies, cfg)

    if kind == "sage_consensus":
        from eta_engine.strategies.sage_consensus_strategy import (
            SageConsensusConfig,
            SageConsensusStrategy,
        )

        cfg = SageConsensusConfig(
            min_conviction=float(extras.get("sage_min_conviction", 0.75)),
        )
        return SageConsensusStrategy(cfg)

    if kind == "htf_routed":
        from eta_engine.strategies.htf_routed_strategy import (
            HtfRoutedConfig,
            HtfRoutedStrategy,
        )

        cfg = HtfRoutedConfig()
        stacked = extras.get("entry", extras)
        if isinstance(stacked, dict):
            tf_params = stacked.get("trend_follow", {}).get("params", {})
            mr_params = stacked.get("mean_revert", {}).get("params", {})
            if isinstance(tf_params, dict) and tf_params:
                cfg.trend_follow_config = tf_params
            if isinstance(mr_params, dict) and mr_params:
                cfg.mean_revert_config = mr_params
        return HtfRoutedStrategy(cfg)

    if kind == "sweep_reclaim":
        from eta_engine.strategies.sweep_reclaim_strategy import (
            SweepReclaimConfig,
            SweepReclaimStrategy,
        )

        cfg = SweepReclaimConfig(
            level_lookback=int(extras.get("level_lookback", 20)),
            reclaim_window=int(extras.get("reclaim_window", 3)),
            min_wick_pct=float(extras.get("min_wick_pct", extras.get("wick_pct_min", 0.6))),
            min_volume_z=float(extras.get("min_volume_z", extras.get("volume_z_min", 0.8))),
            rr_target=float(extras.get("rr_target", 2.0)),
            atr_stop_mult=float(extras.get("atr_stop_mult", 1.5)),
            max_trades_per_day=int(extras.get("max_trades_per_day", 3)),
        )
        return SweepReclaimStrategy(cfg)

    if kind == "regime_gated":
        from eta_engine.strategies.regime_gated_strategy import (
            RegimeGatedConfig,
            RegimeGatedStrategy,
        )
        return RegimeGatedStrategy(RegimeGatedConfig())

    if kind == "mtf_scalp":
        from eta_engine.strategies.mtf_scalp_strategy import (
            MtfScalpConfig,
            MtfScalpStrategy,
        )

        mtf_raw = extras.get("mtf_scalp_config", {})
        cfg = MtfScalpConfig(
            htf_bars_per_aggregate=int(mtf_raw.get("htf_bars_per_aggregate", 15)),
            htf_ema_period=int(mtf_raw.get("htf_ema_period", 200)),
            htf_atr_period=int(mtf_raw.get("htf_atr_period", 14)),
            htf_atr_pct_min=float(mtf_raw.get("htf_atr_pct_min", 0.05)),
            htf_atr_pct_max=float(mtf_raw.get("htf_atr_pct_max", 0.50)),
            ltf_recent_high_lookback=int(mtf_raw.get("ltf_recent_high_lookback", 5)),
            ltf_fast_ema_period=int(mtf_raw.get("ltf_fast_ema_period", 9)),
            ltf_atr_period=int(mtf_raw.get("ltf_atr_period", 14)),
            ltf_atr_stop_mult=float(mtf_raw.get("ltf_atr_stop_mult", 1.5)),
            ltf_rr_target=float(mtf_raw.get("ltf_rr_target", 2.0)),
            risk_per_trade_pct=float(mtf_raw.get("risk_per_trade_pct", 0.005)),
            min_bars_between_trades=int(mtf_raw.get("min_bars_between_trades", 30)),
            max_trades_per_day=int(mtf_raw.get("max_trades_per_day", 6)),
            warmup_bars=int(mtf_raw.get("warmup_bars", 3000)),
            allow_long=bool(mtf_raw.get("allow_long", True)),
            allow_short=bool(mtf_raw.get("allow_short", True)),
        )
        return MtfScalpStrategy(cfg)

    if kind == "confluence_scorecard":
        from eta_engine.strategies.confluence_scorecard import (
            ConfluenceScorecardConfig,
            ConfluenceScorecardStrategy,
        )

        sc_cfg_raw = extras.get("scorecard_config", {})
        if isinstance(sc_cfg_raw, dict):
            sc_cfg = ConfluenceScorecardConfig(
                min_score=int(sc_cfg_raw.get("min_score", 3)),
                a_plus_score=int(sc_cfg_raw.get("a_plus_score", 4)),
                a_plus_size_mult=float(sc_cfg_raw.get("a_plus_size_mult", 1.5)),
                fast_ema=int(sc_cfg_raw.get("fast_ema", 9)),
                mid_ema=int(sc_cfg_raw.get("mid_ema", 21)),
                slow_ema=int(sc_cfg_raw.get("slow_ema", 50)),
            )
        else:
            sc_cfg = ConfluenceScorecardConfig()

        sub_kind = extras.get("sub_strategy_kind", "")
        if sub_kind:
            try:
                from eta_engine.scripts.run_research_grid import (
                    _build_strategy_factory,
                    _with_cross_asset_ref_provider,
                    _with_funding_rate_provider,
                )
                sub_extras = dict(extras.get("sub_strategy_extras", {}))
                if "per_ticker_optimal" in extras and "per_ticker_optimal" not in sub_extras:
                    sub_extras["per_ticker_optimal"] = extras["per_ticker_optimal"]
                sub_factory = _build_strategy_factory(sub_kind, sub_extras)

                if sub_kind == "cross_asset_divergence":
                    ref_asset = str(sub_extras.get("reference_asset", extras.get("per_ticker_optimal", ""))).upper()
                    bot_symbol = str(extras.get("per_ticker_optimal", "MNQ")).upper()
                    if ref_asset == "ES1" or "MNQ" in bot_symbol or "NQ" in bot_symbol:
                        sub_factory = _with_cross_asset_ref_provider(
                            sub_factory, bot_symbol=bot_symbol,
                            ref_symbol="ES1", ref_timeframe="5m",
                        )
                    elif ref_asset == "ETH" or "BTC" in bot_symbol:
                        sub_factory = _with_cross_asset_ref_provider(
                            sub_factory, bot_symbol=bot_symbol,
                            ref_symbol="ETH", ref_timeframe="1h",
                        )
                elif sub_kind == "funding_rate":
                    sub_factory = _with_funding_rate_provider(sub_factory)

                sub_strategy = sub_factory()
            except (ValueError, ImportError):
                sub_strategy = None
            if sub_strategy is not None and hasattr(sub_strategy, "maybe_enter"):
                return ConfluenceScorecardStrategy(sub_strategy, sc_cfg)

        return ConfluenceScorecardStrategy(None, sc_cfg)

    if kind == "mbt_funding_basis":
        from eta_engine.feeds.cme_basis_provider import build_basis_provider
        from eta_engine.strategies.mbt_funding_basis_strategy import (
            MBTFundingBasisConfig,
            MBTFundingBasisStrategy,
        )

        cfg_raw = extras.get("mbt_funding_basis_config", {})
        cfg = (
            MBTFundingBasisConfig(**cfg_raw)
            if isinstance(cfg_raw, dict) and cfg_raw
            else MBTFundingBasisConfig()
        )
        # Basis-provider dispatch. ``internal_log_return`` (the legacy
        # default) returns None and the strategy keeps its silent built-in
        # fallback. ``log_return_fallback`` wires an explicitly-named
        # provider so audits can confirm the proxy is in use. ``cme_basis``
        # wires the real spot-vs-futures provider; soft-fails to None
        # when the spot CSV is missing.
        provider_kind = str(extras.get("basis_provider_kind", "internal_log_return"))
        spot_csv = extras.get("basis_spot_csv")
        try:
            provider = build_basis_provider(
                provider_kind,
                spot_csv=spot_csv if isinstance(spot_csv, (str)) else None,
            )
        except ValueError:
            provider = None
        return MBTFundingBasisStrategy(cfg, basis_provider=provider)

    if kind == "mbt_overnight_gap":
        from eta_engine.strategies.mbt_overnight_gap_strategy import (
            MBTOvernightGapConfig,
            MBTOvernightGapStrategy,
        )

        cfg_raw = extras.get("mbt_overnight_gap_config", {})
        cfg = (
            MBTOvernightGapConfig(**cfg_raw)
            if isinstance(cfg_raw, dict) and cfg_raw
            else MBTOvernightGapConfig()
        )
        return MBTOvernightGapStrategy(cfg)

    if kind == "met_rth_orb":
        from eta_engine.strategies.met_rth_orb_strategy import (
            METRTHORBConfig,
            METRTHORBStrategy,
        )

        cfg_raw = extras.get("met_rth_orb_config", {})
        cfg = (
            METRTHORBConfig(**cfg_raw)
            if isinstance(cfg_raw, dict) and cfg_raw
            else METRTHORBConfig()
        )
        return METRTHORBStrategy(cfg)

    return None


def _wrap_strategy(
    strategy: object,
) -> Callable[..., StrategySignal]:
    def _evaluate(bars: list[Bar], ctx: StrategyContext) -> StrategySignal:
        if len(bars) < 2:
            return StrategySignal(
                strategy=StrategyId.REGISTRY_ORB,
                side=Side.FLAT,
                rationale_tags=("insufficient_bars",),
            )
        try:
            from eta_engine.backtest.models import BacktestConfig

            current = bars[-1]
            history = bars[:-1]
            hist_bar_data = _to_bar_data_list(history)
            current_bar_data = _to_bar_data(current)
            be_cfg = BacktestConfig(
                start_date=current_bar_data.timestamp,
                end_date=current_bar_data.timestamp,
                symbol=current_bar_data.symbol,
                initial_equity=10000.0,
                risk_per_trade_pct=0.01,
            )
            opened = strategy.maybe_enter(
                current_bar_data,
                hist_bar_data,
                equity=10000.0,
                config=be_cfg,
            )
            if opened is None:
                return StrategySignal(
                    strategy=StrategyId.REGISTRY_ORB,
                    side=Side.FLAT,
                    rationale_tags=("no_signal",),
                )
            side = Side.LONG if opened.side.upper() == "BUY" else Side.SHORT
            return StrategySignal(
                strategy=StrategyId.REGISTRY_ORB,
                side=side,
                entry=float(opened.entry_price),
                stop=float(opened.stop),
                target=float(opened.target),
                confidence=float(getattr(opened, "confluence", 5.0)),
                risk_mult=float(getattr(opened, "leverage", 1.0)),
            )
        except Exception:
            return StrategySignal(
                strategy=StrategyId.REGISTRY_ORB,
                side=Side.FLAT,
                rationale_tags=("bridge_error",),
            )
    return _evaluate


def _to_bar_data(bar: Bar) -> Any:  # noqa: ANN401
    from datetime import UTC, datetime

    from eta_engine.core.data_pipeline import BarData

    ts_raw = bar.ts if isinstance(bar.ts, int) else 0
    try:
        ts_dt = datetime.fromtimestamp(ts_raw / 1000.0, tz=UTC)
    except (ValueError, OSError, OverflowError):
        ts_dt = datetime.now(tz=UTC)

    return BarData(
        timestamp=ts_dt,
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
        volume=float(bar.volume) if hasattr(bar, "volume") else 0.0,
        symbol="",
    )


def _to_bar_data_list(bars: list[Bar]) -> list[Any]:  # noqa: ANN401
    return [_to_bar_data(b) for b in bars]


def _passthrough(bars: list[Bar], ctx: StrategyContext) -> StrategySignal:
    return StrategySignal(
        strategy=StrategyId.REGISTRY_CONFLUENCE,
        side=Side.FLAT,
        rationale_tags=("bridge_not_yet_wired",),
    )


def build_registry_dispatch(
    bot_id: str,
) -> tuple[dict[str, tuple[StrategyId, ...]], dict[StrategyId, Callable[..., StrategySignal]]] | None:
    from eta_engine.strategies.per_bot_registry import get_for_bot, is_bot_active

    if not is_bot_active(bot_id):
        return None

    assignment = get_for_bot(bot_id)
    if assignment is None:
        return None

    sid = _strategy_id_for(assignment)
    if sid is None:
        return None

    if bot_id in _STRATEGY_CACHE:
        callable_fn = _STRATEGY_CACHE[bot_id]
    else:
        callable_fn = _build_callable_for_assignment(assignment)
        if callable_fn is None:
            return None
        _STRATEGY_CACHE[bot_id] = callable_fn

    eligibility = {assignment.symbol.upper(): (sid,)}
    registry = {sid: callable_fn}
    return eligibility, registry

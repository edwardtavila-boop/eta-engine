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
    try:
        from eta_engine.scripts.run_research_grid import _build_strategy_factory

        factory = _build_strategy_factory(kind, extras)

        # Attach daily sage verdicts for sage_daily_gated strategies.
        # The research_grid factory builds the strategy but doesn't
        # attach the verdict provider — that's done separately via
        # _with_daily_sage_provider. Wire it here so bridge dispatch
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
        return _wrap_strategy(strategy)
    except (ValueError, ImportError):
        pass

    # Fallback: some kinds need providers (sage daily verdicts,
    # ensemble voter wiring, macro ETF data). Build them per-kind
    # with best-effort defaults. These will degrade gracefully when
    # providers are absent.
    if kind == "sage_daily_gated":
        from eta_engine.strategies.sage_daily_gated_strategy import (
            SageDailyGatedConfig,
            SageDailyGatedStrategy,
        )

        min_conv = float(extras.get("min_daily_conviction", 0.30))
        strict = bool(extras.get("strict_mode", False))
        cfg = SageDailyGatedConfig(min_daily_conviction=min_conv, strict_mode=strict)
        return _wrap_strategy(SageDailyGatedStrategy(cfg))

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
        return _wrap_strategy(CryptoRegimeTrendStrategy(cfg))

    if kind == "crypto_macro_confluence":
        from eta_engine.strategies.crypto_macro_confluence_strategy import (
            CryptoMacroConfluenceConfig,
            CryptoMacroConfluenceStrategy,
        )

        cfg_raw = extras.get("macro_confluence_config", {})
        cfg = CryptoMacroConfluenceConfig(
            require_etf_flow_alignment=cfg_raw.get("require_etf_flow_alignment", False),
        )
        return _wrap_strategy(CryptoMacroConfluenceStrategy(cfg))

    if kind == "compression_breakout":
        from eta_engine.strategies.compression_breakout_strategy import (
            CompressionBreakoutConfig,
            CompressionBreakoutStrategy,
        )

        preset_name = extras.get("compression_preset", "default")
        if preset_name == "eth":
            cfg = CompressionBreakoutConfig(
                bb_period=20, bb_width_pct_max=0.60,
                close_location_min=0.40, volume_z_min=0.2,
                breakout_lookback=12, cooldown_bars=12,
                rr_target=2.5, atr_stop_mult=1.8,
            )
        elif preset_name == "btc":
            cfg = CompressionBreakoutConfig(
                bb_period=20, bb_width_pct_max=0.50,
                close_location_min=0.50, volume_z_min=0.3,
                breakout_lookback=24, cooldown_bars=24,
                rr_target=2.5, atr_stop_mult=2.0,
            )
        else:
            cfg = CompressionBreakoutConfig()
        return _wrap_strategy(CompressionBreakoutStrategy(cfg))

    if kind == "crypto_trend":
        from eta_engine.strategies.crypto_trend_strategy import (
            CryptoTrendConfig,
            CryptoTrendStrategy,
        )

        cfg = CryptoTrendConfig()
        return _wrap_strategy(CryptoTrendStrategy(cfg))

    if kind == "crypto_meanrev":
        from eta_engine.strategies.crypto_meanrev_strategy import (
            CryptoMeanRevConfig,
            CryptoMeanRevStrategy,
        )

        cfg = CryptoMeanRevConfig()
        return _wrap_strategy(CryptoMeanRevStrategy(cfg))

    if kind == "ensemble_voting":
        from eta_engine.strategies.ensemble_voting_strategy import (
            EnsembleVotingConfig,
            EnsembleVotingStrategy,
        )

        # Build actual voter sub-strategies from the registry voter names.
        # Each voter is a strategy_kind that the bridge knows how to build.
        voter_names = extras.get("voters", [])
        sub_strategies: list = []
        voter_kind_map = {
            "regime_trend": "crypto_regime_trend",
            "regime_trend_etf": "crypto_macro_confluence",
            "sage_daily_gated": "sage_daily_gated",
        }
        for name in voter_names:
            kind = voter_kind_map.get(name, name)
            try:
                from eta_engine.scripts.run_research_grid import _build_strategy_factory
                factory = _build_strategy_factory(kind, extras)
                sub_strategies.append((name, factory()))
            except (ValueError, ImportError):
                pass
        if not sub_strategies:
            return _passthrough

        cfg = EnsembleVotingConfig(
            min_agreement_count=int(extras.get("min_agreement_count", 2)),
        )
        return _wrap_strategy(EnsembleVotingStrategy(sub_strategies, cfg))

    if kind == "sage_consensus":
        from eta_engine.strategies.sage_consensus_strategy import (
            SageConsensusConfig,
            SageConsensusStrategy,
        )

        cfg = SageConsensusConfig(
            min_conviction=float(extras.get("sage_min_conviction", 0.75)),
        )
        return _wrap_strategy(SageConsensusStrategy(cfg))

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
                cfg.trend_follow_config = tf_params  # type: ignore[assignment]
            if isinstance(mr_params, dict) and mr_params:
                cfg.mean_revert_config = mr_params  # type: ignore[assignment]
        return _wrap_strategy(HtfRoutedStrategy(cfg))

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
        return _wrap_strategy(SweepReclaimStrategy(cfg))

    if kind == "regime_gated":
        from eta_engine.strategies.regime_gated_strategy import (
            RegimeGatedConfig,
            RegimeGatedStrategy,
        )

        cfg = RegimeGatedConfig()
        return _wrap_strategy(RegimeGatedStrategy(cfg))

    if kind == "mtf_scalp":
        from eta_engine.strategies.mtf_scalp_strategy import (
            MtfScalpConfig,
            MtfScalpStrategy,
        )

        cfg = MtfScalpConfig()
        return _wrap_strategy(MtfScalpStrategy(cfg))

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
            fake = type("_Fake", (), {"strategy_kind": sub_kind, "extras": extras.get("sub_strategy_extras", {})})()
            sub_callable = _build_callable_for_assignment(fake)
            if sub_callable is None:
                return None
            # The sub_callable is a wrapper — we need the raw strategy object.
            # For composition, build the sub-strategy directly.
            try:
                from eta_engine.scripts.run_research_grid import _build_strategy_factory
                sub_factory = _build_strategy_factory(sub_kind, extras.get("sub_strategy_extras", {}))
                sub_strategy = sub_factory()
            except (ValueError, ImportError):
                sub_strategy = None
            if sub_strategy is not None and hasattr(sub_strategy, "maybe_enter"):
                return _wrap_strategy(ConfluenceScorecardStrategy(sub_strategy, sc_cfg))

        return _wrap_strategy(ConfluenceScorecardStrategy(None, sc_cfg))

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

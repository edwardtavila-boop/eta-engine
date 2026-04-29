"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_research_grid
=========================================================
Sweep a matrix of (symbol, timeframe, scorer, threshold, gate) and
emit one comparison table so we can see at a glance which slice of
the configuration space the strategy holds up on.

Why this exists
---------------
We've been running walk-forward one config at a time, copying
numbers between research-log entries by hand. That doesn't scale
once we have 33 datasets × 2 scorers × multiple gate options. This
harness runs the matrix in one shot and writes a single dated
markdown report that's directly comparable across rows.

Default matrix
--------------
Picks the longest-history dataset per (symbol, timeframe) via the
data library. Tries the global scorer + the MNQ-tuned scorer.
Tries gated + ungated. Six configs total by default; override the
list at the top of ``main()`` for a custom run.

Output
------
* stdout — single comparison table
* ``docs/research_log/research_grid_<utc-stamp>.md`` — the same
  table in markdown, plus per-row details. Re-runnable; the
  filename contains a timestamp so previous runs are preserved.

This becomes the "did anything regress" smoke test for any change
to the engines, scorers, or feature pipeline.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


@dataclass(frozen=True)
class ResearchCell:
    """One row in the research matrix."""

    label: str
    symbol: str
    timeframe: str
    scorer_name: str  # "global" or "mnq"
    threshold: float
    block_regimes: frozenset[str] | None
    window_days: int
    step_days: int
    min_trades_per_window: int
    strategy_kind: str = "confluence"  # canonical per-bot strategy selector
    # Per-bot strategy knobs forwarded from per_bot_registry.extras.
    # Resolved through the shared strategy factory so registry-backed
    # tuning stays aligned across research, paper soak, and drift checks.
    extras: dict[str, object] = field(default_factory=dict)


@dataclass
class CellResult:
    cell: ResearchCell
    n_windows: int
    n_positive_oos: int
    agg_is_sharpe: float
    agg_oos_sharpe: float
    avg_oos_degradation: float
    deflated_sharpe: float
    fold_dsr_median: float
    fold_dsr_pass_fraction: float
    pass_gate: bool
    note: str = ""


def _filter_extras(extras: dict[str, object], prefix: str) -> dict[str, object]:
    """Resolve the per-strategy config dict from a registry extras blob.

    Two supported shapes:

    1. Nested config (preferred):
         extras={"crypto_orb_config": {"range_minutes": 240, ...}}
       Returns ``{"range_minutes": 240, ...}``.

    2. Flat prefixed keys (legacy):
         extras={"crypto_orb_range_minutes": 240}
       Returns ``{"range_minutes": 240}``.

    The nested form is preferred because it round-trips to JSON
    cleanly and groups all knobs for one strategy together. Flat
    keys remain supported so older registry rows don't break.
    Unknown / non-strategy keys (e.g. ``deactivated``,
    ``alt_strategy_kind``) are ignored.
    """
    nested_key = f"{prefix}_config"
    nested = extras.get(nested_key)
    if isinstance(nested, dict):
        return dict(nested)
    pre = f"{prefix}_"
    return {
        k[len(pre):]: v
        for k, v in extras.items()
        if k.startswith(pre) and k != nested_key
    }


def _safe_kwargs(cfg_cls: type, kwargs: dict[str, object]) -> dict[str, object]:
    """Drop kwargs that aren't fields on ``cfg_cls``.

    The registry carries forward-looking knobs (e.g. ``session_cutoff
    _hour_utc``) that may not exist on the current strategy config. We
    accept the lag silently rather than crashing the whole grid run —
    the strategy author can add the field later and it'll start being
    honored automatically. Logged once per cell when keys are dropped
    so the gap is visible during research, not hidden.
    """
    import dataclasses
    valid = {f.name for f in dataclasses.fields(cfg_cls)}
    accepted = {k: v for k, v in kwargs.items() if k in valid}
    dropped = sorted(set(kwargs) - valid)
    if dropped:
        print(f"      [extras] {cfg_cls.__name__}: dropped unknown keys {dropped}")
    return accepted


def _merge_strategy_overrides(
    extras: dict[str, object],
    prefix: str,
    cfg_cls: type,
    *,
    include_direct_fields: bool = False,
    aliases: dict[str, str] | None = None,
) -> dict[str, object]:
    """Merge nested/prefixed config with legacy direct-key fallbacks."""
    import dataclasses

    merged = _filter_extras(extras, prefix)
    valid = {f.name for f in dataclasses.fields(cfg_cls)}
    if include_direct_fields:
        for field_name in valid:
            if field_name in extras and field_name not in merged:
                merged[field_name] = extras[field_name]
    for source_key, target_key in (aliases or {}).items():
        if (
            source_key in extras
            and target_key in valid
            and target_key not in merged
        ):
            merged[target_key] = extras[source_key]
    return merged


def _build_orb_factory(
    extras: dict[str, object] | None = None,
    *,
    crypto: bool = False,
) -> Callable[[], object]:
    extras = extras or {}
    if crypto:
        from eta_engine.strategies.crypto_orb_strategy import (
            CryptoORBConfig,
            crypto_orb_strategy,
        )

        overrides = _filter_extras(extras, "crypto_orb") or _filter_extras(extras, "orb")
        cfg = CryptoORBConfig(
            **_safe_kwargs(CryptoORBConfig, overrides),
        )
        return lambda: crypto_orb_strategy(cfg)

    from eta_engine.strategies.orb_strategy import ORBConfig, ORBStrategy

    cfg = ORBConfig(
        **_safe_kwargs(ORBConfig, _filter_extras(extras, "orb")),
    )
    return lambda: ORBStrategy(cfg)


def _build_drb_factory(
    extras: dict[str, object] | None = None,
) -> Callable[[], object]:
    extras = extras or {}
    from eta_engine.strategies.drb_strategy import DRBConfig, DRBStrategy

    cfg = DRBConfig(
        **_safe_kwargs(DRBConfig, _filter_extras(extras, "drb")),
    )
    return lambda: DRBStrategy(cfg)


def _build_crypto_regime_trend_config(extras: dict[str, object]):  # type: ignore[no-untyped-def]  # noqa: ANN202
    from eta_engine.strategies.crypto_regime_trend_strategy import (
        CryptoRegimeTrendConfig,
    )

    overrides = _merge_strategy_overrides(
        extras,
        "crypto_regime_trend",
        CryptoRegimeTrendConfig,
        include_direct_fields=True,
    )
    return CryptoRegimeTrendConfig(
        **_safe_kwargs(CryptoRegimeTrendConfig, overrides),
    )


def _build_sage_consensus_config(extras: dict[str, object]):  # type: ignore[no-untyped-def]  # noqa: ANN202
    from eta_engine.strategies.sage_consensus_strategy import SageConsensusConfig

    overrides = _merge_strategy_overrides(
        extras,
        "sage_consensus",
        SageConsensusConfig,
        include_direct_fields=True,
        aliases={
            "sage_min_conviction": "min_conviction",
            "sage_min_consensus": "min_consensus",
            "sage_min_alignment": "min_alignment",
        },
    )
    return SageConsensusConfig(
        **_safe_kwargs(SageConsensusConfig, overrides),
    )


def _build_macro_confluence_config(extras: dict[str, object]):  # type: ignore[no-untyped-def]  # noqa: ANN202
    from eta_engine.strategies.crypto_macro_confluence_strategy import (
        CryptoMacroConfluenceConfig,
        MacroConfluenceConfig,
    )

    base_cfg = _build_crypto_regime_trend_config(extras)
    filter_overrides = _merge_strategy_overrides(
        extras,
        "macro_confluence",
        MacroConfluenceConfig,
        include_direct_fields=True,
    )

    tier_4_filters = extras.get("tier_4_filters")
    if (
        "etf_csv_path" in extras
        or (isinstance(tier_4_filters, list) and "etf_flow" in tier_4_filters)
    ):
        filter_overrides.setdefault("require_etf_flow_alignment", True)

    filters_cfg = MacroConfluenceConfig(
        **_safe_kwargs(MacroConfluenceConfig, filter_overrides),
    )
    return CryptoMacroConfluenceConfig(base=base_cfg, filters=filters_cfg)


def _build_orb_sage_gated_factory(
    extras: dict[str, object] | None = None,
) -> Callable[[], object]:
    extras = extras or {}
    from eta_engine.strategies.sage_gated_orb_strategy import (
        SageGatedORBConfig,
        SageGatedORBStrategy,
    )

    instrument_class = str(extras.get("instrument_class") or "").lower()
    orb_cfg = _build_orb_factory(extras, crypto=instrument_class == "crypto")().cfg
    sage_cfg = _build_sage_consensus_config(extras)
    overlay_enabled = bool(
        extras.get("overlay_enabled", extras.get("sage_overlay_enabled", True)),
    )
    cfg = SageGatedORBConfig(
        orb=orb_cfg,
        sage=sage_cfg,
        overlay_enabled=overlay_enabled,
    )
    return lambda: SageGatedORBStrategy(cfg)


def _build_sage_daily_gated_factory(
    extras: dict[str, object] | None = None,
) -> Callable[[], object]:
    extras = extras or {}
    from eta_engine.strategies.generic_sage_daily_gate import (
        GenericSageDailyGateConfig,
        GenericSageDailyGateStrategy,
    )
    from eta_engine.strategies.sage_daily_gated_strategy import (
        SageDailyGatedConfig,
        SageDailyGatedStrategy,
    )

    gate_overrides = _filter_extras(extras, "sage_daily_gated")
    if "min_daily_conviction" in extras and "min_daily_conviction" not in gate_overrides:
        gate_overrides["min_daily_conviction"] = extras["min_daily_conviction"]
    if "strict_mode" in extras and "strict_mode" not in gate_overrides:
        gate_overrides["strict_mode"] = extras["strict_mode"]
    if "sage_min_daily_conviction" in extras and "min_daily_conviction" not in gate_overrides:
        gate_overrides["min_daily_conviction"] = extras["sage_min_daily_conviction"]
    if "sage_strict_mode" in extras and "strict_mode" not in gate_overrides:
        gate_overrides["strict_mode"] = extras["sage_strict_mode"]

    min_daily_conviction = float(gate_overrides.get("min_daily_conviction", 0.30))
    strict_mode = bool(gate_overrides.get("strict_mode", False))
    underlying_strategy = str(
        extras.get("underlying_strategy") or "crypto_macro_confluence",
    ).lower()

    if underlying_strategy not in ("", "crypto_macro_confluence"):
        if underlying_strategy == "sage_daily_gated":
            msg = "sage_daily_gated cannot wrap itself"
            raise ValueError(msg)
        sub_factory = _build_strategy_factory(underlying_strategy, extras)
        gate_cfg = GenericSageDailyGateConfig(
            min_daily_conviction=min_daily_conviction,
            strict_mode=strict_mode,
        )
        return lambda: GenericSageDailyGateStrategy(sub_factory(), gate_cfg)

    cfg = SageDailyGatedConfig(
        base=_build_macro_confluence_config(extras),
        min_daily_conviction=min_daily_conviction,
        strict_mode=strict_mode,
    )
    return lambda: SageDailyGatedStrategy(cfg)


def _build_crypto_strategy_factory(  # type: ignore[no-untyped-def]  # noqa: ANN202
    kind: str, extras: dict[str, object] | None = None,
):
    """Return a zero-arg factory that builds a fresh crypto strategy
    instance per walk-forward window. Per-bot extras prefixed with the
    strategy_kind (e.g. ``crypto_orb_range_minutes``) get applied to the
    config dataclass; unknown keys are silently ignored so the registry
    can carry non-strategy fields too."""
    extras = extras or {}
    if kind == "crypto_orb":
        from eta_engine.strategies.crypto_orb_strategy import (
            CryptoORBConfig,
            crypto_orb_strategy,
        )
        cfg = CryptoORBConfig(
            **_safe_kwargs(CryptoORBConfig, _filter_extras(extras, "crypto_orb")),
        )
        return lambda: crypto_orb_strategy(cfg)
    if kind == "crypto_trend":
        from eta_engine.strategies.crypto_trend_strategy import (
            CryptoTrendConfig,
            CryptoTrendStrategy,
        )
        cfg = CryptoTrendConfig(
            **_safe_kwargs(CryptoTrendConfig, _filter_extras(extras, "crypto_trend")),
        )
        return lambda: CryptoTrendStrategy(cfg)
    if kind == "crypto_meanrev":
        from eta_engine.strategies.crypto_meanrev_strategy import (
            CryptoMeanRevConfig,
            CryptoMeanRevStrategy,
        )
        cfg = CryptoMeanRevConfig(
            **_safe_kwargs(CryptoMeanRevConfig, _filter_extras(extras, "crypto_meanrev")),
        )
        return lambda: CryptoMeanRevStrategy(cfg)
    if kind == "crypto_scalp":
        from eta_engine.strategies.crypto_scalp_strategy import (
            CryptoScalpConfig,
            CryptoScalpStrategy,
        )
        cfg = CryptoScalpConfig(
            **_safe_kwargs(CryptoScalpConfig, _filter_extras(extras, "crypto_scalp")),
        )
        return lambda: CryptoScalpStrategy(cfg)
    if kind == "crypto_regime_trend":
        from eta_engine.strategies.crypto_regime_trend_strategy import (
            CryptoRegimeTrendStrategy,
        )
        cfg = _build_crypto_regime_trend_config(extras)
        return lambda: CryptoRegimeTrendStrategy(cfg)
    if kind == "sage_consensus":
        from eta_engine.strategies.sage_consensus_strategy import (
            SageConsensusStrategy,
        )
        cfg = _build_sage_consensus_config(extras)
        return lambda: SageConsensusStrategy(cfg)
    if kind == "crypto_macro_confluence":
        # Macro-feature confluence (ETF flows, LTH, fear/greed, etc.).
        # Providers are attached at a higher orchestration layer; the
        # grid runs the strategy in its "no providers" degraded form
        # so the gate evaluation is honest about what can be measured
        # without ad-hoc wiring. Strategies with provider-dependent
        # claims should be re-run via their dedicated walk-forward
        # scripts before promotion.
        from eta_engine.strategies.crypto_macro_confluence_strategy import (
            CryptoMacroConfluenceStrategy,
        )
        cfg = _build_macro_confluence_config(extras)
        return lambda: CryptoMacroConfluenceStrategy(cfg)
    if kind == "sage_daily_gated":
        return _build_sage_daily_gated_factory(extras)
    if kind == "grid":
        from eta_engine.strategies.grid_trading_strategy import (
            GridConfig,
            GridTradingStrategy,
        )
        cfg = GridConfig(
            **_safe_kwargs(GridConfig, _filter_extras(extras, "grid")),
        )
        return lambda: GridTradingStrategy(cfg)
    if kind == "compression_breakout":
        # Foundation strategy (2026-04-27): volatility-compression
        # release breakout (BB-width percentile + ATR < ATR_MA, then
        # breakout above N-bar high with trend EMA + volume z + close
        # location filters). Asset-class presets: btc_*, eth_*, sol_*,
        # mnq_*, nq_*. The extras["compression_preset"] string selects.
        from eta_engine.strategies.compression_breakout_strategy import (
            CompressionBreakoutConfig,
            CompressionBreakoutStrategy,
            btc_compression_preset,
            eth_compression_preset,
            mnq_compression_preset,
            nq_compression_preset,
            sol_compression_preset,
        )
        preset_factories = {
            "btc": btc_compression_preset, "eth": eth_compression_preset,
            "sol": sol_compression_preset, "mnq": mnq_compression_preset,
            "nq": nq_compression_preset,
        }
        preset_name = (extras.get("compression_preset") or "btc").lower()
        base_cfg = preset_factories.get(preset_name, btc_compression_preset)()
        # Allow extras to override individual fields via "compression_*"
        overrides = _filter_extras(extras, "compression")
        # Drop the "preset" override (not a CompressionBreakoutConfig field)
        overrides.pop("preset", None)
        cfg = CompressionBreakoutConfig(
            **{**base_cfg.__dict__,
               **_safe_kwargs(CompressionBreakoutConfig, overrides)},
        )
        return lambda: CompressionBreakoutStrategy(cfg)
    if kind == "sweep_reclaim":
        # Foundation strategy (2026-04-27): mechanical Wyckoff
        # spring/upthrust translation. Asset presets:
        # btc_daily_*, eth_daily_*, sol_daily_*, mnq_intraday_*,
        # nq_intraday_*. extras["sweep_preset"] selects.
        from eta_engine.strategies.sweep_reclaim_strategy import (
            SweepReclaimConfig,
            SweepReclaimStrategy,
            btc_daily_sweep_preset,
            eth_daily_sweep_preset,
            mnq_intraday_sweep_preset,
            nq_intraday_sweep_preset,
            sol_daily_sweep_preset,
        )
        preset_factories = {
            "btc": btc_daily_sweep_preset, "eth": eth_daily_sweep_preset,
            "sol": sol_daily_sweep_preset, "mnq": mnq_intraday_sweep_preset,
            "nq": nq_intraday_sweep_preset,
        }
        preset_name = (extras.get("sweep_preset") or "btc").lower()
        base_cfg = preset_factories.get(preset_name, btc_daily_sweep_preset)()
        overrides = _filter_extras(extras, "sweep")
        overrides.pop("preset", None)
        cfg = SweepReclaimConfig(
            **{**base_cfg.__dict__,
               **_safe_kwargs(SweepReclaimConfig, overrides)},
        )
        return lambda: SweepReclaimStrategy(cfg)
    msg = f"unknown crypto strategy_kind: {kind!r}"
    raise ValueError(msg)


def _build_strategy_factory(  # type: ignore[no-untyped-def]  # noqa: ANN202
    kind: str, extras: dict[str, object] | None = None,
):
    extras = extras or {}
    if kind == "orb":
        return _build_orb_factory(extras, crypto=False)
    if kind == "drb":
        return _build_drb_factory(extras)
    if kind == "orb_sage_gated":
        return _build_orb_sage_gated_factory(extras)
    return _build_crypto_strategy_factory(kind, extras)


def _resolve_scorer(name: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    from eta_engine.core.confluence_scorer import (
        score_confluence,
        score_confluence_btc,
        score_confluence_mnq,
    )

    return {
        "global": score_confluence,
        "mnq": score_confluence_mnq,
        "btc": score_confluence_btc,
    }[name]


def run_cell(cell: ResearchCell) -> CellResult:
    """Run one walk-forward sweep and return the headline stats."""
    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.scripts.run_walk_forward_mnq_real import _ctx

    ds = default_library().get(symbol=cell.symbol, timeframe=cell.timeframe)
    if ds is None:
        return CellResult(
            cell=cell, n_windows=0, n_positive_oos=0,
            agg_is_sharpe=0.0, agg_oos_sharpe=0.0,
            avg_oos_degradation=0.0, deflated_sharpe=0.0,
            fold_dsr_median=0.0, fold_dsr_pass_fraction=0.0,
            pass_gate=False, note=f"NO_DATA: {cell.symbol}/{cell.timeframe}",
        )

    bars = default_library().load_bars(ds)
    if not bars:
        return CellResult(
            cell=cell, n_windows=0, n_positive_oos=0,
            agg_is_sharpe=0.0, agg_oos_sharpe=0.0,
            avg_oos_degradation=0.0, deflated_sharpe=0.0,
            fold_dsr_median=0.0, fold_dsr_pass_fraction=0.0,
            pass_gate=False, note="EMPTY_BARS",
        )

    base_cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=ds.symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=cell.threshold,
        max_trades_per_day=10,
    )
    # Honor per-bot walk_forward_overrides from the registry extras —
    # lets bots opt into long-haul mode (daily/weekly cadence) etc.
    # without a global config split.
    wf_overrides = cell.extras.get("walk_forward_overrides") or {}
    if not isinstance(wf_overrides, dict):
        wf_overrides = {}
    wf_kwargs: dict[str, object] = {
        "window_days": cell.window_days,
        "step_days": cell.step_days,
        "anchored": True,
        "oos_fraction": 0.3,
        "min_trades_per_window": cell.min_trades_per_window,
        "strict_fold_dsr_gate": True,
        "fold_dsr_min_pass_fraction": 0.5,
    }
    # If long-haul mode is requested, drop the per-fold strict gate
    # so it doesn't double-gate alongside the long-haul checks.
    if wf_overrides.get("long_haul_mode"):
        wf_kwargs["strict_fold_dsr_gate"] = False
    wf_kwargs.update(wf_overrides)
    wf = WalkForwardConfig(**wf_kwargs)
    # Strategy-factory kinds bypass the scorer/regime/ctx path entirely.
    if cell.strategy_kind in ("orb", "orb_sage_gated", "drb"):
        factory = _build_strategy_factory(cell.strategy_kind, cell.extras)
        res = WalkForwardEngine().run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=wf,
            base_backtest_config=base_cfg,
            ctx_builder=lambda b, h: {},
            strategy_factory=factory,
        )
    elif cell.strategy_kind == "ensemble_voting":
        # Ensemble vote across named sub-strategies. Voters list comes
        # from extras["voters"]; each voter is built via the same
        # crypto factory (so "crypto_regime_trend", "regime_trend_etf",
        # "sage_daily_gated" etc. all work as voter names — assuming
        # they exist as factory keys). Without provider wiring the
        # macro/sage voters degrade to their no-provider baselines;
        # the dedicated ensemble walk-forward script is the production
        # path. Grid evaluation = "what does the vote logic produce
        # without provider context."
        from eta_engine.strategies.ensemble_voting_strategy import (
            EnsembleVotingConfig,
            EnsembleVotingStrategy,
        )
        voter_names = cell.extras.get("voters") or []
        # Aliases for voter names that use a different name in the
        # ensemble extras than in the factory dispatch.
        _alias = {
            "regime_trend": "crypto_regime_trend",
            "regime_trend_etf": "crypto_macro_confluence",
        }
        sub_strategies: list = []
        for name in voter_names:
            kind = _alias.get(name, name)
            try:
                sub_factory = _build_strategy_factory(kind, cell.extras)
            except ValueError:
                # Unknown voter — skip rather than crash. Logged so
                # the gap is visible at grid time.
                print(f"      [ensemble] unknown voter {name!r} (mapped to {kind!r}); skipped")
                continue
            sub_strategies.append((name, sub_factory()))
        if not sub_strategies:
            return CellResult(
                cell=cell, n_windows=0, n_positive_oos=0,
                agg_is_sharpe=0.0, agg_oos_sharpe=0.0,
                avg_oos_degradation=0.0, deflated_sharpe=0.0,
                fold_dsr_median=0.0, fold_dsr_pass_fraction=0.0,
                pass_gate=False,
                note="NO_VOTERS: ensemble_voting needs extras['voters']",
            )
        ens_cfg_kwargs = _safe_kwargs(
            EnsembleVotingConfig,
            _filter_extras(cell.extras, "ensemble_voting"),
        )
        # extras["min_agreement_count"] is the canonical name even
        # without an "ensemble_voting_" prefix; honor it explicitly.
        min_agree = cell.extras.get("min_agreement_count")
        if min_agree is not None:
            ens_cfg_kwargs.setdefault("min_agreement_count", min_agree)
        ens_cfg = EnsembleVotingConfig(**ens_cfg_kwargs)
        # EnsembleVotingStrategy isn't safe to share across windows —
        # voters carry per-bar state. Build per-window via a closure
        # that reconstructs both the voters and the ensemble.
        def _ensemble_factory():  # type: ignore[no-untyped-def]  # noqa: ANN202
            voters = []
            for name in voter_names:
                kind = _alias.get(name, name)
                try:
                    f = _build_strategy_factory(kind, cell.extras)
                except ValueError:
                    continue
                voters.append((name, f()))
            return EnsembleVotingStrategy(voters, ens_cfg)
        res = WalkForwardEngine().run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=wf,
            base_backtest_config=base_cfg,
            ctx_builder=lambda b, h: {},
            strategy_factory=_ensemble_factory,
        )
    elif cell.strategy_kind in (
        "crypto_orb", "crypto_trend", "crypto_meanrev", "crypto_scalp",
        "crypto_regime_trend", "crypto_macro_confluence", "sage_consensus",
        "sage_daily_gated", "grid",
    ):
        # Crypto-specific strategy variants. All share the same
        # maybe_enter(bar, hist, equity, config) -> _Open|None contract
        # as ORB/DRB, so they bypass the confluence-scorer path. The
        # registry wires per-bot defaults; per-bot extras can override
        # individual knobs once we start sweeping params per bot.
        factory = _build_strategy_factory(cell.strategy_kind, cell.extras)
        res = WalkForwardEngine().run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=wf,
            base_backtest_config=base_cfg,
            ctx_builder=lambda b, h: {},
            strategy_factory=factory,
        )
    else:
        res = WalkForwardEngine().run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=wf,
            base_backtest_config=base_cfg,
            ctx_builder=_ctx,
            scorer=_resolve_scorer(cell.scorer_name),
            block_regimes=cell.block_regimes,
        )
    n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0.0) > 0)
    return CellResult(
        cell=cell,
        n_windows=len(res.windows),
        n_positive_oos=n_pos,
        agg_is_sharpe=res.aggregate_is_sharpe,
        agg_oos_sharpe=res.aggregate_oos_sharpe,
        avg_oos_degradation=res.oos_degradation_avg,
        deflated_sharpe=res.deflated_sharpe,
        fold_dsr_median=res.fold_dsr_median,
        fold_dsr_pass_fraction=res.fold_dsr_pass_fraction,
        pass_gate=res.pass_gate,
        note=f"{ds.row_count} bars / {ds.days_span():.0f}d",
    )


def render_table(results: Sequence[CellResult]) -> str:
    header = (
        "| Config | Sym/TF | Scorer | Thr | Gate | W | +OOS | IS Sh | "
        "OOS Sh | Deg% | DSR med | DSR pass% | Verdict | Note |"
    )
    lines = [
        header,
        "|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in results:
        gate_str = (
            ("/".join(sorted(r.cell.block_regimes)) if r.cell.block_regimes else "—")
            if r.cell.block_regimes else "—"
        )
        verdict = "PASS" if r.pass_gate else "FAIL"
        lines.append(
            f"| {r.cell.label} | {r.cell.symbol}/{r.cell.timeframe} | {r.cell.scorer_name} | "
            f"{r.cell.threshold:.1f} | {gate_str} | {r.n_windows} | {r.n_positive_oos} | "
            f"{r.agg_is_sharpe:.3f} | {r.agg_oos_sharpe:.3f} | "
            f"{r.avg_oos_degradation * 100:.1f} | {r.fold_dsr_median:.3f} | "
            f"{r.fold_dsr_pass_fraction * 100:.1f} | {verdict} | {r.note} |"
        )
    return "\n".join(lines)


def _matrix_from_registry() -> list[ResearchCell]:
    """Pull one ResearchCell per bot from strategies.per_bot_registry.

    This is the canonical entry point for the per-bot baseline sweep.
    Hand-rolled cells (the ad-hoc matrix below) stay around for quick
    one-off questions, but the registry-driven sweep is what the
    "is anything regressing across the bot fleet" smoke test reads.
    """
    from eta_engine.strategies.per_bot_registry import all_assignments

    cells: list[ResearchCell] = []
    for a in all_assignments():
        cells.append(
            ResearchCell(
                label=a.bot_id,
                symbol=a.symbol,
                timeframe=a.timeframe,
                scorer_name=a.scorer_name,
                threshold=a.confluence_threshold,
                block_regimes=a.block_regimes if a.block_regimes else None,
                window_days=a.window_days,
                step_days=a.step_days,
                min_trades_per_window=a.min_trades_per_window,
                strategy_kind=a.strategy_kind,
                extras=dict(a.extras),
            )
        )
    return cells


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="run_research_grid")
    p.add_argument(
        "--source",
        choices=("registry", "ad_hoc"),
        default="registry",
        help="registry = run every bot's assigned strategy (default); "
        "ad_hoc = the static research-question matrix below",
    )
    args = p.parse_args()

    base_block = frozenset({"trending_up", "trending_down"})
    if args.source == "registry":
        matrix = _matrix_from_registry()
    else:
        # Ad-hoc cells preserved for one-off research questions.
        matrix = [
            ResearchCell("5m_ungated", "MNQ1", "5m", "global", 7.0, None, 30, 15, 5),
            ResearchCell("5m_gated_mnq", "MNQ1", "5m", "mnq", 5.0, base_block, 30, 15, 5),
            ResearchCell("1h_gated", "MNQ1", "1h", "mnq", 5.0, base_block, 90, 30, 10),
            ResearchCell("4h_gated", "MNQ1", "4h", "mnq", 5.0, base_block, 180, 60, 10),
            ResearchCell("D_NQ1_gated", "NQ1", "D", "mnq", 5.0, base_block, 365, 180, 10),
        ]
    print(f"[research_grid] running {len(matrix)} cells\n")
    results: list[CellResult] = []
    for cell in matrix:
        print(f"  - {cell.label}: {cell.symbol}/{cell.timeframe} ...")
        try:
            r = run_cell(cell)
            results.append(r)
            print(
                f"      -> windows={r.n_windows} "
                f"agg_OOS={r.agg_oos_sharpe:+.3f} pass_frac={r.fold_dsr_pass_fraction*100:.1f}% "
                f"verdict={'PASS' if r.pass_gate else 'FAIL'}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"      -> ERROR: {exc!r}")
            results.append(CellResult(
                cell=cell, n_windows=0, n_positive_oos=0,
                agg_is_sharpe=0.0, agg_oos_sharpe=0.0,
                avg_oos_degradation=0.0, deflated_sharpe=0.0,
                fold_dsr_median=0.0, fold_dsr_pass_fraction=0.0,
                pass_gate=False, note=f"ERROR: {type(exc).__name__}",
            ))

    table = render_table(results)
    print("\n" + table)

    log_dir = ROOT / "docs" / "research_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"research_grid_{stamp}.md"
    log_path.write_text(
        f"# Research Grid — {datetime.now(UTC).isoformat()}\n\n"
        f"Cells: {len(matrix)}\n\n"
        + table + "\n",
        encoding="utf-8",
    )
    print(f"\n[saved to {log_path}]")
    return 0 if any(r.pass_gate for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

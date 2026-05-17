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
* Promotable/PASS runs save to ``docs/research_log`` by default.
* Low-signal/no-data runs save to canonical ignored runtime state under
  ``var/eta_engine/state/research_grid`` by default, so timestamped smoke
  output does not clutter tracked docs.

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

from eta_engine.scripts import workspace_roots  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_DAILY_SAGE_PROVIDER_CACHE: dict[tuple[str, str], object] = {}


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
    wf_mode: str = "unknown"
    dsr_n_trials: int = 0
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
    return {k[len(pre) :]: v for k, v in extras.items() if k.startswith(pre) and k != nested_key}


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
        if source_key in extras and target_key in valid and target_key not in merged:
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
    if "etf_csv_path" in extras or (isinstance(tier_4_filters, list) and "etf_flow" in tier_4_filters):
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


def _get_daily_sage_provider(symbol: str, instrument_class: str = "crypto") -> object:
    """Build/cache a daily-sage provider for generic sage-daily wrappers."""
    key = (symbol, instrument_class)
    if key not in _DAILY_SAGE_PROVIDER_CACHE:
        from eta_engine.scripts.run_eth_sage_daily_walk_forward import (
            _build_daily_verdicts,
        )

        _DAILY_SAGE_PROVIDER_CACHE[key] = _build_daily_verdicts(
            symbol,
            instrument_class=instrument_class,
        )
    return _DAILY_SAGE_PROVIDER_CACHE[key]


def _with_daily_sage_provider(
    factory: Callable[[], object],
    *,
    symbol: str,
    instrument_class: str = "crypto",
) -> Callable[[], object]:
    """Attach daily-sage verdicts when a strategy exposes the hook."""
    provider = _get_daily_sage_provider(symbol, instrument_class)

    def _factory() -> object:
        strategy = factory()
        attach = getattr(strategy, "attach_daily_verdict_provider", None)
        if callable(attach):
            attach(provider)
        return strategy

    return _factory


_CROSS_ASSET_REF_CACHE: dict[str, object] = {}


def _build_cross_asset_ref_provider(
    bot_symbol: str,
    ref_symbol: str,
    ref_timeframe: str,
) -> object:
    """Build a reference-price callable for cross-asset divergence strategies.

    Returns a callable with signature ``(BarData) -> float`` that returns
    the reference asset's close price at-or-before the bar's timestamp.
    """
    from eta_engine.data.library import default_library

    cache_key = f"{ref_symbol}_{ref_timeframe}"
    if cache_key in _CROSS_ASSET_REF_CACHE:
        return _CROSS_ASSET_REF_CACHE[cache_key]

    lib = default_library()
    ds = lib.get(symbol=ref_symbol, timeframe=ref_timeframe)
    if ds is None:

        def _no_ref(bar: object) -> float:
            return 0.0

        _CROSS_ASSET_REF_CACHE[cache_key] = _no_ref
        return _no_ref

    bars = lib.load_bars(ds, require_positive_prices=True)
    if not bars:

        def _no_ref(bar: object) -> float:
            return 0.0

        _CROSS_ASSET_REF_CACHE[cache_key] = _no_ref
        return _no_ref

    ts_to_close: dict[int, float] = {}
    for b in bars:
        ts_to_close[int(b.timestamp.timestamp())] = b.close

    sorted_timestamps = sorted(ts_to_close.keys())

    def _ref_provider(bar: object) -> float:
        bar_ts = int(bar.timestamp.timestamp())  # type: ignore[union-attr]
        best_close = 0.0
        for ts in sorted_timestamps:
            if ts <= bar_ts:
                best_close = ts_to_close[ts]
            else:
                break
        return best_close

    _CROSS_ASSET_REF_CACHE[cache_key] = _ref_provider
    return _ref_provider


def _with_cross_asset_ref_provider(
    factory: object,
    *,
    bot_symbol: str,
    ref_symbol: str,
    ref_timeframe: str,
) -> object:
    """Wrap a strategy factory to attach a cross-asset reference provider."""
    provider = _build_cross_asset_ref_provider(bot_symbol, ref_symbol, ref_timeframe)

    def _factory() -> object:
        strategy = factory()
        attach = getattr(strategy, "attach_reference_provider", None)
        if callable(attach):
            attach(provider)
        return strategy

    return _factory


_FUNDING_RATE_PROVIDER_CACHE: dict[str, object] = {}


def _build_funding_rate_provider() -> object:
    """Build a funding-rate callable from BTCFUND_8h CSV data."""
    from pathlib import Path

    cache_key = "btcfund_default"
    if cache_key in _FUNDING_RATE_PROVIDER_CACHE:
        return _FUNDING_RATE_PROVIDER_CACHE[cache_key]

    funding_paths = [
        workspace_roots.CRYPTO_HISTORY_ROOT / "BTCFUND_8h.csv",
        workspace_roots.MNQ_DATA_ROOT / "BTCFUND_8h.csv",
        workspace_roots.CRYPTO_HISTORY_ROOT / "btc_funding_8h.csv",
    ]

    funding_rows: list[tuple[int, float]] = []
    for fp in funding_paths:
        if fp.exists():
            import csv

            with open(str(fp), newline="") as fh:
                for row in csv.DictReader(fh):
                    try:
                        t_val = row.get("time") or row.get("timestamp") or row.get("timestamp_utc") or ""
                        t = int(float(t_val))
                        r_val = row.get("funding_rate") or row.get("close") or row.get("funding") or "0"
                        r = float(r_val)
                        funding_rows.append((t, r))
                    except (ValueError, KeyError):
                        continue
            if funding_rows:
                break

    if not funding_rows:

        def _no_funding(bar: object) -> float:
            return 0.0

        _FUNDING_RATE_PROVIDER_CACHE[cache_key] = _no_funding
        return _no_funding

    funding_rows.sort(key=lambda x: x[0])

    def _provider(bar: object) -> float:
        bar_ts = int(bar.timestamp.timestamp())  # type: ignore[union-attr]
        best_rate = 0.0
        for ts, rate in funding_rows:
            if ts <= bar_ts:
                best_rate = rate
            else:
                break
        return best_rate

        _FUNDING_RATE_PROVIDER_CACHE[cache_key] = _provider

    return _provider


def _with_funding_rate_provider(factory: object) -> object:
    """Wrap a strategy factory to attach a funding-rate provider."""
    provider = _build_funding_rate_provider()

    def _factory() -> object:
        strategy = factory()
        attach = getattr(strategy, "attach_funding_provider", None)
        if callable(attach):
            attach(provider)
        return strategy

    return _factory


def _build_crypto_strategy_factory(  # type: ignore[no-untyped-def]  # noqa: ANN202
    kind: str,
    extras: dict[str, object] | None = None,
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
            "btc": btc_compression_preset,
            "eth": eth_compression_preset,
            "sol": sol_compression_preset,
            "mnq": mnq_compression_preset,
            "nq": nq_compression_preset,
        }
        preset_name = (extras.get("compression_preset") or "btc").lower()
        base_cfg = preset_factories.get(preset_name, btc_compression_preset)()
        overrides = _merge_strategy_overrides(
            extras,
            "compression",
            CompressionBreakoutConfig,
            include_direct_fields=True,
        )
        overrides.pop("compression_preset", None)
        overrides.pop("preset", None)
        cfg = CompressionBreakoutConfig(
            **{**base_cfg.__dict__, **_safe_kwargs(CompressionBreakoutConfig, overrides)},
        )
        return lambda: CompressionBreakoutStrategy(cfg)
    if kind == "sweep_reclaim":
        from eta_engine.strategies.sweep_reclaim_strategy import (
            SWEEP_PRESET_FACTORIES,
            SweepReclaimConfig,
            SweepReclaimStrategy,
        )

        preset_name = (extras.get("sweep_preset") or "btc").lower()
        if preset_name not in SWEEP_PRESET_FACTORIES:
            supported = ", ".join(sorted(SWEEP_PRESET_FACTORIES))
            raise ValueError(
                f"unknown sweep_preset {preset_name!r}; supported: {supported}",
            )
        base_cfg = SWEEP_PRESET_FACTORIES[preset_name]()
        overrides = _merge_strategy_overrides(
            extras,
            "sweep",
            SweepReclaimConfig,
            include_direct_fields=True,
        )
        overrides.pop("sweep_preset", None)
        overrides.pop("preset", None)
        cfg = SweepReclaimConfig(
            **{**base_cfg.__dict__, **_safe_kwargs(SweepReclaimConfig, overrides)},
        )
        return lambda: SweepReclaimStrategy(cfg)
    if kind == "rsi_mean_reversion":
        from eta_engine.strategies.rsi_mean_reversion_strategy import (
            RSIMeanReversionConfig,
            RSIMeanReversionStrategy,
            btc_rsi_mr_preset,
            eth_rsi_mr_preset,
            mnq_rsi_mr_preset,
            nq_rsi_mr_preset,
        )

        preset_factories = {
            "mnq": mnq_rsi_mr_preset,
            "nq": nq_rsi_mr_preset,
            "btc": btc_rsi_mr_preset,
            "eth": eth_rsi_mr_preset,
        }
        preset_name = (extras.get("rsi_mr_preset") or extras.get("per_ticker_optimal") or "mnq").lower()
        base_cfg = preset_factories.get(preset_name, mnq_rsi_mr_preset)()
        overrides = _merge_strategy_overrides(extras, "rsi_mr", RSIMeanReversionConfig, include_direct_fields=True)
        overrides.pop("rsi_mr_preset", None)
        overrides.pop("preset", None)
        cfg = RSIMeanReversionConfig(
            **{**base_cfg.__dict__, **overrides},
        )
        return lambda: RSIMeanReversionStrategy(cfg)
    if kind == "vwap_reversion":
        from eta_engine.strategies.vwap_reversion_strategy import (
            VWAPReversionConfig,
            VWAPReversionStrategy,
            btc_vwap_mr_preset,
            eth_vwap_mr_preset,
            mnq_vwap_mr_preset,
            nq_vwap_mr_preset,
        )

        preset_factories = {
            "mnq": mnq_vwap_mr_preset,
            "nq": nq_vwap_mr_preset,
            "btc": btc_vwap_mr_preset,
            "eth": eth_vwap_mr_preset,
        }
        preset_name = (extras.get("vwap_mr_preset") or extras.get("per_ticker_optimal") or "mnq").lower()
        base_cfg = preset_factories.get(preset_name, mnq_vwap_mr_preset)()
        overrides = _merge_strategy_overrides(extras, "vwap_mr", VWAPReversionConfig, include_direct_fields=True)
        overrides.pop("vwap_mr_preset", None)
        overrides.pop("preset", None)
        cfg = VWAPReversionConfig(
            **{**base_cfg.__dict__, **overrides},
        )
        return lambda: VWAPReversionStrategy(cfg)
    if kind == "volume_profile":
        from eta_engine.strategies.volume_profile_strategy import (
            VolumeProfileStrategy,
            VolumeProfileStrategyConfig,
            btc_volume_profile_preset,
            eth_volume_profile_preset,
            mnq_volume_profile_preset,
            nq_volume_profile_preset,
        )

        preset_factories = {
            "mnq": mnq_volume_profile_preset,
            "nq": nq_volume_profile_preset,
            "btc": btc_volume_profile_preset,
            "eth": eth_volume_profile_preset,
        }
        preset_name = (extras.get("vol_prof_preset") or extras.get("per_ticker_optimal") or "mnq").lower()
        base_cfg = preset_factories.get(preset_name, mnq_volume_profile_preset)()
        overrides = _merge_strategy_overrides(
            extras,
            "vol_prof",
            VolumeProfileStrategyConfig,
            include_direct_fields=True,
        )
        overrides.pop("vol_prof_preset", None)
        overrides.pop("preset", None)
        cfg = VolumeProfileStrategyConfig(
            **{**base_cfg.__dict__, **overrides},
        )
        return lambda: VolumeProfileStrategy(cfg)
    if kind == "gap_fill":
        from eta_engine.strategies.gap_fill_strategy import (
            GapFillConfig,
            GapFillStrategy,
            btc_gap_fill_preset,
            eth_gap_fill_preset,
            mnq_gap_fill_preset,
            nq_gap_fill_preset,
        )

        preset_factories = {
            "mnq": mnq_gap_fill_preset,
            "nq": nq_gap_fill_preset,
            "btc": btc_gap_fill_preset,
            "eth": eth_gap_fill_preset,
        }
        preset_name = (extras.get("gap_fill_preset") or extras.get("per_ticker_optimal") or "mnq").lower()
        base_cfg = preset_factories.get(preset_name, mnq_gap_fill_preset)()
        overrides = _merge_strategy_overrides(extras, "gap_fill", GapFillConfig, include_direct_fields=True)
        overrides.pop("gap_fill_preset", None)
        overrides.pop("preset", None)
        cfg = GapFillConfig(
            **{**base_cfg.__dict__, **overrides},
        )
        return lambda: GapFillStrategy(cfg)
    if kind == "cross_asset_divergence":
        from eta_engine.strategies.cross_asset_divergence_strategy import (
            CrossAssetDivergenceConfig,
            CrossAssetDivergenceStrategy,
            btc_vs_eth_divergence_preset,
            mnq_vs_es_divergence_preset,
            nq_vs_es_divergence_preset,
        )

        preset_factories = {
            "mnq": mnq_vs_es_divergence_preset,
            "nq": nq_vs_es_divergence_preset,
            "btc": btc_vs_eth_divergence_preset,
            "eth": btc_vs_eth_divergence_preset,
        }
        preset_name = (extras.get("xasset_preset") or extras.get("per_ticker_optimal") or "mnq").lower()
        base_cfg = preset_factories.get(preset_name, mnq_vs_es_divergence_preset)()
        overrides = _merge_strategy_overrides(extras, "xasset", CrossAssetDivergenceConfig, include_direct_fields=True)
        overrides.pop("xasset_preset", None)
        overrides.pop("preset", None)
        cfg = CrossAssetDivergenceConfig(
            **{**base_cfg.__dict__, **overrides},
        )
        return lambda: CrossAssetDivergenceStrategy(cfg)
    if kind == "funding_rate":
        from eta_engine.strategies.funding_rate_strategy import (
            FundingRateStrategy,
            FundingRateStrategyConfig,
            btc_funding_rate_preset,
            eth_funding_rate_preset,
        )

        preset_factories = {
            "btc": btc_funding_rate_preset,
            "eth": eth_funding_rate_preset,
        }
        preset_name = (extras.get("fund_rate_preset") or extras.get("per_ticker_optimal") or "btc").lower()
        base_cfg = preset_factories.get(preset_name, btc_funding_rate_preset)()
        overrides = _merge_strategy_overrides(
            extras,
            "fund_rate",
            FundingRateStrategyConfig,
            include_direct_fields=True,
        )
        overrides.pop("fund_rate_preset", None)
        overrides.pop("preset", None)
        cfg = FundingRateStrategyConfig(
            **{**base_cfg.__dict__, **overrides},
        )
        return lambda: FundingRateStrategy(cfg)
    msg = f"unknown crypto strategy_kind: {kind!r}"
    raise ValueError(msg)


def _build_strategy_factory(  # type: ignore[no-untyped-def]  # noqa: ANN202
    kind: str,
    extras: dict[str, object] | None = None,
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


def _bars_note(ds: object, bars: list[object], max_bars: int | None) -> str:
    """Describe loaded bars, including tradable-price filtering/caps."""
    row_count = int(ds.row_count)  # type: ignore[attr-defined]
    days = float(ds.days_span())
    expected = min(max_bars, row_count) if max_bars is not None else row_count
    if len(bars) < expected:
        if max_bars is not None and max_bars < row_count:
            return f"{len(bars)}/{expected} latest capped tradable positive-price bars ({row_count} raw) / {days:.0f}d"
        return f"{len(bars)}/{row_count} total tradable positive-price bars / {days:.0f}d"
    if max_bars is not None and len(bars) < row_count:
        return f"{len(bars)}/{row_count} latest bars / {days:.0f}d capped"
    return f"{row_count} bars / {days:.0f}d"


def run_cell(cell: ResearchCell, *, max_bars: int | None = None) -> CellResult:
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
            cell=cell,
            n_windows=0,
            n_positive_oos=0,
            agg_is_sharpe=0.0,
            agg_oos_sharpe=0.0,
            avg_oos_degradation=0.0,
            deflated_sharpe=0.0,
            fold_dsr_median=0.0,
            fold_dsr_pass_fraction=0.0,
            pass_gate=False,
            note=f"NO_DATA: {cell.symbol}/{cell.timeframe}",
        )

    bars = default_library().load_bars(
        ds,
        limit=max_bars,
        limit_from="tail" if max_bars is not None else "head",
        require_positive_prices=True,
    )
    if not bars:
        return CellResult(
            cell=cell,
            n_windows=0,
            n_positive_oos=0,
            agg_is_sharpe=0.0,
            agg_oos_sharpe=0.0,
            avg_oos_degradation=0.0,
            deflated_sharpe=0.0,
            fold_dsr_median=0.0,
            fold_dsr_pass_fraction=0.0,
            pass_gate=False,
            note="EMPTY_BARS",
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
                cell=cell,
                n_windows=0,
                n_positive_oos=0,
                agg_is_sharpe=0.0,
                agg_oos_sharpe=0.0,
                avg_oos_degradation=0.0,
                deflated_sharpe=0.0,
                fold_dsr_median=0.0,
                fold_dsr_pass_fraction=0.0,
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
        "crypto_orb",
        "crypto_trend",
        "crypto_meanrev",
        "crypto_scalp",
        "crypto_regime_trend",
        "crypto_macro_confluence",
        "sage_consensus",
        "sage_daily_gated",
        "grid",
        "compression_breakout",
        "sweep_reclaim",
        "rsi_mean_reversion",
        "vwap_reversion",
        "volume_profile",
        "gap_fill",
        "cross_asset_divergence",
        "funding_rate",
    ):
        # Crypto-specific strategy variants. All share the same
        # maybe_enter(bar, hist, equity, config) -> _Open|None contract
        # as ORB/DRB, so they bypass the confluence-scorer path. The
        # registry wires per-bot defaults; per-bot extras can override
        # individual knobs once we start sweeping params per bot.
        factory = _build_strategy_factory(cell.strategy_kind, cell.extras)
        if cell.strategy_kind == "sage_daily_gated":
            factory = _with_daily_sage_provider(
                factory,
                symbol=cell.symbol,
                instrument_class=str(cell.extras.get("instrument_class") or "crypto"),
            )
        res = WalkForwardEngine().run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=wf,
            base_backtest_config=base_cfg,
            ctx_builder=lambda b, h: {},
            strategy_factory=factory,
        )
    elif cell.strategy_kind == "confluence_scorecard":
        sub_kind = str(cell.extras.get("sub_strategy_kind") or "")
        sc_raw = cell.extras.get("scorecard_config") or {}
        sub_extras = cell.extras.get("sub_strategy_extras") or {}
        if not isinstance(sc_raw, dict):
            sc_raw = {}
        if not isinstance(sub_extras, dict):
            sub_extras = {}

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

        def _confluence_factory() -> object:
            if sub_kind:
                try:
                    sub_factory = _build_strategy_factory(sub_kind, sub_extras)
                    if sub_kind == "cross_asset_divergence":
                        ref_asset = str(sub_extras.get("reference_asset", ""))
                        bot_symbol = str(cell.extras.get("per_ticker_optimal", ""))
                        if ref_asset == "ES1" or "MNQ" in bot_symbol or "NQ" in bot_symbol:
                            sub_factory = _with_cross_asset_ref_provider(
                                sub_factory,
                                bot_symbol=bot_symbol,
                                ref_symbol="ES1",
                                ref_timeframe="5m",
                            )
                        elif ref_asset == "ETH" or "BTC" in bot_symbol:
                            sub_factory = _with_cross_asset_ref_provider(
                                sub_factory,
                                bot_symbol=bot_symbol,
                                ref_symbol="ETH",
                                ref_timeframe="1h",
                            )
                    elif sub_kind == "funding_rate":
                        sub_factory = _with_funding_rate_provider(sub_factory)
                    sub = sub_factory()
                    return ConfluenceScorecardStrategy(sub, sc_cfg)
                except (ValueError, ImportError):
                    pass
            return ConfluenceScorecardStrategy(None, sc_cfg)

        res = WalkForwardEngine().run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=wf,
            base_backtest_config=base_cfg,
            ctx_builder=lambda b, h: {},
            strategy_factory=_confluence_factory,
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
        wf_mode="anchored" if wf.anchored else "rolling",
        dsr_n_trials=res.dsr_n_trials,
        note=_bars_note(ds, bars, max_bars),
    )


def render_table(results: Sequence[CellResult]) -> str:
    header = (
        "| Config | Sym/TF | Scorer | Thr | Gate | WF | DSR N | W | +OOS | IS Sh | "
        "OOS Sh | Deg% | DSR med | DSR pass% | Verdict | Note |"
    )
    lines = [
        header,
        "|---|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in results:
        gate_str = (
            ("/".join(sorted(r.cell.block_regimes)) if r.cell.block_regimes else "—") if r.cell.block_regimes else "—"
        )
        verdict = "PASS" if r.pass_gate else "FAIL"
        lines.append(
            f"| {r.cell.label} | {r.cell.symbol}/{r.cell.timeframe} | {r.cell.scorer_name} | "
            f"{r.cell.threshold:.1f} | {gate_str} | {r.wf_mode} | {r.dsr_n_trials} | "
            f"{r.n_windows} | {r.n_positive_oos} | "
            f"{r.agg_is_sharpe:.3f} | {r.agg_oos_sharpe:.3f} | "
            f"{r.avg_oos_degradation * 100:.1f} | {r.fold_dsr_median:.3f} | "
            f"{r.fold_dsr_pass_fraction * 100:.1f} | {verdict} | {r.note} |"
        )
    return "\n".join(lines)


def classify_research_results(results: Sequence[CellResult]) -> str:
    """Classify whether a grid run belongs in tracked docs or runtime state."""
    if any(result.pass_gate for result in results):
        return "promotable"
    if all(
        result.n_windows == 0
        and (result.note.startswith("NO_DATA") or result.note == "EMPTY_BARS" or result.note.startswith("ERROR:"))
        for result in results
    ):
        return "no_data"
    return "low_signal"


def resolve_report_dir(
    *,
    artifact_class: str,
    policy: str = "auto",
    override: Path | None = None,
) -> Path:
    """Return the output directory for a research-grid report."""
    if override is not None:
        return override
    if policy == "docs" or (policy == "auto" and artifact_class == "promotable"):
        return ROOT / "docs" / "research_log"
    return workspace_roots.ETA_RESEARCH_GRID_RUNTIME_DIR


def build_report_path(log_dir: Path, generated_at: datetime) -> Path:
    """Return a collision-resistant research-grid report path."""
    stamp = generated_at.strftime("%Y%m%d_%H%M%S_%f")
    return log_dir / f"research_grid_{stamp}.md"


def render_report(
    *,
    matrix: Sequence[ResearchCell],
    results: Sequence[CellResult],
    table: str,
    generated_at: datetime,
    artifact_class: str,
) -> str:
    """Render a markdown report with an explicit artifact classification."""
    return (
        f"# Research Grid — {generated_at.isoformat()}\n\n"
        f"Cells: {len(matrix)}\n\n"
        f"Artifact class: `{artifact_class}`\n\n" + table + "\n"
    )


def _parse_bot_filter(raw: str | None) -> set[str] | None:
    """Parse comma-separated bot ids from CLI input."""
    if raw is None:
        return None
    parsed = {part.strip() for part in raw.split(",") if part.strip()}
    return parsed or None


def _limit_matrix(
    matrix: Sequence[ResearchCell],
    *,
    bots: set[str] | None = None,
    max_cells: int | None = None,
) -> list[ResearchCell]:
    """Apply operator-requested limits without changing cell semantics."""
    out = [cell for cell in matrix if bots is None or cell.label in bots]
    if max_cells is not None and max_cells >= 0:
        out = out[:max_cells]
    return out


def _matrix_from_registry(*, include_deactivated: bool = False) -> list[ResearchCell]:
    """Pull one ResearchCell per bot from strategies.per_bot_registry.

    This is the canonical entry point for the per-bot baseline sweep.
    Hand-rolled cells (the ad-hoc matrix below) stay around for quick
    one-off questions, but the registry-driven sweep is what the
    "is anything regressing across the bot fleet" smoke test reads.
    """
    from eta_engine.strategies.per_bot_registry import all_assignments, is_active

    cells: list[ResearchCell] = []
    for a in all_assignments():
        if not include_deactivated and not is_active(a):
            continue
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
    p.add_argument(
        "--report-policy",
        choices=("auto", "docs", "runtime"),
        default="auto",
        help="auto = PASS reports to docs, low-signal/no-data reports to ignored runtime state",
    )
    p.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="override report output directory",
    )
    p.add_argument(
        "--bots",
        default=None,
        help="comma-separated bot_ids to run after matrix construction",
    )
    p.add_argument(
        "--max-cells",
        type=int,
        default=None,
        help="run only the first N cells after filtering; useful for quick smoke batches",
    )
    p.add_argument(
        "--max-bars-per-cell",
        type=int,
        default=None,
        help="cap bars loaded per cell for fast smoke runs; omit for full-history research",
    )
    p.add_argument(
        "--include-deactivated",
        action="store_true",
        help="include registry rows explicitly muted via extras['deactivated']",
    )
    args = p.parse_args()

    base_block = frozenset({"trending_up", "trending_down"})
    if args.source == "registry":
        matrix = _matrix_from_registry(include_deactivated=args.include_deactivated)
    else:
        # Ad-hoc cells preserved for one-off research questions.
        matrix = [
            ResearchCell("5m_ungated", "MNQ1", "5m", "global", 7.0, None, 30, 15, 5),
            ResearchCell("5m_gated_mnq", "MNQ1", "5m", "mnq", 5.0, base_block, 30, 15, 5),
            ResearchCell("1h_gated", "MNQ1", "1h", "mnq", 5.0, base_block, 90, 30, 10),
            ResearchCell("4h_gated", "MNQ1", "4h", "mnq", 5.0, base_block, 180, 60, 10),
            ResearchCell("D_NQ1_gated", "NQ1", "D", "mnq", 5.0, base_block, 365, 180, 10),
        ]
    matrix = _limit_matrix(
        matrix,
        bots=_parse_bot_filter(args.bots),
        max_cells=args.max_cells,
    )
    print(f"[research_grid] running {len(matrix)} cells\n")
    results: list[CellResult] = []
    for cell in matrix:
        print(f"  - {cell.label}: {cell.symbol}/{cell.timeframe} ...")
        try:
            r = run_cell(cell, max_bars=args.max_bars_per_cell)
            results.append(r)
            print(
                f"      -> windows={r.n_windows} "
                f"agg_OOS={r.agg_oos_sharpe:+.3f} pass_frac={r.fold_dsr_pass_fraction * 100:.1f}% "
                f"verdict={'PASS' if r.pass_gate else 'FAIL'}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"      -> ERROR: {exc!r}")
            results.append(
                CellResult(
                    cell=cell,
                    n_windows=0,
                    n_positive_oos=0,
                    agg_is_sharpe=0.0,
                    agg_oos_sharpe=0.0,
                    avg_oos_degradation=0.0,
                    deflated_sharpe=0.0,
                    fold_dsr_median=0.0,
                    fold_dsr_pass_fraction=0.0,
                    pass_gate=False,
                    note=f"ERROR: {type(exc).__name__}",
                )
            )

    table = render_table(results)
    print("\n" + table)

    artifact_class = classify_research_results(results)
    generated_at = datetime.now(UTC)
    log_dir = resolve_report_dir(
        artifact_class=artifact_class,
        policy=args.report_policy,
        override=args.report_dir,
    )
    workspace_roots.ensure_dir(log_dir)
    log_path = build_report_path(log_dir, generated_at)
    log_path.write_text(
        render_report(
            matrix=matrix,
            results=results,
            table=table,
            generated_at=generated_at,
            artifact_class=artifact_class,
        ),
        encoding="utf-8",
    )
    print(f"\n[saved to {log_path} artifact_class={artifact_class}]")
    return 0 if any(r.pass_gate for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

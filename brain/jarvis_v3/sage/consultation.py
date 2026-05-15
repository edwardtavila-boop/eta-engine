"""JARVIS sage consultation entry point.

Builds the registry of every shipped school + provides ``consult_sage(ctx)``
which runs all schools and aggregates them into a SageReport.

Wave-5 enhancements (2026-04-27):
  * regime detection auto-tags ctx.detected_regime if not pre-set
  * per-instrument + per-regime activation honored via SchoolBase.applies_to
  * parallel school evaluation (concurrent.futures)
  * memoization cache (LRU on bar timestamp)
  * edge tracker integration: learned weights modulate confluence
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

from eta_engine.brain.jarvis_v3.sage.confluence import aggregate
from eta_engine.brain.jarvis_v3.sage.regime import detect_regime
from eta_engine.brain.jarvis_v3.sage.schools.cross_asset_correlation import CrossAssetCorrelationSchool
from eta_engine.brain.jarvis_v3.sage.schools.dow_theory import DowTheorySchool
from eta_engine.brain.jarvis_v3.sage.schools.elliott_wave import ElliottWaveSchool
from eta_engine.brain.jarvis_v3.sage.schools.fibonacci import FibonacciSchool
from eta_engine.brain.jarvis_v3.sage.schools.funding_basis import FundingBasisSchool
from eta_engine.brain.jarvis_v3.sage.schools.gann import GannSchool
from eta_engine.brain.jarvis_v3.sage.schools.market_profile import MarketProfileSchool
from eta_engine.brain.jarvis_v3.sage.schools.ml_school import MLSchool
from eta_engine.brain.jarvis_v3.sage.schools.neowave import NEoWaveSchool
from eta_engine.brain.jarvis_v3.sage.schools.onchain import OnChainSchool
from eta_engine.brain.jarvis_v3.sage.schools.options_greeks import OptionsGreeksSchool
from eta_engine.brain.jarvis_v3.sage.schools.order_flow import OrderFlowSchool
from eta_engine.brain.jarvis_v3.sage.schools.red_team import RedTeamSchool
from eta_engine.brain.jarvis_v3.sage.schools.risk_management import RiskManagementSchool
from eta_engine.brain.jarvis_v3.sage.schools.seasonality import SeasonalitySchool
from eta_engine.brain.jarvis_v3.sage.schools.sentiment_pressure import SentimentPressureSchool
from eta_engine.brain.jarvis_v3.sage.schools.smc_ict import SmcIctSchool
from eta_engine.brain.jarvis_v3.sage.schools.stat_significance import StatSignificanceSchool
from eta_engine.brain.jarvis_v3.sage.schools.support_resistance import SupportResistanceSchool
from eta_engine.brain.jarvis_v3.sage.schools.trend_following import TrendFollowingSchool
from eta_engine.brain.jarvis_v3.sage.schools.volatility_regime import VolatilityRegimeSchool
from eta_engine.brain.jarvis_v3.sage.schools.vpa import VPASchool
from eta_engine.brain.jarvis_v3.sage.schools.weis_wyckoff import WeisWyckoffSchool
from eta_engine.brain.jarvis_v3.sage.schools.wyckoff import WyckoffSchool

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.sage.base import (
        MarketContext,
        SageReport,
        SchoolBase,
        SchoolVerdict,
    )

logger = logging.getLogger(__name__)


SCHOOLS: dict[str, SchoolBase] = {
    s.NAME: s
    for s in (
        # Classical (10)
        DowTheorySchool(),
        WyckoffSchool(),
        ElliottWaveSchool(),
        FibonacciSchool(),
        GannSchool(),
        SupportResistanceSchool(),
        TrendFollowingSchool(),
        VPASchool(),
        MarketProfileSchool(),
        RiskManagementSchool(),
        # Modern (4)
        SmcIctSchool(),
        OrderFlowSchool(),
        NEoWaveSchool(),
        WeisWyckoffSchool(),
        # Wave-5 additions (8)
        SeasonalitySchool(),
        VolatilityRegimeSchool(),
        StatSignificanceSchool(),
        RedTeamSchool(),
        OptionsGreeksSchool(),
        FundingBasisSchool(),
        OnChainSchool(),
        CrossAssetCorrelationSchool(),
        SentimentPressureSchool(),
        MLSchool(),
    )
}


# Wave-5 #25 (memoization). Keyed on a stable digest of the full context so
# live consultations don't accidentally reuse a report across different
# risk, order-flow, or optional telemetry inputs.
_CACHE: OrderedDict[str, SageReport] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 256


def _cache_key(ctx: MarketContext, enabled: frozenset[str] | None) -> str:
    first_ts = ctx.bars[0].get("ts", "") if ctx.bars else ""
    last_ts = ctx.bars[-1].get("ts", "") if ctx.bars else ""
    last_close = ctx.bars[-1].get("close", 0) if ctx.bars else 0
    payload = (
        ctx.symbol,
        ctx.side,
        round(ctx.entry_price, 4),
        ctx.n_bars,
        str(first_ts),
        str(last_ts),
        round(float(last_close) if last_close else 0, 4),
        ctx.detected_regime or "",
        ctx.instrument_class or "",
        ctx.order_book_imbalance,
        ctx.cumulative_delta,
        ctx.realized_vol,
        ctx.session_phase or "",
        ctx.account_equity_usd,
        ctx.risk_per_trade_pct,
        ctx.stop_distance_pct,
        ctx.onchain,
        ctx.funding,
        ctx.options,
        ctx.peer_returns,
        ctx.sentiment,
        ctx.liquidation,
        sorted(enabled) if enabled else None,
    )
    encoded = json.dumps(
        payload,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.blake2s(encoded, digest_size=16).hexdigest()


def _observe_health(report: SageReport) -> None:
    """Feed the best-effort health monitor without touching the fast path."""
    try:
        from eta_engine.brain.jarvis_v3.sage.health import default_monitor

        monitor = default_monitor()
        monitor.observe(report)
    except Exception as exc:  # noqa: BLE001 -- health is best-effort
        logger.debug("sage health monitor.observe raised %s (non-fatal)", exc)


def consult_sage(
    ctx: MarketContext,
    *,
    enabled: set[str] | None = None,
    parallel: bool = True,
    use_cache: bool = True,
    apply_edge_weights: bool = True,
) -> SageReport:
    """Consult every (or a filtered subset) of the schools.

    Parameters
    ----------
    ctx: MarketContext (input)
    enabled: optional set of school NAMEs; if provided, only these run
    parallel: run schools in a ThreadPoolExecutor (default True). Set False
              for deterministic ordering in tests / debugging.
    use_cache: memoize on (symbol, last bar ts, side, n_bars, enabled)
    apply_edge_weights: multiply each school's WEIGHT by the EdgeTracker's
                       learned modifier (default True; turn off for backtests).
    """
    enabled_fset: frozenset[str] | None = frozenset(enabled) if enabled is not None else None

    if use_cache:
        key = _cache_key(ctx, enabled_fset)
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                _CACHE.move_to_end(key)
        if cached is not None:
            _observe_health(cached)
            return cached

    # Wave-5 #2: auto-detect regime if not already tagged
    if ctx.detected_regime is None and ctx.n_bars >= 25:
        regime, _signals = detect_regime(ctx)
        ctx = ctx.with_regime(regime.value)

    # Filter to applicable schools (instrument/regime gates + enabled set)
    schools_to_run = []
    for name, school in SCHOOLS.items():
        if enabled_fset is not None and name not in enabled_fset:
            continue
        if not school.applies_to(ctx):
            continue
        schools_to_run.append((name, school))

    # School quality gate: suppress schools with sustained poor performance
    # (hit_rate < 0.35 with > 20 observations). Uses edge tracker data.
    quality_filtered = []
    try:
        from eta_engine.brain.jarvis_v3.sage.edge_tracker import get_tracker

        tracker = get_tracker()
        for name, school in schools_to_run:
            edge = tracker.get(name)
            if edge is not None and edge.n_obs > 20 and edge.hit_rate < 0.35:
                logger.debug("school %s supressed: hit_rate=%.2f n=%d", name, edge.hit_rate, edge.n_obs)
                continue
            quality_filtered.append((name, school))
    except Exception:
        quality_filtered = schools_to_run
    schools_to_run = quality_filtered

    # Pre-compute shared features so every school reuses the same work.
    # EMAs, pivots, volume profiles are computed once and cached.
    _precompute_shared_features(ctx)

    verdicts: dict[str, SchoolVerdict] = {}

    def _run(name: str, school: SchoolBase) -> tuple[str, SchoolVerdict | None]:
        try:
            return name, school.analyze(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("school %s raised: %s", name, exc)
            return name, None

    if parallel and len(schools_to_run) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(schools_to_run))) as ex:
            futures = [ex.submit(_run, n, s) for n, s in schools_to_run]
            for f in as_completed(futures):
                name, v = f.result()
                if v is not None:
                    verdicts[name] = v
    else:
        for n, s in schools_to_run:
            name, v = _run(n, s)
            if v is not None:
                verdicts[name] = v

    report = aggregate(
        verdicts,
        SCHOOLS,
        entry_side=ctx.side,
        regime=ctx.detected_regime,
        apply_edge_weights=apply_edge_weights,
    )

    # Wave-6 (2026-04-27): feed each per-school verdict into the
    # health monitor so silently-broken schools (e.g. always NEUTRAL,
    # always raising) get flagged automatically. Lazy import + try/except
    # so health bugs never crash the consultation loop.
    _observe_health(report)

    if use_cache:
        with _CACHE_LOCK:
            _CACHE[key] = report
            _CACHE.move_to_end(key)
            while len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)

    # Drop per-ctx feature cache to prevent unbounded memory growth
    try:
        from eta_engine.brain.jarvis_v3.sage.feature_cache import clear_for_ctx

        clear_for_ctx(ctx)
    except Exception:  # noqa: BLE001
        pass

    return report


def clear_sage_cache() -> int:
    """Drop the memoized cache. Returns the number of entries cleared."""
    with _CACHE_LOCK:
        n = len(_CACHE)
        _CACHE.clear()
        return n


def _precompute_shared_features(ctx: MarketContext) -> None:
    """Eagerly compute shared features into the per-context cache.

    Every school that calls ``get_or_compute`` for these keys gets a
    cache hit instead of recomputing. Skipping this has zero behavioral
    impact -- it's a pure optimization.
    """
    from eta_engine.brain.jarvis_v3.sage.feature_cache import get_or_compute

    n = ctx.n_bars
    if n < 10:
        return
    closes = ctx.closes()
    highs = ctx.highs()
    lows = ctx.lows()
    volumes = ctx.volumes()
    get_or_compute(ctx, "ema_20", lambda: _ema(closes, 20))
    get_or_compute(ctx, "ema_50", lambda: _ema(closes, 50))
    get_or_compute(ctx, "avg_vol_20", lambda: sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0.0)
    get_or_compute(ctx, "pivot_highs", lambda: _find_pivots(highs, kind="high"))
    get_or_compute(ctx, "pivot_lows", lambda: _find_pivots(lows, kind="low"))
    get_or_compute(ctx, "range_high_20", lambda: max(highs[-21:-1]) if len(highs) >= 21 else 0.0)
    get_or_compute(ctx, "range_low_20", lambda: min(lows[-21:-1]) if len(lows) >= 21 else 0.0)


def _find_pivots(values: list[float], lookback: int = 3, *, kind: str = "high") -> list[tuple[int, float]]:
    if kind not in ("high", "low"):
        raise ValueError("kind must be 'high' or 'low'")
    out: list[tuple[int, float]] = []
    for i in range(lookback, len(values) - lookback):
        window = values[i - lookback : i + lookback + 1]
        if kind == "high" and values[i] == max(window) or kind == "low" and values[i] == min(window):
            out.append((i, values[i]))
    return out


def _ema(values: list[float], period: int) -> list[float]:
    if not values or period < 1:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out

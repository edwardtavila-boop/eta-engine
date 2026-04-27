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

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from eta_engine.brain.jarvis_v3.sage.base import (
    MarketContext,
    SageReport,
    SchoolBase,
    SchoolVerdict,
)
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
from eta_engine.brain.jarvis_v3.sage.schools.smc_ict import SmcIctSchool
from eta_engine.brain.jarvis_v3.sage.schools.stat_significance import StatSignificanceSchool
from eta_engine.brain.jarvis_v3.sage.schools.support_resistance import SupportResistanceSchool
from eta_engine.brain.jarvis_v3.sage.schools.trend_following import TrendFollowingSchool
from eta_engine.brain.jarvis_v3.sage.schools.volatility_regime import VolatilityRegimeSchool
from eta_engine.brain.jarvis_v3.sage.schools.vpa import VPASchool
from eta_engine.brain.jarvis_v3.sage.schools.weis_wyckoff import WeisWyckoffSchool
from eta_engine.brain.jarvis_v3.sage.schools.wyckoff import WyckoffSchool

logger = logging.getLogger(__name__)


SCHOOLS: dict[str, SchoolBase] = {
    s.NAME: s for s in (
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
        MLSchool(),
    )
}


# Wave-5 #25 (memoization). Keyed on (symbol, last bar ts, side, n_bars).
_CACHE: dict[tuple, SageReport] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 256


def _cache_key(ctx: MarketContext, enabled: frozenset[str] | None) -> tuple:
    last_bar = ctx.bars[-1] if ctx.bars else {}
    last_ts = last_bar.get("ts") or last_bar.get("timestamp") or last_bar.get("time")
    return (ctx.symbol, str(last_ts), ctx.side, ctx.n_bars,
            enabled if enabled else None)


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
    enabled_fset: frozenset[str] | None = (
        frozenset(enabled) if enabled is not None else None
    )

    if use_cache:
        key = _cache_key(ctx, enabled_fset)
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                return cached

    # Wave-5 #2: auto-detect regime if not already tagged
    if ctx.detected_regime is None and ctx.n_bars >= 25:
        regime, _signals = detect_regime(ctx)
        ctx = MarketContext(
            bars=ctx.bars,
            side=ctx.side,
            entry_price=ctx.entry_price,
            symbol=ctx.symbol,
            bars_by_tf=ctx.bars_by_tf,
            order_book_imbalance=ctx.order_book_imbalance,
            cumulative_delta=ctx.cumulative_delta,
            realized_vol=ctx.realized_vol,
            session_phase=ctx.session_phase,
            account_equity_usd=ctx.account_equity_usd,
            risk_per_trade_pct=ctx.risk_per_trade_pct,
            stop_distance_pct=ctx.stop_distance_pct,
            detected_regime=regime.value,
            instrument_class=ctx.instrument_class,
            # Wave-6 pre-live: preserve scaffold-school payloads on rebuild
            onchain=ctx.onchain,
            funding=ctx.funding,
            options=ctx.options,
        )

    # Filter to applicable schools (instrument/regime gates + enabled set)
    schools_to_run = []
    for name, school in SCHOOLS.items():
        if enabled_fset is not None and name not in enabled_fset:
            continue
        if not school.applies_to(ctx):
            continue
        schools_to_run.append((name, school))

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
        verdicts, SCHOOLS,
        entry_side=ctx.side,
        regime=ctx.detected_regime,
        apply_edge_weights=apply_edge_weights,
    )

    # Wave-6 (2026-04-27): feed each per-school verdict into the
    # health monitor so silently-broken schools (e.g. always NEUTRAL,
    # always raising) get flagged automatically. Lazy import + try/except
    # so health bugs never crash the consultation loop.
    try:
        from eta_engine.brain.jarvis_v3.sage.health import default_monitor
        monitor = default_monitor()
        monitor.observe(report)
    except Exception as exc:  # noqa: BLE001 -- health is best-effort
        logger.debug("sage health monitor.observe raised %s (non-fatal)", exc)

    if use_cache:
        with _CACHE_LOCK:
            if len(_CACHE) > _CACHE_MAX:
                _CACHE.clear()  # simple eviction
            _CACHE[key] = report

    return report


def clear_sage_cache() -> int:
    """Drop the memoized cache. Returns the number of entries cleared."""
    with _CACHE_LOCK:
        n = len(_CACHE)
        _CACHE.clear()
        return n

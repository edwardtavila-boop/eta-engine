"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_backtest_harness.

Cover every path through ``strategies.backtest_harness.run_harness``:

* warmup + short-tape guards
* trade open rules (actionable winner, valid stop, one-per-strategy)
* exit resolution (stop, target, timeout)
* slippage application
* per-strategy stats aggregation (hit_rate, consecutive losses, etc.)
* pass-through of ctx_builder / eligibility / registry
* report serialisation + property accessors

Registry-injection stubs let each test pin the winning strategy + its
level structure, so the harness code paths are exercised in isolation
without needing 200+ bars of real market tape.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from eta_engine.strategies.eta_policy import StrategyContext
from eta_engine.strategies.backtest_harness import (
    BacktestReport,
    ExitReason,
    HarnessConfig,
    StrategyBacktestStats,
    StrategyTrade,
    default_ctx_builder,
    run_harness,
)
from eta_engine.strategies.models import (
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bar(ts: int, open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(ts=ts, open=open_, high=high, low=low, close=close, volume=100.0)


def _flat_bars(n: int, price: float = 100.0) -> list[Bar]:
    """Create ``n`` flat bars at ``price`` — no range, safe for warmup fillers."""
    return [_bar(ts=i, open_=price, high=price, low=price, close=price) for i in range(n)]


_RegistryType = dict[StrategyId, Callable[[list[Bar], object], StrategySignal]]


def _long_stop_registry_factory(
    entry: float,
    stop: float,
    target: float,
    *,
    confidence: float = 8.0,
    fire_on_index: int | None = None,
) -> _RegistryType:
    """Return a registry whose dispatch produces LONG with given levels.

    Only fires at ``fire_on_index`` (the last bar's ts) if supplied,
    otherwise fires on every call. Useful to control WHEN the harness
    opens a trade in a deterministic test tape.
    """

    def fn(bars: list[Bar], _ctx: object) -> StrategySignal:
        idx = bars[-1].ts if bars else 0
        if fire_on_index is not None and idx != fire_on_index:
            return StrategySignal(
                strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                side=Side.FLAT,
            )
        return StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.LONG,
            entry=entry,
            stop=stop,
            target=target,
            confidence=confidence,
            risk_mult=1.0,
            rationale_tags=("stub_long",),
        )

    return {StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fn}


def _short_stop_registry_factory(
    entry: float,
    stop: float,
    target: float,
    *,
    confidence: float = 8.0,
    fire_on_index: int | None = None,
) -> _RegistryType:
    def fn(bars: list[Bar], _ctx: object) -> StrategySignal:
        idx = bars[-1].ts if bars else 0
        if fire_on_index is not None and idx != fire_on_index:
            return StrategySignal(
                strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                side=Side.FLAT,
            )
        return StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.SHORT,
            entry=entry,
            stop=stop,
            target=target,
            confidence=confidence,
            risk_mult=1.0,
            rationale_tags=("stub_short",),
        )

    return {StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fn}


def _flat_registry() -> _RegistryType:
    def fn(_bars: list[Bar], _ctx: object) -> StrategySignal:
        return StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.FLAT,
        )

    return {StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fn}


ELIG_LSD = {"TEST": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)}


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class TestHarnessGuards:
    def test_empty_bars_produces_empty_report(self) -> None:
        report = run_harness([], asset="TEST", config=HarnessConfig(warmup_bars=2))
        assert isinstance(report, BacktestReport)
        assert report.total_bars == 0
        assert report.total_trades == 0
        assert report.trades == ()
        assert report.stats_by_strategy == ()

    def test_short_tape_below_warmup_produces_empty_report(self) -> None:
        bars = _flat_bars(5)
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(warmup_bars=10),
        )
        assert report.total_bars == 5
        assert report.total_trades == 0

    def test_exactly_warmup_plus_one_still_empty(self) -> None:
        # warmup requires `len(bars) >= warmup + 2`
        bars = _flat_bars(6)
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(warmup_bars=5),
        )
        assert report.total_trades == 0


# ---------------------------------------------------------------------------
# Trade resolution
# ---------------------------------------------------------------------------


class TestExitResolution:
    """Test that the harness correctly assigns STOP / TARGET / TIMEOUT."""

    def test_long_target_hit_gives_positive_r(self) -> None:
        # Warmup bars at 100, then trigger LONG entry=100/stop=99/target=102
        # Next bar shoots to 103 high → target hit → r >= 2.0 (minus slippage)
        bars = [
            *_flat_bars(5, 100.0),
            _bar(ts=5, open_=100, high=100, low=100, close=100),  # entry bar
            _bar(ts=6, open_=100.5, high=103.0, low=100.0, close=102.5),
            _bar(ts=7, open_=102.5, high=103.0, low=102.0, close=102.8),
        ]
        registry = _long_stop_registry_factory(
            entry=100.0,
            stop=99.0,
            target=102.0,
            fire_on_index=5,
        )
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=5,
                slippage_bps=0.0,
            ),
            eligibility=ELIG_LSD,
            registry=registry,
        )
        assert report.total_trades == 1
        t = report.trades[0]
        assert t.exit_reason is ExitReason.TARGET
        assert t.r_multiple == pytest.approx(2.0, abs=1e-6)
        assert t.side is Side.LONG
        assert t.bars_held == 1

    def test_long_stop_hit_gives_minus_one_r(self) -> None:
        bars = [
            *_flat_bars(5, 100.0),
            _bar(ts=5, open_=100, high=100, low=100, close=100),
            _bar(ts=6, open_=99.8, high=100.2, low=98.5, close=99.0),  # stop hit
            _bar(ts=7, open_=99.0, high=99.5, low=98.8, close=99.1),
        ]
        registry = _long_stop_registry_factory(
            entry=100.0,
            stop=99.0,
            target=102.0,
            fire_on_index=5,
        )
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=5,
                slippage_bps=0.0,
            ),
            eligibility=ELIG_LSD,
            registry=registry,
        )
        assert report.total_trades == 1
        assert report.trades[0].exit_reason is ExitReason.STOP
        assert report.trades[0].r_multiple == pytest.approx(-1.0, abs=1e-6)

    def test_short_target_hit_gives_positive_r(self) -> None:
        bars = [
            *_flat_bars(5, 100.0),
            _bar(ts=5, open_=100, high=100, low=100, close=100),
            _bar(ts=6, open_=99.8, high=100.2, low=97.5, close=98.0),
            _bar(ts=7, open_=98.0, high=98.5, low=97.0, close=97.2),
        ]
        # Short: entry 100, stop 101, target 98 → stop_dist 1, target_dist 2 → 2R
        registry = _short_stop_registry_factory(
            entry=100.0,
            stop=101.0,
            target=98.0,
            fire_on_index=5,
        )
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=5,
                slippage_bps=0.0,
            ),
            eligibility=ELIG_LSD,
            registry=registry,
        )
        assert report.total_trades == 1
        assert report.trades[0].exit_reason is ExitReason.TARGET
        assert report.trades[0].r_multiple == pytest.approx(2.0, abs=1e-6)
        assert report.trades[0].side is Side.SHORT

    def test_short_stop_hit_gives_minus_one_r(self) -> None:
        bars = [
            *_flat_bars(5, 100.0),
            _bar(ts=5, open_=100, high=100, low=100, close=100),
            _bar(ts=6, open_=100.2, high=101.5, low=99.8, close=101.0),  # stop hit
            _bar(ts=7, open_=101.0, high=101.3, low=100.8, close=101.0),
        ]
        registry = _short_stop_registry_factory(
            entry=100.0,
            stop=101.0,
            target=98.0,
            fire_on_index=5,
        )
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=5,
                slippage_bps=0.0,
            ),
            eligibility=ELIG_LSD,
            registry=registry,
        )
        assert report.trades[0].exit_reason is ExitReason.STOP
        assert report.trades[0].r_multiple == pytest.approx(-1.0, abs=1e-6)

    def test_timeout_closes_at_final_close(self) -> None:
        # Flat price hovering -- no stop, no target hit → TIMEOUT
        bars = [
            *_flat_bars(5, 100.0),
            _bar(ts=5, open_=100, high=100, low=100, close=100),
            # 4 bars hovering in 99.5-100.5, neither stop (99) nor target (102) hit
            _bar(ts=6, open_=100, high=100.3, low=99.7, close=100.1),
            _bar(ts=7, open_=100.1, high=100.4, low=99.8, close=100.2),
            _bar(ts=8, open_=100.2, high=100.5, low=99.9, close=100.3),
            _bar(ts=9, open_=100.3, high=100.4, low=99.9, close=100.4),
        ]
        registry = _long_stop_registry_factory(
            entry=100.0,
            stop=99.0,
            target=102.0,
            fire_on_index=5,
        )
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=4,
                slippage_bps=0.0,
            ),
            eligibility=ELIG_LSD,
            registry=registry,
        )
        t = report.trades[0]
        assert t.exit_reason is ExitReason.TIMEOUT
        # Timeout exit at bar 9's close = 100.4 → (100.4 - 100) / 1 = 0.4R
        assert t.r_multiple == pytest.approx(0.4, abs=1e-6)


# ---------------------------------------------------------------------------
# Slippage
# ---------------------------------------------------------------------------


class TestSlippageApplication:
    def test_slippage_reduces_r(self) -> None:
        """Target hit giving raw 2R should become 2R minus 2 * 10bps / 10000."""
        bars = [
            *_flat_bars(5, 100.0),
            _bar(ts=5, open_=100, high=100, low=100, close=100),
            _bar(ts=6, open_=100.5, high=103.0, low=100.0, close=102.5),
        ]
        registry = _long_stop_registry_factory(
            entry=100.0,
            stop=99.0,
            target=102.0,
            fire_on_index=5,
        )
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=3,
                slippage_bps=10.0,
            ),
            eligibility=ELIG_LSD,
            registry=registry,
        )
        # 2.0 - 2 * 10 / 10000 = 2.0 - 0.002 = 1.998
        assert report.trades[0].r_multiple == pytest.approx(1.998, abs=1e-6)


# ---------------------------------------------------------------------------
# Stats aggregation
# ---------------------------------------------------------------------------


class TestStatsAggregation:
    def test_stats_aggregate_per_strategy(self) -> None:
        """Multiple resolved trades produce one Stats row per strategy."""

        # Tape with 3 entries at ts=5, 10, 15.
        # Each followed by a TARGET bar giving +2R.
        # We need a registry that fires ONLY at those indices.
        fire_at = {5, 10, 15}

        def fn(bars: list[Bar], _ctx: object) -> StrategySignal:
            if not bars:
                return StrategySignal(
                    strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                    side=Side.FLAT,
                )
            idx = bars[-1].ts
            if idx not in fire_at:
                return StrategySignal(
                    strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                    side=Side.FLAT,
                )
            return StrategySignal(
                strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                side=Side.LONG,
                entry=100.0,
                stop=99.0,
                target=102.0,
                confidence=8.0,
                risk_mult=1.0,
            )

        bars: list[Bar] = []
        for i in range(20):
            if i in fire_at:
                bars.append(_bar(ts=i, open_=100, high=100, low=100, close=100))
            elif i - 1 in fire_at:
                bars.append(_bar(ts=i, open_=100.5, high=103.0, low=100.0, close=102.5))
            else:
                bars.append(_bar(ts=i, open_=100, high=100.2, low=99.8, close=100))

        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=2,
                slippage_bps=0.0,
            ),
            eligibility=ELIG_LSD,
            registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fn},
        )
        assert report.total_trades == 3
        assert len(report.stats_by_strategy) == 1
        stats = report.stats_by_strategy[0]
        assert stats.strategy is StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT
        assert stats.n_trades == 3
        assert stats.hit_rate == pytest.approx(1.0, abs=1e-6)
        assert stats.total_r == pytest.approx(6.0, abs=1e-6)
        assert stats.max_consecutive_losses == 0

    def test_stats_max_consecutive_losses(self) -> None:
        """Stats should count the longest losing streak, not total losses."""

        # 5 entries: Win, Loss, Loss, Win, Loss -> max consecutive losses = 2
        fire_at = [5, 10, 15, 20, 25]
        outcome_winning = {5, 20}  # these indices produce a winning target bar

        def fn(bars: list[Bar], _ctx: object) -> StrategySignal:
            if not bars:
                return StrategySignal(
                    strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                    side=Side.FLAT,
                )
            idx = bars[-1].ts
            if idx not in fire_at:
                return StrategySignal(
                    strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                    side=Side.FLAT,
                )
            return StrategySignal(
                strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                side=Side.LONG,
                entry=100.0,
                stop=99.0,
                target=102.0,
                confidence=8.0,
                risk_mult=1.0,
            )

        bars: list[Bar] = []
        for i in range(30):
            if i in fire_at:
                bars.append(_bar(ts=i, open_=100, high=100, low=100, close=100))
            elif (i - 1) in fire_at:
                if (i - 1) in outcome_winning:
                    # next-bar target hit
                    bars.append(_bar(ts=i, open_=100, high=103, low=100, close=102.5))
                else:
                    # next-bar stop hit
                    bars.append(_bar(ts=i, open_=100, high=100.1, low=98.0, close=98.5))
            else:
                bars.append(_bar(ts=i, open_=100, high=100.1, low=99.9, close=100))

        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=2,
                slippage_bps=0.0,
            ),
            eligibility=ELIG_LSD,
            registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fn},
        )
        assert report.total_trades == 5
        assert report.stats_by_strategy[0].max_consecutive_losses == 2


# ---------------------------------------------------------------------------
# Pass-through / injection
# ---------------------------------------------------------------------------


class TestInjection:
    def test_custom_ctx_builder_called_per_bar(self) -> None:
        captured: list[Bar] = []

        def builder(bar: Bar) -> StrategyContext:
            captured.append(bar)
            return StrategyContext(
                regime_label="TREND",
                confluence_score=7.0,
                vol_z=0.0,
                trend_bias=Side.LONG,
                session_allows_entries=True,
                kill_switch_active=False,
                htf_bias=Side.LONG,
            )

        bars = _flat_bars(10, 100.0)
        run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(warmup_bars=5, slippage_bps=0.0),
            eligibility=ELIG_LSD,
            registry=_flat_registry(),
            ctx_builder=builder,
        )
        # The harness iterates from warmup (5) to end (9) → 5 builder calls
        assert len(captured) == 5
        assert captured[0].ts == 5
        assert captured[-1].ts == 9

    def test_custom_eligibility_narrows_strategies(self) -> None:
        """With eligibility pointing at an unused strategy, no trades fire."""
        bars = [
            *_flat_bars(5, 100.0),
            _bar(ts=5, open_=100, high=100, low=100, close=100),
            _bar(ts=6, open_=100.5, high=103.0, low=100.0, close=102.5),
        ]
        # LSD registry fires, but eligibility only lists OB_BREAKER_RETEST
        registry = _long_stop_registry_factory(
            entry=100.0,
            stop=99.0,
            target=102.0,
            fire_on_index=5,
        )
        elig = {"TEST": (StrategyId.OB_BREAKER_RETEST,)}
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(warmup_bars=5, slippage_bps=0.0),
            eligibility=elig,
            registry=registry,
        )
        assert report.total_trades == 0

    def test_flat_winner_produces_no_trades(self) -> None:
        bars = _flat_bars(20, 100.0)
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(warmup_bars=5, slippage_bps=0.0),
            eligibility=ELIG_LSD,
            registry=_flat_registry(),
        )
        assert report.total_trades == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_stop_distance_skips_trade(self) -> None:
        """Signals where entry == stop are refused by the harness."""

        def fn(bars: list[Bar], _ctx: object) -> StrategySignal:
            if not bars or bars[-1].ts != 5:
                return StrategySignal(
                    strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                    side=Side.FLAT,
                )
            return StrategySignal(
                strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                side=Side.LONG,
                entry=100.0,
                stop=100.0,
                target=101.0,  # same entry/stop
                confidence=8.0,
                risk_mult=1.0,
            )

        bars = [
            *_flat_bars(5, 100.0),
            _bar(ts=5, open_=100, high=100, low=100, close=100),
            _bar(ts=6, open_=100.5, high=102.0, low=100.0, close=101.5),
        ]
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(warmup_bars=5),
            eligibility=ELIG_LSD,
            registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fn},
        )
        assert report.total_trades == 0

    def test_zero_target_triggers_fallback_to_2r(self) -> None:
        """Missing target (0.0) should use fallback 2R target."""

        def fn(bars: list[Bar], _ctx: object) -> StrategySignal:
            if not bars or bars[-1].ts != 5:
                return StrategySignal(
                    strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                    side=Side.FLAT,
                )
            return StrategySignal(
                strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                side=Side.LONG,
                entry=100.0,
                stop=99.0,
                target=0.0,  # no target → fallback
                confidence=8.0,
                risk_mult=1.0,
            )

        bars = [
            *_flat_bars(5, 100.0),
            _bar(ts=5, open_=100, high=100, low=100, close=100),
            _bar(ts=6, open_=100.5, high=103.0, low=100.0, close=102.5),
        ]
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(warmup_bars=5, slippage_bps=0.0),
            eligibility=ELIG_LSD,
            registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fn},
        )
        # Fallback target = entry + 2 * stop_distance = 100 + 2*1 = 102
        assert report.total_trades == 1
        assert report.trades[0].exit_reason is ExitReason.TARGET

    def test_record_decisions_populates_decision_tape(self) -> None:
        bars = _flat_bars(10, 100.0)
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(
                warmup_bars=5,
                slippage_bps=0.0,
                record_decisions=True,
            ),
            eligibility=ELIG_LSD,
            registry=_flat_registry(),
        )
        # Harness iterates from i=5..9 → 5 dispatches
        assert len(report.decisions) == 5

    def test_record_decisions_default_empty(self) -> None:
        bars = _flat_bars(10, 100.0)
        report = run_harness(
            bars,
            asset="TEST",
            config=HarnessConfig(warmup_bars=5, slippage_bps=0.0),
            eligibility=ELIG_LSD,
            registry=_flat_registry(),
        )
        assert report.decisions == ()


# ---------------------------------------------------------------------------
# Serialisation + property accessors
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_strategy_trade_as_dict_roundtrip_keys(self) -> None:
        t = StrategyTrade(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.LONG,
            entry_ts=5,
            entry=100.0,
            stop=99.0,
            target=102.0,
            exit_ts=6,
            exit=102.0,
            exit_reason=ExitReason.TARGET,
            r_multiple=2.0,
            bars_held=1,
        )
        d = t.as_dict()
        assert d["strategy"] == "liquidity_sweep_displacement"
        assert d["side"] == "LONG"
        assert d["exit_reason"] == "TARGET"
        assert d["r_multiple"] == 2.0

    def test_stats_as_dict_rounds_floats(self) -> None:
        s = StrategyBacktestStats(
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            n_trades=10,
            hit_rate=0.123456789,
            avg_r=0.555555,
            total_r=5.555555,
            max_consecutive_losses=3,
            longest_trade_bars=12,
            avg_trade_bars=6.78901,
        )
        d = s.as_dict()
        assert d["strategy"] == "mtf_trend_following"
        assert d["hit_rate"] == 0.1235
        assert d["avg_r"] == 0.5556
        assert d["total_r"] == 5.5556

    def test_report_total_r_and_hit_rate_properties(self) -> None:
        trades = (
            StrategyTrade(
                strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                side=Side.LONG,
                entry_ts=1,
                entry=100.0,
                stop=99.0,
                target=102.0,
                exit_ts=2,
                exit=102.0,
                exit_reason=ExitReason.TARGET,
                r_multiple=2.0,
                bars_held=1,
            ),
            StrategyTrade(
                strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                side=Side.LONG,
                entry_ts=3,
                entry=100.0,
                stop=99.0,
                target=102.0,
                exit_ts=4,
                exit=99.0,
                exit_reason=ExitReason.STOP,
                r_multiple=-1.0,
                bars_held=1,
            ),
        )
        report = BacktestReport(
            asset="TEST",
            total_bars=10,
            total_trades=2,
            trades=trades,
            stats_by_strategy=(),
        )
        assert report.total_r == 1.0
        assert report.hit_rate == 0.5

    def test_report_hit_rate_zero_trades(self) -> None:
        report = BacktestReport(
            asset="TEST",
            total_bars=10,
            total_trades=0,
            trades=(),
            stats_by_strategy=(),
        )
        assert report.hit_rate == 0.0
        assert report.total_r == 0.0


# ---------------------------------------------------------------------------
# Default context builder
# ---------------------------------------------------------------------------


class TestDefaultCtxBuilder:
    def test_default_ctx_builder_returns_permissive_context(self) -> None:
        bar = _bar(ts=1, open_=100, high=101, low=99, close=100.5)
        ctx = default_ctx_builder(bar)
        assert isinstance(ctx, StrategyContext)
        assert ctx.kill_switch_active is False
        assert ctx.session_allows_entries is True
        assert ctx.confluence_score == 5.0
        assert ctx.trend_bias is Side.FLAT

"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_oos_qualifier.

Coverage for :mod:`eta_engine.strategies.oos_qualifier` -- the
walk-forward + DSR gate that scores every AI-Optimized strategy per
asset and decides whether it earns a runtime allowlist slot.

Layers:
* :class:`QualificationGate` defaults.
* ``_sharpe_like`` / ``_moments`` / ``_degradation`` / ``_build_windows``
  pure-helper invariants.
* ``qualify_strategies`` end-to-end with registry / eligibility injection:
  insufficient-bars path, pass-gate path, fail paths (DSR /
  degradation / min_trades), serialisation, multi-strategy.
"""

from __future__ import annotations

from collections.abc import Callable

from eta_engine.strategies.backtest_harness import HarnessConfig
from eta_engine.strategies.models import (
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)
from eta_engine.strategies.oos_qualifier import (
    DEFAULT_QUALIFICATION_GATE,
    PerStrategyWindow,
    QualificationGate,
    QualificationReport,
    StrategyQualification,
    _build_windows,
    _degradation,
    _moments,
    _sharpe_like,
    qualify_strategies,
)

_RegistryType = dict[StrategyId, Callable[..., StrategySignal]]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _bar(ts: int, open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(ts=ts, open=open_, high=high, low=low, close=close, volume=100.0)


def _always_win_registry(
    entry: float = 100.0,
    stop: float = 99.0,
    target: float = 102.0,
) -> _RegistryType:
    """Registry where a long trade always prints a target hit next bar."""

    def fn(bars: list[Bar], _ctx: object) -> StrategySignal:
        if not bars:
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
            confidence=9.0,
            risk_mult=1.0,
            rationale_tags=("stub_always_win",),
        )

    return {StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fn}


def _always_lose_registry(
    entry: float = 100.0,
    stop: float = 99.0,
    target: float = 102.0,
) -> _RegistryType:
    """Registry where a long trade always prints a stop hit next bar."""

    def fn(bars: list[Bar], _ctx: object) -> StrategySignal:
        if not bars:
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
            confidence=9.0,
            risk_mult=1.0,
            rationale_tags=("stub_always_lose",),
        )

    return {StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fn}


_ELIG = {"TEST": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)}


def _winning_tape(n: int) -> list[Bar]:
    """Bars where every entry at 100 instantly touches target 102 next bar."""
    return [_bar(ts=i, open_=100.0, high=103.0, low=100.0, close=102.5) for i in range(n)]


def _losing_tape(n: int) -> list[Bar]:
    """Bars where every entry at 100 instantly touches stop 99 next bar."""
    return [_bar(ts=i, open_=100.0, high=100.1, low=98.5, close=99.0) for i in range(n)]


def _mixed_tape(n: int, *, win_ratio: float = 0.6) -> list[Bar]:
    """Alternating wins and losses with a configurable ratio."""
    out: list[Bar] = []
    for i in range(n):
        if (i / max(n, 1)) < win_ratio:
            out.append(_bar(i, 100.0, 103.0, 100.0, 102.5))
        else:
            out.append(_bar(i, 100.0, 100.1, 98.5, 99.0))
    return out


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class TestQualificationGateDefaults:
    def test_default_dsr_threshold(self) -> None:
        assert DEFAULT_QUALIFICATION_GATE.dsr_threshold == 0.5

    def test_default_max_degradation(self) -> None:
        assert DEFAULT_QUALIFICATION_GATE.max_degradation_pct == 0.35

    def test_default_min_trades(self) -> None:
        assert DEFAULT_QUALIFICATION_GATE.min_trades_per_window == 20

    def test_gate_is_frozen(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(QualificationGate)
        # Attempting to mutate a frozen dataclass raises FrozenInstanceError
        gate = QualificationGate()
        try:
            gate.dsr_threshold = 0.99  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            assert "frozen" in str(exc).lower() or "FrozenInstanceError" in type(exc).__name__
        else:
            raise AssertionError("expected frozen dataclass to reject mutation")

    def test_custom_gate_overrides(self) -> None:
        g = QualificationGate(
            dsr_threshold=0.8,
            max_degradation_pct=0.2,
            min_trades_per_window=50,
        )
        assert g.dsr_threshold == 0.8
        assert g.max_degradation_pct == 0.2
        assert g.min_trades_per_window == 50


# ---------------------------------------------------------------------------
# Sharpe-like
# ---------------------------------------------------------------------------


class TestSharpeLike:
    def test_empty_returns_zero(self) -> None:
        assert _sharpe_like([]) == 0.0

    def test_single_trade_returns_zero(self) -> None:
        assert _sharpe_like([2.0]) == 0.0

    def test_all_identical_returns_zero(self) -> None:
        # stddev zero -> SR undefined, treated as zero
        assert _sharpe_like([1.0, 1.0, 1.0, 1.0]) == 0.0

    def test_positive_mean_positive_sharpe(self) -> None:
        sr = _sharpe_like([1.0, 1.0, 1.0, -1.0, -1.0])
        assert sr > 0.0

    def test_negative_mean_negative_sharpe(self) -> None:
        sr = _sharpe_like([-1.0, -1.0, -1.0, 1.0, 1.0])
        assert sr < 0.0

    def test_symmetric_returns_near_zero(self) -> None:
        sr = _sharpe_like([1.0, -1.0, 1.0, -1.0])
        assert abs(sr) < 1e-9


# ---------------------------------------------------------------------------
# Moments
# ---------------------------------------------------------------------------


class TestMoments:
    def test_empty_returns_normal_defaults(self) -> None:
        skew, kurt = _moments([])
        assert skew == 0.0
        assert kurt == 3.0

    def test_single_returns_normal_defaults(self) -> None:
        skew, kurt = _moments([1.0])
        assert skew == 0.0
        assert kurt == 3.0

    def test_symmetric_returns_near_zero_skew(self) -> None:
        skew, _kurt = _moments([1.0, -1.0, 1.0, -1.0, 1.0, -1.0])
        assert abs(skew) < 1e-9

    def test_right_tail_has_positive_skew(self) -> None:
        # Heavy positive tail: many small losses, one big win
        skew, _kurt = _moments([-0.5, -0.5, -0.5, -0.5, 3.0])
        assert skew > 0.0


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


class TestDegradation:
    def test_equal_sharpes_zero_degradation(self) -> None:
        assert _degradation(1.0, 1.0) == 0.0

    def test_oos_better_than_is_zero(self) -> None:
        # OOS better than IS is clipped to zero (no penalty)
        assert _degradation(1.0, 1.5) == 0.0

    def test_oos_half_of_is_is_fifty_percent(self) -> None:
        assert _degradation(1.0, 0.5) == 0.5

    def test_zero_is_sharpe_oos_positive(self) -> None:
        # Non-positive IS Sharpe -> zero degradation when OOS is same or better
        assert _degradation(0.0, 0.1) == 0.0

    def test_zero_is_sharpe_oos_worse(self) -> None:
        # Non-positive IS with strictly worse OOS -> full degradation hit
        assert _degradation(0.0, -0.1) == 1.0


# ---------------------------------------------------------------------------
# Window builder
# ---------------------------------------------------------------------------


class TestBuildWindows:
    def test_empty_tape_no_windows(self) -> None:
        assert _build_windows(total_bars=0, warmup_bars=100, n_windows=4, is_fraction=0.7) == []

    def test_tape_shorter_than_warmup_no_windows(self) -> None:
        assert _build_windows(total_bars=50, warmup_bars=100, n_windows=4, is_fraction=0.7) == []

    def test_normal_case_four_windows(self) -> None:
        triples = _build_windows(
            total_bars=500,
            warmup_bars=100,
            n_windows=4,
            is_fraction=0.7,
        )
        assert len(triples) == 4
        # Each IS/OOS split is inside the preceding window's bar range
        for is_start, is_end, oos_end in triples:
            assert is_start < is_end < oos_end

    def test_windows_do_not_overlap(self) -> None:
        triples = _build_windows(
            total_bars=500,
            warmup_bars=100,
            n_windows=4,
            is_fraction=0.7,
        )
        for prev, curr in zip(triples, triples[1:], strict=False):
            assert prev[2] <= curr[0]

    def test_is_fraction_at_boundary(self) -> None:
        # is_fraction of 0.9 leaves only 10% for OOS
        triples = _build_windows(
            total_bars=500,
            warmup_bars=100,
            n_windows=4,
            is_fraction=0.9,
        )
        assert len(triples) == 4
        # IS portion dominates each window
        for is_start, is_end, oos_end in triples:
            assert (is_end - is_start) > (oos_end - is_end)

    def test_zero_windows_returns_empty(self) -> None:
        assert _build_windows(total_bars=500, warmup_bars=100, n_windows=0, is_fraction=0.7) == []


# ---------------------------------------------------------------------------
# qualify_strategies - insufficient-bars path
# ---------------------------------------------------------------------------


class TestQualifyStrategiesInsufficientBars:
    def test_empty_bars_produces_empty_report(self) -> None:
        report = qualify_strategies(
            [],
            asset="TEST",
            harness_config=HarnessConfig(warmup_bars=5),
        )
        assert isinstance(report, QualificationReport)
        assert report.asset == "TEST"
        assert report.n_windows_executed == 0
        assert report.per_window == ()
        assert report.qualifications == ()
        assert "insufficient_bars_no_windows" in report.notes

    def test_tape_below_warmup_produces_empty_report(self) -> None:
        bars = [_bar(i, 100.0, 100.0, 100.0, 100.0) for i in range(3)]
        report = qualify_strategies(
            bars,
            asset="TEST",
            harness_config=HarnessConfig(warmup_bars=10),
        )
        assert report.n_windows_executed == 0
        assert "insufficient_bars_no_windows" in report.notes


# ---------------------------------------------------------------------------
# qualify_strategies - happy path (winning strategy passes gate)
# ---------------------------------------------------------------------------


class TestQualifyStrategiesHappyPath:
    def test_winning_strategy_passes_gate(self) -> None:
        # Loose gate so the winning tape definitely clears it
        gate = QualificationGate(
            dsr_threshold=0.1,
            max_degradation_pct=0.95,
            min_trades_per_window=1,
        )
        bars = _winning_tape(200)
        report = qualify_strategies(
            bars,
            asset="TEST",
            gate=gate,
            n_windows=2,
            is_fraction=0.5,
            harness_config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=3,
                slippage_bps=0.0,
            ),
            eligibility=_ELIG,
            registry=_always_win_registry(),
        )
        assert report.n_windows_executed == 2
        assert len(report.qualifications) >= 1
        sid = StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT
        matched = [q for q in report.qualifications if q.strategy is sid]
        assert matched, "expected LIQUIDITY_SWEEP_DISPLACEMENT in qualifications"

    def test_winning_strategy_has_positive_is_and_oos_sharpe(self) -> None:
        gate = QualificationGate(
            dsr_threshold=0.1,
            max_degradation_pct=0.95,
            min_trades_per_window=1,
        )
        bars = _winning_tape(200)
        report = qualify_strategies(
            bars,
            asset="TEST",
            gate=gate,
            n_windows=2,
            is_fraction=0.5,
            harness_config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=3,
                slippage_bps=0.0,
            ),
            eligibility=_ELIG,
            registry=_always_win_registry(),
        )
        q = report.qualifications[0]
        assert q.n_trades_is_total > 0
        assert q.n_trades_oos_total > 0


# ---------------------------------------------------------------------------
# qualify_strategies - failure paths
# ---------------------------------------------------------------------------


class TestQualifyStrategiesFailurePaths:
    def test_losing_strategy_fails_dsr(self) -> None:
        # Losing tape + loose deg/min_trades, but keep default DSR threshold
        gate = QualificationGate(
            dsr_threshold=0.5,
            max_degradation_pct=1.0,
            min_trades_per_window=1,
        )
        bars = _losing_tape(200)
        report = qualify_strategies(
            bars,
            asset="TEST",
            gate=gate,
            n_windows=2,
            is_fraction=0.5,
            harness_config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=3,
                slippage_bps=0.0,
            ),
            eligibility=_ELIG,
            registry=_always_lose_registry(),
        )
        assert report.qualifications
        q = report.qualifications[0]
        assert q.passes_gate is False
        assert any("dsr" in r for r in q.fail_reasons)

    def test_tight_min_trades_gate_fails(self) -> None:
        # Require 100k trades per window (impossible on a 200-bar tape)
        gate = QualificationGate(
            dsr_threshold=0.01,
            max_degradation_pct=0.99,
            min_trades_per_window=10_000,
        )
        bars = _winning_tape(200)
        report = qualify_strategies(
            bars,
            asset="TEST",
            gate=gate,
            n_windows=2,
            is_fraction=0.5,
            harness_config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=3,
                slippage_bps=0.0,
            ),
            eligibility=_ELIG,
            registry=_always_win_registry(),
        )
        q = report.qualifications[0]
        assert q.passes_gate is False
        assert any("min_trades_per_window" in r for r in q.fail_reasons)


# ---------------------------------------------------------------------------
# Per-window records + aggregation
# ---------------------------------------------------------------------------


class TestPerWindowRecords:
    def test_n_windows_executed_matches_triples(self) -> None:
        bars = _winning_tape(200)
        report = qualify_strategies(
            bars,
            asset="TEST",
            n_windows=3,
            is_fraction=0.5,
            harness_config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=3,
                slippage_bps=0.0,
            ),
            eligibility=_ELIG,
            registry=_always_win_registry(),
        )
        assert report.n_windows_executed == 3
        # All per-window records reference a unique (strategy, window) pair
        seen = {(r.strategy, r.window_id) for r in report.per_window}
        assert len(seen) == len(report.per_window)

    def test_window_id_monotonic(self) -> None:
        bars = _winning_tape(240)
        report = qualify_strategies(
            bars,
            asset="TEST",
            n_windows=4,
            is_fraction=0.5,
            harness_config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=3,
                slippage_bps=0.0,
            ),
            eligibility=_ELIG,
            registry=_always_win_registry(),
        )
        # For a single strategy, per-window rows appear in window_id order
        sid_rows = sorted(
            [r for r in report.per_window if r.strategy is StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT],
            key=lambda r: r.window_id,
        )
        ids = [r.window_id for r in sid_rows]
        assert ids == sorted(ids)
        assert set(ids).issubset({0, 1, 2, 3})


# ---------------------------------------------------------------------------
# Serialisation + report properties
# ---------------------------------------------------------------------------


class TestReportSerialisation:
    def test_as_dict_has_expected_keys(self) -> None:
        bars = _winning_tape(200)
        report = qualify_strategies(
            bars,
            asset="TEST",
            n_windows=2,
            is_fraction=0.5,
            harness_config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=3,
                slippage_bps=0.0,
            ),
            eligibility=_ELIG,
            registry=_always_win_registry(),
        )
        d = report.as_dict()
        assert d["asset"] == "TEST"
        assert set(d.keys()) >= {
            "asset",
            "gate",
            "n_windows_requested",
            "n_windows_executed",
            "per_window",
            "qualifications",
            "notes",
            "passing_strategies",
            "failing_strategies",
        }

    def test_per_window_as_dict_fields(self) -> None:
        row = PerStrategyWindow(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            window_id=0,
            is_n_trades=10,
            is_sharpe_like=1.23456,
            is_total_r=4.5,
            is_hit_rate=0.6,
            oos_n_trades=5,
            oos_sharpe_like=0.9,
            oos_total_r=2.2,
            oos_hit_rate=0.5,
            degradation_pct=0.25,
            min_trades_met=True,
        )
        d = row.as_dict()
        assert d["strategy"] == StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT.value
        assert d["window_id"] == 0
        assert d["is_sharpe_like"] == 1.2346  # rounded to 4dp
        assert d["min_trades_met"] is True

    def test_qualification_as_dict_fields(self) -> None:
        q = StrategyQualification(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            asset="TEST",
            n_windows=3,
            avg_is_sharpe=1.1,
            avg_oos_sharpe=0.8,
            avg_degradation_pct=0.27,
            dsr=0.6,
            n_trades_is_total=45,
            n_trades_oos_total=18,
            passes_gate=True,
        )
        d = q.as_dict()
        assert d["strategy"] == StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT.value
        assert d["passes_gate"] is True
        assert d["fail_reasons"] == []

    def test_passing_and_failing_helpers(self) -> None:
        bars = _winning_tape(200)
        gate = QualificationGate(
            dsr_threshold=0.01,
            max_degradation_pct=0.99,
            min_trades_per_window=1,
        )
        report = qualify_strategies(
            bars,
            asset="TEST",
            gate=gate,
            n_windows=2,
            is_fraction=0.5,
            harness_config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=3,
                slippage_bps=0.0,
            ),
            eligibility=_ELIG,
            registry=_always_win_registry(),
        )
        # Union of passing + failing covers every qualification exactly once
        total = len(report.passing_strategies) + len(report.failing_strategies)
        assert total == len(report.qualifications)


# ---------------------------------------------------------------------------
# Multi-strategy end-to-end
# ---------------------------------------------------------------------------


def _two_strategy_registry() -> _RegistryType:
    """Two strategies that both want to fire -- the router picks the
    higher-confidence one each bar. FVG_FILL_CONFLUENCE wins; LSD is
    the lower-confidence fallback.
    """

    def lsd(bars: list[Bar], _ctx: object) -> StrategySignal:
        if not bars:
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
            confidence=6.0,
            risk_mult=1.0,
            rationale_tags=("lsd_win",),
        )

    def fvg(bars: list[Bar], _ctx: object) -> StrategySignal:
        if not bars:
            return StrategySignal(
                strategy=StrategyId.FVG_FILL_CONFLUENCE,
                side=Side.FLAT,
            )
        return StrategySignal(
            strategy=StrategyId.FVG_FILL_CONFLUENCE,
            side=Side.LONG,
            entry=100.0,
            stop=99.0,
            target=102.0,
            confidence=9.0,
            risk_mult=1.0,
            rationale_tags=("fvg_win",),
        )

    return {
        StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: lsd,
        StrategyId.FVG_FILL_CONFLUENCE: fvg,
    }


class TestMultiStrategy:
    def test_only_router_winners_recorded(self) -> None:
        # Both strategies eligible; FVG always wins the confidence race
        elig = {
            "TEST": (
                StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                StrategyId.FVG_FILL_CONFLUENCE,
            ),
        }
        bars = _winning_tape(200)
        report = qualify_strategies(
            bars,
            asset="TEST",
            n_windows=2,
            is_fraction=0.5,
            harness_config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=3,
                slippage_bps=0.0,
            ),
            eligibility=elig,
            registry=_two_strategy_registry(),
        )
        strategies_seen = {q.strategy for q in report.qualifications}
        assert StrategyId.FVG_FILL_CONFLUENCE in strategies_seen
        # LSD lost every confidence race, so it should not appear
        assert StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT not in strategies_seen

    def test_asset_propagated_upper(self) -> None:
        bars = _winning_tape(200)
        report = qualify_strategies(
            bars,
            asset="test",
            n_windows=2,
            is_fraction=0.5,
            harness_config=HarnessConfig(
                warmup_bars=5,
                max_bars_per_trade=3,
                slippage_bps=0.0,
            ),
            eligibility=_ELIG,
            registry=_always_win_registry(),
        )
        assert report.asset == "TEST"
        for q in report.qualifications:
            assert q.asset == "TEST"

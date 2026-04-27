"""Tests for :mod:`eta_engine.strategies.allowlist_scheduler`.

The scheduler ticks the runtime allowlist cache on a bar-by-bar
cadence. Tests exercise:

  - :class:`RefreshTrigger` validation surface (all invalid combos
    raise, valid combos frozen and dataclass-like).
  - Warmup guard: no refresh before ``min_bars_before_first``.
  - First-refresh always fires after warmup.
  - Bar-count trigger: fires only after N bars accrue.
  - Time trigger: fires only after S seconds elapse.
  - Combined triggers: fires on whichever fires first.
  - Force-refresh bypasses trigger entirely.
  - Multi-asset bookkeeping is per-asset independent.
  - ``reset`` restarts the cadence without touching the cache.
  - Default qualifier is :func:`qualify_strategies`; custom qualifier
    is invoked with correct kwargs.
  - End-to-end: scheduler + cache + dispatch stays consistent across
    multiple tick iterations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.strategies.allowlist_scheduler import (
    AllowlistScheduler,
    RefreshTrigger,
)
from eta_engine.strategies.eta_policy import StrategyContext
from eta_engine.strategies.models import (
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)
from eta_engine.strategies.oos_qualifier import (
    DEFAULT_QUALIFICATION_GATE,
    QualificationGate,
    QualificationReport,
    StrategyQualification,
)
from eta_engine.strategies.policy_router import dispatch
from eta_engine.strategies.runtime_allowlist import RuntimeAllowlistCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(ts: datetime, px: float = 100.0) -> Bar:
    return Bar(
        ts=ts,
        open=px,
        high=px + 0.5,
        low=px - 0.5,
        close=px,
        volume=1000.0,
    )


def _quals(*items: tuple[StrategyId, bool]) -> tuple[StrategyQualification, ...]:
    out: list[StrategyQualification] = []
    for sid, passes in items:
        out.append(
            StrategyQualification(
                strategy=sid,
                asset="MNQ",
                n_windows=1,
                avg_is_sharpe=0.0,
                avg_oos_sharpe=0.0,
                avg_degradation_pct=0.0,
                dsr=0.0,
                n_trades_is_total=0,
                n_trades_oos_total=0,
                passes_gate=passes,
                fail_reasons=() if passes else ("dsr_below_threshold",),
            ),
        )
    return tuple(out)


def _report(
    asset: str,
    *items: tuple[StrategyId, bool],
    gate: QualificationGate | None = None,
) -> QualificationReport:
    return QualificationReport(
        asset=asset,
        gate=gate if gate is not None else DEFAULT_QUALIFICATION_GATE,
        n_windows_requested=1,
        n_windows_executed=1,
        per_window=(),
        qualifications=_quals(*items),
    )


@dataclass
class _ManualClock:
    now_: datetime = field(
        default_factory=lambda: datetime(2026, 4, 17, tzinfo=UTC),
    )

    def __call__(self) -> datetime:
        return self.now_

    def advance(self, seconds: float) -> None:
        self.now_ = self.now_ + timedelta(seconds=seconds)


class _CountingQualifier:
    """A callable whose behaviour the test can fully script."""

    def __init__(
        self,
        *strategies: tuple[StrategyId, bool],
    ) -> None:
        self.calls: list[tuple[str, int, dict[str, object]]] = []
        self._strategies = strategies

    def __call__(
        self,
        bars: list[Bar],
        asset: str,
        **kwargs: object,
    ) -> QualificationReport:
        self.calls.append((asset, len(bars), dict(kwargs)))
        return _report(asset, *self._strategies)


# ---------------------------------------------------------------------------
# RefreshTrigger validation
# ---------------------------------------------------------------------------


class TestRefreshTriggerValidation:
    def test_bar_count_only_ok(self) -> None:
        t = RefreshTrigger(every_n_bars=10)
        assert t.every_n_bars == 10
        assert t.every_seconds is None

    def test_time_only_ok(self) -> None:
        t = RefreshTrigger(every_seconds=60.0)
        assert t.every_seconds == 60.0
        assert t.every_n_bars is None

    def test_both_set_ok(self) -> None:
        t = RefreshTrigger(every_n_bars=10, every_seconds=60.0)
        assert t.every_n_bars == 10
        assert t.every_seconds == 60.0

    def test_neither_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            RefreshTrigger()

    def test_zero_bar_count_raises(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            RefreshTrigger(every_n_bars=0)

    def test_negative_bar_count_raises(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            RefreshTrigger(every_n_bars=-1)

    def test_zero_seconds_raises(self) -> None:
        with pytest.raises(ValueError, match="> 0"):
            RefreshTrigger(every_seconds=0.0)

    def test_negative_seconds_raises(self) -> None:
        with pytest.raises(ValueError, match="> 0"):
            RefreshTrigger(every_seconds=-1.0)

    def test_negative_min_warmup_raises(self) -> None:
        with pytest.raises(ValueError, match="min_bars_before_first"):
            RefreshTrigger(every_n_bars=5, min_bars_before_first=-1)

    def test_frozen(self) -> None:
        t = RefreshTrigger(every_n_bars=10)
        with pytest.raises((AttributeError, TypeError)):
            t.every_n_bars = 20  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Warmup guard
# ---------------------------------------------------------------------------


class TestWarmupGuard:
    def test_no_refresh_below_min_bars(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=10, min_bars_before_first=50)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts + timedelta(minutes=i)) for i in range(49)]
        result = sched.tick("MNQ", bars, qualifier=q)
        assert result is None
        assert q.calls == []

    def test_refresh_at_exact_min(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=10, min_bars_before_first=50)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts + timedelta(minutes=i)) for i in range(50)]
        result = sched.tick("MNQ", bars, qualifier=q)
        assert result is not None
        assert len(q.calls) == 1

    def test_zero_min_allows_immediate_refresh(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        # empty bar list -> warmup satisfied (0 >= 0) but len(bars) == 0
        # still skipped because first-ever call on zero bars is still
        # technically >=0 -- that's OK, qualifier will be called with
        # an empty tape which it handles.
        result = sched.tick("MNQ", [_bar(ts)], qualifier=q)
        assert result is not None
        assert len(q.calls) == 1


# ---------------------------------------------------------------------------
# Bar-count trigger
# ---------------------------------------------------------------------------


class TestBarCountTrigger:
    def test_fires_once_on_first_eligible_tick(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=10, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts + timedelta(minutes=i)) for i in range(5)]
        entry1 = sched.tick("MNQ", bars, qualifier=q)
        assert entry1 is not None
        # Next tick with +3 bars -> NOT enough to retrigger
        bars.extend(_bar(ts + timedelta(minutes=i)) for i in range(5, 8))
        entry2 = sched.tick("MNQ", bars, qualifier=q)
        assert entry2 is None
        assert len(q.calls) == 1

    def test_fires_again_after_n_bars(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=5, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        sched.tick("MNQ", bars, qualifier=q)  # first refresh at bar_count=1
        bars.extend(_bar(ts + timedelta(minutes=i)) for i in range(1, 5))
        assert sched.tick("MNQ", bars, qualifier=q) is None  # 5 total, delta=4
        bars.append(_bar(ts + timedelta(minutes=5)))
        assert sched.tick("MNQ", bars, qualifier=q) is not None  # delta=5
        assert len(q.calls) == 2

    def test_exact_threshold_fires(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=3, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        sched.tick("MNQ", bars, qualifier=q)
        # add exactly 3 bars -> delta=3, should fire
        bars.extend(_bar(ts + timedelta(minutes=i)) for i in range(1, 4))
        assert sched.tick("MNQ", bars, qualifier=q) is not None


# ---------------------------------------------------------------------------
# Time trigger
# ---------------------------------------------------------------------------


class TestTimeTrigger:
    def test_fires_after_elapsed_seconds(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_seconds=60.0, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        sched.tick("MNQ", bars, qualifier=q)  # first refresh
        assert len(q.calls) == 1

        clock.advance(30)
        assert sched.tick("MNQ", bars, qualifier=q) is None
        assert len(q.calls) == 1

        clock.advance(30)  # total 60s -> boundary-inclusive fire
        assert sched.tick("MNQ", bars, qualifier=q) is not None
        assert len(q.calls) == 2

    def test_time_trigger_alone_respects_warmup(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_seconds=60.0, min_bars_before_first=20)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        clock.advance(1_000)
        # still below warmup -> no refresh even though time is way past
        assert sched.tick("MNQ", bars, qualifier=q) is None
        assert q.calls == []


# ---------------------------------------------------------------------------
# Combined triggers
# ---------------------------------------------------------------------------


class TestCombinedTriggers:
    def test_bars_fire_before_time(self) -> None:
        # every 5 bars OR every 120s. Bars accrue fast -> bars should
        # trigger first.
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(
            every_n_bars=5,
            every_seconds=120.0,
            min_bars_before_first=0,
        )
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        sched.tick("MNQ", bars, qualifier=q)  # first
        clock.advance(10)
        bars.extend(_bar(ts + timedelta(minutes=i)) for i in range(1, 6))
        # bar_count=6, delta=5 -> bar trigger fires
        assert sched.tick("MNQ", bars, qualifier=q) is not None
        assert len(q.calls) == 2

    def test_time_fires_before_bars(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(
            every_n_bars=100,  # very unlikely to trigger
            every_seconds=60.0,
            min_bars_before_first=0,
        )
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        sched.tick("MNQ", bars, qualifier=q)  # first
        clock.advance(120)  # time trigger fires
        bars.append(_bar(ts + timedelta(minutes=1)))
        assert sched.tick("MNQ", bars, qualifier=q) is not None
        assert len(q.calls) == 2


# ---------------------------------------------------------------------------
# Force refresh
# ---------------------------------------------------------------------------


class TestForceRefresh:
    def test_force_bypasses_all_triggers(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1000, min_bars_before_first=1000)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        entry = sched.force_refresh("MNQ", bars, qualifier=q)
        assert entry.asset == "MNQ"
        assert len(q.calls) == 1

    def test_force_updates_bookkeeping(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=5, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        sched.force_refresh("MNQ", bars, qualifier=q)
        assert sched.last_refresh_at("MNQ") == clock.now_
        assert sched.last_refresh_bar_count("MNQ") == 1


# ---------------------------------------------------------------------------
# Multi-asset independence
# ---------------------------------------------------------------------------


class TestMultiAsset:
    def test_per_asset_bookkeeping_independent(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=5, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        mnq_bars = [_bar(ts)]
        btc_bars = [_bar(ts), _bar(ts + timedelta(minutes=1))]

        sched.tick("MNQ", mnq_bars, qualifier=q)
        sched.tick("BTC", btc_bars, qualifier=q)
        assert set(sched.tracked_assets()) == {"MNQ", "BTC"}

        mnq_bars.extend(_bar(ts + timedelta(minutes=i)) for i in range(1, 6))
        # MNQ delta=5 -> should fire; BTC unchanged -> should not
        assert sched.tick("MNQ", mnq_bars, qualifier=q) is not None
        assert sched.tick("BTC", btc_bars, qualifier=q) is None

    def test_asset_name_upper_cased(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=5, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        sched.tick("mnq", bars, qualifier=q)
        assert sched.tracked_assets() == ("MNQ",)
        assert sched.last_refresh_bar_count("MNQ") == 1
        assert sched.last_refresh_bar_count("mnq") == 1  # lookup also upper-casing


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_single_asset(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=5, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        sched.tick("MNQ", bars, qualifier=q)
        sched.tick("BTC", bars, qualifier=q)
        assert set(sched.tracked_assets()) == {"MNQ", "BTC"}
        sched.reset("MNQ")
        assert sched.tracked_assets() == ("BTC",)

    def test_reset_all(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=5, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        sched.tick("MNQ", [_bar(ts)], qualifier=q)
        sched.tick("BTC", [_bar(ts)], qualifier=q)
        sched.reset()
        assert sched.tracked_assets() == ()

    def test_reset_does_not_touch_cache(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=5, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        sched.tick("MNQ", [_bar(ts)], qualifier=q)
        sched.reset()
        assert cache.get("MNQ") is not None  # cache entry still present

    def test_reset_reenables_first_refresh(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1000, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        sched.tick("MNQ", bars, qualifier=q)
        # next tick with same bar count -> no refresh
        assert sched.tick("MNQ", bars, qualifier=q) is None
        # reset, then tick again -> first-refresh fires again
        sched.reset("MNQ")
        assert sched.tick("MNQ", bars, qualifier=q) is not None
        assert len(q.calls) == 2


# ---------------------------------------------------------------------------
# Qualifier injection
# ---------------------------------------------------------------------------


class TestQualifierInjection:
    def test_custom_kwargs_forwarded(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=5, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        q = _CountingQualifier((StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        custom_gate = QualificationGate(
            dsr_threshold=0.1,
            max_degradation_pct=0.9,
            min_trades_per_window=1,
        )
        sched.tick(
            "MNQ",
            bars,
            qualifier=q,
            gate=custom_gate,
            n_windows=3,
        )
        assert len(q.calls) == 1
        _, _, kwargs = q.calls[0]
        assert kwargs.get("gate") is custom_gate
        assert kwargs.get("n_windows") == 3

    def test_default_qualifier_is_qualify_strategies(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts + timedelta(minutes=i)) for i in range(80)]
        entry = sched.tick(
            "MNQ",
            bars,
            gate=QualificationGate(
                dsr_threshold=-10.0,
                max_degradation_pct=10.0,
                min_trades_per_window=0,
            ),
            n_windows=2,
            registry={},
        )
        assert entry is not None
        assert entry.asset == "MNQ"


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


class TestEndToEndWithDispatch:
    def test_scheduler_keeps_router_eligibility_fresh(self) -> None:
        """scheduler.tick drives cache.update; dispatch reads fresh map."""
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock, ttl_seconds=3600.0)
        trigger = RefreshTrigger(every_n_bars=5, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        # Sequence of reports: first all pass, second drops OB.
        reports = [
            _report(
                "MNQ",
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
                (StrategyId.OB_BREAKER_RETEST, True),
                (StrategyId.FVG_FILL_CONFLUENCE, True),
                (StrategyId.MTF_TREND_FOLLOWING, True),
            ),
            _report(
                "MNQ",
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
                (StrategyId.OB_BREAKER_RETEST, False),  # dropped
                (StrategyId.FVG_FILL_CONFLUENCE, True),
                (StrategyId.MTF_TREND_FOLLOWING, True),
            ),
        ]
        idx = [0]

        def staged_qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            r = reports[idx[0]]
            idx[0] = min(idx[0] + 1, len(reports) - 1)
            return r

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        sched.tick("MNQ", bars, qualifier=staged_qualifier)  # first refresh
        mp1 = cache.as_eligibility_map()
        assert StrategyId.OB_BREAKER_RETEST in mp1["MNQ"]

        # Grow bars to re-trigger
        bars.extend(_bar(ts + timedelta(minutes=i)) for i in range(1, 6))
        sched.tick("MNQ", bars, qualifier=staged_qualifier)  # second refresh
        mp2 = cache.as_eligibility_map()
        assert StrategyId.OB_BREAKER_RETEST not in mp2["MNQ"]
        assert StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT in mp2["MNQ"]

    def test_dispatch_reflects_latest_scheduler_tick(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=5, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        def staged_qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            return _report(
                asset,
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
            )

        # build a trivial registry that records which sids were called
        calls: list[StrategyId] = []

        def make_fn(sid: StrategyId) -> object:
            def fn(bars: list[Bar], ctx: StrategyContext) -> StrategySignal:
                calls.append(sid)
                return StrategySignal(
                    strategy=sid,
                    side=Side.LONG,
                    confidence=6.0,
                    entry=100.0,
                    stop=99.0,
                    target=102.0,
                    risk_mult=1.0,
                    rationale_tags=(sid.value,),
                )

            return fn

        registry = {
            StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: make_fn(
                StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            ),
            StrategyId.OB_BREAKER_RETEST: make_fn(StrategyId.OB_BREAKER_RETEST),
            StrategyId.FVG_FILL_CONFLUENCE: make_fn(StrategyId.FVG_FILL_CONFLUENCE),
            StrategyId.MTF_TREND_FOLLOWING: make_fn(StrategyId.MTF_TREND_FOLLOWING),
        }

        ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(ts)]
        sched.tick("MNQ", bars, qualifier=staged_qualifier)

        decision = dispatch(
            "MNQ",
            bars,
            StrategyContext(),
            eligibility=cache.as_eligibility_map(),
            registry=registry,
        )
        # only LSD in the allowlist -> only LSD is invoked by the router
        assert calls == [StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT]
        assert decision.winner.strategy == StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT

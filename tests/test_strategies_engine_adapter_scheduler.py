"""Tests for v0.1.44: ``RouterAdapter`` + ``AllowlistScheduler``.

v0.1.44 plugs the scheduler into ``RouterAdapter.push_bar`` so the
bot's per-bar hot path auto-drives the qualification loop. These
tests exercise the new wiring:

  - Scheduler tick happens BEFORE dispatch on each push_bar.
  - When the scheduler's cache is empty, ``_effective_eligibility``
    returns the static override (or ``None`` if no override is set).
  - When the scheduler's cache has a fresh entry, it becomes the
    effective eligibility map (merged with static on conflict; static
    wins).
  - A failing qualifier does NOT crash push_bar -- the scheduler tick
    is wrapped in a try/except and the router falls back.
  - A kill-switch-active context still dispatches against the scheduler
    eligibility -- the kill switch lives in StrategyContext, not in
    the adapter.
  - The decision sink receives the post-scheduler decision.
  - End-to-end: after enough bars, only passing strategies from the
    staged qualifier see the router.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from eta_engine.strategies.allowlist_scheduler import (
    AllowlistScheduler,
    RefreshTrigger,
)
from eta_engine.strategies.engine_adapter import RouterAdapter
from eta_engine.strategies.models import (
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)
from eta_engine.strategies.oos_qualifier import (
    DEFAULT_QUALIFICATION_GATE,
    QualificationReport,
    StrategyQualification,
)
from eta_engine.strategies.runtime_allowlist import RuntimeAllowlistCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar_dict(ts: int, px: float = 100.0) -> dict[str, float | int]:
    return {
        "ts": ts,
        "open": px,
        "high": px + 0.5,
        "low": px - 0.5,
        "close": px,
        "volume": 1000.0,
    }


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


def _report(asset: str, *items: tuple[StrategyId, bool]) -> QualificationReport:
    return QualificationReport(
        asset=asset,
        gate=DEFAULT_QUALIFICATION_GATE,
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


def _make_registry(
    *allowed: StrategyId,
    action: Side = Side.LONG,
) -> tuple[
    dict[StrategyId, object],
    list[StrategyId],
]:
    """Build a registry whose actionable set is exactly ``allowed``.

    Every strategy in the 6-strategy universe is registered; the ones
    not in ``allowed`` return a FLAT signal (non-actionable) so the
    router never picks them even if eligibility accidentally lets them
    through.
    """
    calls: list[StrategyId] = []
    all_sids = (
        StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
        StrategyId.OB_BREAKER_RETEST,
        StrategyId.FVG_FILL_CONFLUENCE,
        StrategyId.MTF_TREND_FOLLOWING,
        StrategyId.RL_FULL_AUTOMATION,
        StrategyId.REGIME_ADAPTIVE_ALLOCATION,
    )
    allowed_set = set(allowed)
    registry: dict[StrategyId, object] = {}

    def _make(target: StrategyId) -> object:
        def fn(bars: list[Bar], ctx: object) -> StrategySignal:
            calls.append(target)
            if target in allowed_set:
                return StrategySignal(
                    strategy=target,
                    side=action,
                    confidence=6.0,
                    entry=100.0,
                    stop=99.0,
                    target=102.0,
                    risk_mult=1.0,
                    rationale_tags=(target.value,),
                )
            return StrategySignal(
                strategy=target,
                side=Side.FLAT,
                rationale_tags=("noop",),
            )

        return fn

    for sid in all_sids:
        registry[sid] = _make(sid)
    return registry, calls


# ---------------------------------------------------------------------------
# Tick ordering
# ---------------------------------------------------------------------------


class TestSchedulerTicksBeforeDispatch:
    def test_tick_installs_before_dispatch(self) -> None:
        """Scheduler tick happens before dispatch on push_bar."""
        order: list[str] = []

        def qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            order.append("qualifier")
            return _report(asset, (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        registry, _ = _make_registry(StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT)
        # wrap every registry fn so it appends "dispatch" to order
        for sid, fn in list(registry.items()):

            def _wrap(
                original: object,
                target: StrategyId = sid,
            ) -> object:
                def probe(bars: list[Bar], ctx: object) -> StrategySignal:
                    order.append(f"dispatch:{target.value}")
                    return original(bars, ctx)  # type: ignore[misc,no-any-return]

                return probe

            registry[sid] = _wrap(fn)

        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        adapter = RouterAdapter(
            asset="MNQ",
            registry=registry,
            allowlist_scheduler=sched,
            scheduler_kwargs={"qualifier": qualifier},
        )

        adapter.push_bar(_bar_dict(1))
        # Qualifier must come before the LSD dispatch probe.
        q_idx = order.index("qualifier")
        lsd_idx = order.index(
            f"dispatch:{StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT.value}",
        )
        assert q_idx < lsd_idx

    def test_scheduler_tick_runs_every_push(self) -> None:
        calls = {"n": 0}

        def qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            calls["n"] += 1
            return _report(asset, (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        registry, _ = _make_registry(StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT)
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        adapter = RouterAdapter(
            asset="MNQ",
            registry=registry,
            allowlist_scheduler=sched,
            scheduler_kwargs={"qualifier": qualifier},
        )

        for i in range(3):
            adapter.push_bar(_bar_dict(i))
        # qualifier called once per bar due to every_n_bars=1
        assert calls["n"] == 3


# ---------------------------------------------------------------------------
# Effective eligibility resolution
# ---------------------------------------------------------------------------


class TestEffectiveEligibility:
    def test_static_only_unchanged_when_scheduler_cache_empty(self) -> None:
        """A scheduler with no cached entries leaves static eligibility alone."""
        static = {"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)}
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1000, min_bars_before_first=1000)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        adapter = RouterAdapter(
            asset="MNQ",
            eligibility=static,
            allowlist_scheduler=sched,
        )
        eff = adapter._effective_eligibility()  # noqa: SLF001
        assert eff == static

    def test_scheduler_map_used_when_static_none(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        cache.update(_report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        trigger = RefreshTrigger(every_n_bars=1000, min_bars_before_first=1000)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        adapter = RouterAdapter(
            asset="MNQ",
            allowlist_scheduler=sched,
        )
        eff = adapter._effective_eligibility()  # noqa: SLF001
        assert eff == {"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)}

    def test_static_wins_on_same_asset(self) -> None:
        """If both maps have MNQ, static eligibility's tuple wins."""
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        cache.update(
            _report(
                "MNQ",
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
                (StrategyId.OB_BREAKER_RETEST, True),
            ),
        )
        trigger = RefreshTrigger(every_n_bars=1000, min_bars_before_first=1000)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        adapter = RouterAdapter(
            asset="MNQ",
            eligibility={"MNQ": (StrategyId.FVG_FILL_CONFLUENCE,)},
            allowlist_scheduler=sched,
        )
        eff = adapter._effective_eligibility()  # noqa: SLF001
        assert eff is not None
        assert eff["MNQ"] == (StrategyId.FVG_FILL_CONFLUENCE,)

    def test_scheduler_fills_assets_not_in_static(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        cache.update(_report("BTC", (StrategyId.MTF_TREND_FOLLOWING, True)))
        trigger = RefreshTrigger(every_n_bars=1000, min_bars_before_first=1000)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        adapter = RouterAdapter(
            asset="MNQ",
            eligibility={"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)},
            allowlist_scheduler=sched,
        )
        eff = adapter._effective_eligibility()  # noqa: SLF001
        assert eff is not None
        assert eff["MNQ"] == (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)
        assert eff["BTC"] == (StrategyId.MTF_TREND_FOLLOWING,)

    def test_none_when_both_empty(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1000, min_bars_before_first=1000)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)
        adapter = RouterAdapter(asset="MNQ", allowlist_scheduler=sched)
        assert adapter._effective_eligibility() is None  # noqa: SLF001

    def test_no_scheduler_returns_static_as_is(self) -> None:
        static = {"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)}
        adapter = RouterAdapter(asset="MNQ", eligibility=static)
        assert adapter._effective_eligibility() is static  # noqa: SLF001


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


class TestFailureContainment:
    def test_qualifier_exception_does_not_break_push_bar(self) -> None:
        def bad_qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            msg = "qualifier blew up"
            raise RuntimeError(msg)

        registry, calls = _make_registry(StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT)
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        adapter = RouterAdapter(
            asset="MNQ",
            eligibility={"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)},
            registry=registry,
            allowlist_scheduler=sched,
            scheduler_kwargs={"qualifier": bad_qualifier},
        )

        # Should NOT raise, should still return a valid signal.
        sig = adapter.push_bar(_bar_dict(1))
        assert sig is not None  # LSD fired
        # The cache is empty because the qualifier threw; static
        # eligibility still gates dispatch.
        assert cache.get("MNQ") is None

    def test_scheduler_failure_falls_back_to_default(self) -> None:
        """With no static and scheduler broken, dispatch uses DEFAULT_ELIGIBILITY."""

        def bad_qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            msg = "boom"
            raise RuntimeError(msg)

        registry, calls = _make_registry(StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT)
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        adapter = RouterAdapter(
            asset="MNQ",
            registry=registry,
            allowlist_scheduler=sched,
            scheduler_kwargs={"qualifier": bad_qualifier},
        )
        sig = adapter.push_bar(_bar_dict(1))
        # DEFAULT_ELIGIBILITY["MNQ"] includes LSD, so the signal fires.
        assert sig is not None


# ---------------------------------------------------------------------------
# Decision sink still sees post-scheduler decision
# ---------------------------------------------------------------------------


class TestDecisionSinkCooperation:
    def test_sink_receives_decision_with_scheduler_eligibility(self) -> None:
        class _Sink:
            def __init__(self) -> None:
                self.emitted: list[object] = []

            def emit(self, d: object) -> None:
                self.emitted.append(d)

        sink = _Sink()

        def qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            return _report(asset, (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True))

        registry, _ = _make_registry(StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT)
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        adapter = RouterAdapter(
            asset="MNQ",
            registry=registry,
            allowlist_scheduler=sched,
            scheduler_kwargs={"qualifier": qualifier},
            decision_sink=sink,  # type: ignore[arg-type]
        )
        adapter.push_bar(_bar_dict(1))
        assert len(sink.emitted) == 1
        # The emitted decision has eligibility that reflects scheduler state.
        d = sink.emitted[0]
        assert d.eligible == (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)


# ---------------------------------------------------------------------------
# End-to-end: multiple push_bars, only passing strategies dispatched
# ---------------------------------------------------------------------------


class TestEndToEndLiveLoop:
    def test_only_passing_strategies_are_dispatched(self) -> None:
        # Qualifier passes LSD + OB, fails FVG + MTF.
        def qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            return _report(
                asset,
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
                (StrategyId.OB_BREAKER_RETEST, True),
                (StrategyId.FVG_FILL_CONFLUENCE, False),
                (StrategyId.MTF_TREND_FOLLOWING, False),
            )

        registry, calls = _make_registry(
            StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            StrategyId.OB_BREAKER_RETEST,
        )
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        adapter = RouterAdapter(
            asset="MNQ",
            registry=registry,
            allowlist_scheduler=sched,
            scheduler_kwargs={"qualifier": qualifier},
        )

        for i in range(5):
            adapter.push_bar(_bar_dict(i))

        # Across 5 ticks, only LSD + OB should ever have been invoked
        # by dispatch -- FVG/MTF were failing and thus stripped from
        # the eligibility map.
        fired = set(calls)
        assert fired == {
            StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            StrategyId.OB_BREAKER_RETEST,
        }

    def test_static_override_takes_precedence_over_scheduler(self) -> None:
        """Static eligibility survives even when scheduler disagrees."""

        def qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            return _report(
                asset,
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
                (StrategyId.OB_BREAKER_RETEST, True),
            )

        registry, calls = _make_registry(StrategyId.FVG_FILL_CONFLUENCE)
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        trigger = RefreshTrigger(every_n_bars=1, min_bars_before_first=0)
        sched = AllowlistScheduler(cache=cache, trigger=trigger, clock=clock)

        adapter = RouterAdapter(
            asset="MNQ",
            eligibility={"MNQ": (StrategyId.FVG_FILL_CONFLUENCE,)},
            registry=registry,
            allowlist_scheduler=sched,
            scheduler_kwargs={"qualifier": qualifier},
        )

        adapter.push_bar(_bar_dict(1))
        assert adapter.last_decision is not None
        # Static override beats scheduler: FVG is dispatched, not LSD/OB
        assert adapter.last_decision.eligible == (StrategyId.FVG_FILL_CONFLUENCE,)
        assert StrategyId.FVG_FILL_CONFLUENCE in calls

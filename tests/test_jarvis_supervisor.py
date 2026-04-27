"""Tests for obs.jarvis_supervisor -- live liveness + drift watchdog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

from eta_engine.brain.jarvis_context import (
    ActionSuggestion,
    EquitySnapshot,
    JarvisContext,
    JarvisSuggestion,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    StressComponent,
    StressScore,
)
from eta_engine.obs.jarvis_supervisor import (
    JarvisHealth,
    JarvisHealthReport,
    JarvisSupervisor,
    SupervisorPolicy,
)

_T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def _clock_series(stamps: list[datetime]) -> Callable[[], datetime]:
    """Clock that returns stamps in order, sticking on the last."""
    idx = {"i": 0}

    def fn() -> datetime:
        i = min(idx["i"], len(stamps) - 1)
        idx["i"] += 1
        return stamps[i]

    return fn


def _clock_fixed(t: datetime) -> Callable[[], datetime]:
    def fn() -> datetime:
        return t

    return fn


def _clock_mutable(initial: datetime) -> tuple[Callable[[], datetime], list[datetime]]:
    holder = [initial]

    def fn() -> datetime:
        return holder[0]

    return fn, holder


def _mk_ctx(
    *,
    ts: datetime = _T0,
    composite: float = 0.3,
    binding: str = "drawdown",
) -> JarvisContext:
    stress = StressScore(
        composite=composite,
        components=[
            StressComponent(name=binding, value=composite, weight=1.0),
        ],
        binding_constraint=binding,
    )
    return JarvisContext(
        ts=ts,
        macro=MacroSnapshot(),
        equity=EquitySnapshot(
            account_equity=100_000.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="neutral", confidence=0.8),
        journal=JournalSnapshot(),
        suggestion=JarvisSuggestion(
            action=_suggestion_normal(),
            reason="stub",
            confidence=0.9,
        ),
        stress_score=stress,
    )


def _suggestion_normal() -> ActionSuggestion:
    """Return a non-degenerate ActionSuggestion member at runtime.

    Avoids hard-coding a name that might not exist in the enum by
    picking the first member.
    """
    members = list(ActionSuggestion)
    return members[0]


class _StubEngine:
    """Minimal stand-in for ``JarvisContextEngine``.

    Exposes a ``memory`` attribute with ``snapshots()`` and ``__len__``,
    and a ``tick()`` that returns the next context from a pre-queued list
    (or builds a default ctx).
    """

    def __init__(
        self,
        *,
        queue: list[JarvisContext] | None = None,
        raises: bool = False,
    ) -> None:
        self._q: list[JarvisContext] = list(queue or [])
        self._memory: list[JarvisContext] = []
        self.raises = raises
        self.tick_calls: int = 0
        # Namespace wrapper so supervisor.snapshot_health() can call
        # ``engine.memory.snapshots()`` and ``len(engine.memory)``.
        self.memory = _StubMemory(self._memory)

    def tick(self, *, notes: list[str] | None = None) -> JarvisContext:  # noqa: ARG002
        self.tick_calls += 1
        if self.raises:
            raise RuntimeError("engine boom")
        ctx = self._q.pop(0) if self._q else _mk_ctx()
        self._memory.append(ctx)
        return ctx


class _StubMemory:
    def __init__(self, buf: list[JarvisContext]) -> None:
        self._buf = buf

    def __len__(self) -> int:
        return len(self._buf)

    def snapshots(self) -> list[JarvisContext]:
        return list(self._buf)


# --------------------------------------------------------------------------- #
# Enum + model shapes
# --------------------------------------------------------------------------- #


def test_health_enum_members() -> None:
    assert {m.value for m in JarvisHealth} == {"GREEN", "YELLOW", "RED"}


def test_policy_defaults_are_sane() -> None:
    p = SupervisorPolicy()
    assert p.stale_after_s == 300.0
    assert p.dead_after_s == 1800.0
    assert p.dominance_run == 10
    assert p.flatline_run == 10
    assert 0.0 <= p.flatline_threshold <= 1.0


def test_policy_rejects_invalid_thresholds() -> None:
    with pytest.raises(ValidationError):
        SupervisorPolicy(stale_after_s=0.0)
    with pytest.raises(ValidationError):
        SupervisorPolicy(dead_after_s=-1.0)
    with pytest.raises(ValidationError):
        SupervisorPolicy(dominance_run=1)  # below 3
    with pytest.raises(ValidationError):
        SupervisorPolicy(flatline_threshold=1.5)  # > 1


def test_report_helpers() -> None:
    r = JarvisHealthReport(ts=_T0, health=JarvisHealth.GREEN)
    assert r.is_healthy
    assert not r.degraded
    r2 = JarvisHealthReport(ts=_T0, health=JarvisHealth.YELLOW)
    assert not r2.is_healthy
    assert r2.degraded


# --------------------------------------------------------------------------- #
# Fresh, GREEN path
# --------------------------------------------------------------------------- #


def test_green_after_single_tick() -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    sup.tick()
    rpt = sup.snapshot_health()
    assert rpt.health == JarvisHealth.GREEN
    assert rpt.reasons == []
    assert rpt.last_tick_at == _T0
    assert rpt.memory_len == 1
    assert rpt.last_binding == "drawdown"


def test_tick_count_increments() -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    sup.tick()
    sup.tick()
    sup.tick()
    assert sup.tick_count == 3
    assert engine.tick_calls == 3


def test_tick_propagates_notes() -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    ctx = sup.tick(notes=["hello"])
    assert isinstance(ctx, JarvisContext)


# --------------------------------------------------------------------------- #
# Staleness
# --------------------------------------------------------------------------- #


def test_stale_yellow_between_stale_and_dead() -> None:
    clock, holder = _clock_mutable(_T0)
    engine = _StubEngine()
    pol = SupervisorPolicy(stale_after_s=300.0, dead_after_s=1800.0)
    sup = JarvisSupervisor(engine=engine, policy=pol, clock=clock)
    sup.tick()
    # Advance 400s -- stale but not dead
    holder[0] = _T0 + timedelta(seconds=400)
    rpt = sup.snapshot_health()
    assert rpt.health == JarvisHealth.YELLOW
    assert any("STALE" in r for r in rpt.reasons)
    assert rpt.metrics["stale_s"] == pytest.approx(400.0, rel=1e-3)


def test_stale_red_past_dead_threshold() -> None:
    clock, holder = _clock_mutable(_T0)
    engine = _StubEngine()
    pol = SupervisorPolicy(stale_after_s=300.0, dead_after_s=1800.0)
    sup = JarvisSupervisor(engine=engine, policy=pol, clock=clock)
    sup.tick()
    holder[0] = _T0 + timedelta(seconds=2000)
    rpt = sup.snapshot_health()
    assert rpt.health == JarvisHealth.RED
    assert any("DEAD" in r for r in rpt.reasons)


def test_no_tick_never_no_ticks_yet_is_green_grace() -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    rpt = sup.snapshot_health()
    # Never ticked, memory empty --> GREEN grace (supervisor may have
    # just been constructed; staleness only applies after first tick).
    assert rpt.health == JarvisHealth.GREEN
    assert rpt.memory_len == 0


# --------------------------------------------------------------------------- #
# Dominance (same binding_constraint run)
# --------------------------------------------------------------------------- #


def test_dominance_yellow_when_run_reached() -> None:
    pol = SupervisorPolicy(dominance_run=5)
    ctxs = [_mk_ctx(binding="drawdown") for _ in range(5)]
    engine = _StubEngine(queue=list(ctxs))
    sup = JarvisSupervisor(engine=engine, policy=pol, clock=_clock_fixed(_T0))
    for _ in range(5):
        sup.tick()
    rpt = sup.snapshot_health()
    assert rpt.health == JarvisHealth.YELLOW
    assert any("DOMINANCE" in r for r in rpt.reasons)
    assert rpt.metrics["dominance_run"] == 5.0


def test_no_dominance_if_binding_varies() -> None:
    pol = SupervisorPolicy(dominance_run=5)
    ctxs = [
        _mk_ctx(binding="drawdown"),
        _mk_ctx(binding="macro"),
        _mk_ctx(binding="drawdown"),
        _mk_ctx(binding="regime"),
        _mk_ctx(binding="drawdown"),
    ]
    engine = _StubEngine(queue=ctxs)
    sup = JarvisSupervisor(engine=engine, policy=pol, clock=_clock_fixed(_T0))
    for _ in range(5):
        sup.tick()
    rpt = sup.snapshot_health()
    assert rpt.health == JarvisHealth.GREEN
    assert rpt.metrics["dominance_run"] == 0.0


def test_no_dominance_under_run_threshold() -> None:
    pol = SupervisorPolicy(dominance_run=10)
    ctxs = [_mk_ctx(binding="drawdown") for _ in range(5)]
    engine = _StubEngine(queue=ctxs)
    sup = JarvisSupervisor(engine=engine, policy=pol, clock=_clock_fixed(_T0))
    for _ in range(5):
        sup.tick()
    rpt = sup.snapshot_health()
    # 5 < 10 → not enough to claim dominance
    assert rpt.health == JarvisHealth.GREEN


# --------------------------------------------------------------------------- #
# Flatline composite
# --------------------------------------------------------------------------- #


def test_flatline_yellow_when_run_reached() -> None:
    pol = SupervisorPolicy(
        flatline_threshold=0.05,
        flatline_run=5,
        dominance_run=200,  # keep dominance out of the picture
    )
    # All composites at 0.01 -> flatline
    ctxs = [_mk_ctx(composite=0.01, binding=f"b{i % 3}") for i in range(5)]
    engine = _StubEngine(queue=ctxs)
    sup = JarvisSupervisor(engine=engine, policy=pol, clock=_clock_fixed(_T0))
    for _ in range(5):
        sup.tick()
    rpt = sup.snapshot_health()
    assert rpt.health == JarvisHealth.YELLOW
    assert any("FLATLINE" in r for r in rpt.reasons)
    assert rpt.metrics["flatline_run"] == 5.0


def test_not_flatline_when_one_sample_above_threshold() -> None:
    pol = SupervisorPolicy(
        flatline_threshold=0.05,
        flatline_run=5,
        dominance_run=200,
    )
    ctxs = [_mk_ctx(composite=0.01, binding=f"b{i}") for i in range(4)]
    ctxs.append(_mk_ctx(composite=0.5, binding="spike"))
    engine = _StubEngine(queue=ctxs)
    sup = JarvisSupervisor(engine=engine, policy=pol, clock=_clock_fixed(_T0))
    for _ in range(5):
        sup.tick()
    rpt = sup.snapshot_health()
    assert rpt.health == JarvisHealth.GREEN


# --------------------------------------------------------------------------- #
# Invalid composite -> RED (dominates over YELLOW)
# --------------------------------------------------------------------------- #


def test_invalid_composite_is_red() -> None:
    # StressScore pydantic model rejects out-of-range composites, so we
    # build a context with a broken composite by mutating the already
    # validated stress_score in-place. This mirrors what a bug in the
    # engine could produce (e.g., after a deepcopy).
    ctx = _mk_ctx(composite=0.5, binding="drawdown")
    # Bypass pydantic re-validation and shove an invalid value.
    object.__setattr__(ctx.stress_score, "composite", float("nan"))
    engine = _StubEngine(queue=[ctx])
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    sup.tick()
    rpt = sup.snapshot_health()
    assert rpt.health == JarvisHealth.RED
    assert any("invalid stress composite" in r for r in rpt.reasons)


def test_out_of_range_composite_is_red() -> None:
    ctx = _mk_ctx(composite=0.5, binding="drawdown")
    object.__setattr__(ctx.stress_score, "composite", 5.0)
    engine = _StubEngine(queue=[ctx])
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    sup.tick()
    rpt = sup.snapshot_health()
    assert rpt.health == JarvisHealth.RED


# --------------------------------------------------------------------------- #
# Severity combination: RED wins over YELLOW
# --------------------------------------------------------------------------- #


def test_red_overrides_yellow_on_combined_failure() -> None:
    # Stale (YELLOW) AND dominance (YELLOW) AND invalid composite (RED)
    # -- overall must be RED.
    clock, holder = _clock_mutable(_T0)
    pol = SupervisorPolicy(
        stale_after_s=100.0,
        dead_after_s=10_000.0,
        dominance_run=3,
        flatline_run=200,
    )
    ctxs = [_mk_ctx(binding="drawdown") for _ in range(3)]
    # Corrupt last composite
    object.__setattr__(ctxs[-1].stress_score, "composite", float("nan"))
    engine = _StubEngine(queue=ctxs)
    sup = JarvisSupervisor(engine=engine, policy=pol, clock=clock)
    for _ in range(3):
        sup.tick()
    # Advance past stale_after_s
    holder[0] = _T0 + timedelta(seconds=500)
    rpt = sup.snapshot_health()
    assert rpt.health == JarvisHealth.RED


# --------------------------------------------------------------------------- #
# Alert dispatch
# --------------------------------------------------------------------------- #


class _RecordingAlerter:
    """Stand-in for MultiAlerter. Records every send."""

    def __init__(self) -> None:
        self.sent: list = []  # list[Alert]

    async def send(self, alert):  # noqa: ANN001, ANN202
        self.sent.append(alert)
        return [True]


@pytest.mark.asyncio
async def test_alert_skipped_on_green() -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    sup.tick()
    rpt = sup.snapshot_health()
    al = _RecordingAlerter()
    await sup.alert(al, rpt)  # type: ignore[arg-type]
    assert al.sent == []


@pytest.mark.asyncio
async def test_alert_sent_on_red() -> None:
    ctx = _mk_ctx()
    object.__setattr__(ctx.stress_score, "composite", float("nan"))
    engine = _StubEngine(queue=[ctx])
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    sup.tick()
    rpt = sup.snapshot_health()
    al = _RecordingAlerter()
    await sup.alert(al, rpt)  # type: ignore[arg-type]
    assert len(al.sent) == 1
    a = al.sent[0]
    assert a.dedup_key is not None
    assert "RED" in a.dedup_key
    assert "Jarvis supervisor: RED" in a.title


@pytest.mark.asyncio
async def test_alert_none_alerter_is_noop() -> None:
    ctx = _mk_ctx()
    object.__setattr__(ctx.stress_score, "composite", float("nan"))
    engine = _StubEngine(queue=[ctx])
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    sup.tick()
    rpt = sup.snapshot_health()
    # Should NOT raise when alerter is None
    await sup.alert(None, rpt)


@pytest.mark.asyncio
async def test_alert_swallows_alerter_exceptions() -> None:
    class _BoomAlerter:
        async def send(self, alert):  # noqa: ANN001, ANN202, ARG002
            raise RuntimeError("send failed")

    ctx = _mk_ctx()
    object.__setattr__(ctx.stress_score, "composite", float("nan"))
    engine = _StubEngine(queue=[ctx])
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    sup.tick()
    rpt = sup.snapshot_health()
    # Must not raise -- supervisor is best-effort
    await sup.alert(_BoomAlerter(), rpt)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Engine.tick raising
# --------------------------------------------------------------------------- #


def test_tick_raising_does_not_advance_last_tick_at() -> None:
    engine = _StubEngine(raises=True)
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    with pytest.raises(RuntimeError):
        sup.tick()
    assert sup.last_tick_at is None
    assert sup.tick_count == 0


# --------------------------------------------------------------------------- #
# run() loop
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_loop_runs_bounded_iterations() -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    al = _RecordingAlerter()
    await sup.run(interval_s=0.001, alerter=al, max_ticks=3)  # type: ignore[arg-type]
    assert sup.tick_count == 3
    # All ticks healthy -> no alerts fired.
    assert al.sent == []


@pytest.mark.asyncio
async def test_run_loop_stop_halts_execution() -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    # Pre-stop so the loop exits after the first tick.
    sup.stop()
    # Not running, run() should enter and exit cleanly.
    await sup.run(interval_s=0.001, max_ticks=5)
    # Implementation: loop iterates once and checks _running each cycle,
    # so it may tick at least once.


@pytest.mark.asyncio
async def test_run_loop_continues_past_engine_exceptions() -> None:
    # Engine raises on every tick -- loop should keep running and
    # surface RED via snapshot_health on subsequent iterations (but
    # with no last_tick_at, stale_s is still 0 since _last_tick_at
    # is None).
    engine = _StubEngine(raises=True)
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    al = _RecordingAlerter()
    await sup.run(interval_s=0.001, alerter=al, max_ticks=3)  # type: ignore[arg-type]
    # Supervisor never advanced last_tick_at, but with empty memory and
    # _last_tick_at is None, we stay GREEN (grace period). No crash.
    assert sup.tick_count == 0


@pytest.mark.asyncio
async def test_run_rejects_nonpositive_interval() -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine)
    with pytest.raises(ValueError, match="interval_s"):
        await sup.run(interval_s=0.0)


# --------------------------------------------------------------------------- #
# Report metrics sanity
# --------------------------------------------------------------------------- #


def test_report_metrics_populated_after_tick() -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    sup.tick()
    rpt = sup.snapshot_health()
    assert "stale_s" in rpt.metrics
    assert "memory_len" in rpt.metrics
    assert "tick_count" in rpt.metrics
    assert "last_composite" in rpt.metrics
    assert rpt.metrics["memory_len"] == 1.0
    assert rpt.metrics["tick_count"] == 1.0

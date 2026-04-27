"""
Tests for the JARVIS hardening stack (11 opt-in middleware modules).

Covers:
  * brain.avengers.push           -- PushBus / LocalFileNotifier
  * brain.avengers.precedent_cache -- Jaccard + should_skip
  * brain.avengers.calibration_loop -- weight bounds, rehydrate
  * brain.avengers.circuit_breaker -- fail/denial/cost trips, HALF_OPEN recovery
  * brain.avengers.preflight_cache -- TTL, LRU, never caches DENY
  * brain.avengers.adaptive_cron   -- fire_always / sparse_ok / regime override
  * brain.avengers.deadman         -- LIVE / DROWSY / STALE / FROZEN states
  * brain.avengers.promotion       -- ladder, hard-break demote, RETIRE
  * brain.avengers.cost_forecast   -- windowed burn + severity
  * brain.avengers.watchdog        -- HEALTHY / STUCK / OFFLINE classification
  * brain.avengers.hardened_fleet  -- composed middleware behaviour
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from eta_engine.brain.avengers import (
    DRIFT_JOURNAL,
    RED_TEAM_GATED_TRANSITIONS,
    AlertLevel,
    BackgroundTask,
    CalibrationLoop,
    CircuitBreaker,
    CostForecast,
    DeadmanState,
    DeadmanSwitch,
    DriftDetector,
    DriftReport,
    DriftVerdict,
    DryRunExecutor,
    Fleet,
    HardenedFleet,
    LocalFileNotifier,
    PersonaId,
    PrecedentCache,
    PreflightCache,
    PromotionAction,
    PromotionDecision,
    PromotionGate,
    PromotionSpec,
    PromotionStage,
    PushBus,
    RedTeamVerdict,
    RegimeGate,
    RegimeTag,
    SharedCircuitBreaker,
    StageMetrics,
    TaskCategory,
    TaskEnvelope,
    TaskResult,
    Watchdog,
    default_red_team_gate,
    read_drift_journal,
    read_shared_status,
    reset_shared,
)
from eta_engine.brain.avengers.circuit_breaker import BreakerState, BreakerTripped
from eta_engine.brain.avengers.watchdog import HealthStatus
from eta_engine.brain.jarvis_admin import SubsystemId
from eta_engine.brain.model_policy import ModelTier
from eta_engine.scripts import chaos_drill

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env(
    *,
    category: TaskCategory = TaskCategory.STRATEGY_EDIT,
    goal: str = "tighten the ORB breakout filter",
    caller: SubsystemId = SubsystemId.OPERATOR,
) -> TaskEnvelope:
    return TaskEnvelope(
        category=category,
        goal=goal,
        caller=caller,
    )


def _result(
    *,
    success: bool = True,
    reason_code: str = "ok",
    reason: str = "ok",
    cost_multiplier: float = 1.0,
    persona_id: PersonaId = PersonaId.ALFRED,
    tier_used: ModelTier | None = ModelTier.SONNET,
) -> TaskResult:
    return TaskResult(
        task_id="task_" + reason_code,
        persona_id=persona_id,
        tier_used=tier_used,
        success=success,
        artifact="...",
        reason_code=reason_code,
        reason=reason,
        cost_multiplier=cost_multiplier,
    )


# ---------------------------------------------------------------------------
# push.py
# ---------------------------------------------------------------------------


class TestPushBus:
    def test_local_file_notifier_writes_line(self, tmp_path: Path) -> None:
        journal = tmp_path / "alerts.jsonl"
        notif = LocalFileNotifier(path=journal)
        bus = PushBus(notifiers=[notif])
        out = bus.push(
            level=AlertLevel.WARN,
            title="hello",
            body="world",
            source="test",
        )
        assert any(out.values()), "at least one notifier must succeed"
        assert journal.exists()
        rec = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
        assert rec["level"] == "WARN"
        assert rec["title"] == "hello"

    def test_push_bus_survives_notifier_exception(self, tmp_path: Path) -> None:
        class Boom:
            def send(self, alert):  # noqa: ANN001, ANN201
                raise RuntimeError("nope")

            @property
            def name(self) -> str:
                return "boom"

        local = LocalFileNotifier(path=tmp_path / "alerts.jsonl")
        bus = PushBus(notifiers=[Boom(), local])
        # Should not raise.
        out = bus.push(level=AlertLevel.INFO, title="t", body="b")
        # PushBus keys by type name; LocalFileNotifier must succeed,
        # Boom must be captured as False.
        assert out.get("LocalFileNotifier", False) is True
        assert out.get("Boom", True) is False


# ---------------------------------------------------------------------------
# precedent_cache.py
# ---------------------------------------------------------------------------


class TestPrecedentCache:
    def _seed_journal(
        self,
        path: Path,
        *,
        count: int,
        success: bool,
        goal: str,
    ) -> None:
        now = datetime.now(UTC)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for i in range(count):
                rec = {
                    "ts": (now - timedelta(hours=i)).isoformat(),
                    "envelope": {
                        "category": TaskCategory.STRATEGY_EDIT.value,
                        "caller": SubsystemId.OPERATOR.value,
                        "goal": goal,
                    },
                    "result": {
                        "success": success,
                        "artifact": f"artifact {i}",
                    },
                }
                fh.write(json.dumps(rec) + "\n")

    def test_should_skip_when_enough_successful_matches(self, tmp_path: Path) -> None:
        j = tmp_path / "avengers.jsonl"
        self._seed_journal(j, count=5, success=True, goal="tighten orb")
        cache = PrecedentCache(
            journal_path=j,
            min_precedents=3,
            min_similarity=0.3,
        )
        env = _env(goal="tighten orb filter")
        verdict = cache.should_skip(env)
        assert verdict is not None
        assert len(verdict.precedents) >= 3

    def test_no_skip_when_prior_failures(self, tmp_path: Path) -> None:
        j = tmp_path / "avengers.jsonl"
        self._seed_journal(j, count=5, success=False, goal="tighten orb")
        cache = PrecedentCache(
            journal_path=j,
            min_precedents=3,
            min_similarity=0.3,
        )
        verdict = cache.should_skip(_env(goal="tighten orb filter"))
        assert verdict is None


# ---------------------------------------------------------------------------
# calibration_loop.py
# ---------------------------------------------------------------------------


class TestCalibrationLoop:
    def test_weight_bounded_in_unit_interval(self, tmp_path: Path) -> None:
        cal = CalibrationLoop(
            journal_path=tmp_path / "calibration.jsonl",
            rehydrate=False,
        )
        # cold -> 0.5 default
        assert cal.weight(PersonaId.ALFRED, TaskCategory.STRATEGY_EDIT) == 0.5
        # record a success
        cal.record(_env(), _result(persona_id=PersonaId.ALFRED))
        w = cal.weight(PersonaId.ALFRED, TaskCategory.STRATEGY_EDIT)
        assert 0.1 <= w <= 1.0

    def test_rehydrate_accumulates(self, tmp_path: Path) -> None:
        jp = tmp_path / "cal.jsonl"
        # Pre-seed the journal.
        jp.parent.mkdir(parents=True, exist_ok=True)
        with jp.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "persona": PersonaId.ALFRED.value,
                        "category": TaskCategory.STRATEGY_EDIT.value,
                        "success": True,
                    }
                )
                + "\n"
            )
        cal = CalibrationLoop(journal_path=jp, rehydrate=True)
        # Should see the seeded record as 1 success.
        snap = [s for s in cal.snapshot() if s.persona == PersonaId.ALFRED.value]
        assert snap, "expected at least one bucket after rehydrate"
        assert snap[0].successes == 1


# ---------------------------------------------------------------------------
# circuit_breaker.py
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_trips_on_consec_failures(self) -> None:
        br = CircuitBreaker(max_consec_failures=3, cooldown_seconds=0.01)
        assert br.status().state is BreakerState.CLOSED
        for _ in range(3):
            br.record(_result(success=False, reason_code="executor_error"))
        assert br.status().state is BreakerState.OPEN
        with pytest.raises(BreakerTripped):
            br.pre_dispatch()

    def test_trips_on_consec_denials(self) -> None:
        br = CircuitBreaker(max_consec_denials=2, cooldown_seconds=0.01)
        br.record(_result(success=False, reason_code="jarvis_denied"))
        br.record(_result(success=False, reason_code="jarvis_denied"))
        assert br.status().state is BreakerState.OPEN

    def test_trips_on_cost_burst(self) -> None:
        br = CircuitBreaker(max_cost_per_minute=2.0, cooldown_seconds=0.01)
        # 3 opus calls at 5.0x -> 15.0 in the minute window, way over 2.0
        br.record(_result(cost_multiplier=5.0, persona_id=PersonaId.BATMAN, tier_used=ModelTier.OPUS))
        assert br.status().state is BreakerState.OPEN

    def test_half_open_closes_on_probe_success(self) -> None:
        br = CircuitBreaker(max_consec_failures=1, cooldown_seconds=0.0)
        br.record(_result(success=False, reason_code="executor_error"))
        assert br.status().state is BreakerState.OPEN
        # Cooldown of 0s means the next pre_dispatch flips to HALF_OPEN.
        br.pre_dispatch()
        assert br.status().state is BreakerState.HALF_OPEN
        # A successful probe should close it.
        br.record(_result(success=True))
        assert br.status().state is BreakerState.CLOSED


# ---------------------------------------------------------------------------
# preflight_cache.py
# ---------------------------------------------------------------------------


class TestPreflightCache:
    def test_put_get_hit(self) -> None:
        cache = PreflightCache(ttl_seconds=60.0)
        cache.put(
            category="c",
            caller="caller.x",
            action_type="llm",
            verdict="APPROVE",
        )
        assert (
            cache.get(
                category="c",
                caller="caller.x",
                action_type="llm",
            )
            == "APPROVE"
        )

    def test_never_caches_deny(self) -> None:
        cache = PreflightCache(ttl_seconds=60.0)
        cache.put(
            category="c",
            caller="caller.x",
            action_type="llm",
            verdict="DENIED",
        )
        assert (
            cache.get(
                category="c",
                caller="caller.x",
                action_type="llm",
            )
            is None
        )

    def test_ttl_expiry(self) -> None:
        # Controlled clock.
        t = [datetime(2026, 1, 1, tzinfo=UTC)]

        def clk() -> datetime:
            return t[0]

        cache = PreflightCache(ttl_seconds=10.0, clock=clk)
        cache.put(category="c", caller="a", action_type="llm", verdict="APPROVE")
        # Warp 20s forward.
        t[0] = t[0] + timedelta(seconds=20)
        assert cache.get(category="c", caller="a", action_type="llm") is None


# ---------------------------------------------------------------------------
# adaptive_cron.py
# ---------------------------------------------------------------------------


class TestAdaptiveCron:
    def test_fire_always_always_fires(self) -> None:
        gate = RegimeGate(regime_getter=lambda: RegimeTag.CALM, calm_skip_ratio=3)
        for _ in range(5):
            d = gate.should_fire(BackgroundTask.AUDIT_SUMMARIZE)
            assert d.fire is True, "fire-always tasks must never skip"

    def test_sparse_ok_skipped_in_calm(self) -> None:
        gate = RegimeGate(regime_getter=lambda: RegimeTag.CALM, calm_skip_ratio=3)
        fires = [gate.should_fire(BackgroundTask.DRIFT_SUMMARY).fire for _ in range(6)]
        # 1-in-3 schedule -> indices 0 and 3 fire.
        assert sum(fires) == 2

    def test_stressed_always_fires(self) -> None:
        gate = RegimeGate(regime_getter=lambda: RegimeTag.STRESSED)
        d = gate.should_fire(BackgroundTask.DRIFT_SUMMARY)
        assert d.fire is True


# ---------------------------------------------------------------------------
# deadman.py
# ---------------------------------------------------------------------------


class TestDeadmanSwitch:
    def _ds(self, tmp_path: Path, *, clock: datetime | None = None) -> DeadmanSwitch:
        t = [clock or datetime.now(UTC)]

        def clk() -> datetime:
            return t[0]

        ds = DeadmanSwitch(
            sentinel_path=tmp_path / "op.sentinel",
            journal_path=tmp_path / "op.jsonl",
            soft_stale_hours=2.0,
            hard_stale_hours=6.0,
            freeze_hours=24.0,
            clock=clk,
        )
        ds._t = t  # stash for mutation
        return ds

    def test_record_activity_creates_sentinel_and_live(self, tmp_path: Path) -> None:
        ds = self._ds(tmp_path)
        ds.record_activity(source="test")
        assert ds.state() is DeadmanState.LIVE
        assert ds.last_activity() is not None

    def test_stale_blocks_expensive_category(self, tmp_path: Path) -> None:
        ds = self._ds(tmp_path)
        ds.record_activity(source="bootstrap")
        # Jump 10 hours forward -> STALE.
        ds._t[0] = ds._t[0] + timedelta(hours=10)
        assert ds.state() is DeadmanState.STALE
        # STRATEGY_EDIT is in _STALE_BLOCKED.
        d = ds.decide(_env(category=TaskCategory.STRATEGY_EDIT))
        assert d.allow is False
        # SIMPLE_EDIT is NOT in _STALE_BLOCKED.
        d2 = ds.decide(_env(category=TaskCategory.SIMPLE_EDIT))
        assert d2.allow is True

    def test_frozen_allows_only_safe_list(self, tmp_path: Path) -> None:
        ds = self._ds(tmp_path)
        ds.record_activity(source="bootstrap")
        # Jump 100 hours -> FROZEN.
        ds._t[0] = ds._t[0] + timedelta(hours=100)
        assert ds.state() is DeadmanState.FROZEN
        # log_parsing is FROZEN_ALLOWED.
        d = ds.decide(_env(category=TaskCategory.LOG_PARSING))
        assert d.allow is True
        # strategy_edit is not.
        d2 = ds.decide(_env(category=TaskCategory.STRATEGY_EDIT))
        assert d2.allow is False

    def test_invalid_threshold_ordering_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            DeadmanSwitch(
                sentinel_path=tmp_path / "x",
                soft_stale_hours=10.0,
                hard_stale_hours=5.0,
                freeze_hours=20.0,
            )


# ---------------------------------------------------------------------------
# promotion.py
# ---------------------------------------------------------------------------


class TestPromotionGate:
    def _gate(self, tmp_path: Path) -> PromotionGate:
        return PromotionGate(
            state_path=tmp_path / "promotion.json",
            journal_path=tmp_path / "promotion.jsonl",
        )

    def test_hold_without_data(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        gate.register("strat_A")
        decision = gate.evaluate("strat_A")
        assert decision.action is PromotionAction.HOLD
        assert decision.from_stage is PromotionStage.SHADOW

    def test_promote_shadow_to_paper_when_clean(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        gate.register("strat_B")
        gate.update_metrics(
            "strat_B",
            StageMetrics(
                trades=100,
                days_active=30.0,
                sharpe=2.0,
                max_dd_pct=1.0,
                win_rate=0.6,
                mean_slippage_bps=1.0,
            ),
        )
        decision = gate.evaluate("strat_B")
        assert decision.action is PromotionAction.PROMOTE
        assert decision.to_stage is PromotionStage.PAPER

    def test_hard_break_demotes(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        gate.register("strat_C", stage=PromotionStage.LIVE_1LOT)
        gate.update_metrics(
            "strat_C",
            StageMetrics(
                trades=50,
                days_active=10.0,
                sharpe=-1.0,
                max_dd_pct=15.0,
                win_rate=0.3,
                mean_slippage_bps=10.0,
            ),
        )
        decision = gate.evaluate("strat_C")
        assert decision.action is PromotionAction.DEMOTE
        assert decision.to_stage is PromotionStage.PAPER

    def test_shadow_hard_break_retires(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        gate.register("strat_D")
        gate.update_metrics(
            "strat_D",
            StageMetrics(
                trades=50,
                days_active=10.0,
                sharpe=-1.0,
                max_dd_pct=15.0,
                win_rate=0.3,
            ),
        )
        decision = gate.evaluate("strat_D")
        assert decision.action is PromotionAction.RETIRE
        assert decision.to_stage is PromotionStage.RETIRED

    def test_apply_mutates_state(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        gate.register("strat_E")
        gate.update_metrics(
            "strat_E",
            StageMetrics(
                trades=100,
                days_active=30.0,
                sharpe=2.0,
                max_dd_pct=1.0,
                win_rate=0.6,
                mean_slippage_bps=1.0,
            ),
        )
        decision = gate.evaluate("strat_E")
        spec = gate.apply(decision)
        assert spec.current_stage is PromotionStage.PAPER
        # State file written.
        assert (tmp_path / "promotion.json").exists()


# ---------------------------------------------------------------------------
# cost_forecast.py
# ---------------------------------------------------------------------------


class TestCostForecast:
    def test_empty_journal_yields_green(self, tmp_path: Path) -> None:
        cf = CostForecast(
            journal_path=tmp_path / "nope.jsonl",
            monthly_cap_usd=100.0,
        )
        report = cf.snapshot()
        assert report.severity == "GREEN"
        assert report.projected_monthly == 0.0

    def test_over_cap_is_red(self, tmp_path: Path) -> None:
        j = tmp_path / "avengers.jsonl"
        j.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        # Simulate big 24h spend. 1000 dispatches * 5x cost * $0.06/call
        # over 24h -> projection ~$9125/mo. Cap at $200 -> RED.
        lines = []
        for i in range(1000):
            lines.append(
                json.dumps(
                    {
                        "ts": (now - timedelta(minutes=i)).isoformat(),
                        "envelope": {
                            "category": TaskCategory.STRATEGY_EDIT.value,
                            "caller": SubsystemId.OPERATOR.value,
                            "goal": "x",
                        },
                        "result": {
                            "cost_multiplier": 5.0,
                            "success": True,
                        },
                    }
                )
            )
        j.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cf = CostForecast(journal_path=j, monthly_cap_usd=200.0)
        report = cf.snapshot()
        assert report.severity == "RED"
        assert report.projected_monthly > 200.0


# ---------------------------------------------------------------------------
# watchdog.py
# ---------------------------------------------------------------------------


class TestWatchdog:
    def _bus(self, tmp_path: Path) -> PushBus:
        return PushBus(
            notifiers=[
                LocalFileNotifier(path=tmp_path / "alerts.jsonl"),
            ]
        )

    def test_offline_when_no_heartbeat(self, tmp_path: Path) -> None:
        wd = Watchdog(
            journal_path=tmp_path / "empty.jsonl",
            push_bus=self._bus(tmp_path),
            stuck_minutes=1.0,
            offline_minutes=5.0,
            lookback_minutes=30.0,
        )
        report = wd.sweep()
        # Every persona should be OFFLINE.
        statuses = {h.persona: h.status for h in report.daemons}
        for p in ("JARVIS", "BATMAN", "ALFRED", "ROBIN"):
            assert statuses[p] is HealthStatus.OFFLINE

    def test_healthy_with_fresh_heartbeat(self, tmp_path: Path) -> None:
        jp = tmp_path / "journal.jsonl"
        now = datetime.now(UTC)
        lines = []
        for p in ("JARVIS", "BATMAN", "ALFRED", "ROBIN"):
            lines.append(
                json.dumps(
                    {
                        "ts": now.isoformat(),
                        "kind": "heartbeat",
                        "persona": p,
                    }
                )
            )
        jp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        wd = Watchdog(
            journal_path=jp,
            push_bus=self._bus(tmp_path),
            stuck_minutes=5.0,
            offline_minutes=15.0,
            lookback_minutes=60.0,
        )
        report = wd.sweep()
        for h in report.daemons:
            assert h.status is HealthStatus.HEALTHY

    def test_self_persona_skipped(self, tmp_path: Path) -> None:
        wd = Watchdog(
            journal_path=tmp_path / "empty.jsonl",
            push_bus=self._bus(tmp_path),
            stuck_minutes=1.0,
            offline_minutes=5.0,
            lookback_minutes=30.0,
            self_persona="JARVIS",
        )
        report = wd.sweep()
        personas = {h.persona for h in report.daemons}
        assert "JARVIS" not in personas
        assert "BATMAN" in personas


# ---------------------------------------------------------------------------
# hardened_fleet.py
# ---------------------------------------------------------------------------


class TestHardenedFleet:
    def test_passthrough_when_no_guards(self, tmp_path: Path) -> None:
        fleet = Fleet(
            executor=DryRunExecutor(),
            journal_path=tmp_path / "j.jsonl",
        )
        hfleet = HardenedFleet(fleet)
        result = hfleet.dispatch(_env())
        assert result.success is True

    def test_breaker_short_circuits(self, tmp_path: Path) -> None:
        fleet = Fleet(
            executor=DryRunExecutor(),
            journal_path=tmp_path / "j.jsonl",
        )
        br = CircuitBreaker(max_consec_failures=1, cooldown_seconds=3600)
        br.record(_result(success=False, reason_code="executor_error"))
        assert br.status().state is BreakerState.OPEN
        hfleet = HardenedFleet(
            fleet,
            breaker=br,
            push_bus=PushBus(notifiers=[LocalFileNotifier(path=tmp_path / "a.jsonl")]),
        )
        res = hfleet.dispatch(_env())
        assert res.success is False
        assert res.reason_code == "breaker_open"

    def test_deadman_frozen_blocks_expensive(self, tmp_path: Path) -> None:
        fleet = Fleet(
            executor=DryRunExecutor(),
            journal_path=tmp_path / "j.jsonl",
        )
        t = [datetime.now(UTC)]

        def clk() -> datetime:
            return t[0]

        ds = DeadmanSwitch(
            sentinel_path=tmp_path / "sentinel",
            journal_path=tmp_path / "op.jsonl",
            soft_stale_hours=1.0,
            hard_stale_hours=2.0,
            freeze_hours=3.0,
            clock=clk,
        )
        ds.record_activity(source="bootstrap")
        t[0] = t[0] + timedelta(hours=10)
        assert ds.state() is DeadmanState.FROZEN
        hfleet = HardenedFleet(
            fleet,
            deadman=ds,
            push_bus=PushBus(notifiers=[LocalFileNotifier(path=tmp_path / "a.jsonl")]),
        )
        res = hfleet.dispatch(_env(category=TaskCategory.STRATEGY_EDIT))
        assert res.success is False
        assert res.reason_code == "deadman_blocked"

    def test_calibration_records_on_dispatch(self, tmp_path: Path) -> None:
        fleet = Fleet(
            executor=DryRunExecutor(),
            journal_path=tmp_path / "j.jsonl",
        )
        cal = CalibrationLoop(
            journal_path=tmp_path / "cal.jsonl",
            rehydrate=False,
        )
        hfleet = HardenedFleet(fleet, calibration=cal)
        hfleet.dispatch(_env())
        snap = cal.snapshot()
        assert len(snap) >= 1


# ---------------------------------------------------------------------------
# shared_breaker.py -- cross-process CircuitBreaker sync
# ---------------------------------------------------------------------------


class TestSharedCircuitBreaker:
    """File-backed breaker state propagates across instances on the same path."""

    def _bpath(self, tmp_path: Path) -> Path:
        return tmp_path / "breaker.json"

    def test_default_path_is_home_jarvis(self) -> None:
        # Sanity: the module-level default points at ~/.jarvis/breaker.json
        # so production wiring lands in the expected location.
        from eta_engine.brain.avengers import DEFAULT_BREAKER_PATH

        assert DEFAULT_BREAKER_PATH.name == "breaker.json"
        assert DEFAULT_BREAKER_PATH.parent.name == ".jarvis"

    def test_trip_writes_open_to_disk(self, tmp_path: Path) -> None:
        path = self._bpath(tmp_path)
        br = SharedCircuitBreaker(
            path=path,
            max_consec_failures=2,
            cooldown_seconds=60,
            rehydrate_on_init=False,
        )
        br.record(_result(success=False, reason_code="executor_error"))
        br.record(_result(success=False, reason_code="executor_error"))
        assert br.status().state is BreakerState.OPEN
        # Disk state must reflect OPEN with v1 schema and timestamps.
        disk = read_shared_status(path=path)
        assert disk is not None
        assert disk["version"] == 1
        assert disk["state"] == "OPEN"
        assert disk["tripped_at"] is not None
        assert disk["reopen_at"] is not None
        assert disk["last_reason"]

    def test_cross_process_trip_adoption(self, tmp_path: Path) -> None:
        """A second instance constructed after A tripped adopts OPEN."""
        path = self._bpath(tmp_path)
        a = SharedCircuitBreaker(
            path=path,
            max_consec_failures=1,
            cooldown_seconds=3600,
            rehydrate_on_init=False,
        )
        a.record(_result(success=False, reason_code="executor_error"))
        assert a.status().state is BreakerState.OPEN

        # B is brand-new; rehydrate_on_init=True (default) reads disk.
        b = SharedCircuitBreaker(path=path, cooldown_seconds=3600)
        assert b.status().state is BreakerState.OPEN
        with pytest.raises(BreakerTripped):
            b.pre_dispatch()

    def test_cross_process_close_broadcast(self, tmp_path: Path) -> None:
        """When A resets, a live instance B adopts CLOSED on refresh."""
        path = self._bpath(tmp_path)
        a = SharedCircuitBreaker(
            path=path,
            max_consec_failures=1,
            cooldown_seconds=3600,
            rehydrate_on_init=False,
        )
        a.record(_result(success=False, reason_code="executor_error"))

        b = SharedCircuitBreaker(path=path, cooldown_seconds=3600)
        assert b.status().state is BreakerState.OPEN  # adopted from disk

        a.reset()  # writes CLOSED to disk
        # B's pre_dispatch refreshes from disk before evaluating; the
        # shared CLOSED broadcast resets B to CLOSED.
        b.pre_dispatch()  # should not raise
        assert b.status().state is BreakerState.CLOSED

    def test_half_open_never_persists(self, tmp_path: Path) -> None:
        """Disk only stores OPEN/CLOSED; HALF_OPEN is process-local."""
        path = self._bpath(tmp_path)
        t = [datetime(2026, 1, 1, tzinfo=UTC)]

        def clk() -> datetime:
            return t[0]

        br = SharedCircuitBreaker(
            path=path,
            max_consec_failures=1,
            cooldown_seconds=10,
            rehydrate_on_init=False,
            clock=clk,
        )
        br.record(_result(success=False, reason_code="executor_error"))
        assert br.status().state is BreakerState.OPEN

        # Warp past cooldown -> next pre_dispatch flips to HALF_OPEN.
        t[0] = t[0] + timedelta(seconds=30)
        br.pre_dispatch()
        assert br.status().state is BreakerState.HALF_OPEN
        # Disk must still read OPEN -- HALF_OPEN is never persisted.
        disk = read_shared_status(path=path)
        assert disk is not None
        assert disk["state"] == "OPEN"

    def test_schema_version_mismatch_ignored(self, tmp_path: Path) -> None:
        """A file written with version != 1 is treated as missing."""
        path = self._bpath(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 9999,
                    "state": "OPEN",
                    "tripped_at": datetime.now(UTC).isoformat(),
                    "reopen_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "last_reason": "future schema",
                    "written_at": datetime.now(UTC).isoformat(),
                    "writer_pid": 1,
                }
            ),
            encoding="utf-8",
        )
        br = SharedCircuitBreaker(path=path, rehydrate_on_init=True)
        # Default state is CLOSED; the v9999 file must not coerce OPEN.
        assert br.status().state is BreakerState.CLOSED

    def test_corrupt_json_handled_gracefully(self, tmp_path: Path) -> None:
        """Garbage in the state file does not crash the breaker."""
        path = self._bpath(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        br = SharedCircuitBreaker(path=path, rehydrate_on_init=True)
        assert br.status().state is BreakerState.CLOSED
        # read_shared_status should also return None on corrupt file.
        assert read_shared_status(path=path) is None

    def test_reset_shared_helper_writes_closed(self, tmp_path: Path) -> None:
        """The operator CLI hook writes a CLOSED record atomically."""
        path = self._bpath(tmp_path)
        br = SharedCircuitBreaker(
            path=path,
            max_consec_failures=1,
            cooldown_seconds=3600,
            rehydrate_on_init=False,
        )
        br.record(_result(success=False, reason_code="executor_error"))
        assert read_shared_status(path=path)["state"] == "OPEN"

        assert reset_shared(path=path) is True
        status = read_shared_status(path=path)
        assert status is not None
        assert status["state"] == "CLOSED"
        assert status["last_reason"] == "operator_reset"

    def test_past_reopen_moves_local_to_half_open(self, tmp_path: Path) -> None:
        """Disk OPEN with a past reopen_at -> refreshed instance goes HALF_OPEN."""
        path = self._bpath(tmp_path)
        # Write an OPEN record whose cooldown already elapsed.
        now = datetime.now(UTC)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "OPEN",
                    "tripped_at": (now - timedelta(minutes=30)).isoformat(),
                    "reopen_at": (now - timedelta(minutes=1)).isoformat(),
                    "last_reason": "stale cooldown",
                    "written_at": now.isoformat(),
                    "writer_pid": 0,
                }
            ),
            encoding="utf-8",
        )
        br = SharedCircuitBreaker(path=path, rehydrate_on_init=True)
        # Refresh-on-init should have moved local state to HALF_OPEN.
        assert br.status().state is BreakerState.HALF_OPEN

    def test_missing_file_read_returns_none(self, tmp_path: Path) -> None:
        assert read_shared_status(path=tmp_path / "absent.json") is None

    def test_status_includes_writer_pid(self, tmp_path: Path) -> None:
        """The writer_pid field is populated so dashboards can show origin."""
        import os as _os

        path = self._bpath(tmp_path)
        br = SharedCircuitBreaker(
            path=path,
            max_consec_failures=1,
            cooldown_seconds=60,
            rehydrate_on_init=False,
        )
        br.record(_result(success=False, reason_code="executor_error"))
        status = read_shared_status(path=path)
        assert status is not None
        assert status["writer_pid"] == _os.getpid()


# ---------------------------------------------------------------------------
# promotion.py -- red team gate hook
# ---------------------------------------------------------------------------


class TestPromotionRedTeam:
    """PromotionGate consults a red team gate before committing PROMOTE."""

    def _full_metrics(self) -> StageMetrics:
        """Metrics that clear every threshold on SHADOW->PAPER."""
        return StageMetrics(
            trades=100,
            days_active=30.0,
            sharpe=2.0,
            max_dd_pct=1.0,
            win_rate=0.6,
            mean_slippage_bps=1.0,
        )

    def test_no_gate_passthrough_promotes(self, tmp_path: Path) -> None:
        """Without a red team gate, all thresholds cleared -> PROMOTE."""
        gate = PromotionGate(
            state_path=tmp_path / "promotion.json",
            journal_path=tmp_path / "promotion.jsonl",
            red_team_gate=None,
        )
        gate.register("no_rt")
        gate.update_metrics("no_rt", self._full_metrics())
        decision = gate.evaluate("no_rt")
        assert decision.action is PromotionAction.PROMOTE
        assert decision.to_stage is PromotionStage.PAPER

    def test_approval_lets_promotion_through(self, tmp_path: Path) -> None:
        calls: list[tuple[str, str]] = []

        def approve(spec: PromotionSpec, tentative: PromotionDecision) -> RedTeamVerdict:
            calls.append((spec.strategy_id, tentative.to_stage.value))
            return RedTeamVerdict(approve=True, reasons=["looks ok"], risk_score=0.2)

        gate = PromotionGate(
            state_path=tmp_path / "p.json",
            journal_path=tmp_path / "p.jsonl",
            red_team_gate=approve,
        )
        gate.register("rt_approve")
        gate.update_metrics("rt_approve", self._full_metrics())
        decision = gate.evaluate("rt_approve")
        assert decision.action is PromotionAction.PROMOTE
        assert calls == [("rt_approve", PromotionStage.PAPER.value)]

        # The verdict is retained for introspection until apply() consumes it.
        stored = gate.last_red_team_verdict("rt_approve")
        assert stored is not None
        assert stored.approve is True
        assert stored.risk_score == 0.2

    def test_veto_converts_promote_to_hold(self, tmp_path: Path) -> None:
        def veto(spec: PromotionSpec, tentative: PromotionDecision) -> RedTeamVerdict:
            return RedTeamVerdict(
                approve=False,
                reasons=["regime mismatch", "correlation too high"],
                risk_score=0.85,
            )

        gate = PromotionGate(
            state_path=tmp_path / "p.json",
            journal_path=tmp_path / "p.jsonl",
            red_team_gate=veto,
        )
        gate.register("rt_veto")
        gate.update_metrics("rt_veto", self._full_metrics())
        decision = gate.evaluate("rt_veto")
        assert decision.action is PromotionAction.HOLD
        assert decision.from_stage is PromotionStage.SHADOW
        assert decision.to_stage is PromotionStage.SHADOW
        # The veto reasons are prefixed with a canonical red_team_blocked tag.
        joined = "\n".join(decision.reasons)
        assert "red_team_blocked" in joined
        assert "risk_score=0.85" in joined
        assert "regime mismatch" in joined
        assert "correlation too high" in joined

    def test_callable_raises_fails_closed(self, tmp_path: Path) -> None:
        """A broken red team callable must not silently PROMOTE."""

        def boom(spec: PromotionSpec, tentative: PromotionDecision) -> RedTeamVerdict:
            raise RuntimeError("red team is offline")

        gate = PromotionGate(
            state_path=tmp_path / "p.json",
            journal_path=tmp_path / "p.jsonl",
            red_team_gate=boom,
        )
        gate.register("rt_boom")
        gate.update_metrics("rt_boom", self._full_metrics())
        decision = gate.evaluate("rt_boom")
        assert decision.action is PromotionAction.HOLD
        joined = "\n".join(decision.reasons)
        assert "red_team_blocked" in joined
        assert "red_team callable raised" in joined
        assert "RuntimeError" in joined
        stored = gate.last_red_team_verdict("rt_boom")
        assert stored is not None
        assert stored.approve is False
        assert stored.risk_score == 1.0

    def test_live_1lot_to_live_full_is_ungated(self, tmp_path: Path) -> None:
        """Sizing transition is owned by risk-budget; red team not consulted."""
        calls: list[str] = []

        def tracker(spec: PromotionSpec, tentative: PromotionDecision) -> RedTeamVerdict:
            calls.append(f"{spec.current_stage.value}->{tentative.to_stage.value}")
            return RedTeamVerdict(approve=False, reasons=["would veto"])

        gate = PromotionGate(
            state_path=tmp_path / "p.json",
            journal_path=tmp_path / "p.jsonl",
            red_team_gate=tracker,
        )
        gate.register("rt_live", stage=PromotionStage.LIVE_1LOT)
        gate.update_metrics(
            "rt_live",
            StageMetrics(
                trades=200,
                days_active=45.0,
                sharpe=1.8,
                max_dd_pct=1.0,
                win_rate=0.55,
                mean_slippage_bps=1.5,
            ),
        )
        decision = gate.evaluate("rt_live")
        # Transition should PROMOTE without consulting the gate.
        assert decision.action is PromotionAction.PROMOTE
        assert decision.to_stage is PromotionStage.LIVE_FULL
        assert calls == [], "red team must not be called on LIVE_1LOT->LIVE_FULL"

    def test_apply_stamps_red_team_into_journal(self, tmp_path: Path) -> None:
        """apply() persists the verdict under the 'red_team' key."""

        def approve(spec: PromotionSpec, tentative: PromotionDecision) -> RedTeamVerdict:
            return RedTeamVerdict(
                approve=True,
                reasons=["passed adversarial review"],
                risk_score=0.1,
            )

        journal = tmp_path / "p.jsonl"
        gate = PromotionGate(
            state_path=tmp_path / "p.json",
            journal_path=journal,
            red_team_gate=approve,
        )
        gate.register("rt_journal")
        gate.update_metrics("rt_journal", self._full_metrics())
        decision = gate.evaluate("rt_journal")
        gate.apply(decision)

        # After apply() consumes the verdict the introspection cache clears.
        assert gate.last_red_team_verdict("rt_journal") is None

        # The applied journal entry must carry the red_team payload.
        apply_records = [json.loads(ln) for ln in journal.read_text(encoding="utf-8").splitlines() if ln.strip()]
        apply_rec = next(r for r in apply_records if r.get("event") == "apply")
        assert "red_team" in apply_rec
        rt = apply_rec["red_team"]
        assert rt["approve"] is True
        assert rt["risk_score"] == 0.1
        assert "passed adversarial review" in rt["reasons"]

    def test_gated_transitions_are_exactly_two(self) -> None:
        """The gated set is intentionally frozen at two entries."""
        expected = frozenset(
            {
                (PromotionStage.SHADOW, PromotionStage.PAPER),
                (PromotionStage.PAPER, PromotionStage.LIVE_1LOT),
            }
        )
        assert expected == RED_TEAM_GATED_TRANSITIONS
        # Terminal sizing transition must NOT be in the gated set.
        assert (PromotionStage.LIVE_1LOT, PromotionStage.LIVE_FULL) not in RED_TEAM_GATED_TRANSITIONS

    def test_hold_without_thresholds_does_not_call_gate(self, tmp_path: Path) -> None:
        """When threshold checks already produce HOLD, red team is skipped."""
        calls: list[str] = []

        def tracker(spec: PromotionSpec, tentative: PromotionDecision) -> RedTeamVerdict:
            calls.append(spec.strategy_id)
            return RedTeamVerdict(approve=True)

        gate = PromotionGate(
            state_path=tmp_path / "p.json",
            journal_path=tmp_path / "p.jsonl",
            red_team_gate=tracker,
        )
        gate.register("rt_hold_thr")
        # Metrics fail min_days threshold -> HOLD before the red team branch.
        gate.update_metrics(
            "rt_hold_thr",
            StageMetrics(
                trades=100,
                days_active=1.0,
                sharpe=2.0,
                max_dd_pct=1.0,
                win_rate=0.6,
                mean_slippage_bps=1.0,
            ),
        )
        decision = gate.evaluate("rt_hold_thr")
        assert decision.action is PromotionAction.HOLD
        assert calls == []


# ---------------------------------------------------------------------------
# drift_detector.py -- backtest-to-live divergence
# ---------------------------------------------------------------------------


class TestDriftDetector:
    """DriftDetector compares BT vs live return streams and emits a verdict."""

    def _det(self, tmp_path: Path, **kw: object) -> DriftDetector:
        return DriftDetector(
            journal_path=tmp_path / "drift.jsonl",
            **kw,  # type: ignore[arg-type]
        )

    def test_identical_distributions_ok(self, tmp_path: Path) -> None:
        det = self._det(tmp_path)
        # Deterministic alternating sequence: mean == 0 in both.
        series = [0.01 if i % 2 == 0 else -0.01 for i in range(100)]
        report = det.check("same", series, series)
        assert report.verdict is DriftVerdict.OK
        assert report.recommendation is None
        assert report.bt_sample_size == 100
        assert report.live_sample_size == 100

    def test_opposite_drift_auto_demote(self, tmp_path: Path) -> None:
        det = self._det(tmp_path)
        # Strong positive BT vs strong negative live -> huge Sharpe delta.
        bt = [0.005 + (0.001 if i % 2 == 0 else -0.001) for i in range(252)]
        lv = [-0.005 + (0.001 if i % 2 == 0 else -0.001) for i in range(60)]
        report = det.check("opposite", bt, lv)
        assert report.verdict is DriftVerdict.AUTO_DEMOTE
        assert report.recommendation is PromotionAction.DEMOTE
        # Should also have sane summary fields.
        assert report.sharpe_bt > 0.0
        assert report.sharpe_live < 0.0
        assert report.mean_return_delta < 0.0

    def test_under_sample_returns_ok_with_insufficient_reason(self, tmp_path: Path) -> None:
        det = self._det(tmp_path, min_live_samples=30)
        bt = [0.01, -0.01] * 100
        lv = [0.01, -0.01] * 5  # only 10 samples
        report = det.check("undersampled", bt, lv)
        assert report.verdict is DriftVerdict.OK
        assert report.recommendation is None
        assert any("insufficient live samples" in r for r in report.reasons)

    def test_degenerate_backtest_returns_ok(self, tmp_path: Path) -> None:
        det = self._det(tmp_path, min_live_samples=20)
        bt = [0.01]  # only 1 point -> degenerate
        lv = [0.01 if i % 2 == 0 else -0.01 for i in range(30)]
        report = det.check("bt_degen", bt, lv)
        assert report.verdict is DriftVerdict.OK
        assert any("insufficient backtest samples" in r for r in report.reasons)

    def test_threshold_ordering_validated(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            DriftDetector(
                warn_sharpe_delta_sigma=2.5,
                demote_sharpe_delta_sigma=1.5,  # demote < warn is nonsense
                journal_path=tmp_path / "drift.jsonl",
            )
        with pytest.raises(ValueError):
            DriftDetector(
                warn_kl=0.5,
                demote_kl=0.1,  # demote < warn
                journal_path=tmp_path / "drift.jsonl",
            )
        with pytest.raises(ValueError):
            DriftDetector(
                warn_sharpe_delta_sigma=-1.0,  # negative
                journal_path=tmp_path / "drift.jsonl",
            )
        with pytest.raises(ValueError):
            DriftDetector(
                bins=1,  # must be >= 2
                journal_path=tmp_path / "drift.jsonl",
            )

    def test_journal_readback_round_trip(self, tmp_path: Path) -> None:
        journal = tmp_path / "drift.jsonl"
        det = DriftDetector(journal_path=journal, min_live_samples=5)
        series = [0.01 if i % 2 == 0 else -0.01 for i in range(50)]
        det.check("round_trip", series, series)
        records = read_drift_journal(journal, n=10)
        assert len(records) == 1
        assert records[0]["strategy_id"] == "round_trip"
        assert records[0]["verdict"] == DriftVerdict.OK.value

    def test_journal_readback_missing_file(self, tmp_path: Path) -> None:
        assert read_drift_journal(tmp_path / "nope.jsonl") == []

    def test_mean_delta_recorded_correctly(self, tmp_path: Path) -> None:
        det = self._det(tmp_path)
        bt = [0.002] * 100
        lv = [0.001] * 30
        report = det.check("mean_delta", bt, lv)
        # mean(live) - mean(bt) = 0.001 - 0.002 = -0.001 (approximately)
        assert abs(report.mean_return_delta - (-0.001)) < 1e-9

    def test_clock_injection_stamps_generated_at(self, tmp_path: Path) -> None:
        fixed = datetime(2026, 2, 14, 12, 0, tzinfo=UTC)
        det = DriftDetector(
            journal_path=tmp_path / "drift.jsonl",
            min_live_samples=5,
            clock=lambda: fixed,
        )
        report = det.check("clock_inj", [0.01, -0.01] * 20, [0.01, -0.01] * 20)
        assert report.generated_at == fixed

    def test_non_finite_inputs_are_filtered(self, tmp_path: Path) -> None:
        det = self._det(tmp_path, min_live_samples=5)
        bt = [0.01, float("inf"), -0.01, float("nan"), 0.005]
        lv = [0.01, -0.01, float("nan"), 0.005, -0.005, 0.01]
        report = det.check("finite", bt, lv)
        # Non-finite stripped: 3 clean BT samples, 5 clean live samples.
        assert report.bt_sample_size == 3
        assert report.live_sample_size == 5

    def test_default_journal_path_is_dot_jarvis(self) -> None:
        """The module-level default journal lands next to the other JARVIS logs."""
        assert DRIFT_JOURNAL.name == "drift.jsonl"
        assert DRIFT_JOURNAL.parent.name == ".jarvis"

    def test_report_is_frozen_model(self, tmp_path: Path) -> None:
        """DriftReport is immutable; downstream code can't mutate it."""
        det = self._det(tmp_path, min_live_samples=5)
        report = det.check("frozen", [0.01] * 10, [0.01] * 10)
        assert isinstance(report, DriftReport)
        with pytest.raises((ValueError, TypeError)):
            report.verdict = DriftVerdict.AUTO_DEMOTE  # type: ignore[misc]


# ---------------------------------------------------------------------------
# chaos_drill.py -- monthly safety-system regression runner
# ---------------------------------------------------------------------------


class TestChaosDrill:
    """Every drill (v0.1.56: 16 of them) must pass against isolated sandboxes."""

    def test_all_drills_pass_in_sandbox(self, tmp_path: Path) -> None:
        results = chaos_drill.run_drills(sandbox=tmp_path / "chaos")
        # v0.1.56 CHAOS DRILL CLOSURE: 4 legacy drills + 12 new surface drills.
        assert len(results) == len(chaos_drill.ALL_DRILLS)
        failures = [r for r in results if not r["passed"]]
        assert not failures, "All chaos drills must pass; failures:\n" + "\n".join(
            f"  {r['drill']}: {r['details']}" for r in failures
        )
        # Every result should have a drill name, details, and observed dict.
        for r in results:
            assert r["drill"] in chaos_drill.ALL_DRILLS
            assert isinstance(r["details"], str)
            assert isinstance(r["observed"], dict)

    def test_unknown_drill_reports_failure(self, tmp_path: Path) -> None:
        results = chaos_drill.run_drills(
            ["breaker", "bogus"],
            sandbox=tmp_path / "chaos",
        )
        bogus = [r for r in results if r["drill"] == "bogus"]
        assert bogus, "unknown drill must still surface in the result set"
        assert bogus[0]["passed"] is False
        assert "unknown drill" in bogus[0]["details"]

    def test_subset_selection(self, tmp_path: Path) -> None:
        """Running a subset returns only the named drills."""
        results = chaos_drill.run_drills(
            ["push"],
            sandbox=tmp_path / "chaos",
        )
        assert len(results) == 1
        assert results[0]["drill"] == "push"
        assert results[0]["passed"] is True

    def test_drill_isolation_per_sandbox(self, tmp_path: Path) -> None:
        """Each drill writes into its own sub-directory."""
        sandbox = tmp_path / "chaos"
        chaos_drill.run_drills(sandbox=sandbox)
        # After the run, each sub-sandbox directory should exist.
        for name in chaos_drill.ALL_DRILLS:
            assert (sandbox / name).exists(), f"drill {name} did not get its own sandbox dir"

    def test_format_report_contains_pass_markers(self, tmp_path: Path) -> None:
        results = chaos_drill.run_drills(sandbox=tmp_path / "chaos")
        report = chaos_drill.format_report(results)
        assert "EVOLUTIONARY TRADING ALGO // CHAOS DRILL" in report
        assert "[PASS]" in report
        n = len(chaos_drill.ALL_DRILLS)
        assert f"SUMMARY: {n}/{n}" in report

    def test_main_returns_zero_on_all_pass(self, capsys) -> None:  # noqa: ANN001
        """CLI exit code is 0 when every drill passes."""
        exit_code = chaos_drill.main(["all"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "CHAOS DRILL" in captured.out
        n = len(chaos_drill.ALL_DRILLS)
        assert f"{n}/{n} drills passed" in captured.out

    def test_main_json_output_structure(self, capsys) -> None:  # noqa: ANN001
        """--json emits a parseable envelope with passed/failed/total."""
        exit_code = chaos_drill.main(["all", "--json"])
        captured = capsys.readouterr()
        assert exit_code == 0
        payload = json.loads(captured.out)
        n = len(chaos_drill.ALL_DRILLS)
        assert payload["total"] == n
        assert payload["passed"] == n
        assert payload["failed"] == 0
        assert len(payload["results"]) == n

    def test_main_rejects_unknown_flag(self, capsys) -> None:  # noqa: ANN001
        exit_code = chaos_drill.main(["--foo"])
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "unknown flag" in captured.err

    def test_main_help_flag(self, capsys) -> None:  # noqa: ANN001
        exit_code = chaos_drill.main(["--help"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "CHAOS DRILL" in captured.out or "chaos" in captured.out.lower()

    def test_drill_funcs_table_matches_all_drills(self) -> None:
        """The drill catalogue and the public DRILL_FUNCS must agree."""
        assert set(chaos_drill.DRILL_FUNCS.keys()) == set(chaos_drill.ALL_DRILLS)


class TestChaosDrillCronHandler:
    """The CHAOS_DRILL BackgroundTask must be wired end-to-end:
    enum -> owner -> cadence -> category -> goal -> cron handler.
    """

    def test_enum_exists_and_owned_by_alfred(self) -> None:
        from eta_engine.brain.avengers import TASK_OWNERS

        assert BackgroundTask.CHAOS_DRILL in TASK_OWNERS
        assert TASK_OWNERS[BackgroundTask.CHAOS_DRILL] == "ALFRED"

    def test_cadence_is_monthly_first_at_3am(self) -> None:
        from eta_engine.brain.avengers.dispatch import TASK_CADENCE

        assert TASK_CADENCE[BackgroundTask.CHAOS_DRILL] == "0 3 1 * *"

    def test_crontab_contains_chaos_drill_entry(self) -> None:
        """deploy/cron/avengers.crontab wires CHAOS_DRILL to run_task."""
        from pathlib import Path

        import eta_engine

        # Package root -> parent is eta_engine/
        repo = Path(eta_engine.__file__).resolve().parent
        crontab = repo / "deploy" / "cron" / "avengers.crontab"
        assert crontab.exists()
        text = crontab.read_text(encoding="utf-8")
        assert "CHAOS_DRILL" in text
        assert "0 3 1 * *" in text

    def test_handler_writes_report_and_history(self, tmp_path: Path) -> None:
        """_task_chaos_drill runs the drills and journals to state_dir."""
        from eta_engine.deploy.scripts.run_task import _task_chaos_drill

        out = _task_chaos_drill(tmp_path)
        n = len(chaos_drill.ALL_DRILLS)
        assert out["total"] == n
        assert out["passed"] == n
        assert out["failed"] == 0
        report_file = tmp_path / "chaos_drill.json"
        history_file = tmp_path / "chaos_drill_history.jsonl"
        assert report_file.exists()
        assert history_file.exists()
        report = json.loads(report_file.read_text(encoding="utf-8"))
        assert report["total"] == n
        assert report["passed"] == n
        assert len(report["results"]) == n
        # History appends one JSONL row per run.
        history_lines = history_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(history_lines) == 1
        assert json.loads(history_lines[0])["total"] == n

    def test_handler_history_appends(self, tmp_path: Path) -> None:
        """Running twice produces two JSONL rows without overwriting."""
        from eta_engine.deploy.scripts.run_task import _task_chaos_drill

        _task_chaos_drill(tmp_path)
        _task_chaos_drill(tmp_path)
        history_file = tmp_path / "chaos_drill_history.jsonl"
        history_lines = history_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(history_lines) == 2


# ---------------------------------------------------------------------------
# JARVIS dashboard drift-card wiring (scripts/jarvis_dashboard.py)
# ---------------------------------------------------------------------------


class TestDashboardDriftPanel:
    """_render_drift() + collect_state() surface the drift journal correctly.

    The drift card reads ``~/.jarvis/drift.jsonl`` written by
    ``DriftDetector._journal``. Tests monkeypatch the module-level
    ``DRIFT_JOURNAL`` constant so they never touch the real home dir.
    """

    @pytest.fixture
    def dashboard_module(self, tmp_path: Path, monkeypatch):
        """Import jarvis_dashboard with DRIFT_JOURNAL pointed at a temp path."""
        import eta_engine.scripts.jarvis_dashboard as mod

        drift_path = tmp_path / "drift.jsonl"
        monkeypatch.setattr(mod, "DRIFT_JOURNAL", drift_path)
        return mod, drift_path

    def test_render_drift_no_data_when_journal_missing(
        self,
        dashboard_module,
    ) -> None:
        mod, _ = dashboard_module
        out = mod._render_drift()
        assert out["state"] == "NO_DATA"
        assert "journal" in out

    def test_render_drift_surfaces_last_verdict(
        self,
        dashboard_module,
    ) -> None:
        """Writes a real DriftReport, confirms field mapping."""
        from eta_engine.brain.avengers.drift_detector import (
            DriftDetector,
            DriftVerdict,
        )

        mod, drift_path = dashboard_module
        # Use the real detector so we exercise the exact journal schema.
        detector = DriftDetector(journal_path=drift_path)
        # Backtest: tight, high-sharpe series. Live: big drawdown -> forces
        # an AUTO_DEMOTE verdict so we can assert on a non-trivial path.
        bt = [0.004] * 30 + [0.0035] * 30
        lv = [-0.006] * 30
        report = detector.check("strat_X", bt, lv, journal=True)
        assert report.verdict in {DriftVerdict.WARN, DriftVerdict.AUTO_DEMOTE}

        out = mod._render_drift()
        assert out["state"] == report.verdict.value
        assert out["strategy_id"] == "strat_X"
        assert out["kl"] is not None
        assert out["sharpe_delta"] is not None
        assert out["mean_delta"] is not None
        assert out["n_live"] == report.live_sample_size
        assert out["n_backtest"] == report.bt_sample_size
        assert out["entries"] == 1
        # Rolling counts must include the verdict we just saw.
        assert out["counts"].get(report.verdict.value) == 1
        # Reasons become a single joined string.
        assert isinstance(out["reason"], str)
        assert len(out["reason"]) > 0

    def test_render_drift_counts_rolling_window(
        self,
        dashboard_module,
    ) -> None:
        """Multiple entries accumulate per-verdict counts."""
        mod, drift_path = dashboard_module
        # Hand-write 3 entries, mixed verdicts.
        drift_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"verdict": "OK", "generated_at": "2026-04-24T00:00:00+00:00"},
            {"verdict": "WARN", "generated_at": "2026-04-24T00:05:00+00:00"},
            {
                "verdict": "AUTO_DEMOTE",
                "generated_at": "2026-04-24T00:10:00+00:00",
                "strategy_id": "strat_Z",
                "reasons": ["kl exceeded"],
            },
        ]
        drift_path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n",
            encoding="utf-8",
        )
        out = mod._render_drift()
        assert out["state"] == "AUTO_DEMOTE"  # last entry
        assert out["strategy_id"] == "strat_Z"
        assert out["reason"] == "kl exceeded"
        assert out["counts"] == {"OK": 1, "WARN": 1, "AUTO_DEMOTE": 1}
        assert out["entries"] == 3

    def test_render_drift_joins_multiple_reasons(
        self,
        dashboard_module,
    ) -> None:
        mod, drift_path = dashboard_module
        drift_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "verdict": "WARN",
            "generated_at": "2026-04-24T00:00:00+00:00",
            "reasons": ["kl=0.20 >= warn=0.15", "sharpe delta = 1.7 sigma"],
        }
        drift_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        out = mod._render_drift()
        assert "kl=0.20" in out["reason"]
        assert "sharpe delta" in out["reason"]
        assert "; " in out["reason"]  # joined with "; "

    def test_render_drift_tolerates_malformed_journal(
        self,
        dashboard_module,
    ) -> None:
        """Garbage lines are skipped by read_drift_journal() without raising."""
        mod, drift_path = dashboard_module
        drift_path.parent.mkdir(parents=True, exist_ok=True)
        drift_path.write_text(
            '{"verdict": "OK", "generated_at": "2026-04-24T00:00:00+00:00"}\n'
            "this is not json\n"
            '{"verdict": "WARN", "generated_at": "2026-04-24T00:01:00+00:00"}\n',
            encoding="utf-8",
        )
        out = mod._render_drift()
        # Malformed line silently dropped; last *valid* entry still wins.
        assert out["state"] == "WARN"
        assert out["entries"] == 2

    def test_collect_state_includes_drift_key(
        self,
        dashboard_module,
    ) -> None:
        """collect_state() must expose 'drift' to the HTML layer."""
        mod, _ = dashboard_module
        state = mod.collect_state()
        assert "drift" in state
        # All other core panels should still be present.
        for key in ("breaker", "deadman", "forecast", "daemons", "promotion", "calibration", "journal", "alerts"):
            assert key in state

    def test_html_template_has_drift_card_slots(self) -> None:
        """The HTML still carries every element id our JS binds against."""
        import eta_engine.scripts.jarvis_dashboard as mod

        html = mod.INDEX_HTML
        for elt_id in ("drift-state", "drift-kl", "drift-dsharpe", "drift-dmean", "drift-n", "drift-reason"):
            assert f'id="{elt_id}"' in html


# ---------------------------------------------------------------------------
# Default red-team callable (brain.avengers.promotion.default_red_team_gate)
# ---------------------------------------------------------------------------


class TestDefaultRedTeamGate:
    """The default red-team vetoes fragile clearances and wires in for free.

    The fragility rule: the PromotionGate already verified the strategy
    cleared its stage thresholds. The red team's job is to ask how much
    MARGIN that clearance had. A strategy that cleared trades by one
    trade or sharpe by 0.01 has almost certainly been overfit. The
    default veto-fails all five fragility checks.
    """

    def _fat_margin(self) -> StageMetrics:
        """Metrics that clear SHADOW thresholds with plenty of margin."""
        return StageMetrics(
            trades=100,
            days_active=30.0,
            sharpe=2.0,
            max_dd_pct=1.0,
            win_rate=0.6,
            mean_slippage_bps=1.0,
        )

    def _shadow_promote(self, metrics: StageMetrics) -> tuple[PromotionSpec, PromotionDecision]:
        from datetime import UTC, datetime

        spec = PromotionSpec(
            strategy_id="strat",
            current_stage=PromotionStage.SHADOW,
            entered_stage_at=datetime.now(UTC),
            metrics=metrics,
        )
        decision = PromotionDecision(
            strategy_id="strat",
            from_stage=PromotionStage.SHADOW,
            to_stage=PromotionStage.PAPER,
            action=PromotionAction.PROMOTE,
            reasons=["ok"],
            metrics=metrics,
        )
        return spec, decision

    # --- positive / negative path -------------------------------------

    def test_approves_on_fat_margins(self) -> None:
        spec, decision = self._shadow_promote(self._fat_margin())
        verdict = default_red_team_gate(spec, decision)
        assert verdict.approve is True
        assert verdict.reasons == []
        assert verdict.risk_score == 0.0

    def test_vetoes_tight_sharpe_clearance(self) -> None:
        """Sharpe clears 1.0 but only barely (1.02 < 1.0 * 1.10 = 1.10)."""
        m = StageMetrics(
            trades=100,
            days_active=30.0,
            sharpe=1.02,
            max_dd_pct=1.0,
            win_rate=0.6,
            mean_slippage_bps=1.0,
        )
        spec, decision = self._shadow_promote(m)
        verdict = default_red_team_gate(spec, decision)
        assert verdict.approve is False
        assert any("sharpe=" in r for r in verdict.reasons)
        assert verdict.risk_score > 0

    def test_vetoes_tight_drawdown_clearance(self) -> None:
        """max_dd 4.8 clears 5.0 but fails 0.9 * 5.0 = 4.5 ceiling."""
        m = StageMetrics(
            trades=100,
            days_active=30.0,
            sharpe=2.0,
            max_dd_pct=4.8,
            win_rate=0.6,
            mean_slippage_bps=1.0,
        )
        spec, decision = self._shadow_promote(m)
        verdict = default_red_team_gate(spec, decision)
        assert verdict.approve is False
        assert any("max_dd_pct=" in r for r in verdict.reasons)

    def test_vetoes_tight_win_rate(self) -> None:
        m = StageMetrics(
            trades=100,
            days_active=30.0,
            sharpe=2.0,
            max_dd_pct=1.0,
            win_rate=0.455,
            mean_slippage_bps=1.0,
        )
        spec, decision = self._shadow_promote(m)
        verdict = default_red_team_gate(spec, decision)
        assert verdict.approve is False
        assert any("win_rate=" in r for r in verdict.reasons)

    def test_vetoes_undersampled_trades(self) -> None:
        """trades=55 clears min=50 but not 1.3 * 50 = 65 safety floor."""
        m = StageMetrics(
            trades=55,
            days_active=30.0,
            sharpe=2.0,
            max_dd_pct=1.0,
            win_rate=0.6,
            mean_slippage_bps=1.0,
        )
        spec, decision = self._shadow_promote(m)
        verdict = default_red_team_gate(spec, decision)
        assert verdict.approve is False
        assert any("trades=" in r for r in verdict.reasons)

    def test_slippage_floor_only_fires_on_paper_to_live(self) -> None:
        """SHADOW->PAPER tolerates low slippage; PAPER->LIVE flags it."""
        from datetime import UTC, datetime

        # SHADOW->PAPER: zero slippage still approved.
        m = StageMetrics(
            trades=100,
            days_active=30.0,
            sharpe=2.0,
            max_dd_pct=1.0,
            win_rate=0.6,
            mean_slippage_bps=0.0,
        )
        spec, decision = self._shadow_promote(m)
        verdict = default_red_team_gate(spec, decision)
        assert verdict.approve is True

        # PAPER->LIVE_1LOT: zero slippage triggers a veto.
        m2 = StageMetrics(
            trades=200,
            days_active=30.0,
            sharpe=2.0,
            max_dd_pct=1.0,
            win_rate=0.7,
            mean_slippage_bps=0.0,
        )
        spec2 = PromotionSpec(
            strategy_id="s2",
            current_stage=PromotionStage.PAPER,
            entered_stage_at=datetime.now(UTC),
            metrics=m2,
        )
        d2 = PromotionDecision(
            strategy_id="s2",
            from_stage=PromotionStage.PAPER,
            to_stage=PromotionStage.LIVE_1LOT,
            action=PromotionAction.PROMOTE,
            reasons=["ok"],
            metrics=m2,
        )
        verdict2 = default_red_team_gate(spec2, d2)
        assert verdict2.approve is False
        assert any("slippage" in r.lower() for r in verdict2.reasons)

    def test_risk_score_scales_with_failures(self) -> None:
        """Each failed check adds 1/5 to risk_score."""
        # Fail every fragility check: all tight.
        m = StageMetrics(
            trades=55,
            days_active=30.0,
            sharpe=1.02,
            max_dd_pct=4.8,
            win_rate=0.455,
            mean_slippage_bps=1.0,
        )
        spec, decision = self._shadow_promote(m)
        verdict = default_red_team_gate(spec, decision)
        assert verdict.approve is False
        # 4 of 5 checks can fire on SHADOW->PAPER (slippage check is skipped).
        assert verdict.risk_score >= 0.6
        assert verdict.risk_score <= 1.0

    def test_never_raises_on_unknown_stage_thresholds(self) -> None:
        """If thresholds lookup yields None, default to approve."""
        from datetime import UTC, datetime

        m = StageMetrics(trades=0, days_active=0.0, sharpe=0.0)
        spec = PromotionSpec(
            strategy_id="unknown",
            current_stage=PromotionStage.RETIRED,
            entered_stage_at=datetime.now(UTC),
            metrics=m,
        )
        decision = PromotionDecision(
            strategy_id="unknown",
            from_stage=PromotionStage.RETIRED,
            to_stage=PromotionStage.SHADOW,
            action=PromotionAction.PROMOTE,
            reasons=[],
            metrics=m,
        )
        verdict = default_red_team_gate(
            spec,
            decision,
            thresholds={},  # empty -> thresholds.get() returns None
        )
        assert verdict.approve is True

    # --- wired-in-default integration --------------------------------

    def test_gate_default_is_real_red_team(self, tmp_path: Path) -> None:
        """PromotionGate() with no kwargs uses default_red_team_gate.

        A strategy that cleared thresholds tight (trades=50 equals
        min_trades, not 1.3x) now gets HELD instead of PROMOTEd.
        """
        gate = PromotionGate(
            state_path=tmp_path / "promotion.json",
            journal_path=tmp_path / "promotion.jsonl",
        )
        gate.register("tight")
        # Clears SHADOW thresholds exactly, no margin.
        gate.update_metrics(
            "tight",
            StageMetrics(
                trades=50,
                days_active=14.0,
                sharpe=1.0,
                max_dd_pct=5.0,
                win_rate=0.45,
                mean_slippage_bps=1.0,
            ),
        )
        decision = gate.evaluate("tight")
        # Would have been PROMOTE; default red-team vetoes to HOLD.
        assert decision.action is PromotionAction.HOLD
        assert any("red_team_blocked" in r for r in decision.reasons)

    def test_explicit_none_disables_default(self, tmp_path: Path) -> None:
        """red_team_gate=None preserves the old approve-all behaviour."""
        gate = PromotionGate(
            state_path=tmp_path / "promotion.json",
            journal_path=tmp_path / "promotion.jsonl",
            red_team_gate=None,
        )
        gate.register("tight2")
        gate.update_metrics(
            "tight2",
            StageMetrics(
                trades=50,
                days_active=14.0,
                sharpe=1.0,
                max_dd_pct=5.0,
                win_rate=0.45,
                mean_slippage_bps=1.0,
            ),
        )
        decision = gate.evaluate("tight2")
        assert decision.action is PromotionAction.PROMOTE


# ---------------------------------------------------------------------------
# PushBus time-based deduplication (brain.avengers.push.PushBus)
# ---------------------------------------------------------------------------


class TestPushBusDedup:
    """PushBus suppresses repeat (level, title, source) tuples within a window."""

    class _Counter:
        """Notifier stub that counts send() calls."""

        def __init__(self) -> None:
            self.calls = 0

        def send(self, _alert) -> bool:  # noqa: ANN001
            self.calls += 1
            return True

    def test_dedup_suppresses_repeat_titles(self) -> None:
        from eta_engine.brain.avengers import AlertLevel, PushBus

        remote = self._Counter()
        bus = PushBus([remote], dedup_window_seconds=600.0)
        bus.push(AlertLevel.WARN, "task_x failed", "boom")
        bus.push(AlertLevel.WARN, "task_x failed", "boom again")
        bus.push(AlertLevel.WARN, "task_x failed", "boom 3")
        assert remote.calls == 1  # only first fires

    def test_dedup_different_titles_all_fire(self) -> None:
        from eta_engine.brain.avengers import AlertLevel, PushBus

        remote = self._Counter()
        bus = PushBus([remote], dedup_window_seconds=600.0)
        bus.push(AlertLevel.WARN, "task_a failed", "...")
        bus.push(AlertLevel.WARN, "task_b failed", "...")
        bus.push(AlertLevel.WARN, "task_c failed", "...")
        assert remote.calls == 3  # all distinct

    def test_critical_always_fires(self) -> None:
        """CRITICAL breaks through dedup -- kill-switch must never be silent."""
        from eta_engine.brain.avengers import AlertLevel, PushBus

        remote = self._Counter()
        bus = PushBus([remote], dedup_window_seconds=600.0)
        bus.push(AlertLevel.CRITICAL, "breaker tripped", "")
        bus.push(AlertLevel.CRITICAL, "breaker tripped", "")
        bus.push(AlertLevel.CRITICAL, "breaker tripped", "")
        assert remote.calls == 3

    def test_dedup_window_zero_disables(self) -> None:
        from eta_engine.brain.avengers import AlertLevel, PushBus

        remote = self._Counter()
        bus = PushBus([remote], dedup_window_seconds=0.0)
        for _ in range(5):
            bus.push(AlertLevel.WARN, "same title", "")
        assert remote.calls == 5

    def test_local_file_notifier_always_writes_even_on_dup(
        self,
        tmp_path: Path,
    ) -> None:
        """Local audit trail must be complete even under heavy dedup."""
        from eta_engine.brain.avengers import (
            AlertLevel,
            LocalFileNotifier,
            PushBus,
        )

        local_path = tmp_path / "alerts.jsonl"
        local = LocalFileNotifier(path=local_path)
        remote = self._Counter()
        bus = PushBus([local, remote], dedup_window_seconds=600.0)
        for _ in range(5):
            bus.push(AlertLevel.WARN, "dup title", "msg")
        # Remote throttled to 1.
        assert remote.calls == 1
        # Local audit captured all 5.
        lines = local_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5


# ---------------------------------------------------------------------------
# run_task observability (deploy.scripts.run_task.main + _task_chaos_drill)
# ---------------------------------------------------------------------------


class _CaptureNotifier:
    """Notifier stub that records every alert (level, title, source)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, alert) -> bool:  # noqa: ANN001
        self.sent.append((alert.level.value, alert.title, alert.source))
        return True


@pytest.fixture()
def capture_bus():
    """Install a capture-only PushBus on the default_bus module cache.

    Yields the capture notifier. Teardown restores ``_default_bus=None``
    so later tests rebuild the bus from env vars via ``default_bus()``.

    NB: ``eta_engine.brain.avengers.push`` resolves to the re-exported
    ``push`` function (not the submodule) because of the package-level
    import-as-attribute shadow. ``importlib.import_module`` sidesteps
    that and hands back the real submodule.
    """
    import importlib

    from eta_engine.brain.avengers import PushBus

    push_mod = importlib.import_module("eta_engine.brain.avengers.push")

    capture = _CaptureNotifier()
    bus = PushBus([capture], dedup_window_seconds=0.0)
    push_mod.set_default_bus(bus)
    try:
        yield capture
    finally:
        push_mod.set_default_bus(None)


class TestRunTaskAlertWiring:
    """Failed tasks + failed chaos drills push via the PushBus."""

    def test_main_pushes_on_handler_exception(
        self,
        tmp_path: Path,
        monkeypatch,
        capture_bus,
    ) -> None:
        capture = capture_bus
        from eta_engine.deploy.scripts import run_task

        def boom(_s, _l) -> dict:
            msg = "synthetic explosion"
            raise RuntimeError(msg)

        monkeypatch.setitem(
            run_task.HANDLERS,
            BackgroundTask.KAIZEN_RETRO,
            boom,
        )
        rc = run_task.main(
            [
                "KAIZEN_RETRO",
                "--state-dir",
                str(tmp_path / "state"),
                "--log-dir",
                str(tmp_path / "log"),
            ]
        )
        assert rc == 2  # failure exit code
        # Push fired exactly once with WARN + task-namespaced source.
        assert len(capture.sent) == 1
        level, title, source = capture.sent[0]
        assert level == "WARN"
        assert "KAIZEN_RETRO" in title
        assert source == "run_task:KAIZEN_RETRO"

    def test_chaos_drill_pushes_on_failure(
        self,
        tmp_path: Path,
        monkeypatch,
        capture_bus,
    ) -> None:
        """If any drill fails, _task_chaos_drill pushes CRITICAL."""
        capture = capture_bus
        from eta_engine.deploy.scripts.run_task import _task_chaos_drill

        # Synthesize a failing drill result set by monkeypatching run_drills.
        fake_results = [
            {"name": "breaker_isolation", "passed": True, "detail": "ok"},
            {"name": "deadman_trigger", "passed": False, "detail": "stuck"},
            {"name": "daemon_restart", "passed": True, "detail": "ok"},
            {"name": "drift_autodemote", "passed": False, "detail": "timeout"},
        ]
        monkeypatch.setattr(
            "eta_engine.scripts.chaos_drill.run_drills",
            lambda: fake_results,
        )
        out = _task_chaos_drill(tmp_path)
        assert out["failed"] == 2
        # One CRITICAL alert naming both failing drills.
        assert len(capture.sent) == 1
        level, title, source = capture.sent[0]
        assert level == "CRITICAL"
        assert "2/4 FAILED" in title
        assert source == "chaos_drill"

    def test_chaos_drill_no_push_on_all_pass(
        self,
        tmp_path: Path,
        monkeypatch,
        capture_bus,
    ) -> None:
        capture = capture_bus
        from eta_engine.deploy.scripts.run_task import _task_chaos_drill

        monkeypatch.setattr(
            "eta_engine.scripts.chaos_drill.run_drills",
            lambda: [{"name": f"d{i}", "passed": True} for i in range(4)],
        )
        out = _task_chaos_drill(tmp_path)
        assert out["failed"] == 0
        assert capture.sent == []

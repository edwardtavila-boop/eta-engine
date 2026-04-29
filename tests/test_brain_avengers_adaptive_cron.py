from __future__ import annotations

from eta_engine.brain.avengers.adaptive_cron import RegimeGate, RegimeTag
from eta_engine.brain.avengers.dispatch import BackgroundTask


def test_regime_gate_skips_sparse_tasks_in_calm_regime_by_ratio() -> None:
    gate = RegimeGate(regime_getter=lambda: RegimeTag.CALM, calm_skip_ratio=3)

    decisions = [gate.should_fire(BackgroundTask.DRIFT_SUMMARY) for _ in range(4)]

    assert [d.fire for d in decisions] == [True, False, False, True]
    assert [d.call_idx for d in decisions] == [0, 1, 2, 3]
    assert all(d.reason == "calm regime, 1-in-3 schedule" for d in decisions)


def test_regime_gate_fires_safety_tasks_and_stressed_regime_tasks() -> None:
    calm_gate = RegimeGate(regime_getter=lambda: RegimeTag.CALM)
    stressed_gate = RegimeGate(regime_getter=lambda: RegimeTag.STRESSED)

    always = calm_gate.should_fire(BackgroundTask.KAIZEN_RETRO)
    stressed = stressed_gate.should_fire(BackgroundTask.LOG_COMPACT)

    assert always.fire is True
    assert always.reason == "fire-always task"
    assert stressed.fire is True
    assert stressed.reason == "fire on STRESSED"


def test_regime_gate_defaults_to_normal_and_reset_clears_counters() -> None:
    gate = RegimeGate(calm_skip_ratio=0)

    normal = gate.should_fire(BackgroundTask.DASHBOARD_ASSEMBLE)
    gate.reset()
    after_reset = gate.should_fire(BackgroundTask.DASHBOARD_ASSEMBLE)

    assert gate.calm_skip_ratio == 1
    assert normal.fire is True
    assert normal.reason == "normal regime"
    assert after_reset.call_idx == 0

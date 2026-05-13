from __future__ import annotations

from eta_engine.brain.avengers.base import DryRunExecutor, PersonaId, make_envelope
from eta_engine.brain.avengers.fleet import Fleet
from eta_engine.brain.model_policy import TaskCategory


def test_fleet_routes_categories_to_locked_personas(tmp_path) -> None:
    fleet = Fleet(executor=DryRunExecutor(), journal_path=tmp_path / "avengers.jsonl")

    batman = fleet.dispatch(make_envelope(category=TaskCategory.RED_TEAM_SCORING, goal="red-team promotion candidate"))
    alfred = fleet.dispatch(make_envelope(category=TaskCategory.TEST_RUN, goal="write a focused pytest"))
    robin = fleet.dispatch(make_envelope(category=TaskCategory.LOG_PARSING, goal="summarize logs"))

    assert batman.persona_id is PersonaId.BATMAN
    assert alfred.persona_id is PersonaId.ALFRED
    assert robin.persona_id is PersonaId.ROBIN
    assert all(result.success for result in [batman, alfred, robin])


def test_fleet_metrics_roll_up_calls_failures_and_cost(tmp_path) -> None:
    fleet = Fleet(executor=DryRunExecutor(), journal_path=tmp_path / "avengers.jsonl")

    result = fleet.dispatch(make_envelope(category=TaskCategory.FORMATTING, goal="fix imports"))
    metrics = fleet.metrics()

    assert result.persona_id is PersonaId.ROBIN
    assert metrics.total_calls == 1
    assert metrics.total_cost == 0.2
    assert metrics.calls_by_persona == {PersonaId.ROBIN.value: 1}
    assert metrics.failures_by_persona == {}
    assert metrics.last_call_ts is not None


def test_fleet_pool_records_each_requested_persona(tmp_path) -> None:
    fleet = Fleet(executor=DryRunExecutor(), journal_path=tmp_path / "avengers.jsonl")
    envelope = make_envelope(category=TaskCategory.TEST_RUN, goal="write regression tests")

    results = fleet.pool(envelope, personas=[PersonaId.ALFRED, PersonaId.ROBIN])

    assert [result.persona_id for result in results] == [PersonaId.ALFRED, PersonaId.ROBIN]
    assert results[0].success is True
    assert results[1].reason_code == "tier_mismatch"
    metrics = fleet.metrics()
    assert metrics.calls_by_persona == {
        PersonaId.ALFRED.value: 1,
        PersonaId.ROBIN.value: 1,
    }
    assert metrics.failures_by_persona == {PersonaId.ROBIN.value: 1}

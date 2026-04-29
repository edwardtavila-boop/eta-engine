from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.brain.jarvis_v3.budget import BudgetTracker
from eta_engine.brain.model_policy import ModelTier, TaskCategory


def test_budget_tracker_records_spend_and_downshifts_opus() -> None:
    now = datetime(2026, 4, 29, tzinfo=UTC)
    tracker = BudgetTracker(hourly_budget=10, daily_budget=100)
    tracker.record(ModelTier.OPUS, TaskCategory.DEBUG, now=now)
    tracker.record(ModelTier.OPUS, TaskCategory.DEBUG, now=now + timedelta(minutes=1))

    status = tracker.status(now=now + timedelta(minutes=2))
    routed, reason = tracker.routed_tier(
        ModelTier.OPUS,
        TaskCategory.DEBUG,
        now=now + timedelta(minutes=2),
    )

    assert status.hourly_spend == 10.0
    assert status.tier_state == "CRITICAL"
    assert routed is ModelTier.SONNET
    assert "budget downshift" in reason


def test_budget_tracker_never_downshifts_pinned_architectural_category() -> None:
    now = datetime(2026, 4, 29, tzinfo=UTC)
    tracker = BudgetTracker(hourly_budget=5, daily_budget=100)
    tracker.record(ModelTier.OPUS, TaskCategory.DEBUG, now=now)

    routed, reason = tracker.routed_tier(
        ModelTier.OPUS,
        TaskCategory.RED_TEAM_SCORING,
        now=now,
    )

    assert routed is ModelTier.OPUS
    assert "pinned" in reason


def test_budget_tracker_save_load_round_trip(tmp_path) -> None:
    now = datetime(2026, 4, 29, tzinfo=UTC)
    path = tmp_path / "budget.json"
    tracker = BudgetTracker(hourly_budget=20, daily_budget=200)
    tracker.record(ModelTier.HAIKU, TaskCategory.LOG_PARSING, now=now)
    tracker.save(path)

    loaded = BudgetTracker.load(path)

    assert loaded.hourly_budget == 20
    assert loaded.daily_budget == 200
    assert loaded.status(now=now).daily_spend == 0.2

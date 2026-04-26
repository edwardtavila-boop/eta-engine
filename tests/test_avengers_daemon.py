"""
Tests for the 24/7 Avenger daemon supervisor
(``apex_predator.brain.avengers.daemon``).

Covers:
  * ``is_due`` cron matcher edge-cases (*, */N, exact, range, weekday)
  * ``envelope_for_task`` -> correct TaskCategory -> correct tier -> persona
  * ``AvengerDaemon.due_tasks`` filters by owner + cron + dedupe-per-minute
  * ``AvengerDaemon.tick`` produces a heartbeat, dispatches due tasks,
    and appends BOTH the dispatch journal line AND the heartbeat line.
  * ``AvengerDaemon.run_forever`` honors ``max_ticks``, writes PID file,
    survives an exception in the tick loop without exiting.
  * JARVIS daemon has no BackgroundTask lane -- tick only heartbeats
    and ``_admin_note`` reports sibling PID state.
  * ``run_daemon_cli`` rejects unknown persona names.
"""
from __future__ import annotations

import json
import types
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from apex_predator.brain.avengers import (
    TASK_CADENCE,
    TASK_OWNERS,
    VALID_PERSONAS,
    AvengerDaemon,
    BackgroundTask,
    DaemonHeartbeat,
    Fleet,
    envelope_for_task,
    is_due,
    run_daemon_cli,
)
from apex_predator.brain.avengers.daemon import _pid_path
from apex_predator.brain.model_policy import ModelTier, tier_for

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_journal(tmp_path: Path) -> Path:
    return tmp_path / "avengers.jsonl"


@pytest.fixture
def isolated_pid_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force ~/.jarvis/ to point at a tmp dir so PID files don't leak."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path / ".jarvis"


def _fleet_with_journal(path: Path) -> Fleet:
    return Fleet(journal_path=path)


def _fixed_clock(dt: datetime):
    def _clk() -> datetime:
        return dt
    return _clk


# ---------------------------------------------------------------------------
# Cron matcher
# ---------------------------------------------------------------------------


class TestIsDue:
    def test_star_matches_everything(self):
        now = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        assert is_due("* * * * *", now) is True

    def test_exact_minute(self):
        now = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        assert is_due("30 * * * *", now) is True
        assert is_due("29 * * * *", now) is False

    def test_star_slash_n(self):
        # */5 matches 0,5,10,15,...55
        fires  = datetime(2026, 4, 23, 14, 15, tzinfo=UTC)
        misses = datetime(2026, 4, 23, 14, 16, tzinfo=UTC)
        assert is_due("*/5 * * * *", fires) is True
        assert is_due("*/5 * * * *", misses) is False

    def test_comma_and_range(self):
        now = datetime(2026, 4, 23, 14, 25, tzinfo=UTC)
        assert is_due("25,55 * * * *", now) is True
        assert is_due("20-30 * * * *", now) is True
        assert is_due("0-10 * * * *", now) is False

    def test_weekday_range(self):
        # 2026-04-23 is a Thursday (py weekday=3, cron=4).
        thu = datetime(2026, 4, 23, 14, 25, tzinfo=UTC)
        sat = datetime(2026, 4, 25, 14, 25, tzinfo=UTC)
        # cron "1-5" = Mon-Fri (cron Mon=1..Fri=5)
        assert is_due("25 * * * 1-5", thu) is True
        assert is_due("25 * * * 1-5", sat) is False

    def test_malformed_expression_returns_false(self):
        now = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        assert is_due("not even close", now) is False
        assert is_due("* * *", now) is False  # too few fields

    def test_every_cadence_in_dispatch_is_parseable(self):
        """Sanity: every cron string we ship parses without error."""
        now = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        for expr in TASK_CADENCE.values():
            # Must return a bool, never raise
            result = is_due(expr, now)
            assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# envelope_for_task
# ---------------------------------------------------------------------------


class TestEnvelopeForTask:
    @pytest.mark.parametrize("task", list(BackgroundTask))
    def test_category_resolves_to_correct_tier(self, task: BackgroundTask):
        """Every BackgroundTask -> TaskCategory -> ModelTier that matches
        the task's owner persona's locked tier.
        """
        env = envelope_for_task(task)
        owner = TASK_OWNERS[task]
        category_tier = tier_for(env.category)
        expected_tier = {
            "BATMAN": ModelTier.OPUS,
            "ALFRED": ModelTier.SONNET,
            "ROBIN":  ModelTier.HAIKU,
        }[owner]
        assert category_tier == expected_tier, (
            f"{task.value} -> {env.category.value} -> {category_tier} "
            f"but owner {owner} expects {expected_tier}"
        )

    def test_envelope_includes_task_id_in_context(self):
        env = envelope_for_task(BackgroundTask.KAIZEN_RETRO)
        assert env.context.get("background_task") == "KAIZEN_RETRO"

    def test_rationale_mentions_scheduler(self):
        env = envelope_for_task(BackgroundTask.LOG_COMPACT)
        assert "scheduled" in env.rationale
        assert "LOG_COMPACT" in env.rationale


# ---------------------------------------------------------------------------
# Daemon: due_tasks + dedupe
# ---------------------------------------------------------------------------


class TestDueTasks:
    def test_jarvis_daemon_sees_no_tasks(self, tmp_journal: Path):
        # JARVIS owns no BackgroundTasks; due_tasks for him should always
        # be empty regardless of cron match.
        d = AvengerDaemon(
            persona="JARVIS",
            fleet=_fleet_with_journal(tmp_journal),
            journal_path=tmp_journal,
            sleep_fn=lambda _s: None,
        )
        now = datetime(2026, 4, 23, 23, 0, tzinfo=UTC)  # matches KAIZEN_RETRO
        assert d.due_tasks(now) == []

    def test_persona_sees_only_owned_tasks(self, tmp_journal: Path):
        d = AvengerDaemon(
            persona="ALFRED",
            fleet=_fleet_with_journal(tmp_journal),
            journal_path=tmp_journal,
            sleep_fn=lambda _s: None,
        )
        # 23:00 daily matches KAIZEN_RETRO (ALFRED) but NOT TWIN_VERDICT
        # (BATMAN, 22:00). 23:00 also matches hourly LOG_COMPACT (ROBIN,
        # "0 * * * *") but that's owned by Robin so Alfred doesn't see it.
        now = datetime(2026, 4, 23, 23, 0, tzinfo=UTC)
        due = d.due_tasks(now)
        assert BackgroundTask.KAIZEN_RETRO in due
        assert BackgroundTask.TWIN_VERDICT not in due
        assert BackgroundTask.LOG_COMPACT not in due

    def test_same_minute_dedupe(self, tmp_journal: Path):
        d = AvengerDaemon(
            persona="ROBIN",
            fleet=_fleet_with_journal(tmp_journal),
            journal_path=tmp_journal,
            sleep_fn=lambda _s: None,
        )
        # "* * * * *" fires every minute -> DASHBOARD_ASSEMBLE is ROBIN's
        now = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        first = d.due_tasks(now)
        assert BackgroundTask.DASHBOARD_ASSEMBLE in first
        # Second call within the same minute returns empty for the dedupe
        second = d.due_tasks(now)
        assert BackgroundTask.DASHBOARD_ASSEMBLE not in second
        # Advance one minute -> it fires again
        later = datetime(2026, 4, 23, 14, 31, tzinfo=UTC)
        third = d.due_tasks(later)
        assert BackgroundTask.DASHBOARD_ASSEMBLE in third


# ---------------------------------------------------------------------------
# Daemon: tick + heartbeat
# ---------------------------------------------------------------------------


class TestTick:
    def test_jarvis_tick_emits_heartbeat_only(
        self,
        tmp_journal: Path,
        isolated_pid_dir: Path,  # noqa: ARG002 -- just for isolation
    ):
        now = datetime(2026, 4, 23, 23, 0, tzinfo=UTC)
        d = AvengerDaemon(
            persona="JARVIS",
            fleet=_fleet_with_journal(tmp_journal),
            clock=_fixed_clock(now),
            sleep_fn=lambda _s: None,
            journal_path=tmp_journal,
        )
        hb = d.tick()
        assert isinstance(hb, DaemonHeartbeat)
        assert hb.persona == "JARVIS"
        assert hb.tasks_due == []
        assert hb.tasks_ok == 0
        assert hb.tasks_failed == 0
        # Note contains sibling PID state (all OFFLINE in isolation)
        assert "BATMAN=" in hb.note
        assert "ALFRED=" in hb.note
        assert "ROBIN=" in hb.note
        # Journal has exactly one heartbeat line
        lines = tmp_journal.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["kind"] == "heartbeat"
        assert rec["persona"] == "persona.jarvis"

    def test_persona_tick_dispatches_due_tasks(
        self,
        tmp_journal: Path,
        isolated_pid_dir: Path,  # noqa: ARG002
    ):
        # 23:00 daily -> KAIZEN_RETRO (ALFRED) fires.
        #
        # Every BackgroundTask now has a local handler in
        # deploy/scripts/run_task.py HANDLERS, so a due task is
        # executed locally and tasks_ok goes up without the fleet
        # dispatcher being invoked. The fleet-call-count assertion
        # would only fire if the task had no local handler; that's
        # now covered by test_local_handler_task_bypasses_fleet_dispatch
        # (which asserts calls_by_persona stays empty on local
        # tasks). Here we just verify the tick wires the task through
        # and tasks_ok reflects the success.
        now = datetime(2026, 4, 23, 23, 0, tzinfo=UTC)
        fleet = _fleet_with_journal(tmp_journal)
        d = AvengerDaemon(
            persona="ALFRED",
            fleet=fleet,
            clock=_fixed_clock(now),
            sleep_fn=lambda _s: None,
            journal_path=tmp_journal,
        )
        hb = d.tick()
        assert BackgroundTask.KAIZEN_RETRO.value in hb.tasks_due
        # tasks_ok + tasks_failed == len(tasks_due). DryRunExecutor
        # + local handler both always succeed on the dry-run path.
        assert hb.tasks_ok >= 1

    def test_tick_index_monotonic(
        self,
        tmp_journal: Path,
        isolated_pid_dir: Path,  # noqa: ARG002
    ):
        now = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        d = AvengerDaemon(
            persona="JARVIS",
            fleet=_fleet_with_journal(tmp_journal),
            clock=_fixed_clock(now),
            sleep_fn=lambda _s: None,
            journal_path=tmp_journal,
        )
        hb1 = d.tick()
        hb2 = d.tick()
        hb3 = d.tick()
        assert (hb1.tick_index, hb2.tick_index, hb3.tick_index) == (0, 1, 2)

    @pytest.mark.parametrize(
        ("persona", "task"),
        [
            ("ROBIN", BackgroundTask.DASHBOARD_ASSEMBLE),
            ("ALFRED", BackgroundTask.SHADOW_TICK),
            ("BATMAN", BackgroundTask.STRATEGY_MINE),
        ],
    )
    def test_local_handler_task_bypasses_fleet_dispatch(
        self,
        persona: str,
        task: BackgroundTask,
        tmp_journal: Path,
        isolated_pid_dir: Path,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ):
        import apex_predator.brain.avengers.daemon as dm

        now = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        fleet = _fleet_with_journal(tmp_journal)

        def _boom(_envelope):
            raise AssertionError("fleet dispatch should not run for local handler tasks")

        monkeypatch.setattr(fleet, "dispatch", _boom)
        monkeypatch.setattr(
            dm,
            "_run_local_background_task",
            lambda active_task: {"written": f"{active_task.value.lower()}.json"}
            if active_task is task
            else None,
        )

        d = AvengerDaemon(
            persona=persona,
            fleet=fleet,
            clock=_fixed_clock(now),
            sleep_fn=lambda _s: None,
            journal_path=tmp_journal,
        )
        monkeypatch.setattr(d, "due_tasks", lambda _now: [task])
        hb = d.tick()
        assert task.value in hb.tasks_due
        assert hb.tasks_ok >= 1
        lines = tmp_journal.read_text(encoding="utf-8").splitlines()
        assert any('"provider": "local_handler"' in line for line in lines)

    def test_prompt_warmup_local_handler_preserves_estimated_spend(
        self,
        tmp_journal: Path,
        isolated_pid_dir: Path,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ):
        import apex_predator.brain.avengers.daemon as dm

        fleet = _fleet_with_journal(tmp_journal)
        monkeypatch.setattr(
            fleet,
            "dispatch",
            lambda _envelope: (_ for _ in ()).throw(
                AssertionError("fleet dispatch should not run for prompt warmup"),
            ),
        )
        monkeypatch.setattr(
            dm,
            "_run_local_background_task",
            lambda task: {"warmed": 3, "failed": 0, "est_cost_usd": 0.0123}
            if task is BackgroundTask.PROMPT_WARMUP
            else None,
        )

        d = AvengerDaemon(
            persona="ROBIN",
            fleet=fleet,
            clock=_fixed_clock(datetime(2026, 4, 23, 14, 30, tzinfo=UTC)),
            sleep_fn=lambda _s: None,
            journal_path=tmp_journal,
        )
        monkeypatch.setattr(d, "due_tasks", lambda _now: [BackgroundTask.PROMPT_WARMUP])
        hb = d.tick()
        assert BackgroundTask.PROMPT_WARMUP.value in hb.tasks_due
        rec = json.loads(tmp_journal.read_text(encoding="utf-8").splitlines()[-2])
        result = rec["result"]
        assert result["provider"] == "local_handler"
        assert result["billing_mode"] == "anthropic_api"
        assert result["billable_usd"] == 0.0123


# ---------------------------------------------------------------------------
# Daemon: run_forever
# ---------------------------------------------------------------------------


class TestRunForever:
    def test_honors_max_ticks(
        self,
        tmp_journal: Path,
        isolated_pid_dir: Path,  # noqa: ARG002
    ):
        now = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        d = AvengerDaemon(
            persona="JARVIS",
            fleet=_fleet_with_journal(tmp_journal),
            clock=_fixed_clock(now),
            sleep_fn=lambda _s: None,
            journal_path=tmp_journal,
        )
        ticks = d.run_forever(max_ticks=3)
        assert ticks == 3
        # 3 heartbeats appended
        lines = tmp_journal.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3

    def test_pid_file_lifecycle(
        self,
        tmp_journal: Path,
        isolated_pid_dir: Path,
    ):
        now = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        d = AvengerDaemon(
            persona="BATMAN",
            fleet=_fleet_with_journal(tmp_journal),
            clock=_fixed_clock(now),
            sleep_fn=lambda _s: None,
            journal_path=tmp_journal,
        )
        d.run_forever(max_ticks=1)
        # After run_forever returns, the PID file is cleaned up.
        pid_path = _pid_path("BATMAN")
        assert not pid_path.exists()

    def test_crash_in_tick_is_swallowed(
        self,
        tmp_journal: Path,
        isolated_pid_dir: Path,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A raising tick() must not kill the daemon loop."""
        now = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        d = AvengerDaemon(
            persona="JARVIS",
            fleet=_fleet_with_journal(tmp_journal),
            clock=_fixed_clock(now),
            sleep_fn=lambda _s: None,
            journal_path=tmp_journal,
        )
        crash_count = {"n": 0}
        original_tick = d.tick

        def _flaky_tick() -> DaemonHeartbeat:
            crash_count["n"] += 1
            if crash_count["n"] == 1:
                msg = "synthetic crash"
                raise RuntimeError(msg)
            return original_tick()

        monkeypatch.setattr(d, "tick", _flaky_tick)
        ticks = d.run_forever(max_ticks=3)
        # Even though tick #1 raised, we still ran 3 iterations.
        assert ticks == 3
        assert crash_count["n"] == 3


# ---------------------------------------------------------------------------
# run_daemon_cli
# ---------------------------------------------------------------------------


class TestRunDaemonCli:
    def test_rejects_unknown_persona(self):
        with pytest.raises(ValueError, match="unknown persona"):
            run_daemon_cli("Thanos", tick_seconds=0.01, max_ticks=1)

    def test_all_valid_personas_enumerated(self):
        # Insurance against a rename somewhere in the codebase.
        assert frozenset({
            "JARVIS", "BATMAN", "ALFRED", "ROBIN",
        }) == VALID_PERSONAS

    def test_runs_and_exits_cleanly(
        self,
        tmp_path: Path,
        isolated_pid_dir: Path,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Point the default journal to tmp for this test so we don't
        # write into the real ~/.jarvis/avengers.jsonl.
        journal = tmp_path / "avengers.jsonl"
        import apex_predator.brain.avengers.daemon as dm
        monkeypatch.setattr(dm, "AVENGERS_JOURNAL", journal)
        ticks = run_daemon_cli(
            "ROBIN",
            fleet=_fleet_with_journal(journal),
            tick_seconds=1.0,
            max_ticks=2,
        )
        assert ticks == 2

    def test_uses_default_fleet_when_none_supplied(
        self,
        tmp_path: Path,
        isolated_pid_dir: Path,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ):
        journal = tmp_path / "avengers.jsonl"
        import apex_predator.brain.avengers.daemon as dm

        fallback_fleet = _fleet_with_journal(journal)
        monkeypatch.setattr(dm, "AVENGERS_JOURNAL", journal)
        monkeypatch.setattr(dm, "_default_fleet", lambda: fallback_fleet)

        ticks = run_daemon_cli(
            "JARVIS",
            tick_seconds=1.0,
            max_ticks=1,
        )
        assert ticks == 1


class TestAnthropicClientFallback:
    def test_build_http_client_retries_without_http2(self, monkeypatch: pytest.MonkeyPatch):
        import apex_predator.brain.avengers.daemon as dm

        class FakeClient:
            def __init__(self, *, http2: bool, **_kwargs) -> None:
                self.http2 = http2

        def _client_factory(*, http2: bool, **kwargs):
            if http2:
                raise RuntimeError("missing h2 support")
            return FakeClient(http2=http2, **kwargs)

        fake_httpx = types.SimpleNamespace(
            Client=_client_factory,
            Limits=lambda **kwargs: ("limits", kwargs),
            Timeout=lambda *args, **kwargs: ("timeout", args, kwargs),
        )

        client = dm._build_anthropic_http_client(fake_httpx)
        assert isinstance(client, FakeClient)
        assert client.http2 is False

"""
Tests for the Avengers fleet (``eta_engine.brain.avengers``).

Covers:
  * TaskEnvelope / TaskResult pydantic round-trip
  * Persona tier-lock guardrails (BATMAN=Opus, ALFRED=Sonnet, ROBIN=Haiku)
  * Injected Executor invocation + DryRunExecutor default
  * JARVIS pre-flight via ActionType.LLM_INVOCATION
  * JSONL audit journal append
  * Fleet routing by category and by requested_tier override
  * Fleet.pool multi-persona broadcast
  * Fleet metrics accumulation
  * Streamlit console module imports without Streamlit runtime
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eta_engine.brain.avengers import (
    AVENGERS_JOURNAL,
    COST_RATIO,
    Alfred,
    Batman,
    DryRunExecutor,
    Fleet,
    FleetMetrics,
    Persona,
    PersonaId,
    Robin,
    TaskBucket,
    TaskCategory,
    TaskEnvelope,
    TaskResult,
    describe_persona,
    make_envelope,
)
from eta_engine.brain.avengers.base import (
    PERSONA_BUCKET,
    PERSONA_TIER,
    append_journal,
)
from eta_engine.brain.jarvis_admin import (
    ActionType,
    SubsystemId,
    Verdict,
)
from eta_engine.brain.model_policy import ModelTier, tier_for

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_journal(tmp_path: Path) -> Path:
    return tmp_path / "avengers.jsonl"


class _RecordingExecutor:
    """Executor double that echoes what it received and tracks invocations.

    Tests use this instead of DryRunExecutor when they need to assert on
    the prompts the persona built.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        tier: ModelTier,
        system_prompt: str,
        user_prompt: str,
        envelope: TaskEnvelope,
    ) -> str:
        self.calls.append(
            {
                "tier": tier,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "envelope": envelope,
            },
        )
        return f"ARTIFACT({tier.value})::{envelope.goal}"


class _BoomExecutor:
    """Executor that always raises -- for the error-path test."""

    def __call__(
        self,
        *,
        tier: ModelTier,
        system_prompt: str,
        user_prompt: str,
        envelope: TaskEnvelope,
    ) -> str:
        raise RuntimeError("executor boom")


# ---------------------------------------------------------------------------
# TaskEnvelope / TaskResult
# ---------------------------------------------------------------------------


class TestEnvelope:
    def test_make_envelope_defaults(self) -> None:
        env = make_envelope(
            category=TaskCategory.DEBUG,
            goal="fix a failing test",
        )
        assert isinstance(env, TaskEnvelope)
        assert env.category == TaskCategory.DEBUG
        assert env.goal == "fix a failing test"
        assert env.caller == SubsystemId.OPERATOR
        assert env.context == {}
        assert env.task_id  # default factory ran
        assert env.requested_tier is None

    def test_make_envelope_context_passthrough(self) -> None:
        env = make_envelope(
            category=TaskCategory.LOG_PARSING,
            goal="summarize latest errors",
            caller=SubsystemId.AUTOPILOT_WATCHDOG,
            rationale="recent spike in tracebacks",
            recent_logs=["a", "b"],
            count=2,
        )
        assert env.rationale == "recent spike in tracebacks"
        assert env.context == {"recent_logs": ["a", "b"], "count": 2}
        assert env.caller == SubsystemId.AUTOPILOT_WATCHDOG

    def test_envelope_json_roundtrip(self) -> None:
        env = make_envelope(
            category=TaskCategory.CODE_REVIEW,
            goal="review change",
        )
        raw = env.model_dump_json()
        back = TaskEnvelope.model_validate_json(raw)
        assert back.task_id == env.task_id
        assert back.category == env.category


# ---------------------------------------------------------------------------
# Persona tier lock
# ---------------------------------------------------------------------------


class TestTierLock:
    @pytest.mark.parametrize(
        ("persona_cls", "persona_id", "expected_tier"),
        [
            (Batman, PersonaId.BATMAN, ModelTier.OPUS),
            (Alfred, PersonaId.ALFRED, ModelTier.SONNET),
            (Robin, PersonaId.ROBIN, ModelTier.HAIKU),
        ],
    )
    def test_persona_is_locked_to_tier(
        self,
        persona_cls: type[Persona],
        persona_id: PersonaId,
        expected_tier: ModelTier,
    ) -> None:
        p = persona_cls()
        assert p.persona_id == persona_id
        assert p.tier == expected_tier
        assert PERSONA_TIER[persona_id] == expected_tier

    def test_batman_supported_categories_are_opus_only(self) -> None:
        for cat in Batman.supported_categories():
            assert tier_for(cat) == ModelTier.OPUS

    def test_alfred_supported_categories_are_sonnet_only(self) -> None:
        for cat in Alfred.supported_categories():
            assert tier_for(cat) == ModelTier.SONNET

    def test_robin_supported_categories_are_haiku_only(self) -> None:
        for cat in Robin.supported_categories():
            assert tier_for(cat) == ModelTier.HAIKU

    def test_batman_rejects_routine_task(self, tmp_journal: Path) -> None:
        # TaskCategory.REFACTOR routes to Sonnet -- Batman must refuse.
        env = make_envelope(category=TaskCategory.REFACTOR, goal="extract fn")
        res = Batman(journal_path=tmp_journal).dispatch(env)
        assert res.success is False
        assert res.reason_code == "tier_mismatch"
        assert res.artifact == ""

    def test_alfred_rejects_architectural_task(
        self,
        tmp_journal: Path,
    ) -> None:
        env = make_envelope(
            category=TaskCategory.RED_TEAM_SCORING,
            goal="audit promotion",
        )
        res = Alfred(journal_path=tmp_journal).dispatch(env)
        assert res.success is False
        assert res.reason_code == "tier_mismatch"

    def test_robin_rejects_sonnet_task(self, tmp_journal: Path) -> None:
        env = make_envelope(
            category=TaskCategory.CODE_REVIEW,
            goal="review diff",
        )
        res = Robin(journal_path=tmp_journal).dispatch(env)
        assert res.success is False
        assert res.reason_code == "tier_mismatch"


# ---------------------------------------------------------------------------
# Executor plumbing
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_batman_happy_path_with_recording_executor(
        self,
        tmp_journal: Path,
    ) -> None:
        exe = _RecordingExecutor()
        env = make_envelope(
            category=TaskCategory.ADVERSARIAL_REVIEW,
            goal="attack the new sizing policy",
            rationale="pre-promotion check",
        )
        res = Batman(executor=exe, journal_path=tmp_journal).dispatch(env)
        assert res.success is True
        assert res.reason_code == "ok"
        assert res.tier_used == ModelTier.OPUS
        assert res.cost_multiplier == COST_RATIO[ModelTier.OPUS]
        assert "ARTIFACT(opus)" in res.artifact
        # prompt assertions
        assert len(exe.calls) == 1
        assert exe.calls[0]["tier"] == ModelTier.OPUS
        assert "BATMAN" in exe.calls[0]["system_prompt"]  # type: ignore[index]
        assert (
            "attack the new sizing policy"
            in (
                exe.calls[0]["user_prompt"]  # type: ignore[index]
            )
        )

    def test_alfred_happy_path(self, tmp_journal: Path) -> None:
        exe = _RecordingExecutor()
        env = make_envelope(
            category=TaskCategory.DOC_WRITING,
            goal="update CLAUDE.md",
        )
        res = Alfred(executor=exe, journal_path=tmp_journal).dispatch(env)
        assert res.success is True
        assert res.tier_used == ModelTier.SONNET
        assert "ALFRED" in exe.calls[0]["system_prompt"]  # type: ignore[index]

    def test_robin_happy_path(self, tmp_journal: Path) -> None:
        exe = _RecordingExecutor()
        env = make_envelope(
            category=TaskCategory.COMMIT_MESSAGE,
            goal="draft commit msg",
        )
        res = Robin(executor=exe, journal_path=tmp_journal).dispatch(env)
        assert res.success is True
        assert res.tier_used == ModelTier.HAIKU
        assert "ROBIN" in exe.calls[0]["system_prompt"]  # type: ignore[index]

    def test_executor_exception_becomes_failed_result(
        self,
        tmp_journal: Path,
    ) -> None:
        env = make_envelope(
            category=TaskCategory.DEBUG,
            goal="never mind",
        )
        res = Alfred(
            executor=_BoomExecutor(),
            journal_path=tmp_journal,
        ).dispatch(env)
        assert res.success is False
        assert res.reason_code == "executor_error"
        assert "executor boom" in res.reason

    def test_dryrun_executor_produces_markdown_structure(self) -> None:
        env = make_envelope(
            category=TaskCategory.DEBUG,
            goal="test dryrun",
        )
        out = DryRunExecutor()(
            tier=ModelTier.SONNET,
            system_prompt="SYS",
            user_prompt="USR",
            envelope=env,
        )
        assert out.startswith("# DRY-RUN (sonnet)")
        assert "SYS" in out
        assert "USR" in out


# ---------------------------------------------------------------------------
# JARVIS pre-flight
# ---------------------------------------------------------------------------


class _DenyingAdmin:
    """Stand-in JarvisAdmin that always DENIES -- for the deny path test."""

    def request_approval(self, req, *, ctx=None):
        from eta_engine.brain.jarvis_admin import ActionResponse
        from eta_engine.brain.jarvis_context import (
            ActionSuggestion,
            SessionPhase,
        )

        return ActionResponse(
            request_id=req.request_id,
            verdict=Verdict.DENIED,
            reason="denied by test double",
            reason_code="test_denied",
            jarvis_action=ActionSuggestion.STAND_ASIDE,
            stress_composite=0.9,
            session_phase=SessionPhase.MORNING,
        )


class _ApprovingAdmin:
    """Stand-in JarvisAdmin that APPROVES with the right selected_model."""

    def __init__(self, tier: ModelTier) -> None:
        self._tier = tier

    def request_approval(self, req, *, ctx=None):
        from eta_engine.brain.jarvis_admin import ActionResponse
        from eta_engine.brain.jarvis_context import (
            ActionSuggestion,
            SessionPhase,
        )

        assert req.action == ActionType.LLM_INVOCATION
        return ActionResponse(
            request_id=req.request_id,
            verdict=Verdict.APPROVED,
            reason="test approved",
            reason_code="llm_ok",
            jarvis_action=ActionSuggestion.TRADE,
            stress_composite=0.1,
            session_phase=SessionPhase.MORNING,
            selected_model=self._tier,
        )


class TestJarvisPreflight:
    def test_denial_short_circuits_persona(self, tmp_journal: Path) -> None:
        exe = _RecordingExecutor()
        env = make_envelope(
            category=TaskCategory.DEBUG,
            goal="should not run",
        )
        res = Alfred(
            executor=exe,
            admin=_DenyingAdmin(),  # type: ignore[arg-type]
            journal_path=tmp_journal,
        ).dispatch(env)
        assert res.success is False
        assert res.reason_code == "jarvis_denied"
        assert res.jarvis_verdict == Verdict.DENIED
        # Executor must NOT have been called.
        assert exe.calls == []

    def test_approval_permits_dispatch(self, tmp_journal: Path) -> None:
        exe = _RecordingExecutor()
        env = make_envelope(
            category=TaskCategory.DEBUG,
            goal="should run",
        )
        res = Alfred(
            executor=exe,
            admin=_ApprovingAdmin(ModelTier.SONNET),  # type: ignore[arg-type]
            journal_path=tmp_journal,
        ).dispatch(env)
        assert res.success is True
        assert res.jarvis_verdict == Verdict.APPROVED
        assert len(exe.calls) == 1


# ---------------------------------------------------------------------------
# JSONL journal
# ---------------------------------------------------------------------------


class TestJournal:
    def test_dispatch_writes_one_line_per_call(
        self,
        tmp_journal: Path,
    ) -> None:
        batman = Batman(executor=_RecordingExecutor(), journal_path=tmp_journal)
        for _ in range(3):
            batman.dispatch(
                make_envelope(
                    category=TaskCategory.RED_TEAM_SCORING,
                    goal="attack idea",
                ),
            )
        lines = tmp_journal.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        for raw in lines:
            rec = json.loads(raw)
            assert rec["persona"] == PersonaId.BATMAN.value
            assert rec["envelope"]["category"] == "red_team_scoring"
            assert rec["result"]["success"] is True

    def test_append_journal_ignores_os_error(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        # Point to an unwritable path and assert we still don't raise.
        class _ExplodingPath:
            parent = Path("/nope")

            def open(self, *args, **kwargs):
                raise OSError("no room at the inn")

        env = make_envelope(category=TaskCategory.DEBUG, goal="noop")
        res = TaskResult(
            task_id=env.task_id,
            persona_id=PersonaId.ALFRED,
            tier_used=ModelTier.SONNET,
            success=True,
            artifact="x",
            reason_code="ok",
            reason="ok",
            cost_multiplier=1.0,
        )
        # Call through the module function -- defensive parent.mkdir is
        # wrapped in the function, so we use a real tmp dir to let mkdir
        # succeed, then have the open explode.
        target = tmp_path / "locked.jsonl"
        target.write_text("", encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("boom")

        monkeypatch.setattr(Path, "open", _boom)
        append_journal(
            target,
            envelope=env,
            result=res,
            persona_id=PersonaId.ALFRED,
        )  # must not raise


# ---------------------------------------------------------------------------
# Fleet
# ---------------------------------------------------------------------------


class TestFleet:
    def test_fleet_routes_by_category(self, tmp_journal: Path) -> None:
        fleet = Fleet(journal_path=tmp_journal)
        # Opus-bound
        r1 = fleet.dispatch(
            make_envelope(
                category=TaskCategory.RED_TEAM_SCORING,
                goal="attack proposal",
            ),
        )
        assert r1.persona_id == PersonaId.BATMAN
        # Sonnet-bound
        r2 = fleet.dispatch(
            make_envelope(
                category=TaskCategory.REFACTOR,
                goal="rename module",
            ),
        )
        assert r2.persona_id == PersonaId.ALFRED
        # Haiku-bound
        r3 = fleet.dispatch(
            make_envelope(
                category=TaskCategory.LOG_PARSING,
                goal="tail logs",
            ),
        )
        assert r3.persona_id == PersonaId.ROBIN

    def test_requested_tier_override(self, tmp_journal: Path) -> None:
        fleet = Fleet(journal_path=tmp_journal)
        env = make_envelope(
            category=TaskCategory.RED_TEAM_SCORING,
            goal="audit but route to Alfred by override",
        )
        env.requested_tier = ModelTier.SONNET
        res = fleet.dispatch(env)
        # Alfred refuses because category routes to Opus, tier-mismatch is
        # the CORRECT outcome -- it proves the guardrail survives the
        # override route.
        assert res.persona_id == PersonaId.ALFRED
        assert res.reason_code == "tier_mismatch"

    def test_pool_dispatches_to_every_persona(
        self,
        tmp_journal: Path,
    ) -> None:
        fleet = Fleet(journal_path=tmp_journal)
        env = make_envelope(
            category=TaskCategory.STRATEGY_EDIT,  # routes to Sonnet
            goal="review sizing change",
        )
        results = fleet.pool(env)
        assert {r.persona_id for r in results} == {
            PersonaId.BATMAN,
            PersonaId.ALFRED,
            PersonaId.ROBIN,
        }
        # Only Alfred should succeed; the others should tier-mismatch.
        by_persona = {r.persona_id: r for r in results}
        assert by_persona[PersonaId.ALFRED].success is True
        assert by_persona[PersonaId.BATMAN].reason_code == "tier_mismatch"
        assert by_persona[PersonaId.ROBIN].reason_code == "tier_mismatch"

    def test_metrics_accumulate(self, tmp_journal: Path) -> None:
        fleet = Fleet(journal_path=tmp_journal)
        fleet.dispatch(
            make_envelope(
                category=TaskCategory.RED_TEAM_SCORING,
                goal="x",
            ),
        )
        fleet.dispatch(
            make_envelope(category=TaskCategory.REFACTOR, goal="y"),
        )
        fleet.dispatch(
            make_envelope(category=TaskCategory.LOG_PARSING, goal="z"),
        )
        m = fleet.metrics()
        assert isinstance(m, FleetMetrics)
        assert m.total_calls == 3
        assert m.calls_by_persona[PersonaId.BATMAN.value] == 1
        assert m.calls_by_persona[PersonaId.ALFRED.value] == 1
        assert m.calls_by_persona[PersonaId.ROBIN.value] == 1
        # Cost: Opus(5) + Sonnet(1) + Haiku(0.2) == 6.2
        assert abs(m.total_cost - 6.2) < 1e-6

    def test_describe_has_all_personas(self) -> None:
        fleet = Fleet()
        lines = fleet.describe()
        assert len(lines) == 4
        joined = "\n".join(lines)
        assert "jarvis" in joined
        assert "batman" in joined
        assert "alfred" in joined
        assert "robin" in joined


# ---------------------------------------------------------------------------
# Persona introspection helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_describe_persona_formats(self) -> None:
        assert "opus" in describe_persona(PersonaId.BATMAN)
        assert "sonnet" in describe_persona(PersonaId.ALFRED)
        assert "haiku" in describe_persona(PersonaId.ROBIN)
        assert "deterministic" in describe_persona(PersonaId.JARVIS)

    def test_persona_bucket_mapping(self) -> None:
        assert PERSONA_BUCKET[PersonaId.BATMAN] == TaskBucket.ARCHITECTURAL
        assert PERSONA_BUCKET[PersonaId.ALFRED] == TaskBucket.ROUTINE
        assert PERSONA_BUCKET[PersonaId.ROBIN] == TaskBucket.GRUNT
        assert PERSONA_BUCKET[PersonaId.JARVIS] is None

    def test_avengers_journal_path_under_home(self) -> None:
        assert AVENGERS_JOURNAL.name == "avengers.jsonl"
        assert AVENGERS_JOURNAL.parent.name == ".jarvis"


# ---------------------------------------------------------------------------
# Streamlit console surface
# ---------------------------------------------------------------------------


class TestConsoleImport:
    def test_console_module_imports_without_streamlit_runtime(self) -> None:
        # The console module should be importable even if streamlit isn't
        # actively running -- it gates UI work behind `if st is not None`.
        mod_path = Path(__file__).resolve().parents[2] / "launchers" / "avengers_console.py"
        assert mod_path.exists(), f"missing launcher: {mod_path}"
        # Direct import via importlib -- the launcher folder isn't a
        # package so we can't `import launchers.avengers_console`.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "avengers_console_smoke",
            mod_path,
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "main")
        assert hasattr(mod, "_PERSONA_BADGE")

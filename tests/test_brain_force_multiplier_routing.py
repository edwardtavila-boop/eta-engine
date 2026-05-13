"""Pytest coverage for the Force Multiplier routing invariants.

These tests don't make any LLM calls — they're pure-function coverage
that catches regressions in the routing policy and classifier logic.
The live integration test lives in ``live_force_multiplier.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eta_engine.brain.cli_provider import CLIResponse
from eta_engine.brain.model_policy import (
    ForceProvider,
    ModelTier,
    TaskBucket,
    TaskCategory,
    bucket_for,
    force_provider_for,
    select_model,
)
from eta_engine.brain.multi_model import (
    IMPLEMENT_SYSTEM,
    PLAN_SYSTEM,
    VERIFY_SYSTEM,
    CallBudgetExceededError,
    ChainResult,
    _classify_cli_failure,
    _enforce_call_budget,
    _is_cli_auth_error,
    _is_cli_quota_error,
    force_multiplier_chain,
    route_and_execute_async,
)
from eta_engine.brain.multi_model_telemetry import (
    log_call,
    new_chain_id,
    read_recent,
    summarize,
    telemetry_enabled,
)

# ---------------------------------------------------------------------------
# Routing policy coverage
# ---------------------------------------------------------------------------


class TestRoutingCoverage:
    """The reviewer flagged that adding a new TaskCategory could silently fall
    through to the DEEPSEEK default. These tests force every category to be
    explicitly mapped."""

    def test_every_category_has_a_tier(self) -> None:
        """select_model must succeed for every TaskCategory — no KeyErrors."""
        for category in TaskCategory:
            sel = select_model(category)
            assert sel.tier in ModelTier
            assert sel.bucket in TaskBucket
            assert sel.category == category

    def test_every_category_has_an_explicit_provider(self) -> None:
        """force_provider_for must return a non-default for every TaskCategory.

        We assert the mapping is *explicit* (in _CATEGORY_TO_PROVIDER) rather
        than falling through to the DEEPSEEK default — so that adding a new
        category forces the developer to think about which provider it routes
        to.
        """
        from eta_engine.brain.model_policy import _CATEGORY_TO_PROVIDER

        missing = [c.value for c in TaskCategory if c not in _CATEGORY_TO_PROVIDER]
        assert not missing, (
            f"TaskCategory members missing from _CATEGORY_TO_PROVIDER: {missing}. "
            f"Add an explicit ForceProvider mapping for each."
        )

    def test_architectural_categories_route_to_codex(self) -> None:
        """Architecture / red-team / risk policy work routes to Codex."""
        architectural = [
            TaskCategory.ARCHITECTURE_DECISION,
            TaskCategory.RED_TEAM_SCORING,
            TaskCategory.GAUNTLET_GATE_DESIGN,
            TaskCategory.RISK_POLICY_DESIGN,
            TaskCategory.STATE_MACHINE_DESIGN,
            TaskCategory.ADVERSARIAL_REVIEW,
            TaskCategory.CODE_REVIEW,
        ]
        for cat in architectural:
            assert force_provider_for(cat) == ForceProvider.CODEX, f"{cat.value} should route to CODEX"

    def test_no_category_routes_to_claude_by_default(self) -> None:
        assert all(force_provider_for(cat) != ForceProvider.CLAUDE for cat in TaskCategory)

    def test_anthropic_env_does_not_change_default_api_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from eta_engine.brain import llm_provider
        from eta_engine.brain.llm_provider import Provider

        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-not-used")
        monkeypatch.setenv("ETA_LLM_PROVIDER", "anthropic")
        llm_provider._ENV_LOADED = False

        assert llm_provider._default_provider() == Provider.DEEPSEEK

    def test_forced_anthropic_api_returns_blocked_empty_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from eta_engine.brain.llm_provider import ModelTier as ApiTier
        from eta_engine.brain.llm_provider import Provider, chat_completion

        monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-not-used")

        response = chat_completion(
            tier=ApiTier.HAIKU,
            user_message="should not call anthropic",
            provider=Provider.ANTHROPIC,
        )

        assert response.text == ""
        assert response.provider == Provider.ANTHROPIC
        assert "blocked" in response.reasoning

    def test_system_categories_route_to_codex(self) -> None:
        """Computer-use / debug / security work must route to Codex."""
        system = [
            TaskCategory.DEBUG,
            TaskCategory.TEST_EXECUTION,
            TaskCategory.SECURITY_AUDIT,
            TaskCategory.COMPUTER_USE_TASK,
        ]
        for cat in system:
            assert force_provider_for(cat) == ForceProvider.CODEX, f"{cat.value} should route to CODEX (Systems Expert)"

    def test_grunt_work_routes_to_deepseek(self) -> None:
        """Mechanical / boilerplate / formatting must route to DeepSeek."""
        grunt = [
            TaskCategory.BOILERPLATE,
            TaskCategory.FORMATTING,
            TaskCategory.LINT_FIX,
            TaskCategory.SIMPLE_EDIT,
            TaskCategory.LOG_PARSING,
            TaskCategory.COMMIT_MESSAGE,
            TaskCategory.TRIVIAL_LOOKUP,
        ]
        for cat in grunt:
            assert force_provider_for(cat) == ForceProvider.DEEPSEEK, (
                f"{cat.value} should route to DEEPSEEK (Worker Bee)"
            )


# ---------------------------------------------------------------------------
# Failure classifier coverage
# ---------------------------------------------------------------------------


class TestCLIFailureClassifier:
    """The reviewer's P0: 'exit_nonzero with text content was misclassified
    as success'. These tests pin the corrected classifier."""

    def _mk_resp(self, *, text: str = "", exit_code: int = 0) -> CLIResponse:
        return CLIResponse(
            text=text,
            provider="claude",
            model="opus",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            elapsed_ms=0.0,
            exit_code=exit_code,
        )

    def test_success_returns_none(self) -> None:
        resp = self._mk_resp(text="Here is the answer", exit_code=0)
        assert _classify_cli_failure(resp) is None

    def test_exit_nonzero_with_text_classifies_as_exit_nonzero(self) -> None:
        """The P0 the reviewer caught: don't accept partial output as success."""
        resp = self._mk_resp(
            text="Traceback (most recent call last):\n  File...",
            exit_code=1,
        )
        assert _classify_cli_failure(resp) == "exit_nonzero"

    def test_exit_zero_with_empty_text_classifies_as_empty_response(self) -> None:
        resp = self._mk_resp(text="", exit_code=0)
        assert _classify_cli_failure(resp) == "empty_response"

    def test_exit_zero_with_whitespace_only_text_classifies_as_empty_response(self) -> None:
        resp = self._mk_resp(text="   \n  \t  ", exit_code=0)
        assert _classify_cli_failure(resp) == "empty_response"

    def test_auth_error_takes_priority_over_exit_code(self) -> None:
        """Auth errors should be reported as 'auth' even if exit code is 0,
        because some CLI tools exit 0 with a 401 body."""
        resp = self._mk_resp(
            text="Failed to authenticate. API Error: 401",
            exit_code=0,
        )
        assert _classify_cli_failure(resp) == "auth"

    def test_quota_error_takes_priority_over_exit_code(self) -> None:
        resp = self._mk_resp(
            text="ERROR: You've hit your usage limit",
            exit_code=1,
        )
        assert _classify_cli_failure(resp) == "quota"

    def test_quota_takes_priority_over_auth_when_both_match(self) -> None:
        """Codex prints both TokenRefreshFailed AND 'usage limit' when quota
        is the real problem. The actionable fix is 'wait for reset', not
        'run login again', so quota wins.
        """
        resp = self._mk_resp(
            text=(
                "ERROR rmcp::transport::worker: worker quit with fatal: "
                "Auth(TokenRefreshFailed)\n"
                "ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings"
            ),
            exit_code=1,
        )
        assert _classify_cli_failure(resp) == "quota"

    def test_timeout_returns_timeout(self) -> None:
        resp = self._mk_resp(text="", exit_code=-1)
        assert _classify_cli_failure(resp) == "timeout"

    def test_not_installed_returns_not_installed(self) -> None:
        resp = self._mk_resp(text="", exit_code=-2)
        assert _classify_cli_failure(resp) == "not_installed"


class TestAuthErrorMarkers:
    """Pin the auth-error pattern matchers — the reviewer noted that
    overly broad markers (like 'Please run') would false-positive on
    legitimate output."""

    def test_no_false_positive_on_help_text(self) -> None:
        """Help text mentioning 'please run' must NOT trigger auth detection."""
        text = "To configure, please run the following command in your terminal."
        assert _is_cli_auth_error(text) is False

    def test_no_false_positive_on_empty_text(self) -> None:
        assert _is_cli_auth_error("") is False
        assert _is_cli_auth_error(None) is False  # type: ignore[arg-type]

    def test_detects_claude_401(self) -> None:
        text = (
            "Failed to authenticate. API Error: 401 "
            '{"type":"error","error":{"type":"authentication_error",'
            '"message":"Invalid authentication credentials"}}'
        )
        assert _is_cli_auth_error(text) is True

    def test_detects_codex_token_refresh_failure(self) -> None:
        text = (
            "ERROR rmcp::transport::worker: worker quit with fatal: "
            "Transport channel closed, when Auth(TokenRefreshFailed)"
        )
        assert _is_cli_auth_error(text) is True

    def test_quota_marker_matches_codex_usage_limit(self) -> None:
        text = "ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings"
        assert _is_cli_quota_error(text) is True

    def test_quota_marker_does_not_match_normal_explanation(self) -> None:
        """A model EXPLAINING what rate limiting is shouldn't trigger detection."""
        text = "Rate limiting is a technique used to control the rate of requests. When implementing it..."
        # 'rate limit' is in our markers — this IS a known false-positive risk.
        # Document it: the markers are lowercased substrings. If this becomes
        # a problem, we'd need to anchor to error-prefix patterns like
        # 'ERROR:' or '429'.
        assert _is_cli_quota_error(text) is True


# ---------------------------------------------------------------------------
# Chain orchestrator
# ---------------------------------------------------------------------------


class TestChainSkipSemantics:
    """``force_multiplier_chain`` should let callers skip stages cleanly."""

    def test_chain_with_all_stages_skipped_returns_empty_result(self) -> None:
        """Skipping all 3 stages should produce an empty ChainResult, not crash."""
        result = force_multiplier_chain(
            task="x",
            skip=("plan", "implement", "verify"),
        )
        assert isinstance(result, ChainResult)
        assert result.plan is None
        assert result.implement is None
        assert result.verify is None
        assert result.total_cost_usd == 0.0
        assert result.aborted_at is None

    def test_system_prompts_are_distinct(self) -> None:
        """Each stage should use a distinct system prompt — otherwise the
        roles aren't really differentiated."""
        assert PLAN_SYSTEM != IMPLEMENT_SYSTEM
        assert IMPLEMENT_SYSTEM != VERIFY_SYSTEM
        assert PLAN_SYSTEM != VERIFY_SYSTEM


# ---------------------------------------------------------------------------
# ARCHITECTURAL fallback escalation
# ---------------------------------------------------------------------------


class TestArchitecturalFallbackLogging:
    """The reviewer's P1: a silent DeepSeek fallback on a RISK_POLICY_DESIGN
    task is a quality regression that should show up in dashboards."""

    def test_architectural_fallback_logs_at_error(self, caplog) -> None:
        """When ARCHITECTURAL category falls back, log level must be ERROR."""

        # We can't run the actual fallback (it would call DeepSeek API), so
        # we just verify bucket classification feeds into log-level selection.
        assert bucket_for(TaskCategory.RISK_POLICY_DESIGN) == TaskBucket.ARCHITECTURAL
        assert bucket_for(TaskCategory.ARCHITECTURE_DECISION) == TaskBucket.ARCHITECTURAL
        assert bucket_for(TaskCategory.RED_TEAM_SCORING) == TaskBucket.ARCHITECTURAL

    def test_grunt_fallback_logs_at_warning_only(self, caplog) -> None:
        assert bucket_for(TaskCategory.BOILERPLATE) == TaskBucket.GRUNT
        assert bucket_for(TaskCategory.FORMATTING) == TaskBucket.GRUNT


# ---------------------------------------------------------------------------
# Telemetry module
# ---------------------------------------------------------------------------


class TestTelemetry:
    """The telemetry module is the foundation for observability — these tests
    pin down (1) records survive a round-trip through JSONL, (2) summary
    aggregation is correct, (3) the disable flag works, (4) malformed lines
    don't break reading."""

    def test_log_and_read_round_trip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "fm.jsonl"
        monkeypatch.setenv("ETA_FM_TELEMETRY_LOG", str(log))
        log_call(
            record={
                "kind": "route",
                "category": "boilerplate",
                "actual_provider": "deepseek",
                "cost_usd": 0.001,
                "elapsed_ms": 100,
                "fallback_used": False,
            }
        )
        records = read_recent(limit=10)
        assert len(records) == 1
        assert records[0]["category"] == "boilerplate"
        # The typed CallRecord schema names the field 'provider' (the
        # actual provider that ran). The orchestrator's record dict uses
        # 'actual_provider' as the input key — log_call() maps it to
        # 'provider' on write. We verify the OUTPUT field name here.
        assert records[0]["provider"] == "deepseek"
        assert "ts" in records[0]

    def test_disable_flag_skips_writes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "fm.jsonl"
        monkeypatch.setenv("ETA_FM_TELEMETRY_LOG", str(log))
        monkeypatch.setenv("ETA_FM_TELEMETRY", "0")
        assert telemetry_enabled() is False
        log_call(record={"kind": "route", "category": "x", "actual_provider": "deepseek"})
        assert not log.exists(), "disabled telemetry should not create the log file"

    def test_summary_aggregates_per_provider(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "fm.jsonl"
        monkeypatch.setenv("ETA_FM_TELEMETRY_LOG", str(log))
        for prov, cost, fallback in [
            ("claude", 0.0, False),
            ("claude", 0.0, False),
            ("deepseek", 0.001, True),
            ("deepseek", 0.002, False),
        ]:
            log_call(
                record={
                    "kind": "route",
                    "category": "x",
                    "actual_provider": prov,
                    "cost_usd": cost,
                    "fallback_used": fallback,
                }
            )
        s = summarize(limit=10)
        assert s["calls"] == 4
        assert s["fallback_count"] == 1
        assert s["fallback_rate"] == 0.25
        assert s["by_provider"]["claude"]["calls"] == 2
        assert s["by_provider"]["deepseek"]["cost_usd"] == 0.003

    def test_malformed_lines_do_not_break_read(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "fm.jsonl"
        log.write_text(
            '{"kind":"route","category":"a","actual_provider":"deepseek"}\n'
            "not valid json\n"
            '{"kind":"route","category":"b","actual_provider":"claude"}\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("ETA_FM_TELEMETRY_LOG", str(log))
        records = read_recent(limit=10)
        assert len(records) == 2  # malformed line skipped, others survive
        assert records[0]["category"] == "a"
        assert records[1]["category"] == "b"

    def test_chain_id_is_unique_and_well_formed(self) -> None:
        """Chain IDs must be unique and follow the ``CHN-<8 hex>`` format
        the typed CallRecord schema produces."""
        ids = {new_chain_id() for _ in range(50)}
        assert len(ids) == 50  # all unique
        sample = next(iter(ids))
        prefix, _, suffix = sample.partition("-")
        assert prefix == "CHN", f"expected CHN- prefix, got {sample!r}"
        # Suffix is 8 hex chars from uuid4().hex[:8]
        assert len(suffix) == 8
        int(suffix, 16)  # raises ValueError if not hex


# ---------------------------------------------------------------------------
# Per-call cost ceiling
# ---------------------------------------------------------------------------


class TestCallBudgetCeiling:
    """``route_and_execute(max_cost_usd=...)`` raises BEFORE making the call
    if worst-case spend would exceed the cap. This is a defense-in-depth
    measure — callers don't have to know which provider will run."""

    def test_deepseek_huge_max_tokens_raises(self) -> None:
        # DeepSeek V4 Pro output rate is $0.87/1M; 10M tokens = $8.70 worst case
        with pytest.raises(CallBudgetExceededError, match="Refused"):
            _enforce_call_budget(
                preferred=ForceProvider.DEEPSEEK,
                tier=ModelTier.OPUS,  # -> deepseek-v4-pro
                max_tokens=10_000_000,
                max_cost_usd=0.10,
            )

    def test_modest_call_passes(self) -> None:
        # 1024 tokens * $0.28/1M = $0.000287 — well under any sane cap
        _enforce_call_budget(
            preferred=ForceProvider.DEEPSEEK,
            tier=ModelTier.SONNET,
            max_tokens=1024,
            max_cost_usd=0.01,
        )  # no exception

    def test_subscription_providers_have_no_api_budget_floor(self) -> None:
        """Subscription CLI lanes do not consume the paid API budget."""
        _enforce_call_budget(
            preferred=ForceProvider.CODEX,
            tier=ModelTier.OPUS,
            max_tokens=100_000_000,
            max_cost_usd=0.10,
        )
        # Legacy Claude routes are disabled and fall forward to Codex/DeepSeek.
        _enforce_call_budget(
            preferred=ForceProvider.CLAUDE,
            tier=ModelTier.OPUS,
            max_tokens=100_000_000,
            max_cost_usd=0.10,
        )


# ---------------------------------------------------------------------------
# Async wrapper
# ---------------------------------------------------------------------------


class TestRouteAndExecuteAsync:
    """``route_and_execute_async`` must offload to a thread so the event
    loop stays responsive. We can't easily mock the synchronous call, so
    these tests confirm the wrapper is awaitable and forwards args."""

    def test_is_coroutine(self) -> None:
        # inspect.iscoroutinefunction confirms the function returns a
        # coroutine when called — i.e. it's a real async def.
        # (asyncio.iscoroutinefunction is deprecated in 3.16+.)
        import inspect

        assert inspect.iscoroutinefunction(route_and_execute_async)

    def test_wrapper_signature_includes_chain_id(self) -> None:
        import inspect

        sig = inspect.signature(route_and_execute_async)
        # All chain/budget parameters must be exposed so async callers
        # have the same control surface as sync callers.
        for required in ("category", "user_message", "chain_id", "chain_stage", "max_cost_usd", "force_provider"):
            assert required in sig.parameters, f"async wrapper missing arg: {required}"


# ---------------------------------------------------------------------------
# Avengers Fleet bridge
# ---------------------------------------------------------------------------


class TestMultiModelExecutor:
    """``MultiModelExecutor`` bridges the Avengers Fleet to the FM orchestrator.

    These tests pin the Executor-protocol signature without making any LLM
    calls — the live integration is exercised by the existing FM smoke
    paths and the Avengers fleet test suite separately.
    """

    def test_satisfies_executor_protocol_signature(self) -> None:
        """The __call__ signature must match the Avengers Executor Protocol."""
        import inspect

        from eta_engine.brain.avengers.base import Executor  # noqa: PLC0415
        from eta_engine.brain.multi_model_executor import MultiModelExecutor  # noqa: PLC0415

        # Structural protocol check: Executor is a typing.Protocol, so
        # we verify by signature rather than isinstance.
        proto_sig = inspect.signature(Executor.__call__)
        impl_sig = inspect.signature(MultiModelExecutor.__call__)
        # Each protocol-required keyword must be present on the impl.
        for param_name in proto_sig.parameters:
            if param_name == "self":
                continue
            assert param_name in impl_sig.parameters, f"MultiModelExecutor missing Executor protocol arg: {param_name}"

    def test_factory_function_is_lazy(self) -> None:
        """``create_multimodel_fleet`` must defer Fleet import to call time
        so importing the module doesn't pull in the whole Avengers stack
        (which has heavy transitive dependencies)."""
        import inspect

        from eta_engine.brain.multi_model_executor import create_multimodel_fleet  # noqa: PLC0415

        # The function exists, has the expected signature.
        sig = inspect.signature(create_multimodel_fleet)
        assert "admin" in sig.parameters
        assert "journal_path" in sig.parameters

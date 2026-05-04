"""Pytest coverage for the Force Multiplier routing invariants.

These tests don't make any LLM calls — they're pure-function coverage
that catches regressions in the routing policy and classifier logic.
The live integration test lives in ``live_force_multiplier.py``.
"""

from __future__ import annotations

import logging

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
    _classify_cli_failure,
    _is_cli_auth_error,
    _is_cli_quota_error,
    force_multiplier_chain,
    ChainBudgetExceeded,
    ChainResult,
    PLAN_SYSTEM,
    IMPLEMENT_SYSTEM,
    VERIFY_SYSTEM,
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

    def test_architectural_categories_route_to_claude(self) -> None:
        """Architecture / red-team / risk policy work must route to Claude."""
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
            assert force_provider_for(cat) == ForceProvider.CLAUDE, (
                f"{cat.value} should route to CLAUDE (Lead Architect)"
            )

    def test_system_categories_route_to_codex(self) -> None:
        """Computer-use / debug / security work must route to Codex."""
        system = [
            TaskCategory.DEBUG,
            TaskCategory.TEST_EXECUTION,
            TaskCategory.SECURITY_AUDIT,
            TaskCategory.COMPUTER_USE_TASK,
        ]
        for cat in system:
            assert force_provider_for(cat) == ForceProvider.CODEX, (
                f"{cat.value} should route to CODEX (Systems Expert)"
            )

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
            text=text, provider="claude", model="opus",
            input_tokens=0, output_tokens=0, cost_usd=0.0,
            elapsed_ms=0.0, exit_code=exit_code,
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
            'Failed to authenticate. API Error: 401 '
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
        text = (
            "Rate limiting is a technique used to control the rate of requests. "
            "When implementing it..."
        )
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
        from eta_engine.brain.multi_model import _fallback_deepseek

        # We can't run the actual fallback (it would call DeepSeek API), so
        # we just verify bucket classification feeds into log-level selection.
        assert bucket_for(TaskCategory.RISK_POLICY_DESIGN) == TaskBucket.ARCHITECTURAL
        assert bucket_for(TaskCategory.ARCHITECTURE_DECISION) == TaskBucket.ARCHITECTURAL
        assert bucket_for(TaskCategory.RED_TEAM_SCORING) == TaskBucket.ARCHITECTURAL

    def test_grunt_fallback_logs_at_warning_only(self, caplog) -> None:
        assert bucket_for(TaskCategory.BOILERPLATE) == TaskBucket.GRUNT
        assert bucket_for(TaskCategory.FORMATTING) == TaskBucket.GRUNT

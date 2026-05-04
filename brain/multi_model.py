"""
Multi-Model Force Multiplier Orchestrator (Wave-19).

Routes every task to the best provider based on 2026 strengths:

  CLAUDE   (Lead Architect) — planning, architecture, code review, red team
  DEEPSEEK (Worker Bee)     — high-volume generation, boilerplate, grunt work
  CODEX    (Systems Expert) — debugging, test execution, security audits

All three providers use existing subscriptions:
  * Claude  → subscription CLI (Claude Pro) — no API key
  * Codex   → subscription CLI (ChatGPT Plus/Pro) — no API key
  * DeepSeek → cheap API ($0.14/1M input) — DEEPSEEK_API_KEY

Graceful fallback: if a CLI provider is unavailable, tasks route to DeepSeek API.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from eta_engine.brain.cli_provider import (
    CLIResponse,
    call_claude,
    call_codex,
    check_claude_available,
    check_codex_available,
    cli_provider_status,
)
from eta_engine.brain.llm_provider import (
    LLMResponse,
    ModelTier,
    chat_completion,
    native_provider_info,
)
from eta_engine.brain.model_policy import (
    ForceProvider,
    TaskCategory,
    force_provider_for,
    select_model,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Orchestrated response
# ---------------------------------------------------------------------------

@dataclass
class MultiModelResponse:
    """Unified response from any provider."""
    text: str
    provider: ForceProvider
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_ms: float = 0.0
    category: TaskCategory | None = None
    tier: ModelTier | None = None
    fallback_used: bool = False
    fallback_reason: str = ""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def route_and_execute(
    *,
    category: TaskCategory,
    system_prompt: str = "",
    user_message: str,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    workspace: str | None = None,
    force_provider: ForceProvider | None = None,
) -> MultiModelResponse:
    """Route a task to the best provider and execute.

    Provider selection (in priority order):
      1. ``force_provider`` override (caller's explicit choice)
      2. ``force_provider_for(category)`` policy lookup
      3. Falls back to DeepSeek if the chosen provider is unavailable

    Returns ``MultiModelResponse`` with ``fallback_used=True`` if the
    preferred provider was unavailable and DeepSeek handled the task.
    """
    selection = select_model(category)
    preferred = force_provider or force_provider_for(category)

    logger.info(
        "routing category=%s preferred=%s tier=%s",
        category.value, preferred.value, selection.tier.value,
    )

    # --- CLAUDE path ---
    if preferred == ForceProvider.CLAUDE:
        if not check_claude_available():
            return _fallback_deepseek(
                category=category, tier=selection.tier,
                system_prompt=system_prompt, user_message=user_message,
                max_tokens=max_tokens, temperature=temperature,
                fallback_reason="claude CLI not installed (run: npm install -g @anthropic-ai/claude-code)",
            )
        cli_resp = _claude_call(
            tier=selection.tier, system_prompt=system_prompt,
            user_message=user_message, max_tokens=max_tokens,
            workspace=workspace,
        )
        failure = _classify_cli_failure(cli_resp)
        if failure is None:
            return _wrap_cli(cli_resp, ForceProvider.CLAUDE, category, selection.tier)
        return _fallback_deepseek(
            category=category, tier=selection.tier,
            system_prompt=system_prompt, user_message=user_message,
            max_tokens=max_tokens, temperature=temperature,
            fallback_reason=_claude_fallback_reason(failure),
        )

    # --- CODEX path ---
    if preferred == ForceProvider.CODEX:
        if not check_codex_available():
            return _fallback_deepseek(
                category=category, tier=selection.tier,
                system_prompt=system_prompt, user_message=user_message,
                max_tokens=max_tokens, temperature=temperature,
                fallback_reason="codex CLI not installed (run: npm install -g @openai/codex)",
            )
        cli_resp = _codex_call(
            tier=selection.tier, system_prompt=system_prompt,
            user_message=user_message, workspace=workspace,
        )
        failure = _classify_cli_failure(cli_resp)
        if failure is None:
            return _wrap_cli(cli_resp, ForceProvider.CODEX, category, selection.tier)
        return _fallback_deepseek(
            category=category, tier=selection.tier,
            system_prompt=system_prompt, user_message=user_message,
            max_tokens=max_tokens, temperature=temperature,
            fallback_reason=_codex_fallback_reason(failure),
        )

    # --- DEEPSEEK path (default) ---
    return _execute_deepseek(
        category=category, tier=selection.tier,
        system_prompt=system_prompt, user_message=user_message,
        max_tokens=max_tokens, temperature=temperature,
    )


# ---------------------------------------------------------------------------
# Provider execution helpers
# ---------------------------------------------------------------------------

def _claude_call(
    *,
    tier: ModelTier,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    workspace: str | None,
) -> CLIResponse:
    return call_claude(
        system_prompt=system_prompt,
        user_message=user_message,
        model=_claude_model_for_tier(tier),
        max_tokens=max_tokens,
        max_budget_usd=1.00,
        workspace=workspace,
    )


def _codex_call(
    *,
    tier: ModelTier,
    system_prompt: str,
    user_message: str,
    workspace: str | None,
) -> CLIResponse:
    return call_codex(
        system_prompt=system_prompt,
        user_message=user_message,
        model=_codex_model_for_tier(tier),
        workspace=workspace,
    )


def _wrap_cli(
    resp: CLIResponse,
    provider: ForceProvider,
    category: TaskCategory,
    tier: ModelTier,
) -> MultiModelResponse:
    return MultiModelResponse(
        text=resp.text,
        provider=provider,
        model=resp.model,
        elapsed_ms=resp.elapsed_ms,
        category=category,
        tier=tier,
    )


def _claude_fallback_reason(failure: str) -> str:
    return {
        "auth": "claude CLI not authenticated for -p — run `claude setup-token`, falling back to DeepSeek",
        "quota": "claude CLI quota exhausted — falling back to DeepSeek",
        "exit_nonzero": "claude CLI failed with non-zero exit — falling back to DeepSeek",
        "timeout": "claude CLI timed out — falling back to DeepSeek",
        "not_installed": "claude CLI not installed — falling back to DeepSeek",
        "empty_response": "claude CLI returned empty text — falling back to DeepSeek",
    }.get(failure, f"claude CLI failure ({failure}) — falling back to DeepSeek")


def _codex_fallback_reason(failure: str) -> str:
    return {
        "auth": "codex CLI not authenticated — run `codex login`, falling back to DeepSeek",
        "quota": "codex CLI quota exhausted (ChatGPT Plus/Pro monthly limit) — falling back to DeepSeek",
        "exit_nonzero": "codex CLI failed with non-zero exit — falling back to DeepSeek",
        "timeout": "codex CLI timed out — falling back to DeepSeek",
        "not_installed": "codex CLI not installed — falling back to DeepSeek",
        "empty_response": "codex CLI returned empty text — falling back to DeepSeek",
    }.get(failure, f"codex CLI failure ({failure}) — falling back to DeepSeek")


def _execute_deepseek(
    *,
    category: TaskCategory,
    tier: ModelTier,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> MultiModelResponse:
    resp: LLMResponse = chat_completion(
        tier=tier,
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return MultiModelResponse(
        text=resp.text,
        provider=ForceProvider.DEEPSEEK,
        model=resp.model,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        cost_usd=resp.cost_usd,
        category=category,
        tier=tier,
    )


def _fallback_deepseek(
    *,
    category: TaskCategory,
    tier: ModelTier,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
    fallback_reason: str,
) -> MultiModelResponse:
    # Architectural / risk-policy work is the LAST place we want a silent
    # quality regression. If those tasks fall back to DeepSeek, escalate
    # the log level so it shows up in dashboards and on-call alerts.
    from eta_engine.brain.model_policy import TaskBucket, bucket_for  # noqa: PLC0415
    is_architectural = bucket_for(category) == TaskBucket.ARCHITECTURAL
    log_fn = logger.error if is_architectural else logger.warning
    log_fn(
        "Falling back to DeepSeek (category=%s bucket=%s): %s",
        category.value,
        bucket_for(category).value,
        fallback_reason,
    )
    resp: LLMResponse = chat_completion(
        tier=tier,
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return MultiModelResponse(
        text=resp.text,
        provider=ForceProvider.DEEPSEEK,
        model=resp.model,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        cost_usd=resp.cost_usd,
        category=category,
        tier=tier,
        fallback_used=True,
        fallback_reason=fallback_reason,
    )


# ---------------------------------------------------------------------------
# Model selection per provider
# ---------------------------------------------------------------------------

def _claude_model_for_tier(tier: ModelTier) -> str:
    mapping = {
        ModelTier.OPUS: "opus",
        ModelTier.SONNET: "sonnet",
        ModelTier.HAIKU: "haiku",
    }
    return mapping.get(tier, "sonnet")


def _codex_model_for_tier(tier: ModelTier) -> str:
    """Codex CLI on a ChatGPT subscription only supports a subset of models.

    ``o3`` and ``o4-mini`` are API-only and reject ChatGPT-account auth with
    HTTP 400 ("model not supported"). The CLI default is ``gpt-5.4``, which
    is what ChatGPT Pro/Plus subscribers actually have access to.

    If the operator wants to escalate to a heavier model (e.g. ``o3-pro``),
    they can set ETA_CODEX_MODEL_OVERRIDE in .env.
    """
    override = os.environ.get("ETA_CODEX_MODEL_OVERRIDE", "").strip()
    if override:
        return override
    return "gpt-5.4"  # ChatGPT-subscription default; works for all tiers


_AUTH_MARKERS: tuple[str, ...] = (
    "authentication fails",
    "invalid authentication",
    "failed to authenticate",
    "api error: 401",
    "401 unauthorized",
    "authentication required",
    "tokenrefreshfailed",
    "authentication_error",
)

_QUOTA_MARKERS: tuple[str, ...] = (
    "usage limit",
    "rate limit",
    "quota exceeded",
    "purchase more credits",
    "resourceexhausted",
    "too many requests",
    "insufficient_quota",
)


def _is_cli_auth_error(text: str) -> bool:
    """True only when text matches a known auth-error pattern (no false positives)."""
    if not text:
        return False
    lo = text.lower()
    return any(m in lo for m in _AUTH_MARKERS)


def _is_cli_quota_error(text: str) -> bool:
    """True when text matches a known quota / rate-limit pattern."""
    if not text:
        return False
    lo = text.lower()
    return any(m in lo for m in _QUOTA_MARKERS)


def _classify_cli_failure(resp: CLIResponse) -> str | None:
    """Return a short reason if the CLI response is unusable, else None.

    Order matters:
      1. Special exit codes (-1 = timeout, -2 = not_installed) win first.
      2. Then auth / quota patterns (regardless of exit code — both Claude
         and Codex sometimes exit 0 with an error body).
      3. Any other non-zero exit = ``exit_nonzero`` (the partial-output
         case the reviewer flagged: a truncated traceback is NOT success).
      4. Empty text on exit 0 = ``empty_response``.
    """
    if resp.exit_code == -1:
        return "timeout"
    if resp.exit_code == -2:
        return "not_installed"

    text = resp.text or ""
    # Quota is checked BEFORE auth because some CLIs print
    # ``TokenRefreshFailed`` as a side-effect of an OAuth refresh that
    # was rate-limited by the quota system. The user's actionable fix
    # in that case is "wait for the quota reset", not "re-login".
    if _is_cli_quota_error(text):
        return "quota"
    if _is_cli_auth_error(text):
        return "auth"

    if resp.exit_code != 0:
        return "exit_nonzero"
    if not text.strip():
        return "empty_response"
    return None


# ---------------------------------------------------------------------------
# Status / health
# ---------------------------------------------------------------------------

def force_multiplier_status() -> dict[str, Any]:
    """Return a status dict for dashboards and health checks."""
    cli_status = cli_provider_status()
    api_status = native_provider_info()

    return {
        "mode": "force_multiplier",
        "providers": {
            "claude": {
                "available": cli_status["claude_available"],
                "command": cli_status["claude_command"],
                "role": "Lead Architect — planning, architecture, code review",
            },
            "codex": {
                "available": cli_status["codex_available"],
                "command": cli_status["codex_command"],
                "role": "Systems Expert — debugging, test execution, security",
            },
            "deepseek": {
                "available": api_status["deepseek_key_configured"],
                "model": api_status["models"],
                "role": "Worker Bee — high-volume generation, boilerplate, grunt",
            },
        },
        "routing_table": {
            cat.value: force_provider_for(TaskCategory(cat)).value
            for cat in TaskCategory
        },
        "fallback": "All tasks fall back to DeepSeek API if preferred provider unavailable",
    }


# ---------------------------------------------------------------------------
# Force-Multiplier chain orchestrator (Wave-19)
# ---------------------------------------------------------------------------
#
# The canonical pipeline ties all three providers into one workflow:
#
#   1. PLAN      (CLAUDE)    — architect the work, define interfaces & risks
#   2. IMPLEMENT (DEEPSEEK)  — bulk-generate the actual code from the plan
#   3. VERIFY    (CODEX)     — run the tests, audit security, report results
#
# Each step receives the previous step's output as additional context. Steps
# can be individually skipped (e.g. when Codex is out of monthly quota) and
# the pipeline gracefully falls back to DeepSeek for any unavailable provider.

PLAN_SYSTEM = (
    "You are the Lead Architect. Produce a concise, numbered plan: "
    "interfaces, files to touch, risks, acceptance criteria. "
    "No implementation code — just the design."
)

IMPLEMENT_SYSTEM = (
    "You are the Worker Bee. Implement the plan exactly. "
    "Produce the code/edits needed. "
    "If the plan is ambiguous, prefer the smallest viable implementation."
)

VERIFY_SYSTEM = (
    "You are the Systems Expert. Verify the implementation by listing: "
    "the tests that should be run, what failure looks like, "
    "any security or correctness concerns the implementation missed. "
    "Be specific about commands and expected output."
)


class ChainBudgetExceededError(RuntimeError):
    """Raised when ``force_multiplier_chain`` would exceed its cost ceiling."""


class ChainAbortedError(RuntimeError):
    """Raised when a chain stage produced empty output and downstream stages
    would receive blank context."""


@dataclass
class ChainResult:
    """Result of a Force Multiplier chain run."""

    task: str
    plan: MultiModelResponse | None = None
    implement: MultiModelResponse | None = None
    verify: MultiModelResponse | None = None
    total_elapsed_ms: float = 0.0
    total_cost_usd: float = 0.0
    fallbacks_used: list[str] = field(default_factory=list)
    aborted_at: str | None = None  # set if a stage failed and chain stopped

    @property
    def final_output(self) -> str:
        """The last successful step's output."""
        for step in (self.verify, self.implement, self.plan):
            if step is not None:
                return step.text
        return ""


_DEFAULT_MAX_CHAIN_COST = 1.00  # USD ceiling for a full plan+implement+verify run
_DEFAULT_MAX_CONTEXT_CHARS = 24_000  # ~6k tokens — keeps us under model context limits


def force_multiplier_chain(
    *,
    task: str,
    workspace: str | None = None,
    skip: tuple[str, ...] = (),
    plan_category: TaskCategory = TaskCategory.ARCHITECTURE_DECISION,
    implement_category: TaskCategory = TaskCategory.SKELETON_SCAFFOLD,
    verify_category: TaskCategory = TaskCategory.TEST_EXECUTION,
    max_tokens: int = 4096,
    max_total_cost_usd: float = _DEFAULT_MAX_CHAIN_COST,
    max_context_chars: int = _DEFAULT_MAX_CONTEXT_CHARS,
    abort_on_empty: bool = True,
) -> ChainResult:
    """Run the canonical 3-stage Force Multiplier pipeline.

    Stages
    ------
    1. ``plan``      — CLAUDE (Lead Architect)  designs the work
    2. ``implement`` — DEEPSEEK (Worker Bee)    produces the code
    3. ``verify``    — CODEX (Systems Expert)   runs/audits the work

    Parameters
    ----------
    task : str
        The user-facing task description (e.g. "Add OCO bracket retry to crypto venue").
    skip : tuple[str, ...]
        Step names to skip — useful when a provider is out of quota.
        Example: ``skip=("verify",)`` when Codex monthly limit is hit.
    *_category : TaskCategory
        Override which TaskCategory drives the routing for each stage.
        The defaults map to CLAUDE/DEEPSEEK/CODEX respectively.
    workspace : str | None
        Working directory passed through to CLI providers.
    max_tokens : int
        Per-step token budget for the API/CLI calls.

    Notes
    -----
    Each stage's output is fed as context into the next stage's user message,
    so CODEX sees both the plan AND the implementation when verifying.
    Steps that fall back (preferred provider unavailable) are recorded in
    ``ChainResult.fallbacks_used``.
    """
    result = ChainResult(task=task)
    plan_text = ""
    impl_text = ""

    def _truncate(s: str, limit: int) -> str:
        if len(s) <= limit:
            return s
        return s[:limit] + f"\n[...truncated {len(s) - limit} chars to fit context]"

    def _check_budget(stage: str) -> bool:
        """Raise ``ChainBudgetExceededError`` if running stage would blow the cap.

        Cost is tracked AFTER each stage completes (we don't know the next
        stage's cost in advance). So this only catches "we already overspent
        on plan/implement and shouldn't run verify".
        """
        if result.total_cost_usd > max_total_cost_usd:
            logger.error(
                "[chain] budget exceeded before stage=%s: cost=$%.4f > cap=$%.4f",
                stage, result.total_cost_usd, max_total_cost_usd,
            )
            result.aborted_at = stage
            raise ChainBudgetExceededError(
                f"Chain cost ${result.total_cost_usd:.4f} > cap ${max_total_cost_usd:.2f} "
                f"before running {stage}"
            )
        return True

    def _check_stage_output(stage: str, resp: MultiModelResponse) -> bool:
        """Return True if downstream stages can use this output. False = abort.

        Empty output from a fallback (e.g. DeepSeek returned nothing because
        the API key was rejected) would feed garbage into the next stage.
        """
        if not resp.text.strip() and abort_on_empty:
            logger.error(
                "[chain] stage=%s returned empty text — aborting chain "
                "(provider=%s fallback=%s reason=%s)",
                stage, resp.provider.value, resp.fallback_used, resp.fallback_reason,
            )
            result.aborted_at = stage
            return False
        return True

    # --- Stage 1: PLAN (CLAUDE) ---
    if "plan" not in skip:
        logger.info("[chain] stage=plan task=%s", task[:80])
        plan = route_and_execute(
            category=plan_category,
            system_prompt=PLAN_SYSTEM,
            user_message=task,
            max_tokens=max_tokens,
            workspace=workspace,
        )
        result.plan = plan
        plan_text = plan.text
        result.total_elapsed_ms += plan.elapsed_ms
        result.total_cost_usd += plan.cost_usd
        if plan.fallback_used:
            result.fallbacks_used.append(f"plan: {plan.fallback_reason}")
        if not _check_stage_output("plan", plan):
            return result

    # --- Stage 2: IMPLEMENT (DEEPSEEK) ---
    if "implement" not in skip:
        _check_budget("implement")
        logger.info("[chain] stage=implement task=%s", task[:80])
        plan_excerpt = _truncate(plan_text, max_context_chars) if plan_text else ""
        impl_msg = task if not plan_excerpt else f"PLAN:\n{plan_excerpt}\n\nTASK: {task}"
        implement = route_and_execute(
            category=implement_category,
            system_prompt=IMPLEMENT_SYSTEM,
            user_message=impl_msg,
            max_tokens=max_tokens,
            workspace=workspace,
        )
        result.implement = implement
        impl_text = implement.text
        result.total_elapsed_ms += implement.elapsed_ms
        result.total_cost_usd += implement.cost_usd
        if implement.fallback_used:
            result.fallbacks_used.append(f"implement: {implement.fallback_reason}")
        if not _check_stage_output("implement", implement):
            return result

    # --- Stage 3: VERIFY (CODEX) ---
    if "verify" not in skip:
        _check_budget("verify")
        logger.info("[chain] stage=verify task=%s", task[:80])
        verify_parts = [f"TASK: {task}"]
        # Halve the per-section budget so plan + impl together fit in max_context_chars.
        section_budget = max_context_chars // 2
        if plan_text:
            verify_parts.append(f"PLAN:\n{_truncate(plan_text, section_budget)}")
        if impl_text:
            verify_parts.append(f"IMPLEMENTATION:\n{_truncate(impl_text, section_budget)}")
        verify_msg = "\n\n".join(verify_parts)
        verify = route_and_execute(
            category=verify_category,
            system_prompt=VERIFY_SYSTEM,
            user_message=verify_msg,
            max_tokens=max_tokens,
            workspace=workspace,
        )
        result.verify = verify
        result.total_elapsed_ms += verify.elapsed_ms
        result.total_cost_usd += verify.cost_usd
        if verify.fallback_used:
            result.fallbacks_used.append(f"verify: {verify.fallback_reason}")

    return result

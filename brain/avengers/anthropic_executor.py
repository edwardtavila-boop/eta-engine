"""
APEX PREDATOR  //  brain.avengers.anthropic_executor
====================================================
Production Avengers executor backed by the real Anthropic API.

Conforms to the existing ``Executor`` protocol from ``brain.avengers.base``
(``__call__(*, tier, system_prompt, user_prompt, envelope) -> str``) so it
drops into ``Fleet(executor=AnthropicExecutor(...))`` without any other
code changes.

Internally:
  * Splits the persona prompt into cacheable prefix (system_prompt) and
    per-call suffix (user_prompt). The persona-level system prompt is the
    stable doctrine + role text -- exactly the shape Anthropic's prompt
    cache rewards (90% input-token discount on cache hit).
  * Delegates to ``AnthropicClaudeClient.call()`` which handles the SDK
    invocation + cost accounting.
  * Pushes the structured ``ClaudeCallResult`` to a ``UsageTracker`` so the
    cost-governor's quota gate can see real spend.

Activation
----------
The avengers_daemon wires this in only when ``APEX_AVENGERS_LIVE=1``
*and* ``ANTHROPIC_API_KEY`` is present. Otherwise the daemon stays on
``DryRunExecutor`` and no live Claude calls happen.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from apex_predator.brain.jarvis_v3.claude_layer.prompt_cache import (
    AnthropicClaudeClient,
    ClaudeCallRequest,
    PromptCacheTracker,
    build_cached_prompt,
)

if TYPE_CHECKING:
    from apex_predator.brain.avengers.base import TaskEnvelope
    from apex_predator.brain.jarvis_v3.claude_layer.usage_tracker import (
        UsageTracker,
    )
    from apex_predator.brain.model_policy import ModelTier


class AnthropicExecutor:
    """Live Anthropic-backed Avengers executor.

    Parameters
    ----------
    sdk_client
        An ``anthropic.Anthropic`` instance. Duck-typed (must expose
        ``.messages.create(...)``) so tests can inject a fake.
    cache_tracker
        Local mirror of Anthropic's prompt cache. If omitted, a fresh
        tracker is created (per-executor cache state).
    usage
        ``UsageTracker`` to record real $ cost into. Optional -- if
        omitted, calls succeed but spend is not tracked. Production
        wiring always passes the daemon's tracker.
    max_tokens
        Default ``max_tokens`` per call. Personas can't override per-call
        today; tune this when you tune output verbosity. 1024 is a
        reasonable default for the verdict-shape outputs personas produce.
    """

    def __init__(
        self,
        *,
        sdk_client: object,
        cache_tracker: PromptCacheTracker | None = None,
        usage: UsageTracker | None = None,
        max_tokens: int = 1024,
    ) -> None:
        self._cache_tracker = cache_tracker or PromptCacheTracker()
        self._usage = usage
        self._max_tokens = max_tokens
        self._client = AnthropicClaudeClient(sdk_client, self._cache_tracker)

    def __call__(
        self,
        *,
        tier: ModelTier,
        system_prompt: str,
        user_prompt: str,
        envelope: TaskEnvelope,
    ) -> str:
        # Persona-level system_prompt is the stable cacheable prefix.
        # Per-call user_prompt is the variable suffix.
        # The CachedPrompt.system field is legacy -- leave empty.
        prompt = build_cached_prompt(
            system="",
            prefix=system_prompt,
            suffix=user_prompt,
        )
        req = ClaudeCallRequest(
            model=tier,
            prompt=prompt,
            max_tokens=self._max_tokens,
            persona=envelope.caller.value,
        )
        result = self._client.call(req)
        if self._usage is not None:
            self._usage.record_call(result)
        return result.output_text


__all__ = ["AnthropicExecutor"]

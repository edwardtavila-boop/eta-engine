"""
JARVIS v3 // claude_layer.prompt_cache
======================================
Layer 2 -- prompt caching + Claude client wrapper.

Anthropic's prompt cache has a 5-minute TTL and gives ~90% input-token
discount on reads (25% write overhead). This module:

  1. Splits any prompt into CACHEABLE_PREFIX + VARIABLE_SUFFIX.
  2. Stamps the cache control marker on the prefix.
  3. Maintains a local cache-miss/-hit counter for observability.
  4. Wraps the Anthropic SDK call in a ``ClaudeClient`` protocol so
     tests can inject a fake client.

The prefix should be the STABLE part: doctrine text, persona role
instructions, output schema. The suffix is the PER-CALL context
(current stress, regime, precedent summary).

Cost model (Anthropic, as of 2026):
  * Haiku  4.5: $0.80 / $4  per M tokens (input / output)
  * Sonnet 4.6: $3.00 / $15
  * Opus   4.7: $15.00 / $75
  * Cache read  : 10% of input
  * Cache write : 125% of input

Pure stdlib + pydantic. Network calls are injected via protocol.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from apex_predator.brain.model_policy import ModelTier

# Model pricing per 1M tokens (input, output).
MODEL_PRICES: dict[ModelTier, tuple[float, float]] = {
    ModelTier.HAIKU:  (0.80, 4.00),
    ModelTier.SONNET: (3.00, 15.00),
    ModelTier.OPUS:   (15.00, 75.00),
}

CACHE_READ_MULT = 0.10    # cached input at 10% of full price
CACHE_WRITE_MULT = 1.25   # cache write at 125% of full price
CACHE_TTL_S = 5 * 60      # Anthropic 5-minute TTL


class CachedPrompt(BaseModel):
    """A prompt split into cacheable prefix + per-call suffix."""
    model_config = ConfigDict(frozen=True)

    system:          str
    prefix:          str = Field(min_length=1)
    suffix:          str
    prefix_hash:     str = Field(min_length=8)
    tokens_prefix:   int = Field(ge=0)
    tokens_suffix:   int = Field(ge=0)


class ClaudeCallRequest(BaseModel):
    """What the Claude client needs to make a call."""
    model_config = ConfigDict(frozen=True)

    model:       ModelTier
    prompt:      CachedPrompt
    max_tokens:  int = Field(ge=1, default=512)
    persona:     str = ""   # informational -- which persona is calling


class ClaudeCallResult(BaseModel):
    """Response envelope with cost breakdown."""
    model_config = ConfigDict(frozen=True)

    model:              ModelTier
    persona:            str
    output_text:        str
    input_tokens:       int = Field(ge=0)
    output_tokens:      int = Field(ge=0)
    cached_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    cost_usd:           float = Field(ge=0.0)
    cache_hit:          bool
    ts:                 datetime


class ClaudeClient(Protocol):
    """Protocol every Claude client must implement.

    Production: wraps ``anthropic.Anthropic().messages.create`` with
    the ``anthropic-beta: prompt-caching-2024-07-31`` header.
    Tests: a fake that returns canned responses.
    """

    def call(self, req: ClaudeCallRequest) -> ClaudeCallResult: ...


# ---------------------------------------------------------------------------
# Prompt splitting
# ---------------------------------------------------------------------------

def _approx_tokens(text: str) -> int:
    """Rough 4-chars-per-token approximation. Good enough for cost preview."""
    return max(1, len(text) // 4)


def build_cached_prompt(
    *,
    system: str,
    prefix: str,
    suffix: str,
) -> CachedPrompt:
    """Assemble a prompt with the prefix marked for caching.

    Anthropic requires the prefix to be at least 1024 tokens (Sonnet/Opus)
    or 2048 tokens (Haiku) for caching to kick in. We don't enforce that
    here -- the SDK itself will fall back to non-cached if the prefix is
    too short.
    """
    h = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:12]
    return CachedPrompt(
        system=system,
        prefix=prefix,
        suffix=suffix,
        prefix_hash=h,
        tokens_prefix=_approx_tokens(prefix),
        tokens_suffix=_approx_tokens(suffix),
    )


# ---------------------------------------------------------------------------
# Local cache-hit tracker (mirrors the server-side cache so we can report)
# ---------------------------------------------------------------------------

class PromptCacheTracker:
    """Shadow record of which prefixes are currently cached server-side.

    Anthropic's 5-minute TTL is rolling -- every read refreshes it. We
    mimic that locally so dashboard / cost-governor can accurately guess
    whether the next call will hit cache.
    """

    def __init__(self, ttl_s: float = CACHE_TTL_S) -> None:
        self._last_seen: dict[str, datetime] = {}
        self.ttl = timedelta(seconds=ttl_s)

    def observe(self, prefix_hash: str, now: datetime | None = None) -> bool:
        """Record a call. Return True if the cache was 'hot' at call time."""
        now = now or datetime.now(UTC)
        prev = self._last_seen.get(prefix_hash)
        hit = prev is not None and (now - prev) <= self.ttl
        self._last_seen[prefix_hash] = now
        return hit

    def is_hot(self, prefix_hash: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        prev = self._last_seen.get(prefix_hash)
        return prev is not None and (now - prev) <= self.ttl


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

def estimate_cost(
    model: ModelTier,
    *,
    prefix_tokens: int,
    suffix_tokens: int,
    output_tokens: int,
    cache_hit: bool,
) -> float:
    """Return USD cost for a single call with explicit cache-hit state."""
    in_rate, out_rate = MODEL_PRICES[model]
    # Base input cost depends on cache hit
    if cache_hit:
        # Prefix read at discount
        prefix_cost = prefix_tokens / 1_000_000 * in_rate * CACHE_READ_MULT
    else:
        # Prefix is written (125% of input cost) on a miss
        prefix_cost = prefix_tokens / 1_000_000 * in_rate * CACHE_WRITE_MULT
    # Suffix is always new, paid at full input cost
    suffix_cost = suffix_tokens / 1_000_000 * in_rate
    output_cost = output_tokens / 1_000_000 * out_rate
    return round(prefix_cost + suffix_cost + output_cost, 6)


def cost_of_call(result: ClaudeCallResult) -> float:
    """Recompute cost from a call result (sanity check)."""
    in_rate, out_rate = MODEL_PRICES[result.model]
    cached_cost = result.cached_read_tokens / 1_000_000 * in_rate * CACHE_READ_MULT
    write_cost = result.cache_write_tokens / 1_000_000 * in_rate * CACHE_WRITE_MULT
    fresh_in = max(
        0,
        result.input_tokens - result.cached_read_tokens - result.cache_write_tokens,
    )
    fresh_cost = fresh_in / 1_000_000 * in_rate
    output_cost = result.output_tokens / 1_000_000 * out_rate
    return round(cached_cost + write_cost + fresh_cost + output_cost, 6)


# ---------------------------------------------------------------------------
# Fake client for tests / dry-runs
# ---------------------------------------------------------------------------

class FakeClaudeClient:
    """Deterministic Claude client for tests. Tracks calls + applies cache."""

    def __init__(
        self,
        tracker: PromptCacheTracker | None = None,
        canned_text: str = "VOTE=APPROVE CONFIDENCE=0.70 REASONS=['default stub']",
    ) -> None:
        self.tracker = tracker or PromptCacheTracker()
        self.calls: list[ClaudeCallResult] = []
        self.canned_text = canned_text

    def call(self, req: ClaudeCallRequest) -> ClaudeCallResult:
        now = datetime.now(UTC)
        hit = self.tracker.observe(req.prompt.prefix_hash, now=now)
        prefix_tokens = req.prompt.tokens_prefix
        suffix_tokens = req.prompt.tokens_suffix
        output_tokens = _approx_tokens(self.canned_text)
        cost = estimate_cost(
            req.model,
            prefix_tokens=prefix_tokens,
            suffix_tokens=suffix_tokens,
            output_tokens=output_tokens,
            cache_hit=hit,
        )
        result = ClaudeCallResult(
            model=req.model,
            persona=req.persona,
            output_text=self.canned_text,
            input_tokens=prefix_tokens + suffix_tokens,
            output_tokens=output_tokens,
            cached_read_tokens=prefix_tokens if hit else 0,
            cache_write_tokens=0 if hit else prefix_tokens,
            cost_usd=cost,
            cache_hit=hit,
            ts=now,
        )
        self.calls.append(result)
        return result


# ---------------------------------------------------------------------------
# Anthropic model ID per tier (single source of truth for "which model string
# does the SDK actually accept for this tier"). Update when models rev.
# ---------------------------------------------------------------------------

ANTHROPIC_MODEL_BY_TIER: dict[ModelTier, str] = {
    ModelTier.HAIKU:  "claude-haiku-4-5",
    ModelTier.SONNET: "claude-sonnet-4-6",
    ModelTier.OPUS:   "claude-opus-4-7",
}


# ---------------------------------------------------------------------------
# Production adapter -- wraps anthropic.Anthropic().messages.create() with
# prompt caching + structured cost accounting. Wired by avengers_daemon when
# APEX_AVENGERS_LIVE=1 and ANTHROPIC_API_KEY is present.
# ---------------------------------------------------------------------------

class AnthropicClaudeClient:
    """Thin adapter over the Anthropic SDK.

    Wire it up in production via:

        from anthropic import Anthropic
        from apex_predator.brain.jarvis_v3.claude_layer.prompt_cache import (
            AnthropicClaudeClient, PromptCacheTracker,
        )
        client = AnthropicClaudeClient(Anthropic(), PromptCacheTracker())

    The SDK client is duck-typed (must expose ``.messages.create(...)``) so
    tests can pass a fake without importing the real SDK.
    """

    def __init__(self, sdk_client: object, tracker: PromptCacheTracker) -> None:
        self.sdk = sdk_client
        self.tracker = tracker

    def call(self, req: ClaudeCallRequest) -> ClaudeCallResult:
        model_id = ANTHROPIC_MODEL_BY_TIER[req.model]
        now = datetime.now(UTC)

        # Build system blocks. The cacheable prefix gets `cache_control`;
        # any extra `system` text rides as a non-cached block. Anthropic
        # requires the cached block to come FIRST and meet the minimum
        # token threshold (1024 for Sonnet/Opus, 2048 for Haiku) -- if it
        # doesn't, the SDK silently falls back to non-cached.
        system_blocks: list[dict] = [
            {
                "type": "text",
                "text": req.prompt.prefix,
                "cache_control": {"type": "ephemeral"},
            },
        ]
        if req.prompt.system and req.prompt.system != req.prompt.prefix:
            system_blocks.append({"type": "text", "text": req.prompt.system})

        # The per-call variable suffix becomes the user message. Empty
        # suffix is illegal for Anthropic, so substitute a single space.
        user_content = req.prompt.suffix if req.prompt.suffix else " "

        resp = self.sdk.messages.create(
            model=model_id,
            max_tokens=req.max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user_content}],
        )

        # Extract usage. cache_read / cache_creation may be missing on
        # older SDK versions or non-cached responses -- guard with getattr.
        usage = resp.usage
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cached_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

        # Cost from real usage numbers (not the estimate).
        in_rate, out_rate = MODEL_PRICES[req.model]
        fresh_in = max(0, input_tokens - cached_read - cache_write)
        cost = round(
            cached_read / 1_000_000 * in_rate * CACHE_READ_MULT
            + cache_write / 1_000_000 * in_rate * CACHE_WRITE_MULT
            + fresh_in / 1_000_000 * in_rate
            + output_tokens / 1_000_000 * out_rate,
            6,
        )

        # Mirror the server-side cache locally so cost-governor can
        # predict cache hits on the next call to this prefix.
        self.tracker.observe(req.prompt.prefix_hash, now=now)

        # Concatenate text blocks. Anthropic returns content as a list of
        # typed blocks; for our use case (no tool use yet) all blocks are text.
        output_text_parts: list[str] = []
        for block in (resp.content or []):
            text = getattr(block, "text", None)
            if text:
                output_text_parts.append(text)
        output_text = "".join(output_text_parts)

        return ClaudeCallResult(
            model=req.model,
            persona=req.persona,
            output_text=output_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_read_tokens=cached_read,
            cache_write_tokens=cache_write,
            cost_usd=cost,
            cache_hit=cached_read > 0,
            ts=now,
        )

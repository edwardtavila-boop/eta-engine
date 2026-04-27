"""
JARVIS v3 // claude_layer
=========================
Cost-controlled Claude API integration.

The design principle: **JARVIS is free, Claude is expensive. So make
JARVIS do 99% of the work.** Four cascading layers filter each
request-for-reasoning down to the minimum viable Claude invocation.

Layer 1 -- escalation:
    Cheap deterministic gate. By default, return JARVIS's in-house
    verdict (free). Only escalate to Claude when a strict trigger
    fires (crisis, high stress, critical action, doctrine conflict,
    etc.). Target: ~15% of decisions escalate.

Layer 2 -- prompt_cache:
    When Claude IS invoked, use the Anthropic 5-minute prompt cache
    aggressively. The shared prompt prefix (doctrine + persona role
    + general instructions) is cached once per window. Only the
    varying suffix (current context) pays full price. Target: ~90%
    cache-hit rate on input tokens in steady state.

Layer 3 -- stakes:
    Classify the decision's stakes (LOW / MEDIUM / HIGH / CRITICAL)
    from deterministic features. Map stakes -> model tier. Opus only
    fires for CRITICAL (<5% of escalated cases). Sonnet for HIGH.
    Haiku for MEDIUM. LOW stakes don't escalate at all.

Layer 4 -- distillation:
    Run self-play to generate (context, deterministic_verdict,
    claude_verdict) triples offline. Train a small classifier that
    predicts "will Claude agree with JARVIS here?" In production,
    skip Claude when the classifier is confident JARVIS alone is
    sufficient (target: 50-70% of already-escalated cases).

Supporting pieces:

  * usage_tracker -- per-call cost + quota, hourly/daily ceilings
  * cost_governor -- combined controller exposed as a single
                     ``should_invoke_claude()`` decision
  * prompts       -- JARVIS pre-builds the Claude prompt (so Claude
                     sees a dense structured context, never raw data)

Everything here is pure Python. Actual network calls to Anthropic are
injected via the ``ClaudeClient`` protocol so tests use fakes.
"""

from __future__ import annotations

__all__ = [
    "escalation",
    "prompt_cache",
    "stakes",
    "distillation",
    "usage_tracker",
    "cost_governor",
    "prompts",
]

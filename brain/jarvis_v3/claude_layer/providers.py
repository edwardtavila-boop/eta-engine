"""
JARVIS v3 // claude_layer.providers
====================================
Multi-provider scaffolding (P4 of the supercharged Path C rollout).

Today the live inference path is single-provider: Anthropic Claude via
the ``AnthropicExecutor``. The cascade pyramid (L0-L5) is also single-
provider. This module establishes the pattern for adding Google Gemini,
xAI Grok, and other Anthropic-compatible providers as cheaper sibling
options at each cascade tier.

The integration is deliberately scaffold-first: the ``Provider`` enum,
the ``ProviderInfo`` cost/capability table, and the per-tier preference
helper exist so callers can write provider-aware code today. Concrete
``GoogleGeminiExecutor`` / ``XAIGrokExecutor`` classes are deferred --
they need:
  1. The ``google-genai`` and ``xai-sdk`` packages installed
  2. ``GOOGLE_API_KEY`` and ``XAI_API_KEY`` in env
  3. Per-provider request/response normalization (output schemas differ)
  4. Per-provider cache semantics (Anthropic has prompt cache; Google
     has explicit caching API; xAI has none today)

When those are ready, drop the concrete executor classes next to
``AnthropicExecutor`` and register them in ``EXECUTOR_BY_PROVIDER``.

Cost preference tuning
----------------------
The per-tier preference order is operator-tunable at runtime via the
``APEX_PROVIDER_ORDER_<TIER>`` env vars (e.g.
``APEX_PROVIDER_ORDER_GRUNT="google,anthropic"``). Defaults pick the
historically-cheapest viable provider per tier.

This module is pure (no network, no SDK imports). The actual executor
classes live in ``brain/avengers/anthropic_executor.py`` and (future)
``google_executor.py`` / ``xai_executor.py``.
"""
from __future__ import annotations

import os
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from apex_predator.brain.model_policy import ModelTier, TaskBucket


# ---------------------------------------------------------------------------
# Provider taxonomy
# ---------------------------------------------------------------------------


class Provider(StrEnum):
    """Inference + agent providers the system can dispatch to."""
    ANTHROPIC    = "anthropic"     # messages.create -- AnthropicExecutor
    GOOGLE       = "google"        # gemini-2.5 family -- (future) GoogleGeminiExecutor
    XAI          = "xai"           # grok-4 family -- (future) XAIGrokExecutor
    CLAUDE_CODE  = "claude_code"   # claude --print agent loop -- ClaudeCodeAgentExecutor
    CODEX        = "codex"         # codex exec agent loop -- CodexAgentExecutor


class ProviderMode(StrEnum):
    """Whether a provider runs inference or a tool-using agent loop."""
    INFERENCE = "inference"   # single messages.create-style call
    AGENT     = "agent"       # multi-step loop with tools (Read/Bash/Edit)


# ---------------------------------------------------------------------------
# Cost + capability table
# ---------------------------------------------------------------------------


class ProviderInfo(BaseModel):
    """Per-provider capability + cost descriptor."""
    model_config = ConfigDict(frozen=True)

    provider:        Provider
    mode:            ProviderMode
    # Indicative model id and per-1M-token costs. Used for cost comparison
    # only -- actual model strings are picked by the concrete executor.
    indicative_model: str
    input_per_m_usd:  float = Field(ge=0.0)
    output_per_m_usd: float = Field(ge=0.0)
    # Capabilities flags (informational; concrete executors enforce).
    supports_prompt_cache:  bool = False
    supports_tool_use:      bool = False
    supports_streaming:     bool = False
    supports_extended_thinking: bool = False


# Reference table. Costs are indicative (Apr 2026). The cost-governor
# can use these to estimate spend per provider; the actual SDK call
# surfaces real costs at invocation time.
PROVIDER_INFO: dict[Provider, dict[ModelTier, ProviderInfo]] = {
    Provider.ANTHROPIC: {
        ModelTier.HAIKU: ProviderInfo(
            provider=Provider.ANTHROPIC, mode=ProviderMode.INFERENCE,
            indicative_model="claude-haiku-4-5",
            input_per_m_usd=0.80, output_per_m_usd=4.00,
            supports_prompt_cache=True, supports_tool_use=True,
            supports_streaming=True, supports_extended_thinking=False,
        ),
        ModelTier.SONNET: ProviderInfo(
            provider=Provider.ANTHROPIC, mode=ProviderMode.INFERENCE,
            indicative_model="claude-sonnet-4-6",
            input_per_m_usd=3.00, output_per_m_usd=15.00,
            supports_prompt_cache=True, supports_tool_use=True,
            supports_streaming=True, supports_extended_thinking=True,
        ),
        ModelTier.OPUS: ProviderInfo(
            provider=Provider.ANTHROPIC, mode=ProviderMode.INFERENCE,
            indicative_model="claude-opus-4-7",
            input_per_m_usd=15.00, output_per_m_usd=75.00,
            supports_prompt_cache=True, supports_tool_use=True,
            supports_streaming=True, supports_extended_thinking=True,
        ),
    },
    Provider.GOOGLE: {
        ModelTier.HAIKU: ProviderInfo(
            provider=Provider.GOOGLE, mode=ProviderMode.INFERENCE,
            indicative_model="gemini-2.5-flash",
            input_per_m_usd=0.075, output_per_m_usd=0.30,
            supports_prompt_cache=True, supports_tool_use=True,
            supports_streaming=True, supports_extended_thinking=False,
        ),
        ModelTier.SONNET: ProviderInfo(
            provider=Provider.GOOGLE, mode=ProviderMode.INFERENCE,
            indicative_model="gemini-2.5-pro",
            input_per_m_usd=1.25, output_per_m_usd=10.00,
            supports_prompt_cache=True, supports_tool_use=True,
            supports_streaming=True, supports_extended_thinking=True,
        ),
        # Gemini Pro Ultra at OPUS-equivalent tier -- placeholder until
        # Google publishes the equivalent model id + pricing.
        ModelTier.OPUS: ProviderInfo(
            provider=Provider.GOOGLE, mode=ProviderMode.INFERENCE,
            indicative_model="gemini-2.5-pro-ultra-tbd",
            input_per_m_usd=5.00, output_per_m_usd=20.00,
            supports_prompt_cache=True, supports_tool_use=True,
            supports_streaming=True, supports_extended_thinking=True,
        ),
    },
    Provider.XAI: {
        ModelTier.HAIKU: ProviderInfo(
            provider=Provider.XAI, mode=ProviderMode.INFERENCE,
            indicative_model="grok-4-fast",
            input_per_m_usd=0.50, output_per_m_usd=2.00,
            supports_prompt_cache=False, supports_tool_use=True,
            supports_streaming=True, supports_extended_thinking=False,
        ),
        ModelTier.SONNET: ProviderInfo(
            provider=Provider.XAI, mode=ProviderMode.INFERENCE,
            indicative_model="grok-4",
            input_per_m_usd=3.00, output_per_m_usd=15.00,
            supports_prompt_cache=False, supports_tool_use=True,
            supports_streaming=True, supports_extended_thinking=True,
        ),
        ModelTier.OPUS: ProviderInfo(
            provider=Provider.XAI, mode=ProviderMode.INFERENCE,
            indicative_model="grok-4-heavy",
            input_per_m_usd=10.00, output_per_m_usd=40.00,
            supports_prompt_cache=False, supports_tool_use=True,
            supports_streaming=True, supports_extended_thinking=True,
        ),
    },
    # Agent-mode providers don't have tier breakdowns in the same way --
    # they bill per-call based on the underlying model their CLI uses.
    Provider.CLAUDE_CODE: {
        ModelTier.SONNET: ProviderInfo(  # representative
            provider=Provider.CLAUDE_CODE, mode=ProviderMode.AGENT,
            indicative_model="claude-code-cli",
            input_per_m_usd=3.00, output_per_m_usd=15.00,
            supports_prompt_cache=True, supports_tool_use=True,
            supports_streaming=False, supports_extended_thinking=True,
        ),
    },
    Provider.CODEX: {
        ModelTier.SONNET: ProviderInfo(  # representative
            provider=Provider.CODEX, mode=ProviderMode.AGENT,
            indicative_model="codex-cli",
            input_per_m_usd=2.50, output_per_m_usd=10.00,
            supports_prompt_cache=False, supports_tool_use=True,
            supports_streaming=False, supports_extended_thinking=False,
        ),
    },
}


# ---------------------------------------------------------------------------
# Per-tier preference helper
# ---------------------------------------------------------------------------


# Default order: cheapest viable inference provider for each tier.
# Anthropic stays as the apex (OPUS) by default because its adversarial-
# reasoning quality on RED_TEAM_SCORING / GAUNTLET_GATE_DESIGN is the
# operator's calibration anchor.
_DEFAULT_PROVIDER_ORDER: dict[TaskBucket, tuple[Provider, ...]] = {
    TaskBucket.GRUNT: (
        Provider.GOOGLE,    # gemini-2.5-flash @ $0.075 in / $0.30 out -- 10x cheaper than Haiku
        Provider.ANTHROPIC, # haiku-4-5 fallback -- prompt-cache familiar
        Provider.XAI,       # grok-4-fast fallback -- no cache but resilient
    ),
    TaskBucket.ROUTINE: (
        Provider.GOOGLE,    # gemini-2.5-pro @ $1.25/$10 -- 2x cheaper than Sonnet
        Provider.ANTHROPIC, # sonnet-4-6 fallback
        Provider.XAI,       # grok-4 fallback
    ),
    TaskBucket.ARCHITECTURAL: (
        Provider.ANTHROPIC, # opus-4-7 -- adversarial reasoning quality is the anchor
        Provider.GOOGLE,    # gemini-2.5-pro-ultra fallback
        Provider.XAI,       # grok-4-heavy fallback
    ),
}


def provider_order_for_tier(tier: ModelTier) -> tuple[Provider, ...]:
    """Return the provider preference list for a tier, env-overrideable.

    Priority:
      1. ``APEX_PROVIDER_ORDER_<BUCKET>`` env var (comma-separated)
         -- e.g. ``APEX_PROVIDER_ORDER_GRUNT="anthropic,google"``
      2. The default order baked in above.

    Bucket name is derived from the tier (HAIKU->GRUNT, SONNET->ROUTINE,
    OPUS->ARCHITECTURAL) to match the existing TaskBucket taxonomy.
    """
    bucket = _bucket_for_tier(tier)
    env_var = f"APEX_PROVIDER_ORDER_{bucket.value.upper()}"
    env_val = os.environ.get(env_var, "").strip()
    if env_val:
        items: list[Provider] = []
        for raw in env_val.split(","):
            name = raw.strip().lower()
            if not name:
                continue
            try:
                items.append(Provider(name))
            except ValueError:
                # Ignore unknown provider names rather than crash --
                # the operator can typo and still get a working fallback.
                continue
        if items:
            return tuple(items)
    return _DEFAULT_PROVIDER_ORDER[bucket]


def cheapest_provider_at(
    tier: ModelTier,
    *,
    available: set[Provider] | None = None,
) -> Provider:
    """Return the cheapest provider that has both the tier and the
    operator's preference list considered.

    If ``available`` is supplied, only providers in that set are
    considered (used by the daemon to filter out providers whose
    keys / SDKs are not present at runtime).
    """
    order = provider_order_for_tier(tier)
    for prov in order:
        if available is not None and prov not in available:
            continue
        # Confirm the provider has a ProviderInfo entry for this tier.
        if tier in PROVIDER_INFO.get(prov, {}):
            return prov
    # Last resort: Anthropic (always present in the table).
    return Provider.ANTHROPIC


def cost_at(provider: Provider, tier: ModelTier) -> tuple[float, float]:
    """Return (input_per_m_usd, output_per_m_usd) for a (provider, tier)."""
    info = PROVIDER_INFO.get(provider, {}).get(tier)
    if info is None:
        # Unknown combo -- return Anthropic cost as a safe upper bound.
        anth = PROVIDER_INFO[Provider.ANTHROPIC][tier]
        return anth.input_per_m_usd, anth.output_per_m_usd
    return info.input_per_m_usd, info.output_per_m_usd


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_TIER_TO_BUCKET: dict[ModelTier, TaskBucket] = {
    ModelTier.OPUS:   TaskBucket.ARCHITECTURAL,
    ModelTier.SONNET: TaskBucket.ROUTINE,
    ModelTier.HAIKU:  TaskBucket.GRUNT,
}


def _bucket_for_tier(tier: ModelTier) -> TaskBucket:
    return _TIER_TO_BUCKET[tier]


__all__ = [
    "PROVIDER_INFO",
    "Provider",
    "ProviderInfo",
    "ProviderMode",
    "cheapest_provider_at",
    "cost_at",
    "provider_order_for_tier",
]

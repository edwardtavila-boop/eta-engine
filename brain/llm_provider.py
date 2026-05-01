"""
LLM provider abstraction — DeepSeek V4 native default.
Anthropic/Claude is retained as a fallback.
LiteLLM + OpenRouter for multi-provider routing.
Langfuse traces every call for observability.

Architecture
============
  ETA_LLM_PROVIDER=deepseek   → DeepSeek V4 (native, default)
  ETA_LLM_PROVIDER=anthropic  → Anthropic (legacy fallback)
  ETA_LLM_PROVIDER=litellm    → LiteLLM (unified failover)
  ETA_LLM_PROVIDER=openrouter → OpenRouter (real-time cheapest)
  (auto)                      → DeepSeek if DEEPSEEK_API_KEY set, else Anthropic

Tier → Model mapping (DeepSeek V4)
==================================
  OPUS     → deepseek-v4-pro    (thinking)  — architectural / adversarial
  SONNET   → deepseek-v4-flash  (thinking)  — routine reasoning
  HAIKU    → deepseek-v4-flash  (non-think) — grunt work / cost floor
  REASONER → deepseek-v4-flash  (thinking)  — chain-of-thought

Agent assignments
=================
  BATMAN → V4 Pro     — adversarial review, Red Team scoring
  ALFRED → V4 Flash   — documentation, code review
  ROBIN  → V4 Flash   — log parsing, formatting (non-thinking)

Pricing (per 1M tokens, USD)
=============================
  DeepSeek V4 Flash  $0.14  in / $0.28  out  (~19× cheaper than Claude Sonnet)
  DeepSeek V4 Pro    $0.435 in / $0.87  out  (75% discount until 2026-05-31)
  Claude Haiku       $0.80  in / $4.00  out
  Claude Sonnet      $3.00  in / $15.00 out  (baseline 1.00×)
  Claude Opus        $15.00 in / $75.00 out
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ENV_LOADED = False


def _ensure_dotenv() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    try:
        from dotenv import load_dotenv
        for candidate in (
            Path.cwd() / ".env",
            Path(__file__).resolve().parents[3] / ".env",
        ):
            if candidate.is_file():
                load_dotenv(dotenv_path=str(candidate), override=False)
        _ENV_LOADED = True
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Provider(StrEnum):
    DEEPSEEK = "deepseek"
    ANTHROPIC = "anthropic"
    LITELLM = "litellm"
    OPENROUTER = "openrouter"


class ModelTier(StrEnum):
    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"
    REASONER = "reasoner"


# ---------------------------------------------------------------------------
# Tier → model mapping (single source of truth)
# ---------------------------------------------------------------------------

_TIER_MODEL: dict[tuple[ModelTier, Provider], str] = {
    # DeepSeek V4 native
    (ModelTier.OPUS,     Provider.DEEPSEEK):   "deepseek-v4-pro",
    (ModelTier.SONNET,   Provider.DEEPSEEK):   "deepseek-v4-flash",
    (ModelTier.HAIKU,    Provider.DEEPSEEK):   "deepseek-v4-flash",
    (ModelTier.REASONER, Provider.DEEPSEEK):   "deepseek-v4-flash",
    # Anthropic fallback
    (ModelTier.OPUS,     Provider.ANTHROPIC):  "claude-opus-4-7-20250601",
    (ModelTier.SONNET,   Provider.ANTHROPIC):  "claude-sonnet-4-5-20250929",
    (ModelTier.HAIKU,    Provider.ANTHROPIC):  "claude-haiku-4-5-20251001",
    (ModelTier.REASONER, Provider.ANTHROPIC):  "claude-sonnet-4-5-20250929",
    # LiteLLM unified
    (ModelTier.OPUS,     Provider.LITELLM):    "deepseek/deepseek-v4-pro",
    (ModelTier.SONNET,   Provider.LITELLM):    "deepseek/deepseek-v4-flash",
    (ModelTier.HAIKU,    Provider.LITELLM):    "deepseek/deepseek-v4-flash",
    (ModelTier.REASONER, Provider.LITELLM):    "deepseek/deepseek-v4-flash",
    # OpenRouter cheapest-auto
    (ModelTier.OPUS,     Provider.OPENROUTER): "openrouter/deepseek/deepseek-v4-pro",
    (ModelTier.SONNET,   Provider.OPENROUTER): "openrouter/deepseek/deepseek-v4-flash",
    (ModelTier.HAIKU,    Provider.OPENROUTER): "openrouter/deepseek/deepseek-v4-flash",
    (ModelTier.REASONER, Provider.OPENROUTER): "openrouter/deepseek/deepseek-v4-flash",
}

_COST_1M: dict[str, tuple[float, float]] = {
    # DeepSeek V4
    "deepseek-v4-pro":                            (0.435, 0.87),
    "deepseek-v4-flash":                          (0.14, 0.28),
    # Claude (legacy)
    "claude-opus-4-7-20250601":                   (15.00, 75.00),
    "claude-sonnet-4-5-20250929":                 (3.00, 15.00),
    "claude-haiku-4-5-20251001":                  (0.80, 4.00),
    # LiteLLM prefixed
    "deepseek/deepseek-v4-pro":                   (0.435, 0.87),
    "deepseek/deepseek-v4-flash":                 (0.14, 0.28),
    "anthropic/claude-opus-4-7-20250601":         (15.00, 75.00),
    "anthropic/claude-sonnet-4-5-20250929":       (3.00, 15.00),
    # OpenRouter prefixed
    "openrouter/deepseek/deepseek-v4-pro":        (0.435, 0.87),
    "openrouter/deepseek/deepseek-v4-flash":      (0.14, 0.28),
    "openrouter/anthropic/claude-opus-4-7":       (15.00, 75.00),
    "openrouter/anthropic/claude-sonnet-4-5":     (3.00, 15.00),
}


_COST_1M: dict[str, tuple[float, float]] = {
    "deepseek-reasoner":                          (0.55, 2.19),
    "deepseek-chat":                              (0.27, 1.10),
    "claude-opus-4-7-20250601":                   (15.00, 75.00),
    "claude-sonnet-4-5-20250929":                 (3.00, 15.00),
    "claude-haiku-4-5-20251001":                  (0.80, 4.00),
    "anthropic/claude-opus-4-7-20250601":         (15.00, 75.00),
    "anthropic/claude-sonnet-4-5-20250929":       (3.00, 15.00),
    "deepseek/deepseek-chat":                     (0.27, 1.10),
    "deepseek/deepseek-reasoner":                 (0.55, 2.19),
    "openrouter/anthropic/claude-opus-4-7":       (15.00, 75.00),
    "openrouter/anthropic/claude-sonnet-4-5":     (3.00, 15.00),
    "openrouter/deepseek/deepseek-chat":          (0.27, 1.10),
    "openrouter/deepseek/deepseek-r1":            (0.55, 2.19),
}

_SONNET_BASELINE = 3.00

COST_RATIO: dict[tuple[ModelTier, Provider], float] = {}
for (tier, prov), model in _TIER_MODEL.items():
    inp, _ = _COST_1M.get(model, (0.0, 0.0))
    COST_RATIO[(tier, prov)] = round(inp / _SONNET_BASELINE, 3) if inp else 0.0


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    text: str
    model: str = ""
    provider: Provider = Provider.DEEPSEEK
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Langfuse observability — traces every LLM call
# ---------------------------------------------------------------------------

_LANGFUSE_ENABLED: bool | None = None


def _langfuse_available() -> bool:
    global _LANGFUSE_ENABLED
    if _LANGFUSE_ENABLED is not None:
        return _LANGFUSE_ENABLED
    _LANGFUSE_ENABLED = bool(os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip())
    return _LANGFUSE_ENABLED


def _langfuse_trace(name: str, metadata: dict[str, Any]) -> Any:
    """Create a Langfuse trace that auto-closes. Returns None if unavailable."""
    try:
        from langfuse import Langfuse
        trace = Langfuse().trace(name=name, metadata=metadata)
        return trace
    except Exception:  # noqa: BLE001
        return None


def _langfuse_generation(trace: Any, name: str, model: str, input_data: str,
                          output_data: str, usage: dict[str, int],
                          metadata: dict[str, Any] | None = None) -> None:
    """Log a generation span to Langfuse. Silent no-op when unavailable."""
    if trace is None:
        return
    try:
        trace.generation(
            name=name,
            model=model,
            input={"messages": input_data},
            output={"text": output_data},
            usage={"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0)},
            metadata=metadata or {},
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

def _default_provider() -> Provider:
    """DeepSeek native default. OpenRouter for real-time cheapest. LiteLLM for auto-failover.

    Priority:
      1. ETA_LLM_PROVIDER=openrouter → OpenRouter (real-time cheapest routing)
      2. ETA_LLM_PROVIDER=litellm     → LiteLLM (unified, auto-failover)
      3. ETA_LLM_PROVIDER=anthropic   → explicit Anthropic override
      4. Everything else              → DeepSeek
    """
    _ensure_dotenv()
    explicit = os.environ.get("ETA_LLM_PROVIDER", "").strip().lower()
    if explicit in ("openrouter", "or"):
        if os.environ.get("OPENROUTER_API_KEY", "").strip():
            return Provider.OPENROUTER
        logger.warning("ETA_LLM_PROVIDER=openrouter but no OPENROUTER_API_KEY — falling back")
    if explicit in ("litellm", "lite"):
        if os.environ.get("LITELLM_MASTER_KEY", "").strip():
            return Provider.LITELLM
        logger.warning("ETA_LLM_PROVIDER=litellm but no LITELLM_MASTER_KEY — falling back")
    if explicit in ("anthropic", "claude", "ant"):
        return Provider.ANTHROPIC
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return Provider.DEEPSEEK
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        logger.warning("No DEEPSEEK_API_KEY — falling back to Anthropic")
        return Provider.ANTHROPIC
    return Provider.DEEPSEEK


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def chat_completion(
    *,
    tier: ModelTier = ModelTier.HAIKU,
    system_prompt: str = "",
    user_message: str,
    max_tokens: int = 400,
    temperature: float = 0.7,
    provider: Provider | None = None,
) -> LLMResponse:
    """Single-entry chat completion with Langfuse observability.

    Tier routing:
      OPUS     → DeepSeek-R1 (deepseek-reasoner) — reasoning model
      SONNET   → DeepSeek-V3 (deepseek-chat)     — general purpose
      HAIKU    → DeepSeek-V3 (deepseek-chat)     — cost floor
      REASONER → DeepSeek-R1 (deepseek-reasoner) — chain-of-thought

    Falls back to Anthropic automatically if DeepSeek is unavailable.
    Returns empty ``LLMResponse(text="")`` if no API key is configured.
    """
    if provider is None:
        provider = _default_provider()

    model = _TIER_MODEL.get((tier, provider))
    if model is None:
        logger.warning("No model for tier=%s provider=%s", tier, provider)
        return LLMResponse(text="")

    api_key = _get_api_key(provider)
    if not api_key:
        other = Provider.ANTHROPIC if provider == Provider.DEEPSEEK else Provider.DEEPSEEK
        other_key = _get_api_key(other)
        if other_key:
            logger.info("Falling back to %s (no key for %s)", other.value, provider.value)
            return chat_completion(
                tier=tier, system_prompt=system_prompt, user_message=user_message,
                max_tokens=max_tokens, temperature=temperature, provider=other,
            )
        logger.warning("No API key for any provider")
        return LLMResponse(text="")

    started_at = time.time()
    lf_trace = _langfuse_trace(
        f"completion_{tier.value}_{provider.value}",
        {"tier": tier.value, "provider": provider.value, "model": model},
    )

    try:
        if provider == Provider.DEEPSEEK:
            resp = _call_deepseek(model=model, system_prompt=system_prompt,
                                  user_message=user_message, max_tokens=max_tokens,
                                  temperature=temperature, tier=tier)
        elif provider == Provider.LITELLM:
            resp = _call_litellm(model=model, system_prompt=system_prompt,
                                  user_message=user_message, max_tokens=max_tokens,
                                  temperature=temperature)
        elif provider == Provider.OPENROUTER:
            resp = _call_openrouter(model=model, system_prompt=system_prompt,
                                     user_message=user_message, max_tokens=max_tokens,
                                     temperature=temperature)
        else:
            resp = _call_anthropic(model=model, system_prompt=system_prompt,
                                   user_message=user_message, max_tokens=max_tokens,
                                   temperature=temperature)
    except Exception:
        if lf_trace is not None:
            try:
                lf_trace.generation(
                    name="completion_error", model=model,
                    input={"messages": user_message[:200]},
                    output={"error": "provider failed"},
                )
            except Exception:  # noqa: BLE001
                pass
        raise

    elapsed_ms = (time.time() - started_at) * 1000
    _langfuse_generation(
        lf_trace, name="completion", model=resp.model,
        input_data=user_message[:500],
        output_data=resp.text[:500],
        usage={"input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens},
        metadata={
            "tier": tier.value, "provider": resp.provider.value,
            "cost_usd": resp.cost_usd, "elapsed_ms": round(elapsed_ms),
        },
    )

    return resp


# ---------------------------------------------------------------------------
# Provider-specific implementations
# ---------------------------------------------------------------------------

def _get_api_key(provider: Provider) -> str:
    _ensure_dotenv()
    env_map = {
        Provider.DEEPSEEK: "DEEPSEEK_API_KEY",
        Provider.ANTHROPIC: "ANTHROPIC_API_KEY",
        Provider.LITELLM: "LITELLM_MASTER_KEY",
        Provider.OPENROUTER: "OPENROUTER_API_KEY",
    }
    return os.environ.get(env_map[provider], "").strip()


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    inp_rate, out_rate = _COST_1M.get(model, (0, 0))
    return (input_tokens / 1_000_000 * inp_rate) + (output_tokens / 1_000_000 * out_rate)


def _call_deepseek(
    *, model: str, system_prompt: str, user_message: str,
    max_tokens: int, temperature: float, tier: ModelTier | None = None,
) -> LLMResponse:
    """Call DeepSeek via OpenAI-compatible API with thinking mode control.

    - V4 Pro: thinking ON by default, temperature=1.0, min 1024 tokens
    - V4 Flash SONNET/REASONER: thinking ON by default
    - V4 Flash HAIKU: thinking DISABLED for fastest/cheapest output
    """
    from openai import OpenAI

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=60.0)

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})

    extra_kw: dict[str, Any] = {}

    if model == "deepseek-v4-pro":
        temperature = 1.0
        max_tokens = max(max_tokens, 1024)

    if model == "deepseek-v4-flash":
        if tier is not None and tier == ModelTier.HAIKU:
            extra_kw["thinking"] = {"type": "disabled"}
            temperature = max(temperature, 0.0)

    resp = client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens,
        temperature=temperature, **extra_kw,
    )

    choice = resp.choices[0]
    text = choice.message.content or ""
    reasoning = getattr(choice.message, "reasoning_content", "") or ""

    in_tok = resp.usage.prompt_tokens if resp.usage else 0
    out_tok = resp.usage.completion_tokens if resp.usage else 0

    return LLMResponse(
        text=text.strip(), model=model, provider=Provider.DEEPSEEK,
        input_tokens=in_tok, output_tokens=out_tok,
        cost_usd=_cost(model, in_tok, out_tok),
        reasoning=reasoning,
    )


def _call_anthropic(
    *, model: str, system_prompt: str, user_message: str,
    max_tokens: int, temperature: float,
) -> LLMResponse:
    """Call Anthropic Claude via the official SDK."""
    from anthropic import Anthropic

    client = Anthropic(timeout=30.0)
    kwargs: dict[str, Any] = {
        "model": model, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_message}],
    }
    if system_prompt:
        kwargs["system"] = [{"type": "text", "text": system_prompt}]

    resp = client.messages.create(**kwargs)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    return LLMResponse(
        text=text.strip(), model=model, provider=Provider.ANTHROPIC,
        input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
        cost_usd=_cost(model, resp.usage.input_tokens, resp.usage.output_tokens),
    )


def _call_litellm(
    *, model: str, system_prompt: str, user_message: str,
    max_tokens: int, temperature: float,
) -> LLMResponse:
    """Call via LiteLLM — OpenAI-format API with automatic provider failover."""
    import litellm

    litellm.master_key = _get_api_key(Provider.LITELLM)
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})

    resp = litellm.completion(
        model=model, messages=messages, max_tokens=max_tokens, temperature=temperature,
    )
    choice = resp.choices[0]
    text = choice.message.content or ""
    reasoning = getattr(choice.message, "reasoning_content", "") or ""

    in_tok = resp.usage.prompt_tokens if resp.usage else 0
    out_tok = resp.usage.completion_tokens if resp.usage else 0
    actual_model = getattr(resp, "model", model) or model

    return LLMResponse(
        text=text.strip(), model=actual_model, provider=Provider.LITELLM,
        input_tokens=in_tok, output_tokens=out_tok,
        cost_usd=_cost(model, in_tok, out_tok),
        reasoning=reasoning,
    )


def _call_openrouter(
    *, model: str, system_prompt: str, user_message: str,
    max_tokens: int, temperature: float,
) -> LLMResponse:
    """Call via OpenRouter — real-time cheapest provider routing."""
    from openai import OpenAI

    api_key = _get_api_key(Provider.OPENROUTER)
    client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1", timeout=60.0)

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})

    resp = client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens, temperature=temperature,
    )

    choice = resp.choices[0]
    text = choice.message.content or ""

    in_tok = resp.usage.prompt_tokens if resp.usage else 0
    out_tok = resp.usage.completion_tokens if resp.usage else 0
    actual_model = getattr(resp, "model", model) or model

    return LLMResponse(
        text=text.strip(), model=actual_model, provider=Provider.OPENROUTER,
        input_tokens=in_tok, output_tokens=out_tok,
        cost_usd=_cost(model, in_tok, out_tok),
    )


# ---------------------------------------------------------------------------
# DeepSeekExecutor — plugs into Avengers Fleet as the Executor callable
# ---------------------------------------------------------------------------

class DeepSeekExecutor:
    """Implements the Avengers ``Executor`` Protocol using the provider layer."""

    def __call__(
        self, *, tier, system_prompt: str = "", user_prompt: str = "", envelope: Any = None,
    ) -> str:
        tier_value = tier.value if hasattr(tier, "value") else str(tier)
        ds_tier = ModelTier(tier_value)

        resp = chat_completion(
            tier=ds_tier, system_prompt=system_prompt, user_message=user_prompt,
            max_tokens=1200 if ds_tier in (ModelTier.OPUS, ModelTier.REASONER) else 800,
            temperature=0.3 if ds_tier in (ModelTier.OPUS, ModelTier.REASONER) else 0.7,
        )
        if resp.reasoning:
            logger.debug("%s reasoning (%d chars): %s...",
                         resp.model, len(resp.reasoning), resp.reasoning[:200])
        return resp.text


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def provider_name() -> str:
    return _default_provider().value


def model_for_tier(tier: ModelTier, provider: Provider | None = None) -> str:
    if provider is None:
        provider = _default_provider()
    return _TIER_MODEL.get((tier, provider), "unknown")


def native_provider_info() -> dict[str, Any]:
    """Return a dict suitable for logging / status dashboards."""
    prov = _default_provider()
    tiers = [ModelTier.OPUS, ModelTier.SONNET, ModelTier.HAIKU, ModelTier.REASONER]
    return {
        "provider": prov.value,
        "models": {t.value: model_for_tier(t, prov) for t in tiers},
        "cost_ratios": {f"{t.value}_{prov.value}": COST_RATIO.get((t, prov), 0.0)
                        for t in tiers},
        "deepseek_key_configured": bool(_get_api_key(Provider.DEEPSEEK)),
        "anthropic_key_configured": bool(_get_api_key(Provider.ANTHROPIC)),
        "litellm_key_configured": bool(_get_api_key(Provider.LITELLM)),
        "openrouter_key_configured": bool(_get_api_key(Provider.OPENROUTER)),
        "langfuse_enabled": _langfuse_available(),
    }

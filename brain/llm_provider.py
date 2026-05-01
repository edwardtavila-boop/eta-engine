"""
LLM provider abstraction — DeepSeek is the native default.
Anthropic/Claude is retained as a fallback only.

Architecture
============
Every LLM call flows through ``chat_completion(tier=...)``.
The provider is selected ONCE at startup via ``_default_provider()``:

  ETA_LLM_PROVIDER=deepseek   → DeepSeek (native, default)
  ETA_LLM_PROVIDER=anthropic  → Anthropic (legacy fallback)
  (auto)                      → DeepSeek if DEEPSEEK_API_KEY set, else Anthropic

Tier → Model mapping
====================
  OPUS   (adversarial reasoning)   → DeepSeek-R1 (deepseek-reasoner)  $0.55/1M
  SONNET (routine development)     → DeepSeek-V3 (deepseek-chat)      $0.27/1M
  HAIKU  (mechanical grunt work)   → DeepSeek-V3 (deepseek-chat)      $0.27/1M

Agents & their native models
=============================
  JARVIS  — deterministic admin (no LLM)
  BATMAN  — DeepSeek-R1 (OPUS tier)  — architectural / adversarial
  ALFRED  — DeepSeek-V3 (SONNET tier) — routine / documentation
  ROBIN   — DeepSeek-V3 (HAIKU tier)  — grunt / log parsing

Pricing (per 1M tokens, USD)
=============================
  DeepSeek-V3    $0.27  in / $1.10  out  (≈ 0.09× Claude Sonnet)
  DeepSeek-R1    $0.55  in / $2.19  out  (≈ 0.18×)
  Claude Haiku   $0.80  in / $4.00  out  (≈ 0.27×)
  Claude Sonnet  $3.00  in / $15.00 out  (baseline 1.00×)
  Claude Opus    $15.00 in / $75.00 out  (≈ 5.00×)
"""

from __future__ import annotations

import logging
import os
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


class ModelTier(StrEnum):
    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"


# ---------------------------------------------------------------------------
# Tier → model mapping (single source of truth)
# ---------------------------------------------------------------------------

_TIER_MODEL: dict[tuple[ModelTier, Provider], str] = {
    (ModelTier.OPUS,   Provider.DEEPSEEK):  "deepseek-reasoner",
    (ModelTier.SONNET, Provider.DEEPSEEK):  "deepseek-chat",
    (ModelTier.HAIKU,  Provider.DEEPSEEK):  "deepseek-chat",
    (ModelTier.OPUS,   Provider.ANTHROPIC): "claude-opus-4-7-20250601",
    (ModelTier.SONNET, Provider.ANTHROPIC): "claude-sonnet-4-5-20250929",
    (ModelTier.HAIKU,  Provider.ANTHROPIC): "claude-haiku-4-5-20251001",
}


_COST_1M: dict[str, tuple[float, float]] = {
    "deepseek-reasoner":          (0.55, 2.19),
    "deepseek-chat":              (0.27, 1.10),
    "claude-opus-4-7-20250601":   (15.00, 75.00),
    "claude-sonnet-4-5-20250929": (3.00, 15.00),
    "claude-haiku-4-5-20251001":  (0.80, 4.00),
}

_SONNET_BASELINE = 3.00  # Claude Sonnet input cost per 1M

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
    reasoning: str = ""  # DeepSeek-R1 thinking trace


# ---------------------------------------------------------------------------
# Provider selection — DeepSeek is the native default
# ---------------------------------------------------------------------------

def _default_provider() -> Provider:
    """DeepSeek is the native default. Anthropic is fallback only.

    Priority:
      1. ETA_LLM_PROVIDER=anthropic  → explicit Anthropic override
      2. Everything else             → DeepSeek (auto-detect key OR not)
    """
    _ensure_dotenv()
    explicit = os.environ.get("ETA_LLM_PROVIDER", "").strip().lower()
    if explicit in ("anthropic", "claude", "ant"):
        return Provider.ANTHROPIC

    # DeepSeek is native: try key, then fall back to Anthropic only if
    # DeepSeek key is missing AND Anthropic key is present.
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return Provider.DEEPSEEK
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        logger.warning("No DEEPSEEK_API_KEY — falling back to Anthropic")
        return Provider.ANTHROPIC
    # Neither key — return DeepSeek anyway; chat_completion will return empty
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
    """Single-entry chat completion. DeepSeek-native, Anthropic as fallback.

    Tier routing:
      OPUS   → DeepSeek-R1 (deepseek-reasoner) — reasoning model
      SONNET → DeepSeek-V3 (deepseek-chat)     — general purpose
      HAIKU  → DeepSeek-V3 (deepseek-chat)     — cost floor

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
        # Auto-fallback: try the other provider
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

    if provider == Provider.DEEPSEEK:
        return _call_deepseek(model=model, system_prompt=system_prompt,
                              user_message=user_message, max_tokens=max_tokens,
                              temperature=temperature)
    return _call_anthropic(model=model, system_prompt=system_prompt,
                           user_message=user_message, max_tokens=max_tokens,
                           temperature=temperature)


# ---------------------------------------------------------------------------
# Provider-specific implementations
# ---------------------------------------------------------------------------

def _get_api_key(provider: Provider) -> str:
    _ensure_dotenv()
    env_map = {Provider.DEEPSEEK: "DEEPSEEK_API_KEY", Provider.ANTHROPIC: "ANTHROPIC_API_KEY"}
    return os.environ.get(env_map[provider], "").strip()


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    inp_rate, out_rate = _COST_1M.get(model, (0, 0))
    return (input_tokens / 1_000_000 * inp_rate) + (output_tokens / 1_000_000 * out_rate)


def _call_deepseek(
    *, model: str, system_prompt: str, user_message: str,
    max_tokens: int, temperature: float,
) -> LLMResponse:
    """Call DeepSeek via OpenAI-compatible API. Handles R1 reasoning_content."""
    from openai import OpenAI

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=60.0)

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})

    # DeepSeek-R1 needs temperature fixed at 1.0 and higher max_tokens
    # because the reasoning chain consumes part of the budget.
    if model == "deepseek-reasoner":
        temperature = 1.0
        max_tokens = max(max_tokens, 1024)  # ensure room for reasoning + answer

    resp = client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens, temperature=temperature,
    )

    choice = resp.choices[0]
    text = choice.message.content or ""

    # Extract R1 reasoning trace if present
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


# ---------------------------------------------------------------------------
# DeepSeekExecutor — plugs into Avengers Fleet as the Executor callable
# ---------------------------------------------------------------------------

class DeepSeekExecutor:
    """Implements the Avengers ``Executor`` Protocol using DeepSeek-native API.

    Usage::

        from eta_engine.brain.llm_provider import DeepSeekExecutor
        fleet = Fleet(executor=DeepSeekExecutor())
        daemon = AvengerDaemon(persona="batman", fleet=fleet)
    """

    def __call__(
        self,
        *,
        tier,            # model_policy.ModelTier (compatible by value)
        system_prompt: str = "",
        user_prompt: str = "",
        envelope: Any = None,  # TaskEnvelope — unused by provider
    ) -> str:
        # Convert to llm_provider.ModelTier by value
        tier_value = tier.value if hasattr(tier, "value") else str(tier)
        ds_tier = ModelTier(tier_value)

        resp = chat_completion(
            tier=ds_tier,
            system_prompt=system_prompt,
            user_message=user_prompt,
            max_tokens=1200 if ds_tier == ModelTier.OPUS else 800,
            temperature=0.3 if ds_tier == ModelTier.OPUS else 0.7,
        )
        if resp.reasoning:
            logger.debug("DeepSeek-R1 reasoning (%d chars): %s...",
                         len(resp.reasoning), resp.reasoning[:200])
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
    return {
        "provider": prov.value,
        "models": {
            "opus": model_for_tier(ModelTier.OPUS, prov),
            "sonnet": model_for_tier(ModelTier.SONNET, prov),
            "haiku": model_for_tier(ModelTier.HAIKU, prov),
        },
        "cost_ratios": {f"{t.value}_{prov.value}": COST_RATIO[(t, prov)]
                        for t in ModelTier},
        "deepseek_key_configured": bool(_get_api_key(Provider.DEEPSEEK)),
        "anthropic_key_configured": bool(_get_api_key(Provider.ANTHROPIC)),
    }

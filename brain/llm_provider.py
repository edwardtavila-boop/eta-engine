"""
LLM provider abstraction layer — single entry point for both Anthropic and
OpenAI-compatible (DeepSeek) backends.

Design goals:
  1. Swap providers by changing one env var (ETA_LLM_PROVIDER).
  2. DeepSeek V3/R1 pricing ~5–50× cheaper than Claude — the primary
     motivation for this module.
  3. All existing ModelTier → model name mappings live here.
  4. Call sites import one thing: ``chat_completion()``.
  5. Zero-cost fallback — no API key = deterministic template output.

Pricing (per 1M tokens, USD):
                   Input    Output
  DeepSeek-V3      $0.27    $1.10
  DeepSeek-R1      $0.55    $2.19
  Claude Haiku 4.5 $0.80    $4.00
  Claude Sonnet 4.5 $3.00   $15.00
  Claude Opus 4.7  $15.00   $75.00

Cost ratios (vs Claude Sonnet = 1.0x):
  DeepSeek-V3  ≈ 0.09×
  DeepSeek-R1  ≈ 0.18×
  Claude Haiku ≈ 0.27×
  Claude Sonnet = 1.00×
  Claude Opus  ≈ 5.00×
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Lazy-load .env so API keys are available even without manual dotenv setup.
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
# Tier → model mapping (single source of truth)
# ---------------------------------------------------------------------------

class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"


class ModelTier(StrEnum):
    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"


# Tier → provider → model name
_TIER_MODEL: dict[tuple[ModelTier, Provider], str] = {
    (ModelTier.OPUS,   Provider.DEEPSEEK):  "deepseek-reasoner",       # DeepSeek-R1
    (ModelTier.SONNET, Provider.DEEPSEEK):  "deepseek-chat",           # DeepSeek-V3
    (ModelTier.HAIKU,  Provider.DEEPSEEK):  "deepseek-chat",           # DeepSeek-V3 (cost floor)
    (ModelTier.OPUS,   Provider.ANTHROPIC): "claude-opus-4-7-20250601",
    (ModelTier.SONNET, Provider.ANTHROPIC): "claude-sonnet-4-5-20250929",
    (ModelTier.HAIKU,  Provider.ANTHROPIC): "claude-haiku-4-5-20251001",
}


# Per-1M-token costs: (input, output) USD
_COST_1M: dict[str, Tuple[float, float]] = {
    "deepseek-reasoner":          (0.55, 2.19),
    "deepseek-chat":              (0.27, 1.10),
    "claude-opus-4-7-20250601":   (15.00, 75.00),
    "claude-sonnet-4-5-20250929": (3.00, 15.00),
    "claude-haiku-4-5-20251001":  (0.80, 4.00),
}


# Cost ratio vs SONNET baseline = 1.0
_SONNET_BASELINE_COST = _COST_1M["claude-sonnet-4-5-20250929"][0]  # 3.00

COST_RATIO_PROVIDER: dict[tuple[int, str], float] = {}
for (tier, prov), model in _TIER_MODEL.items():
    inp, _ = _COST_1M.get(model, (0.0, 0.0))
    COST_RATIO_PROVIDER[(tier, prov)] = round(inp / _SONNET_BASELINE_COST, 3) if inp else 0.0


# ---------------------------------------------------------------------------
# Result dataclass
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


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

def _default_provider() -> Provider:
    """Choose the LLM provider based on ETA_LLM_PROVIDER env var.

    Priority:
      1. ETA_LLM_PROVIDER=deepseek  → DeepSeek (OpenAI-compatible)
      2. ETA_LLM_PROVIDER=anthropic → Anthropic/Claude (legacy)
      3. Auto-detect: DEEPSEEK_API_KEY present → DeepSeek, else Anthropic
    """
    _ensure_dotenv()
    explicit = os.environ.get("ETA_LLM_PROVIDER", "").strip().lower()
    if explicit in ("deepseek", "ds", "deepseek-v3", "deepseek-v4"):
        return Provider.DEEPSEEK
    if explicit in ("anthropic", "claude", "ant"):
        return Provider.ANTHROPIC

    # Auto-detect: prefer DeepSeek if key present
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return Provider.DEEPSEEK
    return Provider.ANTHROPIC


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
    provider: Optional[Provider] = None,
    fallback_to_anthropic: bool = False,
) -> LLMResponse:
    """Single-entry chat completion across DeepSeek and Anthropic.

    Tier routing:
      OPUS   → DeepSeek-R1 (deepseek-reasoner) or Claude Opus
      SONNET → DeepSeek-V3 (deepseek-chat)    or Claude Sonnet
      HAIKU  → DeepSeek-V3 (deepseek-chat)    or Claude Haiku

    If DeepSeek is unavailable and fallback_to_anthropic is True,
    falls back to Anthropic. Otherwise returns an empty response
    with cached=False.
    """
    if provider is None:
        provider = _default_provider()

    model = _TIER_MODEL.get((tier, provider))
    if model is None:
        logger.warning("No model for tier=%s provider=%s", tier, provider)
        return LLMResponse(text="", cached=False)

    api_key = _get_api_key(provider)
    if not api_key:
        if fallback_to_anthropic and provider != Provider.ANTHROPIC:
            logger.info("Falling back to Anthropic (no DeepSeek key)")
            return chat_completion(
                tier=tier,
                system_prompt=system_prompt,
                user_message=user_message,
                max_tokens=max_tokens,
                temperature=temperature,
                provider=Provider.ANTHROPIC,
                fallback_to_anthropic=False,
            )
        logger.warning("No API key for provider=%s", provider)
        return LLMResponse(text="", cached=False)

    if provider == Provider.DEEPSEEK:
        return _call_deepseek(
            model=model,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    return _call_anthropic(
        model=model,
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=max_tokens,
        temperature=temperature,
    )


# ---------------------------------------------------------------------------
# Provider-specific implementation
# ---------------------------------------------------------------------------

def _get_api_key(provider: Provider) -> str:
    _ensure_dotenv()
    env_map = {
        Provider.DEEPSEEK:  "DEEPSEEK_API_KEY",
        Provider.ANTHROPIC: "ANTHROPIC_API_KEY",
    }
    return os.environ.get(env_map[provider], "").strip()


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    inp_rate, out_rate = _COST_1M.get(model, (0, 0))
    return (input_tokens / 1_000_000 * inp_rate) + (output_tokens / 1_000_000 * out_rate)


def _call_deepseek(
    *,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> LLMResponse:
    """Call DeepSeek via OpenAI-compatible /v1/chat/completions."""
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        base_url="https://api.deepseek.com",
        timeout=30.0,
    )
    messages: List[Dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})

    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    text = resp.choices[0].message.content or ""
    in_tok = resp.usage.prompt_tokens if resp.usage else 0
    out_tok = resp.usage.completion_tokens if resp.usage else 0

    return LLMResponse(
        text=text.strip(),
        model=model,
        provider=Provider.DEEPSEEK,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=_cost(model, in_tok, out_tok),
    )


def _call_anthropic(
    *,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> LLMResponse:
    """Call Anthropic Claude via the official SDK."""
    from anthropic import Anthropic

    client = Anthropic(timeout=30.0)
    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_message}],
    }
    if system_prompt:
        kwargs["system"] = [{"type": "text", "text": system_prompt}]

    resp = client.messages.create(**kwargs)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    in_tok = resp.usage.input_tokens
    out_tok = resp.usage.output_tokens

    return LLMResponse(
        text=text.strip(),
        model=model,
        provider=Provider.ANTHROPIC,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=_cost(model, in_tok, out_tok),
    )


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def provider_name() -> str:
    """Return the active provider name for logging/monitoring."""
    return _default_provider().value


def model_for_tier(tier: ModelTier, provider: Optional[Provider] = None) -> str:
    """Return the concrete model name for a given tier and provider."""
    if provider is None:
        provider = _default_provider()
    return _TIER_MODEL.get((tier, provider), "unknown")

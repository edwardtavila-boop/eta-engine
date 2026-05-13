"""
LLM provider abstraction - DeepSeek V4 native default.

Operator policy:
  * DeepSeek V4 is the only paid API lane.
  * Codex subscription CLI powers architect/review/admin work outside this file.
  * Anthropic, OpenAI API, LiteLLM, and OpenRouter paths are legacy enum values
    only; ``chat_completion`` blocks them before any network call.

Langfuse traces DeepSeek calls for observability.

Architecture
============
  ETA_LLM_PROVIDER=deepseek   -> DeepSeek V4 (native, default)
  any other value             -> ignored and blocked by operator policy

Tier → Model mapping (DeepSeek V4)
==================================
  OPUS     -> deepseek-v4-pro    (thinking)  - architectural / adversarial
  SONNET   -> deepseek-v4-flash  (thinking)  - routine reasoning
  HAIKU    -> deepseek-v4-flash  (non-think) - grunt work / cost floor
  REASONER -> deepseek-v4-flash  (thinking)  - chain-of-thought

Agent assignments
=================
  BATMAN -> V4 Pro     - adversarial review, Red Team scoring
  ALFRED -> V4 Flash   - documentation, code review
  ROBIN  -> V4 Flash   - log parsing, formatting (non-thinking)

Pricing (per 1M tokens, USD)
=============================
  DeepSeek V4 Flash  $0.14  in / $0.28  out
  DeepSeek V4 Pro    $0.435 in / $0.87  out  (75% discount until 2026-05-31)
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ENV_LOADED = False
_ENV_LOAD_LOCK = threading.Lock()


def _ensure_dotenv() -> None:
    """Load .env files into the process environment.

    Order (first wins, never overrides parent process env):
      1. Real env vars set by the parent (systemd, CI, ``export``)
      2. ``Path.cwd() / .env`` (workspace root when run from there)
      3. ``parents[2] / .env`` (workspace root, regardless of cwd)
      4. ``parents[1] / .env`` (``eta_engine/.env`` — submodule-local)

    ``override=False`` is intentional: in production the parent process
    (systemd, container env, CI secret manager) is the canonical source of
    truth for credentials. .env files are only a developer convenience and
    must NEVER stomp env vars set by the operator.

    Idempotent + thread-safe via ``_ENV_LOAD_LOCK`` to prevent two threads
    from racing through the loader on first call.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    with _ENV_LOAD_LOCK:
        if _ENV_LOADED:  # double-check after acquiring lock
            return
        try:
            from dotenv import load_dotenv

            here = Path(__file__).resolve()
            seen: set[str] = set()
            for candidate in (
                Path.cwd() / ".env",
                here.parents[2] / ".env",  # workspace root
                here.parents[1] / ".env",  # eta_engine/.env (submodule-local)
            ):
                key = str(candidate.resolve()) if candidate.exists() else ""
                if not key or key in seen:
                    continue
                seen.add(key)
                if candidate.is_file():
                    load_dotenv(dotenv_path=str(candidate), override=False)
            _ENV_LOADED = True
        except ImportError:
            _ENV_LOADED = True  # avoid retry storm if python-dotenv missing


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Provider(StrEnum):
    DEEPSEEK = "deepseek"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
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
    (ModelTier.OPUS, Provider.DEEPSEEK): "deepseek-v4-pro",
    (ModelTier.SONNET, Provider.DEEPSEEK): "deepseek-v4-flash",
    (ModelTier.HAIKU, Provider.DEEPSEEK): "deepseek-v4-flash",
    (ModelTier.REASONER, Provider.DEEPSEEK): "deepseek-v4-flash",
    # Anthropic fallback
    (ModelTier.OPUS, Provider.ANTHROPIC): "claude-opus-4-7-20250601",
    (ModelTier.SONNET, Provider.ANTHROPIC): "claude-sonnet-4-5-20250929",
    (ModelTier.HAIKU, Provider.ANTHROPIC): "claude-haiku-4-5-20251001",
    (ModelTier.REASONER, Provider.ANTHROPIC): "claude-sonnet-4-5-20250929",
    # LiteLLM unified
    (ModelTier.OPUS, Provider.LITELLM): "deepseek/deepseek-v4-pro",
    (ModelTier.SONNET, Provider.LITELLM): "deepseek/deepseek-v4-flash",
    (ModelTier.HAIKU, Provider.LITELLM): "deepseek/deepseek-v4-flash",
    (ModelTier.REASONER, Provider.LITELLM): "deepseek/deepseek-v4-flash",
    # OpenRouter cheapest-auto
    (ModelTier.OPUS, Provider.OPENROUTER): "openrouter/deepseek/deepseek-v4-pro",
    (ModelTier.SONNET, Provider.OPENROUTER): "openrouter/deepseek/deepseek-v4-flash",
    (ModelTier.HAIKU, Provider.OPENROUTER): "openrouter/deepseek/deepseek-v4-flash",
    (ModelTier.REASONER, Provider.OPENROUTER): "openrouter/deepseek/deepseek-v4-flash",
    # OpenAI (API-based, separate from Codex CLI)
    (ModelTier.OPUS, Provider.OPENAI): "gpt-5",
    (ModelTier.SONNET, Provider.OPENAI): "gpt-4o",
    (ModelTier.HAIKU, Provider.OPENAI): "gpt-4o-mini",
    (ModelTier.REASONER, Provider.OPENAI): "o3",
}

_COST_1M: dict[str, tuple[float, float]] = {
    # DeepSeek V4
    "deepseek-v4-pro": (0.435, 0.87),
    "deepseek-v4-flash": (0.14, 0.28),
    # Claude (legacy)
    "claude-opus-4-7-20250601": (15.00, 75.00),
    "claude-sonnet-4-5-20250929": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    # LiteLLM prefixed
    "deepseek/deepseek-v4-pro": (0.435, 0.87),
    "deepseek/deepseek-v4-flash": (0.14, 0.28),
    "anthropic/claude-opus-4-7-20250601": (15.00, 75.00),
    "anthropic/claude-sonnet-4-5-20250929": (3.00, 15.00),
    # OpenRouter prefixed
    "openrouter/deepseek/deepseek-v4-pro": (0.435, 0.87),
    "openrouter/deepseek/deepseek-v4-flash": (0.14, 0.28),
    "openrouter/anthropic/claude-opus-4-7": (15.00, 75.00),
    "openrouter/anthropic/claude-sonnet-4-5": (3.00, 15.00),
    # OpenAI
    "gpt-5": (1.25, 10.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "o3": (1.10, 4.40),
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


def _langfuse_trace(name: str, metadata: dict[str, Any]) -> Any:  # noqa: ANN401  (3rd-party return type)
    """Create a Langfuse trace that auto-closes. Returns None if unavailable."""
    try:
        from langfuse import Langfuse

        trace = Langfuse().trace(name=name, metadata=metadata)
        return trace
    except Exception:  # noqa: BLE001
        return None


def _langfuse_generation(
    trace: object,
    name: str,
    model: str,
    input_data: str,  # noqa: ANN401  (3rd-party arg)
    output_data: str,
    usage: dict[str, int],
    metadata: dict[str, Any] | None = None,
) -> None:
    """Log a generation span to Langfuse. Silent no-op when unavailable."""
    if trace is None:
        return
    with contextlib.suppress(Exception):
        trace.generation(
            name=name,
            model=model,
            input={"messages": input_data},
            output={"text": output_data},
            usage={"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0)},
            metadata=metadata or {},
        )


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def _default_provider() -> Provider:
    """Return the only API provider currently allowed by operator policy."""
    _ensure_dotenv()
    explicit = os.environ.get("ETA_LLM_PROVIDER", "").strip().lower()
    if explicit and explicit not in {"deepseek", "ds"}:
        logger.warning(
            "ETA_LLM_PROVIDER=%s ignored: non-DeepSeek API providers are blocked by operator policy",
            explicit,
        )
    return Provider.DEEPSEEK


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def chat_completion(
    *,
    tier: ModelTier = ModelTier.HAIKU,
    system_prompt: str = "",
    user_message: str,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    provider: Provider | None = None,
) -> LLMResponse:
    """Single-entry chat completion with Langfuse observability.

    Tier routing:
      OPUS     → DeepSeek V4 Pro     (thinking)  — architectural / adversarial
      SONNET   → DeepSeek V4 Flash   (thinking)  — routine reasoning
      HAIKU    → DeepSeek V4 Flash   (non-think) — grunt / cost floor
      REASONER → DeepSeek V4 Flash   (thinking)  — chain-of-thought

    Returns empty ``LLMResponse(text="")`` if no DeepSeek API key is configured.
    Non-DeepSeek providers return a blocked response before any network call.
    """
    if provider is None:
        provider = _default_provider()

    if provider != Provider.DEEPSEEK:
        logger.warning(
            "Blocked %s API completion: operator policy allows only DeepSeek V4 API usage",
            provider.value,
        )
        return LLMResponse(
            text="",
            model=_TIER_MODEL.get((tier, provider), ""),
            provider=provider,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            reasoning="blocked_by_operator_policy",
        )

    model = _TIER_MODEL.get((tier, provider))
    if model is None:
        logger.warning("No model for tier=%s provider=%s", tier, provider)
        return LLMResponse(text="")

    api_key = _get_api_key(provider)
    if not api_key:
        logger.warning("No DEEPSEEK_API_KEY configured; refusing non-DeepSeek API fallback")
        return LLMResponse(text="")

    started_at = time.time()
    lf_trace = _langfuse_trace(
        f"completion_{tier.value}_{provider.value}",
        {"tier": tier.value, "provider": provider.value, "model": model},
    )

    try:
        if provider == Provider.DEEPSEEK:
            resp = _call_deepseek(
                model=model,
                system_prompt=system_prompt,
                user_message=user_message,
                max_tokens=max_tokens,
                temperature=temperature,
                tier=tier,
            )
        elif provider == Provider.LITELLM:
            resp = _call_litellm(
                model=model,
                system_prompt=system_prompt,
                user_message=user_message,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        elif provider == Provider.OPENROUTER:
            resp = _call_openrouter(
                model=model,
                system_prompt=system_prompt,
                user_message=user_message,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        else:
            resp = _call_anthropic(
                model=model,
                system_prompt=system_prompt,
                user_message=user_message,
                max_tokens=max_tokens,
                temperature=temperature,
            )
    except Exception:
        if lf_trace is not None:
            with contextlib.suppress(Exception):
                lf_trace.generation(
                    name="completion_error",
                    model=model,
                    input={"messages": user_message[:200]},
                    output={"error": "provider failed"},
                )
        raise

    elapsed_ms = (time.time() - started_at) * 1000
    _langfuse_generation(
        lf_trace,
        name="completion",
        model=resp.model,
        input_data=user_message[:500],
        output_data=resp.text[:500],
        usage={"input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens},
        metadata={
            "tier": tier.value,
            "provider": resp.provider.value,
            "cost_usd": resp.cost_usd,
            "elapsed_ms": round(elapsed_ms),
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
        Provider.OPENAI: "OPENAI_API_KEY",
        Provider.LITELLM: "LITELLM_MASTER_KEY",
        Provider.OPENROUTER: "OPENROUTER_API_KEY",
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
    tier: ModelTier | None = None,
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

    if model == "deepseek-v4-flash" and tier is not None and tier == ModelTier.HAIKU:
        extra_kw["thinking"] = {"type": "disabled"}
        temperature = max(temperature, 0.0)

    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        extra_body=extra_kw if extra_kw else None,
    )

    choice = resp.choices[0]
    text = choice.message.content or ""
    reasoning = getattr(choice.message, "reasoning_content", "") or ""

    in_tok = resp.usage.prompt_tokens if resp.usage else 0
    out_tok = resp.usage.completion_tokens if resp.usage else 0

    return LLMResponse(
        text=text.strip(),
        model=model,
        provider=Provider.DEEPSEEK,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=_cost(model, in_tok, out_tok),
        reasoning=reasoning,
    )


def _call_anthropic(
    *,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> LLMResponse:
    """Blocked legacy Anthropic adapter.

    Kept only so older imports fail closed instead of resurrecting paid
    Anthropic API usage.
    """
    _ = (system_prompt, user_message, max_tokens, temperature)
    return LLMResponse(
        text="",
        model=model,
        provider=Provider.ANTHROPIC,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        reasoning="blocked_by_operator_policy",
    )


def _call_litellm(
    *,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> LLMResponse:
    """Blocked legacy LiteLLM adapter; DeepSeek native is the only API lane."""
    _ = (system_prompt, user_message, max_tokens, temperature)
    return LLMResponse(
        text="",
        model=model,
        provider=Provider.LITELLM,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        reasoning="blocked_by_operator_policy",
    )


def _call_openrouter(
    *,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> LLMResponse:
    """Blocked legacy OpenRouter adapter; DeepSeek native is the only API lane."""
    _ = (system_prompt, user_message, max_tokens, temperature)
    return LLMResponse(
        text="",
        model=model,
        provider=Provider.OPENROUTER,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        reasoning="blocked_by_operator_policy",
    )


# ---------------------------------------------------------------------------
# DeepSeekExecutor — plugs into Avengers Fleet as the Executor callable
# ---------------------------------------------------------------------------


class DeepSeekExecutor:
    """Implements the Avengers ``Executor`` Protocol using the provider layer."""

    def __call__(
        self,
        *,
        tier: ModelTier | str,
        system_prompt: str = "",
        user_prompt: str = "",
        envelope: Any = None,  # noqa: ANN401  (avengers-protocol allows arbitrary envelope)
    ) -> str:
        tier_value = tier.value if hasattr(tier, "value") else str(tier)
        ds_tier = ModelTier(tier_value)

        resp = chat_completion(
            tier=ds_tier,
            system_prompt=system_prompt,
            user_message=user_prompt,
            max_tokens=1200 if ds_tier in (ModelTier.OPUS, ModelTier.REASONER) else 800,
            temperature=0.3 if ds_tier in (ModelTier.OPUS, ModelTier.REASONER) else 0.7,
        )
        if resp.reasoning:
            logger.debug("%s reasoning (%d chars): %s...", resp.model, len(resp.reasoning), resp.reasoning[:200])
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
        "cost_ratios": {f"{t.value}_{prov.value}": COST_RATIO.get((t, prov), 0.0) for t in tiers},
        "deepseek_key_configured": bool(_get_api_key(Provider.DEEPSEEK)),
        "anthropic_key_configured": bool(_get_api_key(Provider.ANTHROPIC)),
        "openai_key_configured": bool(_get_api_key(Provider.OPENAI)),
        "litellm_key_configured": bool(_get_api_key(Provider.LITELLM)),
        "openrouter_key_configured": bool(_get_api_key(Provider.OPENROUTER)),
        "non_deepseek_api_blocked": True,
        "langfuse_enabled": _langfuse_available(),
    }

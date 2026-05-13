"""
Multi-Model Force Multiplier Orchestrator (Wave-19).

Routes every task to the best allowed provider:

  CODEX    (Lead Architect / Systems Expert) - planning, review, debug, security
  DEEPSEEK (Worker Bee)                      - high-volume generation and grunt work

Operator policy:
  * Codex uses the existing subscription CLI, not API billing.
  * DeepSeek V4 is the only paid API lane.
  * Claude/Anthropic API usage is disabled; legacy Claude routes fall forward
    into Codex first, then DeepSeek if Codex is unavailable.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from eta_engine.brain.cli_provider import (
    CLIResponse,
    call_claude,
    call_codex,
    check_codex_available,
    cli_provider_status,
)
from eta_engine.brain.llm_provider import (
    LLMResponse,
    ModelTier,
    Provider,
    chat_completion,
    native_provider_info,
)
from eta_engine.brain.model_policy import (
    ForceProvider,
    TaskCategory,
    force_provider_for,
    select_model,
)
from eta_engine.brain.multi_model_telemetry import log_call, new_chain_id

logger = logging.getLogger(__name__)


def _telemetry_record(
    *,
    kind: str,
    category: TaskCategory,
    preferred: ForceProvider,
    response: MultiModelResponse,
    stage: str | None = None,
    chain_id: str | None = None,
) -> dict[str, Any]:
    """Build the JSON record written to ``state/force_multiplier_calls.jsonl``."""
    return {
        "kind": kind,
        "category": category.value,
        "preferred_provider": preferred.value,
        "actual_provider": response.provider.value,
        "tier": response.tier.value if response.tier else None,
        "model": response.model,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cost_usd": round(response.cost_usd, 6),
        "elapsed_ms": round(response.elapsed_ms, 1),
        "fallback_used": response.fallback_used,
        "fallback_reason": response.fallback_reason or "",
        "stage": stage,
        "chain_id": chain_id,
    }


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


# ---------------------------------------------------------------------------
# Cache layer (wave-25c 2026-05-13)
# ---------------------------------------------------------------------------
# Short-TTL LRU cache keyed on the call signature. Same prompt called twice
# within the TTL window returns the cached response without hitting the
# provider. This dedupes the per-tick narrative-call storm (each supervisor
# tick can emit 17+ identical doc_writing prompts for the same bar).
#
# Disable via ETA_FM_CACHE=0. TTL via ETA_FM_CACHE_TTL_SECONDS (default 300).
# LRU size via ETA_FM_CACHE_MAX (default 256 entries, ~5KB each).

import hashlib as _hashlib  # noqa: E402
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402
from collections import OrderedDict as _OrderedDict  # noqa: E402

_FM_CACHE: "_OrderedDict[str, tuple[float, MultiModelResponse]]" = _OrderedDict()
_FM_CACHE_LOCK = _threading.Lock()
_FM_CACHE_HITS = 0
_FM_CACHE_MISSES = 0


def _fm_cache_enabled() -> bool:
    return os.environ.get("ETA_FM_CACHE", "1").strip() != "0"


def _fm_cache_ttl_seconds() -> float:
    try:
        return float(os.environ.get("ETA_FM_CACHE_TTL_SECONDS", "300"))
    except ValueError:
        return 300.0


def _fm_cache_max() -> int:
    try:
        return int(os.environ.get("ETA_FM_CACHE_MAX", "256"))
    except ValueError:
        return 256


def _fm_cache_negative_ttl_seconds() -> float:
    """Shorter TTL for empty/negative responses. DeepSeek occasionally returns
    empty completions on certain narrative prompts; caching the empty result
    for a short window avoids re-paying the same FM cost while still allowing
    a retry sooner than the positive-TTL window. Default 60s.
    """
    try:
        return float(os.environ.get("ETA_FM_CACHE_NEGATIVE_TTL_SECONDS", "60"))
    except ValueError:
        return 60.0


def _fm_cache_key(
    category: "TaskCategory",
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
    force_provider: "ForceProvider | None",
) -> str:
    payload = "\x1f".join(
        (
            getattr(category, "value", str(category)),
            system_prompt or "",
            user_message or "",
            str(int(max_tokens)),
            f"{float(temperature):.4f}",
            getattr(force_provider, "value", "") if force_provider is not None else "",
        ),
    )
    return _hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _fm_cache_get(key: str) -> "MultiModelResponse | None":
    """Return cached response if present and within TTL.

    Two TTLs apply: positive (full TTL, default 300s) for non-empty
    responses, and negative (shorter, default 60s) for empty completions.
    The shorter negative TTL keeps DeepSeek empty-response prompts from
    re-burning FM cost every tick while still allowing a retry sooner
    than the positive window.
    """
    global _FM_CACHE_HITS, _FM_CACHE_MISSES
    pos_ttl = _fm_cache_ttl_seconds()
    neg_ttl = _fm_cache_negative_ttl_seconds()
    if pos_ttl <= 0 and neg_ttl <= 0:
        _FM_CACHE_MISSES += 1
        return None
    now = _time.time()
    with _FM_CACHE_LOCK:
        entry = _FM_CACHE.get(key)
        if entry is None:
            _FM_CACHE_MISSES += 1
            return None
        ts, response = entry
        is_empty = not bool((response.text or "").strip())
        applicable_ttl = neg_ttl if is_empty else pos_ttl
        if applicable_ttl <= 0 or (now - ts) > applicable_ttl:
            _FM_CACHE.pop(key, None)
            _FM_CACHE_MISSES += 1
            return None
        _FM_CACHE.move_to_end(key)
        _FM_CACHE_HITS += 1
        return response


def _fm_cache_put(key: str, response: "MultiModelResponse") -> None:
    cap = _fm_cache_max()
    if cap <= 0:
        return
    with _FM_CACHE_LOCK:
        _FM_CACHE[key] = (_time.time(), response)
        _FM_CACHE.move_to_end(key)
        while len(_FM_CACHE) > cap:
            _FM_CACHE.popitem(last=False)


def fm_cache_stats() -> dict[str, int]:
    """Operator-readable cache stats — for the FM-rollup script."""
    with _FM_CACHE_LOCK:
        size = len(_FM_CACHE)
    return {
        "size": size,
        "hits": _FM_CACHE_HITS,
        "misses": _FM_CACHE_MISSES,
        "ttl_seconds": int(_fm_cache_ttl_seconds()),
        "max_entries": _fm_cache_max(),
    }


# ---------------------------------------------------------------------------
# Daily-spend circuit breaker (wave-25c rev3 2026-05-13)
# ---------------------------------------------------------------------------
# Hard cap on cumulative FM cost per UTC day. When tripped, route_and_execute
# returns an empty MultiModelResponse so callers hit their template fallback
# path. Prevents a runaway integration or stuck loop from blowing the daily
# DeepSeek budget. Disable via ETA_FM_DAILY_CAP_USD=0. Default $5/day = ~10x
# observed paper-soak rate.

_FM_SPEND_LOCK = _threading.Lock()
_FM_SPEND_DATE: str = ""
_FM_SPEND_TODAY_USD: float = 0.0
_FM_BREAKER_TRIPPED: bool = False
_FM_BREAKER_TRIP_LOGGED: bool = False


def _fm_daily_cap_usd() -> float:
    """Per-UTC-day cumulative cost ceiling. 0 disables the breaker."""
    try:
        return float(os.environ.get("ETA_FM_DAILY_CAP_USD", "5.0"))
    except ValueError:
        return 5.0


def _fm_today_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _fm_breaker_check_then_record(cost_usd: float) -> bool:
    """Update the day's running total and decide whether the breaker is open.

    Returns True if the call should be ALLOWED, False if the breaker is
    tripped (caller should short-circuit to template). The cost_usd
    argument is the SPENT cost of the call that just completed. Pre-call
    checks read the running total without recording.
    """
    global _FM_SPEND_DATE, _FM_SPEND_TODAY_USD, _FM_BREAKER_TRIPPED, _FM_BREAKER_TRIP_LOGGED
    cap = _fm_daily_cap_usd()
    today = _fm_today_key()
    with _FM_SPEND_LOCK:
        if today != _FM_SPEND_DATE:
            # New UTC day — reset rolling counters
            _FM_SPEND_DATE = today
            _FM_SPEND_TODAY_USD = 0.0
            _FM_BREAKER_TRIPPED = False
            _FM_BREAKER_TRIP_LOGGED = False
        _FM_SPEND_TODAY_USD += float(cost_usd or 0.0)
        if cap > 0 and _FM_SPEND_TODAY_USD >= cap:
            _FM_BREAKER_TRIPPED = True
            if not _FM_BREAKER_TRIP_LOGGED:
                logger.warning(
                    "FM daily-cap breaker TRIPPED: spent $%.4f >= $%.2f cap; "
                    "subsequent calls will return empty MultiModelResponse "
                    "(callers fall back to template).",
                    _FM_SPEND_TODAY_USD,
                    cap,
                )
                _FM_BREAKER_TRIP_LOGGED = True
        return not _FM_BREAKER_TRIPPED


def _fm_breaker_open() -> bool:
    """Pre-call check: True if the breaker is currently tripped."""
    today = _fm_today_key()
    with _FM_SPEND_LOCK:
        if today != _FM_SPEND_DATE:
            # New day — implicit reset on next record
            return False
        return _FM_BREAKER_TRIPPED


def fm_breaker_stats() -> dict[str, Any]:
    """Operator-readable breaker state — surfaced via supervisor heartbeat."""
    with _FM_SPEND_LOCK:
        return {
            "date": _FM_SPEND_DATE,
            "spent_today_usd": round(_FM_SPEND_TODAY_USD, 4),
            "cap_usd": round(_fm_daily_cap_usd(), 2),
            "tripped": _FM_BREAKER_TRIPPED,
        }


def route_and_execute(
    *,
    category: TaskCategory,
    system_prompt: str = "",
    user_message: str,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    workspace: str | None = None,
    force_provider: ForceProvider | None = None,
    chain_id: str | None = None,
    chain_stage: str | None = None,
    max_cost_usd: float | None = None,
) -> MultiModelResponse:
    """Route a task to the best provider and execute.

    Provider selection (in priority order):
      1. ``force_provider`` override (caller's explicit choice)
      2. ``force_provider_for(category)`` policy lookup
      3. Falls back to DeepSeek if the chosen provider is unavailable

    Returns ``MultiModelResponse`` with ``fallback_used=True`` if the
    preferred provider was unavailable and DeepSeek handled the task.

    Telemetry: every call writes one record to
    ``var/eta_engine/state/multi_model_telemetry.jsonl`` (set
    ``ETA_FM_TELEMETRY=0`` to disable, ``ETA_FM_TELEMETRY_LOG=<path>`` to
    redirect). ``chain_id`` and ``chain_stage`` link calls made by
    ``force_multiplier_chain`` so a multi-stage run can be reconstructed
    from the log.

    Cache: identical calls (same category + prompts + max_tokens +
    temperature + force_provider) within ``ETA_FM_CACHE_TTL_SECONDS``
    (default 300) return the cached response without hitting the
    provider. Disable via ``ETA_FM_CACHE=0``. Cached responses are NOT
    re-logged to telemetry so the call-count stat in the jsonl reflects
    provider invocations, not request count.

    Budget: pass ``max_cost_usd`` to refuse a call whose worst-case spend
    (max_tokens × per-token rate at the resolved tier) would exceed the cap.
    Raises :class:`CallBudgetExceededError` BEFORE making the LLM call.
    """
    # Cache check before any logging / budget enforcement so a hit is
    # truly side-effect-free.
    cache_key = ""
    if _fm_cache_enabled():
        cache_key = _fm_cache_key(
            category=category,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
            force_provider=force_provider,
        )
        cached = _fm_cache_get(cache_key)
        if cached is not None:
            return cached

    # Wave-25c rev3 (2026-05-13): daily-spend circuit breaker. If the
    # cumulative cost this UTC day has exceeded ETA_FM_DAILY_CAP_USD
    # (default $5/day), short-circuit by returning an empty response so
    # callers fall through to their template path. Pre-call check is a
    # cheap lock + flag read; the cost record happens after the call
    # completes via _fm_breaker_check_then_record.
    if _fm_breaker_open():
        return MultiModelResponse(
            text="",
            provider=force_provider or force_provider_for(category),
            model="",
            fallback_used=True,
            fallback_reason="fm_daily_cap_breaker_tripped",
            category=category,
        )

    selection = select_model(category)
    preferred = force_provider or force_provider_for(category)

    logger.info(
        "routing category=%s preferred=%s tier=%s",
        category.value,
        preferred.value,
        selection.tier.value,
    )

    if max_cost_usd is not None:
        _enforce_call_budget(
            preferred=preferred,
            tier=selection.tier,
            max_tokens=max_tokens,
            max_cost_usd=max_cost_usd,
        )

    response = _route_and_execute_inner(
        category=category,
        selection=selection,
        preferred=preferred,
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=max_tokens,
        temperature=temperature,
        workspace=workspace,
    )

    log_call(
        record=_telemetry_record(
            kind="chain_stage" if chain_stage else "route",
            category=category,
            preferred=preferred,
            response=response,
            stage=chain_stage,
            chain_id=chain_id,
        )
    )
    # Wave-25c rev3: record actual cost against the daily-spend breaker.
    # If this call pushes us over the cap, subsequent calls short-circuit.
    _fm_breaker_check_then_record(response.cost_usd)
    if cache_key:
        _fm_cache_put(cache_key, response)
    return response


def _route_and_execute_inner(
    *,
    category: TaskCategory,
    selection: Any,  # noqa: ANN401  (ModelSelection — avoids TYPE_CHECKING import dance)
    preferred: ForceProvider,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
    workspace: str | None,
) -> MultiModelResponse:
    """Pure routing logic without telemetry. Splits so logging is single-shot."""
    # --- CLAUDE path ---
    if preferred == ForceProvider.CLAUDE:
        return _fallback_codex_or_deepseek(
            category=category,
            tier=selection.tier,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
            workspace=workspace,
            fallback_reason="claude disabled by operator policy",
        )

    # --- CODEX path ---
    if preferred == ForceProvider.CODEX:
        if not check_codex_available():
            return _fallback_deepseek(
                category=category,
                tier=selection.tier,
                system_prompt=system_prompt,
                user_message=user_message,
                max_tokens=max_tokens,
                temperature=temperature,
                fallback_reason="codex CLI not installed (run: npm install -g @openai/codex)",
            )
        cli_resp = _codex_call(
            tier=selection.tier,
            system_prompt=system_prompt,
            user_message=user_message,
            workspace=workspace,
        )
        failure = _classify_cli_failure(cli_resp)
        if failure is None:
            return _wrap_cli(cli_resp, ForceProvider.CODEX, category, selection.tier)
        return _fallback_deepseek(
            category=category,
            tier=selection.tier,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
            fallback_reason=_codex_fallback_reason(failure),
        )

    # --- DEEPSEEK path (default) ---
    return _execute_deepseek(
        category=category,
        tier=selection.tier,
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=max_tokens,
        temperature=temperature,
    )


async def route_and_execute_async(
    *,
    category: TaskCategory,
    system_prompt: str = "",
    user_message: str,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    workspace: str | None = None,
    force_provider: ForceProvider | None = None,
    chain_id: str | None = None,
    chain_stage: str | None = None,
    max_cost_usd: float | None = None,
) -> MultiModelResponse:
    """Awaitable variant of :func:`route_and_execute` for async hot paths.

    The synchronous version calls ``subprocess.run`` (Claude/Codex CLI) and
    blocking HTTP (DeepSeek API). Calling that from inside an asyncio event
    loop stalls the loop for up to ``ETA_CLI_TIMEOUT_SEC`` (default 300s) —
    fatal for trading sessions, supervisors, or anything else that must keep
    pumping events.

    This wrapper offloads the work to ``asyncio.to_thread`` so the loop stays
    responsive. All keyword args mirror :func:`route_and_execute` exactly.

    Use from JARVIS, the live supervisor, or any ``async def`` caller. Sync
    callers should keep using :func:`route_and_execute` — there's no benefit
    to wrapping then unwrapping.
    """
    import asyncio  # noqa: PLC0415  (deferred so sync callers don't import it)

    return await asyncio.to_thread(
        route_and_execute,
        category=category,
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=max_tokens,
        temperature=temperature,
        workspace=workspace,
        force_provider=force_provider,
        chain_id=chain_id,
        chain_stage=chain_stage,
        max_cost_usd=max_cost_usd,
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


def _enforce_call_budget(
    *,
    preferred: ForceProvider,
    tier: ModelTier,
    max_tokens: int,
    max_cost_usd: float,
) -> None:
    """Refuse the call BEFORE issuing it if worst-case spend exceeds the cap.

    Worst case = max_tokens billed at the resolved tier's output rate. We use
    the OUTPUT rate (the more expensive side) for a conservative estimate.
    Subscription CLI calls are reported as cost_usd=0 in the response, so the
    budget only constrains the DeepSeek API path.
    """
    from eta_engine.brain.llm_provider import _COST_1M, _TIER_MODEL  # noqa: PLC0415

    if preferred != ForceProvider.DEEPSEEK:
        return

    # Map ForceProvider -> Provider for the cost lookup.
    if preferred == ForceProvider.DEEPSEEK:
        api_provider = Provider.DEEPSEEK
    elif preferred == ForceProvider.CLAUDE:
        api_provider = Provider.ANTHROPIC
    elif preferred == ForceProvider.CODEX:
        api_provider = Provider.OPENAI
    else:
        return  # unknown provider — skip the check

    model = _TIER_MODEL.get((tier, api_provider))
    if model is None:
        return  # no pricing info — skip silently
    _, output_rate = _COST_1M.get(model, (0.0, 0.0))
    if output_rate <= 0:
        return  # subscription-billed (claude/codex CLI) — no API cost
    worst_case = (max_tokens / 1_000_000.0) * output_rate
    if worst_case > max_cost_usd:
        raise CallBudgetExceededError(
            f"Refused: worst-case ${worst_case:.4f} (max_tokens={max_tokens} × "
            f"{output_rate}/1M for {model}) > cap ${max_cost_usd:.4f}",
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


def _fallback_codex_or_deepseek(
    *,
    category: TaskCategory,
    tier: ModelTier,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
    workspace: str | None,
    fallback_reason: str,
) -> MultiModelResponse:
    """Use Codex subscription CLI for disabled Claude routes, then DeepSeek."""
    if check_codex_available():
        cli_resp = _codex_call(
            tier=tier,
            system_prompt=system_prompt,
            user_message=user_message,
            workspace=workspace,
        )
        failure = _classify_cli_failure(cli_resp)
        if failure is None:
            response = _wrap_cli(cli_resp, ForceProvider.CODEX, category, tier)
            response.fallback_used = True
            response.fallback_reason = fallback_reason
            return response
        fallback_reason = f"{fallback_reason}; codex unavailable: {_codex_fallback_reason(failure)}"
    else:
        fallback_reason = f"{fallback_reason}; codex CLI unavailable"

    return _fallback_deepseek(
        category=category,
        tier=tier,
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=max_tokens,
        temperature=temperature,
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
    return os.environ.get("ETA_CODEX_DEFAULT_MODEL", "gpt-5.5").strip() or "gpt-5.5"


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
                "disabled_by_policy": cli_status["claude_disabled_by_policy"],
                "role": "Disabled legacy lane; Codex handles architecture/review",
            },
            "codex": {
                "available": cli_status["codex_available"],
                "command": cli_status["codex_command"],
                "role": "Lead Architect and Systems Expert",
            },
            "deepseek": {
                "available": api_status["deepseek_key_configured"],
                "model": api_status["models"],
                "role": "Worker Bee — high-volume generation, boilerplate, grunt",
            },
        },
        "routing_table": {cat.value: force_provider_for(TaskCategory(cat)).value for cat in TaskCategory},
        "fallback": "Codex subscription CLI is preferred for admin/review; DeepSeek V4 is the only API fallback",
    }


# ---------------------------------------------------------------------------
# Force-Multiplier chain orchestrator (Wave-19)
# ---------------------------------------------------------------------------
#
# The canonical pipeline ties the allowed providers into one workflow:
#
#   1. PLAN      (CODEX)     — architect the work, define interfaces & risks
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


class CallBudgetExceededError(RuntimeError):
    """Raised by ``route_and_execute`` when ``max_cost_usd`` would be exceeded.

    Computed BEFORE the LLM call: if max_tokens × per-token rate exceeds
    the cap, the call is refused. Use a try/except in callers that want
    to degrade gracefully (e.g. retry with fewer tokens).
    """


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
    1. ``plan``      — CODEX (Lead Architect)   designs the work
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
        The defaults map to CODEX/DEEPSEEK/CODEX respectively.
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
    chain_id = new_chain_id()

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
                stage,
                result.total_cost_usd,
                max_total_cost_usd,
            )
            result.aborted_at = stage
            raise ChainBudgetExceededError(
                f"Chain cost ${result.total_cost_usd:.4f} > cap ${max_total_cost_usd:.2f} before running {stage}"
            )
        return True

    def _check_stage_output(stage: str, resp: MultiModelResponse) -> bool:
        """Return True if downstream stages can use this output. False = abort.

        Empty output from a fallback (e.g. DeepSeek returned nothing because
        the API key was rejected) would feed garbage into the next stage.
        """
        if not resp.text.strip() and abort_on_empty:
            logger.error(
                "[chain] stage=%s returned empty text — aborting chain (provider=%s fallback=%s reason=%s)",
                stage,
                resp.provider.value,
                resp.fallback_used,
                resp.fallback_reason,
            )
            result.aborted_at = stage
            return False
        return True

    # --- Stage 1: PLAN (CODEX) ---
    if "plan" not in skip:
        logger.info("[chain] stage=plan task=%s", task[:80])
        plan = route_and_execute(
            category=plan_category,
            system_prompt=PLAN_SYSTEM,
            user_message=task,
            max_tokens=max_tokens,
            workspace=workspace,
            chain_id=chain_id,
            chain_stage="plan",
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
            chain_id=chain_id,
            chain_stage="implement",
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
            chain_id=chain_id,
            chain_stage="verify",
        )
        result.verify = verify
        result.total_elapsed_ms += verify.elapsed_ms
        result.total_cost_usd += verify.cost_usd
        if verify.fallback_used:
            result.fallbacks_used.append(f"verify: {verify.fallback_reason}")

    return result

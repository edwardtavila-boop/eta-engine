"""
EVOLUTIONARY TRADING ALGO  //  brain.jarvis_v3.local_llm
========================================================
Local-LLM gateway for fan-out summarization (premarket digest, kaizen
ledger compress, decision-journal recap).

Why
---
Anthropic API calls are right for *strategic* decisions (kill-switch
verdict, regime classification, position sizing). They're overkill --
and expensive -- for the bulk text reduction work that hits the
ledger every minute. A local Llama-3-8B (or Qwen-2.5-7B) running
behind an OpenAI-compatible HTTP API (vLLM, llama.cpp's `server`,
ollama, LiteLLM proxy) handles the bulk passes for ~$0 marginal.

Public API
----------

* :class:`LocalLLMGateway` -- HTTP client that talks the OpenAI
  ``/v1/chat/completions`` shape. Routes a chat prompt to a local
  endpoint, returns the assistant message + token counts.
* :func:`is_local_llm_available(url)` -- probe ``GET /v1/models``.
* :func:`fanout(prompts, gateway)` -- run N prompts in parallel, return
  list of completions in input order.

Usage
-----

::

    gw = LocalLLMGateway(url="http://127.0.0.1:8081", model="qwen2.5-7b")
    out = await gw.complete([
        {"role": "system", "content": "You compress operator notes."},
        {"role": "user", "content": "<long ledger paste>"},
    ])
    print(out.text)

The gateway uses ``httpx`` (already in our deps for the Anthropic
client). It does NOT pull in the OpenAI SDK -- the ``/v1/chat/...``
shape is stable enough that a 30-line client is preferable to a
heavyweight dep.

Errors
------

* If the endpoint is unreachable, ``LocalLLMGateway.complete()``
  raises :class:`LocalLLMUnavailable` -- the caller is expected to
  fall back to the Anthropic SDK.
* :class:`LocalLLMRateLimited` is raised on HTTP 429.
* All other 4xx/5xx -> :class:`LocalLLMClientError`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


class LocalLLMClientError(Exception):
    """Generic local-LLM HTTP error."""


class LocalLLMUnavailable(LocalLLMClientError):  # noqa: N818 -- "Unavailable" reads better at call sites
    """Endpoint is unreachable; caller should fall back to remote API."""


class LocalLLMRateLimited(LocalLLMClientError):  # noqa: N818 -- "RateLimited" reads better at call sites
    """Endpoint returned 429."""


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalLLMCompletion:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


def _default_url() -> str:
    return os.environ.get("ETA_LOCAL_LLM_URL", "http://127.0.0.1:8081")


def _default_model() -> str:
    return os.environ.get("ETA_LOCAL_LLM_MODEL", "qwen2.5-7b-instruct")


def is_local_llm_available(url: str | None = None, timeout: float = 0.5) -> bool:
    """Probe the gateway by fetching ``/v1/models``."""
    try:
        import httpx
    except ImportError:
        return False
    try:
        u = (url or _default_url()).rstrip("/") + "/v1/models"
        with httpx.Client(timeout=timeout) as c:
            r = c.get(u)
            return r.status_code == 200
    except Exception as e:  # noqa: BLE001 -- httpx raises a wide set
        log.debug("is_local_llm_available: %s", e)
        return False


class LocalLLMGateway:
    """OpenAI-/v1/chat-shape HTTP client for a local LLM."""

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 1,
    ) -> None:
        self._url = (url or _default_url()).rstrip("/")
        self._model = model or _default_model()
        self._timeout = timeout
        self._max_retries = max_retries

    @property
    def url(self) -> str:
        return self._url

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LocalLLMCompletion:
        """Round-trip a chat completion. Raises on failure."""
        try:
            import httpx
        except ImportError as e:
            raise LocalLLMUnavailable(
                "httpx not installed; pip install httpx",
            ) from e

        body = {
            "model":       self._model,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "stream":      False,
        }
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as c:
                    t0 = asyncio.get_event_loop().time()
                    r = await c.post(
                        f"{self._url}/v1/chat/completions",
                        json=body,
                    )
                    elapsed_ms = (asyncio.get_event_loop().time() - t0) * 1000.0
                if r.status_code == 429:
                    raise LocalLLMRateLimited(
                        f"local LLM rate-limited (HTTP 429): {r.text[:200]}",
                    )
                if r.status_code >= 400:
                    raise LocalLLMClientError(
                        f"local LLM HTTP {r.status_code}: {r.text[:200]}",
                    )
                payload = r.json()
                return _parse_openai_response(payload, self._model, elapsed_ms)
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                last_exc = e
                if attempt < self._max_retries:
                    await asyncio.sleep(0.25 * (2 ** attempt))
                    continue
                raise LocalLLMUnavailable(
                    f"local LLM unreachable at {self._url}: {e}",
                ) from e
            except (LocalLLMRateLimited, LocalLLMClientError):
                raise
            except Exception as e:  # noqa: BLE001 -- final catch-all
                last_exc = e
                if attempt < self._max_retries:
                    await asyncio.sleep(0.25 * (2 ** attempt))
                    continue
                raise LocalLLMClientError(
                    f"local LLM unexpected error: {e}",
                ) from e
        # Should not reach here.
        raise LocalLLMClientError(f"local LLM exhausted retries: {last_exc}")


def _parse_openai_response(
    payload: dict, model: str, latency_ms: float,
) -> LocalLLMCompletion:
    choices = payload.get("choices", [])
    if not choices:
        raise LocalLLMClientError("local LLM returned empty 'choices'")
    msg = (choices[0] or {}).get("message", {}) or {}
    text = msg.get("content", "") or ""
    usage = payload.get("usage", {}) or {}
    return LocalLLMCompletion(
        text=text,
        model=str(payload.get("model", model)),
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
        total_tokens=int(usage.get("total_tokens", 0)),
        latency_ms=latency_ms,
    )


async def fanout(
    prompt_sets: list[list[dict[str, str]]],
    gateway: LocalLLMGateway,
    max_parallel: int = 4,
) -> list[LocalLLMCompletion | Exception]:
    """Run multiple completions concurrently, bounded by ``max_parallel``.

    Returns a list aligned with the inputs. Failed entries hold the
    exception (not raised) so the caller can inspect partial results.
    """
    sem = asyncio.Semaphore(max_parallel)

    async def _one(messages: list[dict[str, str]]) -> LocalLLMCompletion | Exception:
        async with sem:
            try:
                return await gateway.complete(messages)
            except Exception as e:  # noqa: BLE001 -- caller wants partials
                return e

    return await asyncio.gather(*(_one(p) for p in prompt_sets))

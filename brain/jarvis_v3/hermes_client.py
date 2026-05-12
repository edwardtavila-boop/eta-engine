"""Time-boxed sync HTTP client used by JARVIS to call the Nous Hermes Agent.

This is the JARVIS->Hermes half of the bridge. The supervisor hot-path is
sync, so every public function is a blocking call that wraps a per-call
``httpx.Client(timeout=...)``. All HTTP work is wrapped in a broad
``try/except`` -- this module must NEVER raise, because the supervisor
cannot tolerate hot-path exceptions; failures surface as
``HermesResult(ok=False, error="...")``.

Wired against the canonical Hermes Agent ``/v1/`` REST surface:

- ``POST /v1/tools/clarify``          -- ``narrative()``
- ``POST /v1/tools/web_search``       -- ``web_search()``
- ``POST /v1/memory/put``             -- ``memory_persist()``
- ``GET  /v1/memory/get?key=...``     -- ``memory_recall()``
- ``GET  /v1/health``                 -- ``health()``

If Hermes ever moves the tool surface under ``/v1/chat/completions`` with
structured tool-call prompts, swap the endpoint URLs only -- the wrapper
contract (HermesResult / backoff / health-cache) does not change.

Resilience contract:

- **Per-call timeout** (default 1.0s; web_search default 2.0s).
- **Health cache**: ``health()`` caches its last boolean for ``_HEALTH_CACHE_S``
  seconds, keyed off ``time.monotonic()``.
- **Backoff**: a module-level counter ``_consecutive_failures`` ticks up on
  any non-ok result. When it hits ``_BACKOFF_FAIL_THRESHOLD`` (3), every
  subsequent call returns ``error="backoff_active"`` without touching the
  network until ``_BACKOFF_DURATION_S`` (300s) elapses. Any success resets
  the counter.
- **Auth**: optional ``HERMES_TOKEN`` env var becomes
  ``Authorization: Bearer <token>``.

Module-level state (for Phase B agents that need to monkeypatch):

- ``_consecutive_failures: int``     -- failure counter
- ``_backoff_active_until: float``   -- monotonic deadline; 0 when inactive
- ``_health_cached: bool | None``    -- last cached health result
- ``_health_cached_at: float``       -- monotonic ts of last health probe

The ``reset_state()`` test helper zeros all of those.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


DEFAULT_HERMES_URL: str = os.environ.get("HERMES_URL", "http://127.0.0.1:8642")
DEFAULT_TIMEOUT_S: float = 1.0
EXPECTED_HOOKS: tuple[str, ...] = (
    "narrative",
    "web_search",
    "memory_persist",
    "memory_recall",
)

# Backoff parameters
_BACKOFF_FAIL_THRESHOLD: int = 3
_BACKOFF_DURATION_S: float = 300.0  # 5 minutes
_HEALTH_CACHE_S: float = 60.0  # 1 minute


# ---------------------------------------------------------------------------
# Module-level mutable state -- intentionally module globals so the
# supervisor sees a single shared backoff / cache across all call sites.
# Tests monkeypatch ``time.monotonic`` and call ``reset_state()`` to drive
# the state machine.
# ---------------------------------------------------------------------------
_consecutive_failures: int = 0
_backoff_active_until: float = 0.0
_health_cached: bool | None = None
_health_cached_at: float = 0.0


@dataclass(frozen=True)
class HermesResult:
    """Canonical return shape for every public function in this module."""

    ok: bool
    data: Any
    error: str | None
    elapsed_ms: float


def reset_state() -> None:
    """Test helper: clear backoff state and health cache."""
    global _consecutive_failures, _backoff_active_until, _health_cached, _health_cached_at
    _consecutive_failures = 0
    _backoff_active_until = 0.0
    _health_cached = None
    _health_cached_at = 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    """Build the per-request headers, attaching Authorization when HERMES_TOKEN is set."""
    token = os.environ.get("HERMES_TOKEN", "")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _backoff_active() -> bool:
    """Return True if backoff is currently suppressing calls."""
    if _backoff_active_until <= 0.0:
        return False
    return time.monotonic() < _backoff_active_until


def _backoff_active_for_kaizen() -> bool:
    """Public accessor used by the kaizen-loop health probe."""
    return _backoff_active()


def _on_success() -> None:
    """Reset failure counter + clear any pending backoff."""
    global _consecutive_failures, _backoff_active_until
    _consecutive_failures = 0
    _backoff_active_until = 0.0


def _on_failure() -> None:
    """Increment failure counter and arm backoff at threshold."""
    global _consecutive_failures, _backoff_active_until
    _consecutive_failures += 1
    if _consecutive_failures >= _BACKOFF_FAIL_THRESHOLD:
        _backoff_active_until = time.monotonic() + _BACKOFF_DURATION_S


def _result_from_exception(exc: Exception, started_at: float) -> HermesResult:
    """Wrap any exception into the canonical HermesResult."""
    elapsed = (time.monotonic() - started_at) * 1000.0
    err_name = type(exc).__name__
    logger.warning("hermes_client: call failed (%s): %s", err_name, exc)
    return HermesResult(ok=False, data=None, error=err_name, elapsed_ms=elapsed)


def _backoff_result() -> HermesResult:
    """Build the suppressed-by-backoff HermesResult."""
    return HermesResult(ok=False, data=None, error="backoff_active", elapsed_ms=0.0)


def _decode_response(
    response: httpx.Response, started_at: float,
) -> HermesResult:
    """Convert an httpx.Response into a HermesResult.

    Handles HTTP-error status codes (returns ``error="http_<code>"``) and
    JSON-decode failures (returns ``error="json_decode"``). On 2xx with a
    valid JSON body, returns ``ok=True`` carrying the parsed payload.
    """
    elapsed = (time.monotonic() - started_at) * 1000.0
    status = response.status_code
    if status >= 400:
        logger.warning("hermes_client: http %s", status)
        return HermesResult(
            ok=False, data=None, error=f"http_{status}", elapsed_ms=elapsed,
        )
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_client: json decode failed: %s", exc)
        return HermesResult(
            ok=False, data=None, error="json_decode", elapsed_ms=elapsed,
        )
    return HermesResult(ok=True, data=payload, error=None, elapsed_ms=elapsed)


def _url(path: str) -> str:
    """Compose the full URL against the configured Hermes base URL."""
    base = os.environ.get("HERMES_URL", DEFAULT_HERMES_URL).rstrip("/")
    return f"{base}{path}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def narrative(
    verdict: dict[str, Any], *, timeout_s: float = DEFAULT_TIMEOUT_S,
) -> HermesResult:
    """Ask Hermes to render a one-sentence narrative for a JARVIS verdict.

    Wired against ``POST /v1/tools/clarify``. Returns ``HermesResult`` with
    ``data`` set to the parsed JSON response on success.
    """
    started_at = time.monotonic()
    if _backoff_active():
        return _backoff_result()
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(
                _url("/v1/tools/clarify"),
                json={"query": verdict},
                headers=_auth_headers(),
            )
        result = _decode_response(response, started_at)
    except Exception as exc:  # noqa: BLE001
        result = _result_from_exception(exc, started_at)
    if result.ok:
        _on_success()
    else:
        _on_failure()
    return result


def web_search(
    query: str, *, n: int = 3, timeout_s: float = 2.0,
) -> HermesResult:
    """Run a web search through Hermes' web_search tool.

    Wired against ``POST /v1/tools/web_search``. Default timeout is 2.0s
    (web fetches are slower than local tool calls).
    """
    started_at = time.monotonic()
    if _backoff_active():
        return _backoff_result()
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(
                _url("/v1/tools/web_search"),
                json={"query": query, "n": n},
                headers=_auth_headers(),
            )
        result = _decode_response(response, started_at)
    except Exception as exc:  # noqa: BLE001
        result = _result_from_exception(exc, started_at)
    if result.ok:
        _on_success()
    else:
        _on_failure()
    return result


def memory_persist(
    key: str, value: dict[str, Any], *, timeout_s: float = 1.0,
) -> HermesResult:
    """Persist a key/value pair into Hermes-managed memory.

    Wired against ``POST /v1/memory/put``.
    """
    started_at = time.monotonic()
    if _backoff_active():
        return _backoff_result()
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(
                _url("/v1/memory/put"),
                json={"key": key, "value": value},
                headers=_auth_headers(),
            )
        result = _decode_response(response, started_at)
    except Exception as exc:  # noqa: BLE001
        result = _result_from_exception(exc, started_at)
    if result.ok:
        _on_success()
    else:
        _on_failure()
    return result


def memory_recall(key: str, *, timeout_s: float = 1.0) -> HermesResult:
    """Recall a value previously stored via ``memory_persist``.

    Wired against ``GET /v1/memory/get?key=...``.
    """
    started_at = time.monotonic()
    if _backoff_active():
        return _backoff_result()
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.get(
                _url("/v1/memory/get"),
                params={"key": key},
                headers=_auth_headers(),
            )
        result = _decode_response(response, started_at)
    except Exception as exc:  # noqa: BLE001
        result = _result_from_exception(exc, started_at)
    if result.ok:
        _on_success()
    else:
        _on_failure()
    return result


def health() -> bool:
    """Probe ``GET /v1/health`` and cache the result for ``_HEALTH_CACHE_S``.

    The cache key is monotonic time so the test suite can advance it.
    Failure modes (connection refused, timeout, non-2xx) all map to
    ``False`` -- callers should treat this as a best-effort indicator.
    """
    global _health_cached, _health_cached_at
    now = time.monotonic()
    if _health_cached is not None and (now - _health_cached_at) < _HEALTH_CACHE_S:
        return _health_cached
    started_at = now
    if _backoff_active():
        _health_cached = False
        _health_cached_at = now
        return False
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT_S) as client:
            response = client.get(_url("/v1/health"), headers=_auth_headers())
        ok = 200 <= response.status_code < 300
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes_client.health: probe failed (%s): %s",
            type(exc).__name__,
            exc,
        )
        ok = False
    elapsed = (time.monotonic() - started_at) * 1000.0
    logger.debug("hermes_client.health: ok=%s elapsed_ms=%.2f", ok, elapsed)
    _health_cached = ok
    _health_cached_at = now
    if ok:
        _on_success()
    else:
        _on_failure()
    return ok

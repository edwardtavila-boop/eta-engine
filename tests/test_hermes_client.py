"""Tests for the time-boxed Hermes Agent HTTP client used by JARVIS.

Tests use ``monkeypatch`` to replace ``httpx.Client`` with a fake that returns
canned responses. ``reset_state()`` is invoked in an autouse fixture so test
ordering never leaks backoff state or cached health between cases.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from eta_engine.brain.jarvis_v3 import hermes_client

# ---------------------------------------------------------------------------
# Fake httpx scaffolding
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for httpx.Response.

    Provides ``status_code``, ``json()`` and ``text`` so the client code can
    treat it as a real response. ``json()`` raises a JSONDecodeError when
    ``raise_on_json`` is set, matching httpx's behavior for malformed bodies.
    """

    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        *,
        raise_on_json: bool = False,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._raise_on_json = raise_on_json
        self.text = text or (str(payload) if payload is not None else "")

    def json(self) -> Any:
        if self._raise_on_json:
            import json as _json
            raise _json.JSONDecodeError("fake", "doc", 0)
        return self._payload


class _FakeClient:
    """Records every request and returns canned responses in order.

    ``responses`` is a list of ``_FakeResponse`` / Exception instances.
    The instance is built fresh per ``httpx.Client(timeout=...)`` call
    by ``_install_fake``, so each public-API call gets a clean queue.
    Exceptions in the response queue are raised to mimic transport
    failures (timeouts, network errors).
    """

    def __init__(self, responses: list[Any], requests: list[dict[str, Any]]) -> None:
        self._responses = responses
        self.requests = requests

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def _record(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"no more fake responses; got {method} {url}")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._record("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._record("POST", url, **kwargs)


def _install_fake(
    monkeypatch: pytest.MonkeyPatch, responses: list[Any],
) -> list[dict[str, Any]]:
    """Replace ``httpx.Client`` so every ``with httpx.Client(...)`` returns
    a ``_FakeClient`` drawing from ``responses``. The returned ``requests``
    list is mutated in place with every request the client makes.
    """
    requests: list[dict[str, Any]] = []

    def _client_factory(*_args: Any, **_kwargs: Any) -> _FakeClient:
        return _FakeClient(responses, requests)

    monkeypatch.setattr(hermes_client.httpx, "Client", _client_factory)
    return requests


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_hermes_state() -> None:
    """Clear backoff counters + health cache before every test."""
    hermes_client.reset_state()
    yield
    hermes_client.reset_state()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_narrative_returns_HermesResult_shape(monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: N802
    """A successful narrative call returns the canonical HermesResult shape."""
    _install_fake(monkeypatch, [_FakeResponse(200, {"narrative": "looks fine"})])
    result = hermes_client.narrative({"verdict": "OK", "bot": "vwap_mr_mnq"})
    assert isinstance(result, hermes_client.HermesResult)
    assert result.ok is True
    assert result.data is not None
    assert result.error is None
    assert result.elapsed_ms >= 0.0


def test_health_cached_for_60s(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second health() call within the cache window must NOT hit HTTP."""
    requests = _install_fake(
        monkeypatch,
        [_FakeResponse(200, {"status": "ok"})],
    )
    fixed_now = [1000.0]

    def _fake_monotonic() -> float:
        return fixed_now[0]

    monkeypatch.setattr(hermes_client.time, "monotonic", _fake_monotonic)
    assert hermes_client.health() is True
    assert len(requests) == 1
    fixed_now[0] = 1030.0  # 30s later -- still within 60s cache window
    assert hermes_client.health() is True
    assert len(requests) == 1, "health() must serve from cache within 60s"


def test_timeout_returns_ok_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """An httpx.TimeoutException must surface as ok=False with the class name."""
    _install_fake(
        monkeypatch,
        [httpx.TimeoutException("simulated timeout")],
    )
    result = hermes_client.narrative({"verdict": "OK"})
    assert result.ok is False
    assert result.error == "TimeoutException"
    assert result.data is None


def test_3_consecutive_failures_triggers_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """After 3 consecutive failures, the 4th call must be suppressed with
    error=backoff_active and must NOT issue an HTTP request.
    """
    requests = _install_fake(
        monkeypatch,
        [
            httpx.TimeoutException("fail-1"),
            httpx.TimeoutException("fail-2"),
            httpx.TimeoutException("fail-3"),
        ],
    )
    for _ in range(3):
        result = hermes_client.narrative({"verdict": "OK"})
        assert result.ok is False
    assert len(requests) == 3
    # 4th call -- should be suppressed entirely.
    result = hermes_client.narrative({"verdict": "OK"})
    assert result.ok is False
    assert result.error == "backoff_active"
    assert result.elapsed_ms == 0.0
    assert len(requests) == 3, "backoff must suppress HTTP call"


def test_backoff_lifts_after_5min(monkeypatch: pytest.MonkeyPatch) -> None:
    """After 5 minutes of monotonic time, backoff lifts and HTTP resumes."""
    fixed_now = [0.0]

    def _fake_monotonic() -> float:
        return fixed_now[0]

    monkeypatch.setattr(hermes_client.time, "monotonic", _fake_monotonic)
    # First trigger backoff with 3 failures.
    _install_fake(
        monkeypatch,
        [
            httpx.TimeoutException("a"),
            httpx.TimeoutException("b"),
            httpx.TimeoutException("c"),
        ],
    )
    for _ in range(3):
        hermes_client.narrative({"verdict": "OK"})
    # 4th call within backoff -- suppressed.
    suppressed = hermes_client.narrative({"verdict": "OK"})
    assert suppressed.error == "backoff_active"
    # Advance time past the 5 min backoff window.
    fixed_now[0] = 301.0
    requests = _install_fake(monkeypatch, [_FakeResponse(200, {"ok": True})])
    result = hermes_client.narrative({"verdict": "OK"})
    assert result.ok is True
    assert len(requests) == 1, "backoff must lift after 5 min"


def test_successful_call_resets_backoff_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """2 fails + 1 success + 3 fails -> backoff active (counter reset)."""
    _install_fake(
        monkeypatch,
        [
            httpx.TimeoutException("f1"),
            httpx.TimeoutException("f2"),
            _FakeResponse(200, {"narrative": "ok"}),
            httpx.TimeoutException("f3"),
            httpx.TimeoutException("f4"),
            httpx.TimeoutException("f5"),
        ],
    )
    for _ in range(2):
        r = hermes_client.narrative({"verdict": "OK"})
        assert r.ok is False
    # success resets the counter.
    r = hermes_client.narrative({"verdict": "OK"})
    assert r.ok is True
    # Now 3 fresh fails -> backoff trips again.
    for _ in range(3):
        r = hermes_client.narrative({"verdict": "OK"})
        assert r.ok is False
    # 7th call: should be suppressed.
    r = hermes_client.narrative({"verdict": "OK"})
    assert r.error == "backoff_active"


def test_http_500_returns_ok_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTTP 500 response surfaces ok=False with error=http_500."""
    _install_fake(monkeypatch, [_FakeResponse(500, None, text="oops")])
    result = hermes_client.narrative({"verdict": "OK"})
    assert result.ok is False
    assert result.error == "http_500"


def test_malformed_json_response_returns_ok_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 response with malformed JSON surfaces error=json_decode."""
    _install_fake(
        monkeypatch,
        [_FakeResponse(200, None, raise_on_json=True, text="garbage")],
    )
    result = hermes_client.narrative({"verdict": "OK"})
    assert result.ok is False
    assert result.error == "json_decode"


def test_memory_persist_and_recall_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory_persist returns ok=True; memory_recall returns the value."""
    _install_fake(
        monkeypatch,
        [
            _FakeResponse(200, {"ok": True}),
            _FakeResponse(200, {"value": {"a": 1, "b": 2}}),
        ],
    )
    put = hermes_client.memory_persist("session_x", {"a": 1, "b": 2})
    assert put.ok is True
    got = hermes_client.memory_recall("session_x")
    assert got.ok is True
    # data should carry through the recalled value
    assert got.data is not None


def test_authorization_header_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """When HERMES_TOKEN is set, requests carry the Authorization header."""
    monkeypatch.setenv("HERMES_TOKEN", "secret-x")
    requests = _install_fake(monkeypatch, [_FakeResponse(200, {"narrative": "ok"})])
    hermes_client.narrative({"verdict": "OK"})
    assert len(requests) == 1
    headers = requests[0].get("headers") or {}
    assert headers.get("Authorization") == "Bearer secret-x"

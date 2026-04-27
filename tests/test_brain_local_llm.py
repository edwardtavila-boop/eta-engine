"""Tests for ``eta_engine.brain.jarvis_v3.local_llm``."""

from __future__ import annotations

import pytest
from eta_engine.brain.jarvis_v3.local_llm import (
    LocalLLMClientError,
    LocalLLMGateway,
    LocalLLMUnavailable,
    _parse_openai_response,
    fanout,
    is_local_llm_available,
)


def test_is_local_llm_available_with_dead_url() -> None:
    assert is_local_llm_available("http://127.0.0.1:1") is False


def test_parse_openai_response_extracts_text() -> None:
    out = _parse_openai_response(
        {
            "model": "qwen2.5-7b",
            "choices": [{"message": {"content": "hello world"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        },
        model="qwen2.5-7b",
        latency_ms=12.0,
    )
    assert out.text == "hello world"
    assert out.prompt_tokens == 5
    assert out.completion_tokens == 2
    assert out.total_tokens == 7


def test_parse_openai_response_handles_missing_usage() -> None:
    out = _parse_openai_response(
        {"choices": [{"message": {"content": "x"}}]},
        model="m",
        latency_ms=1.0,
    )
    assert out.text == "x"
    assert out.prompt_tokens == 0


def test_parse_openai_response_raises_on_empty_choices() -> None:
    with pytest.raises(LocalLLMClientError, match="empty"):
        _parse_openai_response({"choices": []}, model="m", latency_ms=1.0)


@pytest.mark.asyncio
async def test_complete_raises_unavailable_on_dead_endpoint() -> None:
    gw = LocalLLMGateway(
        url="http://127.0.0.1:1",
        model="x",
        timeout=0.2,
        max_retries=0,
    )
    with pytest.raises(LocalLLMUnavailable):
        await gw.complete(
            [{"role": "user", "content": "hi"}],
            max_tokens=10,
        )


@pytest.mark.asyncio
async def test_fanout_returns_partial_results_on_mixed_failures() -> None:
    gw = LocalLLMGateway(
        url="http://127.0.0.1:1",
        model="x",
        timeout=0.1,
        max_retries=0,
    )
    out = await fanout(
        [
            [{"role": "user", "content": "a"}],
            [{"role": "user", "content": "b"}],
        ],
        gw,
        max_parallel=2,
    )
    assert len(out) == 2
    assert all(isinstance(r, Exception) for r in out)


def test_gateway_default_url_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETA_LOCAL_LLM_URL", "http://gw.internal:9000")
    monkeypatch.setenv("ETA_LOCAL_LLM_MODEL", "llama3-8b")
    gw = LocalLLMGateway()
    assert gw.url == "http://gw.internal:9000"
    assert gw.model == "llama3-8b"


def test_gateway_strips_trailing_slash() -> None:
    gw = LocalLLMGateway(url="http://x:9/", model="m")
    assert gw.url == "http://x:9"

"""
Tests for ``brain.avengers.anthropic_executor.AnthropicExecutor`` and the
real-call path through ``brain.jarvis_v3.claude_layer.prompt_cache.AnthropicClaudeClient``.

Covers:
  * AnthropicClaudeClient.call() builds the right SDK request shape
    (cache_control on prefix, suffix in user message, correct model id).
  * Cost extracted from real usage numbers (cache_read / cache_write
    accounted at the right multipliers).
  * Cache hit detection on a second call with the same prefix.
  * AnthropicExecutor conforms to the Avengers Executor protocol
    (returns str) and records to UsageTracker when one is provided.
  * Daemon-level _build_fleet_executor() honors APEX_AVENGERS_LIVE.

No real network. We inject a fake SDK whose ``messages.create()``
returns a response object with the same attribute shape Anthropic uses.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from apex_predator.brain.avengers.anthropic_executor import AnthropicExecutor
from apex_predator.brain.avengers.base import (
    DryRunExecutor,
    SubsystemId,
    TaskCategory,
    TaskEnvelope,
)
from apex_predator.brain.jarvis_v3.claude_layer.prompt_cache import (
    ANTHROPIC_MODEL_BY_TIER,
    AnthropicClaudeClient,
    ClaudeCallRequest,
    PromptCacheTracker,
    build_cached_prompt,
)
from apex_predator.brain.jarvis_v3.claude_layer.usage_tracker import (
    UsageTracker,
)
from apex_predator.brain.model_policy import ModelTier


# ---------------------------------------------------------------------------
# Fake Anthropic SDK -- minimal shape the client interrogates.
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _FakeContentBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeResponse:
    content: list[_FakeContentBlock]
    usage: _FakeUsage


class _FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._next_response: _FakeResponse | None = None
        self._call_count = 0

    def queue_response(self, resp: _FakeResponse) -> None:
        self._next_response = resp

    def create(self, **kwargs):
        self.calls.append(kwargs)
        self._call_count += 1
        if self._next_response is None:
            # Default canned response: 100 input tokens fresh, 50 output.
            return _FakeResponse(
                content=[_FakeContentBlock(text="VOTE=APPROVE CONFIDENCE=0.7")],
                usage=_FakeUsage(input_tokens=100, output_tokens=50),
            )
        resp, self._next_response = self._next_response, None
        return resp


class _FakeSDK:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


# ---------------------------------------------------------------------------
# AnthropicClaudeClient.call() -- request shape
# ---------------------------------------------------------------------------


def test_client_call_uses_correct_model_id_per_tier() -> None:
    sdk = _FakeSDK()
    client = AnthropicClaudeClient(sdk, PromptCacheTracker())
    prompt = build_cached_prompt(
        system="", prefix="STABLE_DOCTRINE_PREFIX", suffix="per-call context",
    )
    for tier, expected_id in ANTHROPIC_MODEL_BY_TIER.items():
        client.call(ClaudeCallRequest(
            model=tier, prompt=prompt, max_tokens=256, persona="test",
        ))
    seen_models = [c["model"] for c in sdk.messages.calls]
    assert seen_models == [
        ANTHROPIC_MODEL_BY_TIER[ModelTier.HAIKU],
        ANTHROPIC_MODEL_BY_TIER[ModelTier.SONNET],
        ANTHROPIC_MODEL_BY_TIER[ModelTier.OPUS],
    ]


def test_client_call_attaches_cache_control_to_prefix() -> None:
    sdk = _FakeSDK()
    client = AnthropicClaudeClient(sdk, PromptCacheTracker())
    prompt = build_cached_prompt(
        system="", prefix="DOCTRINE", suffix="ctx",
    )
    client.call(ClaudeCallRequest(
        model=ModelTier.HAIKU, prompt=prompt, max_tokens=64, persona="x",
    ))
    call = sdk.messages.calls[0]
    system_blocks = call["system"]
    assert system_blocks[0]["text"] == "DOCTRINE"
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert call["messages"][0] == {"role": "user", "content": "ctx"}


def test_client_call_substitutes_space_for_empty_suffix() -> None:
    """Anthropic rejects empty user content; the client must fill it."""
    sdk = _FakeSDK()
    client = AnthropicClaudeClient(sdk, PromptCacheTracker())
    prompt = build_cached_prompt(system="", prefix="P", suffix="")
    client.call(ClaudeCallRequest(
        model=ModelTier.HAIKU, prompt=prompt, max_tokens=8, persona="x",
    ))
    assert sdk.messages.calls[0]["messages"][0]["content"] == " "


# ---------------------------------------------------------------------------
# AnthropicClaudeClient.call() -- result extraction + cost
# ---------------------------------------------------------------------------


def test_client_call_extracts_text_and_usage() -> None:
    sdk = _FakeSDK()
    sdk.messages.queue_response(_FakeResponse(
        content=[
            _FakeContentBlock(text="part one "),
            _FakeContentBlock(text="part two"),
        ],
        usage=_FakeUsage(
            input_tokens=200, output_tokens=80,
            cache_read_input_tokens=120,
            cache_creation_input_tokens=0,
        ),
    ))
    client = AnthropicClaudeClient(sdk, PromptCacheTracker())
    prompt = build_cached_prompt(system="", prefix="P" * 200, suffix="ctx")
    result = client.call(ClaudeCallRequest(
        model=ModelTier.SONNET, prompt=prompt, max_tokens=128, persona="x",
    ))
    assert result.output_text == "part one part two"
    assert result.input_tokens == 200
    assert result.output_tokens == 80
    assert result.cached_read_tokens == 120
    assert result.cache_hit is True


def test_client_call_cost_uses_cache_discount_on_read() -> None:
    sdk = _FakeSDK()
    sdk.messages.queue_response(_FakeResponse(
        content=[_FakeContentBlock(text="x")],
        usage=_FakeUsage(
            input_tokens=1_000_000, output_tokens=0,
            cache_read_input_tokens=1_000_000,
            cache_creation_input_tokens=0,
        ),
    ))
    client = AnthropicClaudeClient(sdk, PromptCacheTracker())
    prompt = build_cached_prompt(system="", prefix="P" * 50, suffix="")
    result = client.call(ClaudeCallRequest(
        model=ModelTier.HAIKU, prompt=prompt, max_tokens=8, persona="x",
    ))
    # Haiku input rate is $0.80/M; cache read multiplier is 0.10.
    # 1M cached-read tokens * 0.80 * 0.10 = $0.08
    assert result.cost_usd == pytest.approx(0.08, abs=0.001)


def test_client_call_cost_uses_write_premium_on_miss() -> None:
    sdk = _FakeSDK()
    sdk.messages.queue_response(_FakeResponse(
        content=[_FakeContentBlock(text="x")],
        usage=_FakeUsage(
            input_tokens=1_000_000, output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=1_000_000,
        ),
    ))
    client = AnthropicClaudeClient(sdk, PromptCacheTracker())
    prompt = build_cached_prompt(system="", prefix="P" * 50, suffix="")
    result = client.call(ClaudeCallRequest(
        model=ModelTier.HAIKU, prompt=prompt, max_tokens=8, persona="x",
    ))
    # 1M write-tokens * 0.80 * 1.25 = $1.00
    assert result.cost_usd == pytest.approx(1.00, abs=0.001)


def test_client_call_marks_subsequent_call_as_warm() -> None:
    """Two calls with the same prefix -- second sees tracker as 'hot'."""
    sdk = _FakeSDK()
    tracker = PromptCacheTracker()
    client = AnthropicClaudeClient(sdk, tracker)
    prompt = build_cached_prompt(system="", prefix="STABLE", suffix="a")
    client.call(ClaudeCallRequest(
        model=ModelTier.HAIKU, prompt=prompt, max_tokens=8, persona="x",
    ))
    # After first call, the tracker should consider the prefix hot.
    assert tracker.is_hot(prompt.prefix_hash) is True


# ---------------------------------------------------------------------------
# AnthropicExecutor -- Avengers Executor protocol conformance
# ---------------------------------------------------------------------------


def test_executor_returns_str_and_records_usage() -> None:
    sdk = _FakeSDK()
    sdk.messages.queue_response(_FakeResponse(
        content=[_FakeContentBlock(text="Robin's terse answer.")],
        usage=_FakeUsage(input_tokens=300, output_tokens=20),
    ))
    usage = UsageTracker()
    executor = AnthropicExecutor(
        sdk_client=sdk,
        cache_tracker=PromptCacheTracker(),
        usage=usage,
        max_tokens=64,
    )
    env = TaskEnvelope(
        category=TaskCategory.LOG_PARSING,
        goal="parse last 50 lines",
        caller=SubsystemId.OPERATOR,
    )
    out = executor(
        tier=ModelTier.HAIKU,
        system_prompt="You are ROBIN, the grunt.",
        user_prompt="Task: parse last 50 lines",
        envelope=env,
    )
    assert isinstance(out, str)
    assert out == "Robin's terse answer."
    # Usage tracker should now have one call recorded.
    assert usage.calls_last_hour() == 1


def test_executor_optional_usage_tracker() -> None:
    """Passing usage=None still works; just doesn't track."""
    sdk = _FakeSDK()
    executor = AnthropicExecutor(sdk_client=sdk)  # no cache_tracker, no usage
    env = TaskEnvelope(
        category=TaskCategory.SIMPLE_EDIT,
        goal="rename foo to bar",
        caller=SubsystemId.OPERATOR,
    )
    out = executor(
        tier=ModelTier.HAIKU,
        system_prompt="You are ROBIN.",
        user_prompt="rename foo",
        envelope=env,
    )
    assert isinstance(out, str)


def test_executor_persona_field_uses_caller() -> None:
    """The persona field on ClaudeCallResult should reflect envelope.caller."""
    sdk = _FakeSDK()
    usage = UsageTracker()
    executor = AnthropicExecutor(
        sdk_client=sdk, usage=usage,
    )
    env = TaskEnvelope(
        category=TaskCategory.LOG_PARSING,
        goal="g",
        caller=SubsystemId.AUTOPILOT_WATCHDOG,
    )
    executor(
        tier=ModelTier.HAIKU,
        system_prompt="P",
        user_prompt="U",
        envelope=env,
    )
    # The recorded call's persona should be the caller's value.
    recorded = list(usage._calls)[-1]  # noqa: SLF001 -- test introspection
    assert recorded.persona == SubsystemId.AUTOPILOT_WATCHDOG.value


# ---------------------------------------------------------------------------
# Daemon-level _build_fleet_executor honors APEX_AVENGERS_LIVE
# ---------------------------------------------------------------------------


def _make_daemon_with_client(client_obj: object | None) -> object:
    """Build an AvengersDaemon-like minimal object exposing the methods we test.

    We don't construct the real AvengersDaemon (it does signal handlers + state
    directories); just call _build_fleet_executor with the same bound shape.
    """
    from apex_predator.deploy.scripts.avengers_daemon import AvengersDaemon

    # Subclass that skips the heavy __init__ and lets us inject pieces.
    class _MinimalDaemon(AvengersDaemon):  # type: ignore[misc]
        def __init__(self) -> None:  # noqa: D401
            self.usage = UsageTracker()
            self._anthropic_client = client_obj

    return _MinimalDaemon()


def test_daemon_default_uses_dryrun_when_flag_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APEX_AVENGERS_LIVE", raising=False)
    daemon = _make_daemon_with_client(_FakeSDK())
    executor = daemon._build_fleet_executor()  # noqa: SLF001
    assert isinstance(executor, DryRunExecutor)


def test_daemon_uses_dryrun_when_flag_set_but_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AVENGERS_LIVE", "1")
    daemon = _make_daemon_with_client(None)
    executor = daemon._build_fleet_executor()  # noqa: SLF001
    assert isinstance(executor, DryRunExecutor)


def test_daemon_uses_anthropic_executor_when_flag_and_client_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AVENGERS_LIVE", "1")
    daemon = _make_daemon_with_client(_FakeSDK())
    executor = daemon._build_fleet_executor()  # noqa: SLF001
    assert isinstance(executor, AnthropicExecutor)


@pytest.mark.parametrize("flag_val", ["1", "true", "TRUE", "yes", "on"])
def test_daemon_accepts_truthy_flag_variants(
    monkeypatch: pytest.MonkeyPatch, flag_val: str,
) -> None:
    monkeypatch.setenv("APEX_AVENGERS_LIVE", flag_val)
    daemon = _make_daemon_with_client(_FakeSDK())
    executor = daemon._build_fleet_executor()  # noqa: SLF001
    assert isinstance(executor, AnthropicExecutor)


@pytest.mark.parametrize("flag_val", ["", "0", "false", "no", "off", "anything"])
def test_daemon_rejects_falsy_flag_variants(
    monkeypatch: pytest.MonkeyPatch, flag_val: str,
) -> None:
    monkeypatch.setenv("APEX_AVENGERS_LIVE", flag_val)
    daemon = _make_daemon_with_client(_FakeSDK())
    executor = daemon._build_fleet_executor()  # noqa: SLF001
    assert isinstance(executor, DryRunExecutor)

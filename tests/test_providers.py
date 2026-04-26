"""
Tests for ``brain.jarvis_v3.claude_layer.providers`` -- multi-provider
scaffolding (P4).
"""
from __future__ import annotations

import pytest

from apex_predator.brain.jarvis_v3.claude_layer.providers import (
    PROVIDER_INFO,
    Provider,
    ProviderMode,
    cheapest_provider_at,
    cost_at,
    provider_order_for_tier,
)
from apex_predator.brain.model_policy import ModelTier


# ---------------------------------------------------------------------------
# PROVIDER_INFO sanity
# ---------------------------------------------------------------------------


def test_provider_info_anthropic_haiku_costs_match_known_pricing() -> None:
    info = PROVIDER_INFO[Provider.ANTHROPIC][ModelTier.HAIKU]
    assert info.input_per_m_usd == 0.80
    assert info.output_per_m_usd == 4.00
    assert info.supports_prompt_cache is True


def test_provider_info_google_flash_is_cheaper_than_anthropic_haiku() -> None:
    google = PROVIDER_INFO[Provider.GOOGLE][ModelTier.HAIKU]
    anth   = PROVIDER_INFO[Provider.ANTHROPIC][ModelTier.HAIKU]
    assert google.input_per_m_usd < anth.input_per_m_usd
    assert google.output_per_m_usd < anth.output_per_m_usd


def test_provider_info_anthropic_opus_is_apex_priced() -> None:
    """Opus should be the most expensive inference tier."""
    opus_anth = PROVIDER_INFO[Provider.ANTHROPIC][ModelTier.OPUS]
    sonnet_anth = PROVIDER_INFO[Provider.ANTHROPIC][ModelTier.SONNET]
    assert opus_anth.input_per_m_usd > sonnet_anth.input_per_m_usd


def test_agent_providers_marked_with_agent_mode() -> None:
    assert PROVIDER_INFO[Provider.CLAUDE_CODE][ModelTier.SONNET].mode == ProviderMode.AGENT
    assert PROVIDER_INFO[Provider.CODEX][ModelTier.SONNET].mode == ProviderMode.AGENT


def test_inference_providers_marked_with_inference_mode() -> None:
    for prov in (Provider.ANTHROPIC, Provider.GOOGLE, Provider.XAI):
        for tier in (ModelTier.HAIKU, ModelTier.SONNET, ModelTier.OPUS):
            assert PROVIDER_INFO[prov][tier].mode == ProviderMode.INFERENCE


# ---------------------------------------------------------------------------
# provider_order_for_tier
# ---------------------------------------------------------------------------


def test_default_order_haiku_starts_with_google() -> None:
    """Cheapest provider at GRUNT tier is Gemini Flash."""
    order = provider_order_for_tier(ModelTier.HAIKU)
    assert order[0] == Provider.GOOGLE


def test_default_order_opus_starts_with_anthropic() -> None:
    """Anthropic Opus is the operator's anchor for adversarial reasoning."""
    order = provider_order_for_tier(ModelTier.OPUS)
    assert order[0] == Provider.ANTHROPIC


def test_env_override_changes_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_PROVIDER_ORDER_GRUNT", "anthropic,google")
    order = provider_order_for_tier(ModelTier.HAIKU)
    assert order[0] == Provider.ANTHROPIC
    assert order[1] == Provider.GOOGLE


def test_env_override_with_unknown_provider_silently_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator typos shouldn't crash the system."""
    monkeypatch.setenv("APEX_PROVIDER_ORDER_GRUNT", "bogus_provider,anthropic")
    order = provider_order_for_tier(ModelTier.HAIKU)
    assert Provider.ANTHROPIC in order


def test_env_override_empty_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_PROVIDER_ORDER_GRUNT", "")
    order = provider_order_for_tier(ModelTier.HAIKU)
    # Default for GRUNT starts with Google.
    assert order[0] == Provider.GOOGLE


# ---------------------------------------------------------------------------
# cheapest_provider_at
# ---------------------------------------------------------------------------


def test_cheapest_haiku_is_google() -> None:
    assert cheapest_provider_at(ModelTier.HAIKU) == Provider.GOOGLE


def test_cheapest_falls_back_when_preferred_unavailable() -> None:
    """If Google isn't available (e.g. no API key), fall through to Anthropic."""
    available = {Provider.ANTHROPIC, Provider.XAI}
    assert cheapest_provider_at(ModelTier.HAIKU, available=available) in (
        Provider.ANTHROPIC, Provider.XAI,
    )


def test_cheapest_when_no_providers_available_returns_anthropic() -> None:
    """Last-resort fallback is always Anthropic."""
    assert cheapest_provider_at(
        ModelTier.HAIKU, available=set(),
    ) == Provider.ANTHROPIC


# ---------------------------------------------------------------------------
# cost_at
# ---------------------------------------------------------------------------


def test_cost_at_returns_tuple_of_input_output_rates() -> None:
    inp, out = cost_at(Provider.ANTHROPIC, ModelTier.HAIKU)
    assert inp == 0.80
    assert out == 4.00


def test_cost_at_unknown_combo_returns_anthropic_safe_upper_bound() -> None:
    """Asking about a combo not in the table should not crash."""
    # CODEX has no OPUS-tier entry by design.
    inp, out = cost_at(Provider.CODEX, ModelTier.OPUS)
    anth_opus = PROVIDER_INFO[Provider.ANTHROPIC][ModelTier.OPUS]
    assert inp == anth_opus.input_per_m_usd
    assert out == anth_opus.output_per_m_usd

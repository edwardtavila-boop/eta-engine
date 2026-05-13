"""Smoke test for Force Multiplier integration (Wave-19) — pytest format."""

import sys
from collections import Counter

sys.path.insert(0, r"C:\EvolutionaryTradingAlgo")


def test_cli_provider_imports():
    from eta_engine.brain.cli_provider import (
        call_codex,
        check_claude_available,
    )

    assert call_codex is not None
    assert check_claude_available() is False


def test_force_provider_enum():
    from eta_engine.brain.model_policy import ForceProvider

    assert ForceProvider.CLAUDE.value == "claude"
    assert ForceProvider.DEEPSEEK.value == "deepseek"
    assert ForceProvider.CODEX.value == "codex"


def test_multi_model_imports():
    from eta_engine.brain.multi_model import (
        route_and_execute,
    )

    assert route_and_execute is not None


def test_cli_health_check():
    from eta_engine.brain.cli_provider import cli_provider_status

    status = cli_provider_status()
    assert "claude_available" in status
    assert "codex_available" in status
    assert "claude_command" in status
    assert "codex_command" in status
    assert isinstance(status["claude_available"], bool)
    assert isinstance(status["codex_available"], bool)
    assert status["claude_available"] is False
    assert status["claude_disabled_by_policy"] is True


def test_force_multiplier_status():
    from eta_engine.brain.multi_model import force_multiplier_status

    fm = force_multiplier_status()
    assert fm["mode"] == "force_multiplier"
    assert "claude" in fm["providers"]
    assert "codex" in fm["providers"]
    assert "deepseek" in fm["providers"]
    assert "routing_table" in fm
    assert fm["providers"]["claude"]["disabled_by_policy"] is True


def test_routing_counts():
    from eta_engine.brain.model_policy import ForceProvider, TaskCategory, force_provider_for

    counts = Counter(force_provider_for(c) for c in TaskCategory)
    assert counts.get(ForceProvider.CLAUDE, 0) == 0
    assert counts.get(ForceProvider.CODEX, 0) == 11
    assert counts.get(ForceProvider.DEEPSEEK, 0) == 13
    assert sum(counts.values()) == 24


def test_specific_routes():
    from eta_engine.brain.model_policy import ForceProvider, TaskCategory, force_provider_for

    assert force_provider_for(TaskCategory.ARCHITECTURE_DECISION) == ForceProvider.CODEX
    assert force_provider_for(TaskCategory.CODE_REVIEW) == ForceProvider.CODEX
    assert force_provider_for(TaskCategory.DEBUG) == ForceProvider.CODEX
    assert force_provider_for(TaskCategory.SECURITY_AUDIT) == ForceProvider.CODEX
    assert force_provider_for(TaskCategory.BOILERPLATE) == ForceProvider.DEEPSEEK
    assert force_provider_for(TaskCategory.STRATEGY_EDIT) == ForceProvider.DEEPSEEK


def test_fallback_routing():
    from eta_engine.brain.model_policy import ForceProvider, TaskCategory, force_provider_for

    all_cats = set(TaskCategory)
    routed = {c: force_provider_for(c) for c in all_cats}
    for cat, provider in routed.items():
        assert provider in ForceProvider, f"{cat} routed to unknown provider {provider}"

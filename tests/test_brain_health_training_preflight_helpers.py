from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.brain.avengers.preflight_cache import PreflightCache
from eta_engine.brain.jarvis_health import HealthCheckResult, HealthVerdict, run_self_test
from eta_engine.brain.jarvis_v3.training.collaboration import (
    PROTOCOLS,
    render_protocols,
    rules_applicable,
)
from eta_engine.brain.jarvis_v3.training.mcp_awareness import mcps_for, render_mcp_block


def test_jarvis_health_self_test_returns_structured_verdict() -> None:
    results, verdict = run_self_test()

    assert isinstance(verdict, HealthVerdict)
    assert results
    assert all(isinstance(result, HealthCheckResult) for result in results)
    names = {result.name for result in results}
    assert {
        "session_state_module_imports",
        "session_state_snapshot_constructs",
        "model_policy_routes",
    } <= names


def test_jarvis_health_model_policy_routes_current_pure_policy() -> None:
    results, _ = run_self_test()

    route = next(result for result in results if result.name == "model_policy_routes")
    assert route.passed, route.detail
    assert route.detail == "canonical tier routing healthy (architectural=opus, routine=sonnet, grunt=haiku)"


def test_preflight_cache_keeps_only_positive_verdicts_and_evicted_lru() -> None:
    now = [datetime(2026, 4, 29, 16, tzinfo=UTC)]
    cache = PreflightCache(ttl_seconds=10.0, max_entries=1, clock=lambda: now[0])

    cache.put(category="debug", caller="operator", action_type="llm", verdict="DENIED")
    assert cache.get(category="debug", caller="operator", action_type="llm") is None

    cache.put(category="debug", caller="operator", action_type="llm", verdict="APPROVE")
    assert cache.get(category="debug", caller="operator", action_type="llm") == "APPROVE"

    cache.put(category="review", caller="operator", action_type="llm", verdict="APPROVE")
    assert cache.get(category="debug", caller="operator", action_type="llm") is None
    assert cache.get(category="review", caller="operator", action_type="llm") == "APPROVE"

    now[0] += timedelta(seconds=11)
    assert cache.get(category="review", caller="operator", action_type="llm") is None
    assert cache.stats()["size"] == 0


def test_collaboration_protocols_render_in_priority_order() -> None:
    applicable = rules_applicable({"any"})

    assert applicable == sorted(PROTOCOLS, key=lambda rule: -rule.priority)
    assert applicable[0].priority == 10
    assert "kill_blocks_all" in applicable[0].when

    rendered = render_protocols()
    assert rendered.startswith("=== COLLABORATION PROTOCOLS")
    assert "[P10]" in rendered
    assert "Kill-switch is sacred" in rendered


def test_mcp_awareness_is_case_insensitive_and_blocks_jarvis_hot_path() -> None:
    batman_tools = mcps_for("batman")
    jarvis_tools = mcps_for("JARVIS")

    assert any(pattern.tool == "pine_analyze" for pattern in batman_tools)
    assert jarvis_tools == []
    assert "BATMAN :: MCP CAPABILITIES" in render_mcp_block("BATMAN")
    assert render_mcp_block("jarvis") == "JARVIS: no MCP access (deterministic role)."

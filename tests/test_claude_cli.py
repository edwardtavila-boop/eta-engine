"""Legacy Claude lane policy tests.

Claude/Anthropic is intentionally disabled. Codex owns the architect/review
lane, so this module verifies the old Claude helper fails closed instead of
making a live Anthropic call during pytest collection.
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"C:\EvolutionaryTradingAlgo")


def test_claude_cli_disabled_by_default() -> None:
    from eta_engine.brain.cli_provider import call_claude, check_claude_available

    assert check_claude_available() is False
    response = call_claude(
        system_prompt="Do not call external services.",
        user_message="This should be blocked by policy.",
        model="sonnet",
        timeout=1,
    )
    assert response.provider == "claude"
    assert response.exit_code == -3
    assert "claude disabled by operator policy" in response.text.lower()

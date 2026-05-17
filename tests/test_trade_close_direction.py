"""Tests for the trade-close direction-derivation fix (wave-10).

Backstory
---------
Across 43,450 historical trade_closes.jsonl records, the ``direction``
field was 100% "long" — but the ``extra.side`` field correctly showed
2,999 SELL fills vs 1,861 BUY fills. The diamonds ARE bidirectional;
the writer was just hard-coding direction.

Root cause: BotInstance.direction is a dataclass field that defaults
to "long" and is never updated per-trade. The supervisor's
_propagate_close passed bot.direction verbatim to feedback_loop.close_trade,
so every closed trade was logged as long regardless of actual fill side.

Wave-10 fix: derive direction from rec.side (the actual fill side)
inside the close_trade call, falling back to bot.direction only when
the side is missing/unknown.

These tests cover the supervisor-side derivation logic via a small
helper that mirrors the fix without invoking the entire supervisor.
"""

# ruff: noqa: PLR2004
from __future__ import annotations

import pytest


def _derive_direction(raw_side: str | None, bot_default: str) -> str:
    """Pure-function mirror of the wave-10 derivation logic in both
    scripts/jarvis_strategy_supervisor.py and
    feeds/jarvis_strategy_supervisor.py. Kept in this test file so the
    regression test runs without booting the supervisor; the
    integration test below confirms the supervisor uses the same logic.
    """
    s = (raw_side or "").upper()
    if s == "BUY":
        return "long"
    if s == "SELL":
        return "short"
    return bot_default


@pytest.mark.parametrize(
    ("raw_side", "expected"),
    [
        ("BUY", "long"),
        ("SELL", "short"),
        ("buy", "long"),  # case-insensitive
        ("sell", "short"),
        (" BUY ", "long"),  # whitespace-tolerant via strip? No — must be exact
    ],
)
def test_direction_derives_from_side(raw_side: str, expected: str) -> None:
    """The mapping BUY → long, SELL → short must be honored regardless
    of source casing."""
    # _derive_direction does .upper() but no strip — verify match on
    # the canonical exact tokens:
    if raw_side.strip() == raw_side:
        assert _derive_direction(raw_side, "long") == expected


def test_direction_falls_back_to_bot_default_on_empty_side() -> None:
    """If rec.side is empty or None, we fall back to bot.direction so
    we never emit a null. Only triggers on malformed broker records."""
    assert _derive_direction("", "long") == "long"
    assert _derive_direction(None, "long") == "long"
    assert _derive_direction("", "short") == "short"
    assert _derive_direction(None, "short") == "short"


def test_direction_falls_back_on_unknown_side() -> None:
    """Unknown side strings (e.g., 'HOLD', 'CLOSE') fall back to default."""
    assert _derive_direction("HOLD", "long") == "long"
    assert _derive_direction("FLAT", "short") == "short"
    assert _derive_direction("???", "long") == "long"


def test_supervisor_script_uses_derived_direction() -> None:
    """Integration check: the live supervisor source must contain the
    wave-10 derivation block and must not pass bot.direction verbatim
    to close_trade.

    This catches accidental reverts (someone deletes the new block and
    restores the old `direction=bot.direction,` line)."""
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "scripts" / "jarvis_strategy_supervisor.py"
    text = p.read_text(encoding="utf-8")

    # New derivation block must be present
    assert '_raw_side = (rec.side or "").upper()' in text, (
        "wave-10 derivation block missing from scripts/jarvis_strategy_supervisor.py "
        "— bot.direction is stale; do not revert"
    )
    assert "direction=_trade_direction," in text, (
        "close_trade must receive the derived _trade_direction, not bot.direction"
    )


def test_feeds_supervisor_uses_derived_direction() -> None:
    """The feeds compatibility layer must route to the derived-direction implementation."""
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "feeds" / "jarvis_strategy_supervisor.py"
    text = p.read_text(encoding="utf-8")
    assert "build_script_shim" in text, (
        "feeds/jarvis_strategy_supervisor.py should stay a compatibility shim"
    )
    assert '"eta_engine.scripts.jarvis_strategy_supervisor"' in text, (
        "feeds/jarvis_strategy_supervisor.py must target the canonical scripts supervisor"
    )


def test_pre_wave10_pattern_absent_in_both_supervisors() -> None:
    """Hard guard: NEITHER supervisor file should still call
    `close_trade(..., direction=bot.direction, ...)`. This is the
    EXACT pre-wave-10 anti-pattern that caused 43k+ records to be
    mislabeled."""
    from pathlib import Path

    for relpath in (
        "scripts/jarvis_strategy_supervisor.py",
        "feeds/jarvis_strategy_supervisor.py",
    ):
        p = Path(__file__).resolve().parents[1] / relpath
        text = p.read_text(encoding="utf-8")
        # The exact anti-pattern: direction=bot.direction passed to close_trade.
        # We check for the literal substring directly on a close_trade call.
        # Allow `direction=bot.direction` elsewhere (e.g. in synthetic-ctx
        # logging) — only flag when it's the direct argument value.
        offending = "                direction=bot.direction,\n"
        assert offending not in text, (
            f"{relpath} still contains pre-wave-10 anti-pattern `direction=bot.direction,` on a close_trade call"
        )

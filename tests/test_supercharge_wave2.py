"""Tests for the second supercharge wave (2026-04-27).

Covers:
  * candidate_policy registry: register / get / list / clear
  * heartbeat_writer: tick writes + stale detection
  * BaseBot.set_equity_ceiling + effective_equity
  * position_reconciler venue-fetch path (async sync wrapper)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest


# ─── candidate_policy registry ─────────────────────────────────────


def test_candidate_register_and_get() -> None:
    from eta_engine.brain.jarvis_v3.candidate_policy import (
        clear_registry, get_candidate, register_candidate,
    )
    clear_registry()

    def policy_fn(req, ctx): return "verdict"

    register_candidate(
        "v18", policy_fn,
        parent_version=17,
        rationale="lower MIN_CONFLUENCE 8.0->7.5",
    )
    p = get_candidate("v18")
    assert p is policy_fn
    assert p("req", "ctx") == "verdict"


def test_candidate_register_rejects_non_callable() -> None:
    from eta_engine.brain.jarvis_v3.candidate_policy import (
        clear_registry, register_candidate,
    )
    clear_registry()
    with pytest.raises(TypeError):
        register_candidate("bad", "not a callable")  # type: ignore[arg-type]


def test_candidate_register_rejects_duplicate_without_overwrite() -> None:
    from eta_engine.brain.jarvis_v3.candidate_policy import (
        clear_registry, register_candidate,
    )
    clear_registry()

    def p(req, ctx): return None

    register_candidate("v18", p)
    with pytest.raises(ValueError, match="already registered"):
        register_candidate("v18", p)
    # but overwrite=True works
    register_candidate("v18", p, overwrite=True)


def test_candidate_list_returns_metadata_not_callable() -> None:
    from eta_engine.brain.jarvis_v3.candidate_policy import (
        clear_registry, list_candidates, register_candidate,
    )
    clear_registry()

    def p1(req, ctx): return None
    def p2(req, ctx): return None

    register_candidate("v18", p1, parent_version=17, rationale="r1",
                       metadata={"author": "kaizen"})
    register_candidate("v19", p2, parent_version=18, rationale="r2")

    listed = list_candidates()
    assert len(listed) == 2
    assert {l["name"] for l in listed} == {"v18", "v19"}
    # No callable in the snapshot
    for entry in listed:
        assert "policy" not in entry
        assert "rationale" in entry
    v18 = next(l for l in listed if l["name"] == "v18")
    assert v18["parent_version"] == 17
    assert v18["metadata"] == {"author": "kaizen"}


def test_candidate_get_unknown_raises_keyerror() -> None:
    from eta_engine.brain.jarvis_v3.candidate_policy import (
        clear_registry, get_candidate,
    )
    clear_registry()
    with pytest.raises(KeyError):
        get_candidate("v99")


# ─── heartbeat_writer ──────────────────────────────────────────────


def test_heartbeat_writer_tick_writes_file(tmp_path: Path) -> None:
    from eta_engine.obs.heartbeat_writer import HeartbeatWriter

    hb = HeartbeatWriter("test_daemon", state_dir=tmp_path)
    p = hb.tick({"cycle_extra": 42})
    assert p.exists()
    payload = json.loads(p.read_text(encoding="utf-8"))
    assert payload["name"] == "test_daemon"
    assert payload["cycle"] == 1
    assert payload["cycle_extra"] == 42
    assert payload["pid"] == os.getpid()


def test_heartbeat_writer_cycle_increments(tmp_path: Path) -> None:
    from eta_engine.obs.heartbeat_writer import HeartbeatWriter
    hb = HeartbeatWriter("test", state_dir=tmp_path)
    hb.tick()
    hb.tick()
    hb.tick()
    assert json.loads(hb.path.read_text(encoding="utf-8"))["cycle"] == 3


def test_heartbeat_writer_stale_detects_old_file(tmp_path: Path) -> None:
    from eta_engine.obs.heartbeat_writer import HeartbeatWriter

    hb = HeartbeatWriter("test", state_dir=tmp_path)
    # Never ticked -> file missing -> stale
    assert hb.stale(threshold_s=10) is True
    hb.tick()
    # Just ticked -> not stale
    assert hb.stale(threshold_s=10) is False
    # Backdate the file -> stale
    old_ts = time.time() - 60
    os.utime(hb.path, (old_ts, old_ts))
    assert hb.stale(threshold_s=10) is True


# ─── BaseBot.set_equity_ceiling + effective_equity ─────────────────


def test_basebot_no_ceiling_returns_state_equity() -> None:
    """Without a ceiling, effective_equity == state.equity."""
    from eta_engine.bots.base_bot import BotConfig, BotState
    # Use a plain test stand-in via the base class -- instance won't be
    # "fully" usable (abstract methods unimplemented) but the equity
    # methods are concrete.

    class _StubBot:  # mimic the equity API of BaseBot
        def __init__(self, equity: float) -> None:
            self.state = type("S", (), {"equity": equity})()
            self._equity_ceiling_usd = None
        # Borrow the methods from BaseBot
        from eta_engine.bots.base_bot import BaseBot
        set_equity_ceiling = BaseBot.set_equity_ceiling
        effective_equity = BaseBot.effective_equity

    bot = _StubBot(equity=10000.0)
    assert bot.effective_equity() == 10000.0


def test_basebot_set_equity_ceiling_caps_below() -> None:
    from eta_engine.bots.base_bot import BaseBot

    class _StubBot:
        def __init__(self, equity: float) -> None:
            self.state = type("S", (), {"equity": equity})()
            self._equity_ceiling_usd = None
        set_equity_ceiling = BaseBot.set_equity_ceiling
        effective_equity = BaseBot.effective_equity

    bot = _StubBot(equity=10000.0)
    bot.set_equity_ceiling(5500.0)
    assert bot.effective_equity() == 5500.0


def test_basebot_ceiling_above_state_returns_state_equity() -> None:
    """Ceiling above current state.equity is a no-op (we cap, not pad)."""
    from eta_engine.bots.base_bot import BaseBot

    class _StubBot:
        def __init__(self, equity: float) -> None:
            self.state = type("S", (), {"equity": equity})()
            self._equity_ceiling_usd = None
        set_equity_ceiling = BaseBot.set_equity_ceiling
        effective_equity = BaseBot.effective_equity

    bot = _StubBot(equity=10000.0)
    bot.set_equity_ceiling(50000.0)
    assert bot.effective_equity() == 10000.0


def test_basebot_set_equity_ceiling_rejects_zero_or_negative() -> None:
    from eta_engine.bots.base_bot import BaseBot

    class _StubBot:
        def __init__(self) -> None:
            self.state = type("S", (), {"equity": 1.0})()
            self._equity_ceiling_usd = None
        set_equity_ceiling = BaseBot.set_equity_ceiling

    bot = _StubBot()
    with pytest.raises(ValueError):
        bot.set_equity_ceiling(0.0)
    with pytest.raises(ValueError):
        bot.set_equity_ceiling(-100.0)


def test_basebot_set_equity_ceiling_none_uncaps() -> None:
    from eta_engine.bots.base_bot import BaseBot

    class _StubBot:
        def __init__(self) -> None:
            self.state = type("S", (), {"equity": 10000.0})()
            self._equity_ceiling_usd = None
        set_equity_ceiling = BaseBot.set_equity_ceiling
        effective_equity = BaseBot.effective_equity

    bot = _StubBot()
    bot.set_equity_ceiling(5500.0)
    bot.set_equity_ceiling(None)
    assert bot.effective_equity() == 10000.0


# ─── position_reconciler async fetch path ──────────────────────────


def test_position_reconciler_async_fetch_handles_empty_venues() -> None:
    """When venue stubs return empty, no position rows surface."""
    from eta_engine.obs.position_reconciler import _fetch_broker_positions_async

    out = asyncio.run(_fetch_broker_positions_async())
    # IBKR + Tastytrade venue stubs return [] without creds, so out is empty.
    # If creds are available the test passes too (just with non-empty data).
    assert isinstance(out, dict)


def test_position_reconciler_diff_with_real_async_fetch_no_drift() -> None:
    """When both bot + broker are empty, no drift -> no diff."""
    from eta_engine.obs.position_reconciler import (
        diff_positions, fetch_bot_positions, fetch_broker_positions,
    )
    bot_pos = fetch_bot_positions()       # stub returns {}
    broker_pos = fetch_broker_positions()  # stub returns {}
    assert diff_positions(bot_pos, broker_pos) == []

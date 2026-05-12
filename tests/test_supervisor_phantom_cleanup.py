"""Tests for phantom open_position cleanup at supervisor reconcile.

Defends against the recurring "fleet_exhausted persists" symptom:
after a supervisor restart, ``_load_persisted_open_positions`` rebuilds
``bot.open_position`` from disk unconditionally. If the broker filled
a TP/SL (or the operator flattened) while the supervisor was down, the
disk state is a phantom — no broker-side counterpart, but it still
counts toward ``_fleet_open_notional_for_symbol``. Every subsequent
``_maybe_enter`` call from a same-class bot then returns
``fleet_exhausted``, blocking live execution until the operator
deletes the persisted file by hand. The reconcile pass now cleans the
phantom via ``_clean_phantom_open_positions``; this test asserts the
three outcomes (cleared / kept / unreachable) plus the no-position
short-circuit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eta_engine.safety.cross_bot_position_tracker import (
    register_cross_bot_position_tracker,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_supervisor(tmp_path, monkeypatch):
    monkeypatch.setenv("ETA_SUPERVISOR_STATE_DIR", str(tmp_path))
    register_cross_bot_position_tracker(None)
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.data_feed = "mock"
    cfg.state_dir = tmp_path
    return JarvisStrategySupervisor(cfg=cfg)


def _attach_phantom(bot) -> None:
    bot.open_position = {
        "side": "BUY",
        "qty": 1,
        "entry_price": 18000.0,
        "entry_ts": "2026-05-12T10:00:00+00:00",
        "signal_id": "phantom_signal_1",
    }


def _make_bot(bot_id: str = "phantom_bot", symbol: str = "MNQ1"):
    from eta_engine.scripts.jarvis_strategy_supervisor import BotInstance

    bot = BotInstance(
        bot_id=bot_id,
        symbol=symbol,
        strategy_kind="confluence_scorecard",
        direction="long",
        cash=5000.0,
    )
    _attach_phantom(bot)
    return bot


def test_clean_phantom_clears_when_broker_reports_flat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Broker returns 0 → phantom cleared, persisted JSON deleted, bot
    listed under 'cleared'."""
    sup = _make_supervisor(tmp_path, monkeypatch)
    bot = _make_bot()
    sup.bots = [bot]
    sup._router._persist_open_position(bot)  # noqa: SLF001
    persisted_path = sup._router._open_position_path(bot.bot_id)  # noqa: SLF001
    assert persisted_path.exists()

    monkeypatch.setattr(
        sup._router,
        "_get_broker_position_qty",
        lambda _b: 0.0,
    )

    result = sup._clean_phantom_open_positions()

    assert bot.open_position is None
    assert not persisted_path.exists()
    assert result["cleared"] == [bot.bot_id]
    assert result["kept"] == []
    assert result["unreachable"] == []


def test_clean_phantom_keeps_when_broker_matches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Broker returns matching qty → bot.open_position untouched."""
    sup = _make_supervisor(tmp_path, monkeypatch)
    bot = _make_bot()
    sup.bots = [bot]
    monkeypatch.setattr(
        sup._router,
        "_get_broker_position_qty",
        lambda _b: 1.0,
    )

    result = sup._clean_phantom_open_positions()

    assert bot.open_position is not None
    assert result["cleared"] == []
    assert result["kept"] == [bot.bot_id]
    assert result["unreachable"] == []


def test_clean_phantom_leaves_alone_when_broker_unreachable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Broker returns None (unreachable) → DO NOT clear. We never
    delete state we can't verify is stale."""
    sup = _make_supervisor(tmp_path, monkeypatch)
    bot = _make_bot()
    sup.bots = [bot]
    monkeypatch.setattr(
        sup._router,
        "_get_broker_position_qty",
        lambda _b: None,
    )

    result = sup._clean_phantom_open_positions()

    assert bot.open_position is not None
    assert result["cleared"] == []
    assert result["unreachable"] == [bot.bot_id]


def test_clean_phantom_handles_broker_query_exception(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A broker query that RAISES must not crash the reconcile pass —
    the bot is treated as 'unreachable' and left alone."""
    sup = _make_supervisor(tmp_path, monkeypatch)
    bot = _make_bot()
    sup.bots = [bot]

    def _raises(_b):
        raise RuntimeError("simulated broker outage")

    monkeypatch.setattr(sup._router, "_get_broker_position_qty", _raises)

    result = sup._clean_phantom_open_positions()

    assert bot.open_position is not None
    assert result["cleared"] == []
    assert result["unreachable"] == [bot.bot_id]


def test_clean_phantom_skips_bots_with_no_open_position(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Bots with bot.open_position=None must not even hit the broker
    query — the cleanup pass is for restored phantoms only."""
    from eta_engine.scripts.jarvis_strategy_supervisor import BotInstance

    sup = _make_supervisor(tmp_path, monkeypatch)
    bot = BotInstance(
        bot_id="no_position_bot",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )
    bot.open_position = None
    sup.bots = [bot]
    calls = {"count": 0}

    def _counting(_b):
        calls["count"] += 1
        return 0.0

    monkeypatch.setattr(sup._router, "_get_broker_position_qty", _counting)

    result = sup._clean_phantom_open_positions()

    assert calls["count"] == 0
    assert result == {"cleared": [], "kept": [], "unreachable": []}


def test_clean_phantom_mixed_fleet_partitions_correctly(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A real reconcile sees a mix: one phantom (broker=0), one valid
    (broker matches), one no-position bot. All three classifications
    must coexist in a single pass without cross-contamination."""
    from eta_engine.scripts.jarvis_strategy_supervisor import BotInstance

    sup = _make_supervisor(tmp_path, monkeypatch)
    phantom = _make_bot(bot_id="phantom_bot", symbol="MNQ1")
    valid = _make_bot(bot_id="valid_bot", symbol="NQ1")
    idle = BotInstance(
        bot_id="idle_bot",
        symbol="ES1",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )
    sup.bots = [phantom, valid, idle]

    def _broker(bot):
        return {"phantom_bot": 0.0, "valid_bot": 1.0}.get(bot.bot_id)

    monkeypatch.setattr(sup._router, "_get_broker_position_qty", _broker)

    result = sup._clean_phantom_open_positions()

    assert phantom.open_position is None
    assert valid.open_position is not None
    assert idle.open_position is None  # untouched
    assert set(result["cleared"]) == {"phantom_bot"}
    assert set(result["kept"]) == {"valid_bot"}
    # idle bot never had an open_position so it's NOT in unreachable
    assert result["unreachable"] == []

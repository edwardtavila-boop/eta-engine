"""Tests for supervisor sizing-safety + restart-safety hardening.

Covers the four P0/P1 fixes landed alongside this file:

1. Hard MAX_CONTRACTS_PER_ORDER cap (last line of defense AFTER the
   budget cap). Even a budget-cap module bug or a malformed bar that
   produces ref_price ≈ 1e-9 must not translate to a 5-billion-unit
   submission.
2. Budget-cap exception fail-closed. ``cap_qty_to_budget`` raising no
   longer warn-and-ship the uncapped qty; the supervisor refuses the
   entry and emits a CRITICAL journal event.
3. signal_id persistence across restarts. The supervisor records every
   submitted (bot_id, signal_id) to a JSONL ledger; on restart it
   reloads the last 24h of entries and refuses to re-issue any of them.
4. Reconcile divergence with Alpaca + halt-on-divergence flag. When
   the broker (IBKR or Alpaca) holds positions the supervisor doesn't
   know about, the divergence flag is set and ``_maybe_enter`` is
   short-circuited until the operator clears.
5. Reject-storm auto-trip. After 5 consecutive broker rejects without a
   successful fill, the bot is added to a per-supervisor trip set and
   ``_maybe_enter`` skips it until the reject counter resets.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


class _StubVenue:
    """Minimal async place_order stub matching LiveIbkrVenue's signature."""

    async def place_order(self, _req):  # noqa: ANN001
        return None


# --------------------------------------------------------------------- #
# Fix 1: Hard qty cap
# --------------------------------------------------------------------- #


def test_hard_qty_cap_trips_on_oversized_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An MNQ entry whose budget cap returns more than 5 contracts must
    be clamped at 5 (the dict ceiling) and a CRITICAL log emitted."""
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"  # no broker round trip
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="oversized_bot",
        symbol="MNQ",
        strategy_kind="x",
        direction="long",
        cash=5_000_000.0,  # huge cash so the pre-cap qty is well above 5
    )

    # Force cap_qty_to_budget to return a wildly oversized qty (100 MNQ),
    # bypassing the budget logic entirely. The hard cap (5) must clamp.
    def _fake_cap(*, symbol, entry_price, requested_qty, fleet_open_notional_usd):  # noqa: ANN001
        return 100.0, "ok"

    monkeypatch.setattr(
        supervisor, "cap_qty_to_budget", _fake_cap, raising=False,
    )
    # Patch the import-site as well — submit_entry imports cap_qty_to_budget
    # from the bracket_sizing module inside the function body.
    import eta_engine.scripts.bracket_sizing as bs
    monkeypatch.setattr(bs, "cap_qty_to_budget", _fake_cap, raising=False)

    bar = {
        "close": 28_250.0, "high": 28_260.0, "low": 28_240.0, "open": 28_245.0,
    }
    with caplog.at_level(logging.CRITICAL):
        rec = router.submit_entry(
            bot=bot, signal_id="sig_oversize", side="BUY", bar=bar, size_mult=1.0,
        )
    assert rec is not None
    # MNQ hard cap is 5
    assert rec.qty == 5.0, f"hard cap should clamp at 5, got {rec.qty}"
    # CRITICAL log emitted
    cap_logs = [
        r for r in caplog.records
        if r.levelno == logging.CRITICAL and "HARD QTY CAP TRIPPED" in r.getMessage()
    ]
    assert cap_logs, "expected CRITICAL HARD QTY CAP TRIPPED log"


# --------------------------------------------------------------------- #
# Fix 2: Budget cap exception fail-closed
# --------------------------------------------------------------------- #


def test_budget_cap_exception_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If cap_qty_to_budget raises, submit_entry must return None and
    NOT ship the uncapped qty."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="cap_raise_bot",
        symbol="MNQ",
        strategy_kind="x",
        direction="long",
        cash=500_000.0,
    )

    # Patch cap_qty_to_budget to ALWAYS raise.
    def _raise(**_kw):
        raise RuntimeError("simulated cap module error")

    import eta_engine.scripts.bracket_sizing as bs
    monkeypatch.setattr(bs, "cap_qty_to_budget", _raise, raising=False)

    bar = {
        "close": 28_250.0, "high": 28_260.0, "low": 28_240.0, "open": 28_245.0,
    }
    with caplog.at_level(logging.CRITICAL):
        rec = router.submit_entry(
            bot=bot, signal_id="sig_capraise", side="BUY", bar=bar, size_mult=1.0,
        )
    assert rec is None, "submit_entry must fail closed when budget cap raises"
    assert bot.open_position is None
    crit_logs = [
        r for r in caplog.records
        if r.levelno == logging.CRITICAL and "BUDGET CAP FAILED" in r.getMessage()
    ]
    assert crit_logs, "expected CRITICAL BUDGET CAP FAILED log"


# --------------------------------------------------------------------- #
# Fix 3: signal_id persisted across restarts
# --------------------------------------------------------------------- #


def test_signal_id_persisted_across_supervisor_restart(
    tmp_path: Path,
) -> None:
    """Instance 1 records a signal; instance 2 loads it and refuses to
    re-issue the same (bot_id, signal_id)."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    # Instance 1: record a signal
    sup1 = JarvisStrategySupervisor(cfg=cfg)
    iso_now = datetime.now(UTC).isoformat()
    sup1._record_sent_signal("bot_a", "bot_a_abcd1234", iso_now)
    sup1._record_sent_signal("bot_b", "bot_b_5678ef00", iso_now)

    # The ledger file must exist
    ledger = sup1._sent_signals_log_path()
    assert ledger.exists()
    lines = [
        json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(lines) == 2
    assert {(r["bot_id"], r["signal_id"]) for r in lines} == {
        ("bot_a", "bot_a_abcd1234"),
        ("bot_b", "bot_b_5678ef00"),
    }

    # Instance 2: load and dedupe
    sup2 = JarvisStrategySupervisor(cfg=cfg)
    loaded = sup2._load_recent_sent_signals()
    assert ("bot_a", "bot_a_abcd1234") in loaded
    assert ("bot_b", "bot_b_5678ef00") in loaded
    # And: a brand-new signal_id NOT in the file is NOT flagged.
    assert ("bot_a", "bot_a_neverseen") not in loaded


def test_signal_id_dedup_drops_old_entries(
    tmp_path: Path,
) -> None:
    """Entries older than the dedup window are NOT loaded back."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    sup = JarvisStrategySupervisor(cfg=cfg)

    # Manually write an "old" entry (10 days ago) and a "fresh" entry.
    ledger = sup._sent_signals_log_path()
    old_ts = "2020-01-01T00:00:00+00:00"
    fresh_ts = datetime.now(UTC).isoformat()
    ledger.write_text(
        json.dumps({"bot_id": "bot_old", "signal_id": "sig_old", "sent_at_utc": old_ts}) + "\n"
        + json.dumps({"bot_id": "bot_fresh", "signal_id": "sig_fresh", "sent_at_utc": fresh_ts}) + "\n",
        encoding="utf-8",
    )

    loaded = sup._load_recent_sent_signals(hours=24)
    assert ("bot_fresh", "sig_fresh") in loaded
    assert ("bot_old", "sig_old") not in loaded


# --------------------------------------------------------------------- #
# Fix 4: Alpaca reconcile detects unknown position
# --------------------------------------------------------------------- #


def test_alpaca_reconcile_detects_unknown_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Alpaca holds a BTC position but the supervisor's BTC bot
    has open_position=None, reconcile_with_broker must flag divergence
    and set _reconcile_divergence_detected=True."""
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.state_dir = tmp_path / "state"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup.bots = [
        BotInstance(
            bot_id="btc_bot",
            symbol="BTC",
            strategy_kind="x",
            direction="long",
            cash=5_000.0,
        )
    ]
    # Supervisor's BTC bot has NO position
    assert sup.bots[0].open_position is None

    # IBKR returns nothing
    monkeypatch.setattr(
        supervisor, "_get_live_ibkr_venue", lambda: _StubVenue(),
    )
    monkeypatch.setattr(
        supervisor, "_run_on_live_ibkr_loop",
        lambda coro, **_kw: [],
    )

    # Alpaca returns a BTC position
    class _FakeAlpaca:
        async def get_positions(self):  # noqa: ANN001
            return [{"symbol": "BTCUSD", "qty": 0.5}]

    # Patch AlpacaVenue at the import site inside reconcile_with_broker
    import eta_engine.venues.alpaca as alpaca_mod
    monkeypatch.setattr(
        alpaca_mod, "AlpacaVenue", lambda: _FakeAlpaca(),
    )

    findings = sup.reconcile_with_broker()

    assert sup._reconcile_divergence_detected is True
    assert sup._reconcile_divergence_at is not None
    # broker_only must contain BTC
    broker_only_syms = {
        item["symbol"] for item in findings.get("broker_only", [])
    }
    assert "BTC" in broker_only_syms, (
        f"expected BTC in broker_only, got {findings}"
    )


# --------------------------------------------------------------------- #
# Fix 4b: Divergence blocks new entries
# --------------------------------------------------------------------- #


def test_reconcile_divergence_blocks_new_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting _reconcile_divergence_detected=True must cause
    _maybe_enter to skip entirely without writing an order or marking
    an open position."""
    monkeypatch.setenv("ETA_KILLSWITCH_DISABLED", "1")
    monkeypatch.delenv("ETA_RECONCILE_DIVERGENCE_ACK", raising=False)

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    cfg.state_dir = tmp_path / "state"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_feed = "mock"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="div_bot",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5_000.0,
    )
    sup.bots.append(bot)

    # Defeat the mock random gate.
    import random as _random
    monkeypatch.setattr(_random, "random", lambda: 0.0)

    # Trip the divergence flag.
    sup._reconcile_divergence_detected = True
    sup._reconcile_divergence_at = datetime.now(UTC)

    bar = {
        "close": 60_000.0, "high": 60_100.0, "low": 59_900.0,
        "open": 60_000.0, "volume": 1.0,
        "ts": "2026-05-01T00:00:00+00:00",
    }

    sup._maybe_enter(bot, bar)

    assert bot.open_position is None
    assert bot.n_entries == 0


def test_reconcile_divergence_ack_env_unblocks_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ETA_RECONCILE_DIVERGENCE_ACK=1 must clear the divergence guard."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup._reconcile_divergence_detected = True
    sup._reconcile_divergence_at = datetime.now(UTC)

    # Without the env, NOT acknowledged.
    monkeypatch.delenv("ETA_RECONCILE_DIVERGENCE_ACK", raising=False)
    assert sup._reconcile_divergence_acknowledged() is False

    # With the env, acknowledged.
    monkeypatch.setenv("ETA_RECONCILE_DIVERGENCE_ACK", "1")
    assert sup._reconcile_divergence_acknowledged() is True


# --------------------------------------------------------------------- #
# Fix 5: Reject-storm auto-trip
# --------------------------------------------------------------------- #


def test_reject_auto_trip_at_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bot with consecutive_broker_rejects >= 5 must be flagged for
    skip and added to the trip set."""
    monkeypatch.delenv("ETA_MAX_CONSECUTIVE_REJECTS", raising=False)

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="storm_bot",
        symbol="MNQ",
        strategy_kind="x",
        direction="long",
        cash=5_000.0,
    )

    # Below threshold: not tripped
    bot.consecutive_broker_rejects = 4
    assert sup._check_reject_auto_trip(bot) is False
    assert "storm_bot" not in sup._reject_tripped_bots

    # At threshold: tripped + CRITICAL log
    bot.consecutive_broker_rejects = 5
    with caplog.at_level(logging.CRITICAL):
        tripped = sup._check_reject_auto_trip(bot)
    assert tripped is True
    assert "storm_bot" in sup._reject_tripped_bots
    storm_logs = [
        r for r in caplog.records
        if r.levelno == logging.CRITICAL and "REJECT STORM TRIP" in r.getMessage()
    ]
    assert storm_logs, "expected CRITICAL REJECT STORM TRIP log"

    # Reset counter -> trip clears
    bot.consecutive_broker_rejects = 0
    cleared = sup._check_reject_auto_trip(bot)
    assert cleared is False
    assert "storm_bot" not in sup._reject_tripped_bots


def test_reject_auto_trip_skips_maybe_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tripped bot's _maybe_enter must short-circuit without firing."""
    monkeypatch.setenv("ETA_KILLSWITCH_DISABLED", "1")

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_feed = "mock"
    sup = JarvisStrategySupervisor(cfg=cfg)
    bot = BotInstance(
        bot_id="storm_skip",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5_000.0,
    )
    sup.bots.append(bot)

    # Defeat mock random
    import random as _random
    monkeypatch.setattr(_random, "random", lambda: 0.0)

    # Force the trip
    bot.consecutive_broker_rejects = 5

    bar = {
        "close": 60_000.0, "high": 60_100.0, "low": 59_900.0,
        "open": 60_000.0, "volume": 1.0,
        "ts": "2026-05-01T00:00:00+00:00",
    }
    sup._maybe_enter(bot, bar)
    assert bot.open_position is None
    assert bot.n_entries == 0
    assert "storm_skip" in sup._reject_tripped_bots

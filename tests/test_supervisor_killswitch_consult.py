"""Tests for the supervisor's catastrophic-verdict latch consult.

Covers:
  * run_forever refuses to start (returns 3) when the latch file at
    ETA_KILL_SWITCH_LATCH_PATH is TRIPPED.
  * run_forever proceeds past the boot consult when latch is ARMED.
  * In-session: a verdict that trips the latch mid-session causes the
    next ``_maybe_enter`` call to skip the entry without writing a
    pending order.
  * Daily killswitch import failure is fail-closed: ``_maybe_enter``
    refuses the entry instead of swallowing the ImportError.
  * Boot bypass via ``ETA_LATCH_BOOT_BYPASS=1`` proceeds with a CRITICAL
    log line even when the latch is TRIPPED.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _write_tripped_latch(path: Path) -> None:
    """Write a TRIPPED latch JSON record at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "state": "TRIPPED",
                "tripped_at_utc": "2026-04-30T12:00:00+00:00",
                "reason": "test trip: daily loss 6.02% >= cap 6%",
                "scope": "global",
                "action": "FLATTEN_ALL",
                "severity": "CRITICAL",
                "evidence": {"daily_loss_pct": 6.02, "cap_pct": 6.0},
                "cleared_at_utc": None,
                "cleared_by": None,
            }
        ),
        encoding="utf-8",
    )


def _write_armed_latch(path: Path) -> None:
    """Write an ARMED latch JSON record at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "state": "ARMED",
                "tripped_at_utc": None,
                "reason": None,
                "scope": None,
                "action": None,
                "severity": None,
                "evidence": {},
                "cleared_at_utc": None,
                "cleared_by": None,
            }
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# run_forever boot-gate tests
# --------------------------------------------------------------------------- #
def test_run_forever_refuses_when_latch_tripped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TRIPPED latch must cause run_forever to return 3 without
    loading bots / bootstrapping JARVIS / entering the tick loop."""
    latch_path = tmp_path / "kill_switch_latch.json"
    _write_tripped_latch(latch_path)
    monkeypatch.setenv("ETA_KILL_SWITCH_LATCH_PATH", str(latch_path))
    monkeypatch.delenv("ETA_LATCH_BOOT_BYPASS", raising=False)

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.tick_s = 0.05
    sup = JarvisStrategySupervisor(cfg=cfg)

    rc = sup.run_forever()
    assert rc == 3
    assert not sup.bots  # bot loading must NOT have run


def test_run_forever_proceeds_when_latch_armed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ARMED latch must let run_forever proceed past the boot
    consult. We short-circuit by having load_bots return 0, which makes
    run_forever return 1 — proving the latch consult was passed."""
    latch_path = tmp_path / "kill_switch_latch.json"
    _write_armed_latch(latch_path)
    monkeypatch.setenv("ETA_KILL_SWITCH_LATCH_PATH", str(latch_path))
    monkeypatch.delenv("ETA_LATCH_BOOT_BYPASS", raising=False)

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.tick_s = 0.05
    sup = JarvisStrategySupervisor(cfg=cfg)
    # Make load_bots a no-op returning 0 so run_forever exits with rc=1
    # right after the boot consult — that is enough to prove we passed
    # the latch gate (would have been rc=3 if the latch had blocked).
    monkeypatch.setattr(sup, "load_bots", lambda: 0)

    rc = sup.run_forever()
    assert rc == 1  # "no active bots loaded; exiting" -- past the latch


def test_latch_boot_bypass_env_logs_critical_and_proceeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ETA_LATCH_BOOT_BYPASS=1 with a TRIPPED latch must:
    * NOT return 3 from run_forever
    * Emit a CRITICAL log line so the bypass is auditable
    """
    latch_path = tmp_path / "kill_switch_latch.json"
    _write_tripped_latch(latch_path)
    monkeypatch.setenv("ETA_KILL_SWITCH_LATCH_PATH", str(latch_path))
    monkeypatch.setenv("ETA_LATCH_BOOT_BYPASS", "1")

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.tick_s = 0.05
    sup = JarvisStrategySupervisor(cfg=cfg)
    monkeypatch.setattr(sup, "load_bots", lambda: 0)

    with caplog.at_level(logging.CRITICAL):
        rc = sup.run_forever()
    # Bypass passed the gate -> rc != 3 (we hit the no-bots exit at rc=1)
    assert rc == 1
    # CRITICAL log line proves the bypass was audited
    bypass_logs = [
        rec for rec in caplog.records if rec.levelno == logging.CRITICAL and "ETA_LATCH_BOOT_BYPASS" in rec.getMessage()
    ]
    assert bypass_logs, "expected a CRITICAL log mentioning ETA_LATCH_BOOT_BYPASS"


# --------------------------------------------------------------------------- #
# In-session latch consult inside _maybe_enter
# --------------------------------------------------------------------------- #
def test_maybe_enter_skipped_when_latch_tripped_mid_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot the supervisor with an ARMED latch, then trip it mid-session
    (simulating a verdict landing during runtime). The next call to
    _maybe_enter must return early without writing a pending order or
    creating an open position on the bot."""
    latch_path = tmp_path / "kill_switch_latch.json"
    _write_armed_latch(latch_path)
    monkeypatch.setenv("ETA_KILL_SWITCH_LATCH_PATH", str(latch_path))
    monkeypatch.delenv("ETA_LATCH_BOOT_BYPASS", raising=False)
    # Force the mock-data path off so the random-feed gate can't skip
    # _maybe_enter for unrelated reasons.
    monkeypatch.setenv("ETA_SUPERVISOR_FEED", "mock")
    # Disable daily loss killswitch so we isolate the LATCH consult.
    monkeypatch.setenv("ETA_KILLSWITCH_DISABLED", "1")

    from eta_engine.core.kill_switch_latch import KillSwitchLatch
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.tick_s = 0.05
    cfg.data_feed = "mock"  # bypass the mock random skip in _maybe_enter
    sup = JarvisStrategySupervisor(cfg=cfg)
    # Simulate the boot consult having attached the latch — same code
    # path as run_forever() does at startup.
    sup._kill_switch_latch = KillSwitchLatch(latch_path)
    bot = BotInstance(
        bot_id="latch-mid-session",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )
    sup.bots.append(bot)

    # Trip the latch on disk (mid-session). The supervisor's
    # _kill_switch_latch.read() will pick this up.
    _write_tripped_latch(latch_path)

    # Defeat the mock random gate so we know the early-return is the
    # latch, not the dice.
    import random as _random

    monkeypatch.setattr(_random, "random", lambda: 0.0)
    bar = {
        "close": 100.0,
        "high": 101.0,
        "low": 99.0,
        "open": 99.5,
        "volume": 1.0,
        "ts": "2026-05-01T00:00:00+00:00",
    }

    sup._maybe_enter(bot, bar)

    # The latch must have blocked the entry -- no open position recorded.
    assert bot.open_position is None
    assert bot.n_entries == 0


def test_daily_killswitch_fails_closed_on_import_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the daily_loss_killswitch module fails to import inside
    _maybe_enter, the supervisor must REFUSE the entry rather than
    swallowing the error and proceeding.
    """
    latch_path = tmp_path / "kill_switch_latch.json"
    _write_armed_latch(latch_path)
    monkeypatch.setenv("ETA_KILL_SWITCH_LATCH_PATH", str(latch_path))
    monkeypatch.setenv("ETA_SUPERVISOR_FEED", "mock")

    from eta_engine.core.kill_switch_latch import KillSwitchLatch
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.tick_s = 0.05
    cfg.data_feed = "mock"
    sup = JarvisStrategySupervisor(cfg=cfg)
    sup._kill_switch_latch = KillSwitchLatch(latch_path)
    bot = BotInstance(
        bot_id="killswitch-import-fail",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=5000.0,
    )
    sup.bots.append(bot)

    # Defeat the random gate.
    import random as _random

    monkeypatch.setattr(_random, "random", lambda: 0.0)

    # Patch the import so the killswitch module appears missing. The
    # _maybe_enter consult does ``from eta_engine.scripts.daily_loss_killswitch
    # import is_killswitch_tripped`` inside a try/except ImportError; we
    # raise ImportError there to exercise the fail-closed path.
    import builtins

    real_import = builtins.__import__

    def _stub_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "eta_engine.scripts.daily_loss_killswitch":
            msg = "simulated daily_loss_killswitch import failure"
            raise ImportError(msg)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _stub_import)

    bar = {
        "close": 100.0,
        "high": 101.0,
        "low": 99.0,
        "open": 99.5,
        "volume": 1.0,
        "ts": "2026-05-01T00:00:00+00:00",
    }

    sup._maybe_enter(bot, bar)

    # Fail-closed: no entry was recorded
    assert bot.open_position is None
    assert bot.n_entries == 0

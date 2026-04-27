"""Tests for obs.autopilot_watchdog -- require_ack on stale positions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from eta_engine.obs.autopilot_watchdog import (
    AutopilotMode,
    AutopilotWatchdog,
    PositionState,
    WatchdogAlertLevel,
    WatchdogPolicy,
)
from eta_engine.obs.decision_journal import Actor, DecisionJournal

if TYPE_CHECKING:
    from pathlib import Path


_T0 = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)


def _pos(
    *,
    trade_id: str = "t-1",
    symbol: str = "MNQ",
    opened: datetime = _T0,
    last_ack: datetime = _T0,
    stop_distance: float = 10.0,
    open_r: float = 0.0,
) -> PositionState:
    return PositionState(
        trade_id=trade_id,
        symbol=symbol,
        opened_at=opened,
        last_ack_at=last_ack,
        current_stop_distance=stop_distance,
        open_r=open_r,
    )


# --------------------------------------------------------------------------- #
# Model validation
# --------------------------------------------------------------------------- #


def test_position_rejects_empty_trade_id() -> None:
    with pytest.raises(ValidationError):
        _pos(trade_id="")


def test_position_rejects_zero_stop_distance() -> None:
    with pytest.raises(ValidationError):
        _pos(stop_distance=0.0)


def test_policy_rejects_bad_ordering() -> None:
    p = WatchdogPolicy(
        ack_ttl_sec=5000.0,
        tighten_after_sec=100.0,  # wrong
        max_age_sec=10000.0,
    )
    with pytest.raises(ValueError, match="ack_ttl"):
        p.validate_ordering()


def test_policy_rejects_tighten_factor_1_or_above() -> None:
    with pytest.raises(ValidationError):
        WatchdogPolicy(tighten_factor=1.0)


# --------------------------------------------------------------------------- #
# Staleness escalation
# --------------------------------------------------------------------------- #


def test_no_alert_when_fresh() -> None:
    wd = AutopilotWatchdog(clock=lambda: _T0 + timedelta(seconds=60))
    wd.register_position(_pos())
    assert wd.check_all() == []
    assert wd.mode() == AutopilotMode.ACTIVE


def test_require_ack_at_30min() -> None:
    wd = AutopilotWatchdog(clock=lambda: _T0 + timedelta(seconds=1800))
    wd.register_position(_pos())
    alerts = wd.check_all()
    assert len(alerts) == 1
    assert alerts[0].level == WatchdogAlertLevel.REQUIRE_ACK
    assert wd.mode() == AutopilotMode.REQUIRE_ACK


def test_tighten_stop_at_1h() -> None:
    wd = AutopilotWatchdog(clock=lambda: _T0 + timedelta(seconds=3700))
    wd.register_position(_pos(stop_distance=10.0))
    alerts = wd.check_all()
    assert alerts[0].level == WatchdogAlertLevel.TIGHTEN_STOP
    assert alerts[0].suggested_stop_distance == 7.5  # 10 * 0.75


def test_force_flatten_at_2h() -> None:
    wd = AutopilotWatchdog(clock=lambda: _T0 + timedelta(seconds=7300))
    wd.register_position(_pos())
    alerts = wd.check_all()
    assert alerts[0].level == WatchdogAlertLevel.FORCE_FLATTEN
    assert wd.mode() == AutopilotMode.FROZEN


def test_highest_severity_alert_wins_per_position() -> None:
    wd = AutopilotWatchdog(clock=lambda: _T0 + timedelta(seconds=7300))
    wd.register_position(_pos())
    alerts = wd.check_all()
    # 2 hours stale hits both tighten + flatten thresholds -- flatten wins
    assert len(alerts) == 1
    assert alerts[0].level == WatchdogAlertLevel.FORCE_FLATTEN


# --------------------------------------------------------------------------- #
# ack behavior
# --------------------------------------------------------------------------- #


def test_ack_resets_staleness() -> None:
    clock_val = {"t": _T0 + timedelta(seconds=3000)}
    wd = AutopilotWatchdog(clock=lambda: clock_val["t"])
    wd.register_position(_pos())
    # 3000s elapsed -- require_ack
    assert wd.check_all()[0].level == WatchdogAlertLevel.REQUIRE_ACK
    # Ack and check immediately -- no alert
    wd.ack("t-1")
    assert wd.check_all() == []


def test_ack_unknown_raises() -> None:
    wd = AutopilotWatchdog()
    with pytest.raises(KeyError):
        wd.ack("nope")


# --------------------------------------------------------------------------- #
# Multi-position
# --------------------------------------------------------------------------- #


def test_multiple_positions_each_alerted() -> None:
    wd = AutopilotWatchdog(clock=lambda: _T0 + timedelta(seconds=1900))
    wd.register_position(_pos(trade_id="a"))
    wd.register_position(_pos(trade_id="b"))
    wd.register_position(_pos(trade_id="c"))
    alerts = wd.check_all()
    assert {a.trade_id for a in alerts} == {"a", "b", "c"}
    assert all(a.level == WatchdogAlertLevel.REQUIRE_ACK for a in alerts)


def test_remove_position() -> None:
    wd = AutopilotWatchdog(clock=lambda: _T0 + timedelta(seconds=1900))
    wd.register_position(_pos())
    wd.remove_position("t-1")
    assert wd.check_all() == []
    assert wd.mode() == AutopilotMode.ACTIVE


# --------------------------------------------------------------------------- #
# Journal integration
# --------------------------------------------------------------------------- #


def test_alerts_write_to_journal(tmp_path: Path) -> None:
    journal = DecisionJournal(tmp_path / "w.jsonl")
    wd = AutopilotWatchdog(
        clock=lambda: _T0 + timedelta(seconds=1900),
        journal=journal,
    )
    wd.register_position(_pos())
    wd.check_all()
    events = journal.read_all()
    assert len(events) == 1
    assert events[0].actor == Actor.WATCHDOG


def test_force_flatten_marked_executed(tmp_path: Path) -> None:
    journal = DecisionJournal(tmp_path / "w.jsonl")
    wd = AutopilotWatchdog(
        clock=lambda: _T0 + timedelta(seconds=7300),
        journal=journal,
    )
    wd.register_position(_pos())
    wd.check_all()
    from eta_engine.obs.decision_journal import Outcome

    events = journal.read_all()
    assert events[0].outcome == Outcome.EXECUTED


# --------------------------------------------------------------------------- #
# Mode transitions
# --------------------------------------------------------------------------- #


def test_mode_active_when_empty() -> None:
    wd = AutopilotWatchdog()
    assert wd.mode() == AutopilotMode.ACTIVE


def test_mode_require_ack_threshold_boundary() -> None:
    # Exactly at ack_ttl -- require_ack
    wd = AutopilotWatchdog(clock=lambda: _T0 + timedelta(seconds=1800))
    wd.register_position(_pos())
    assert wd.mode() == AutopilotMode.REQUIRE_ACK


def test_mode_frozen_sticky() -> None:
    # Once we've flattened a position, mode stays FROZEN until register_position
    clock_val = {"t": _T0 + timedelta(seconds=7300)}
    wd = AutopilotWatchdog(clock=lambda: clock_val["t"])
    wd.register_position(_pos())
    wd.check_all()  # triggers flatten
    assert wd.mode() == AutopilotMode.FROZEN


# --------------------------------------------------------------------------- #
# JarvisAdmin integration (the "everyone reports to Jarvis" architecture)
# --------------------------------------------------------------------------- #


def test_request_flatten_approval_without_admin_raises() -> None:
    """Calling request_flatten_approval without an admin wired must raise."""
    wd = AutopilotWatchdog(clock=lambda: _T0 + timedelta(seconds=7300))
    wd.register_position(_pos())
    alerts = wd.check_all()
    with pytest.raises(RuntimeError, match="JarvisAdmin"):
        wd.request_flatten_approval(alerts[0])


def test_request_flatten_approval_rejects_non_flatten_alert() -> None:
    """Only FORCE_FLATTEN alerts go through the admin flow."""
    from eta_engine.brain.jarvis_admin import JarvisAdmin  # noqa: PLC0415

    admin = JarvisAdmin()
    wd = AutopilotWatchdog(
        clock=lambda: _T0 + timedelta(seconds=1800),  # REQUIRE_ACK
        admin=admin,
    )
    wd.register_position(_pos())
    alerts = wd.check_all()
    assert alerts[0].level == WatchdogAlertLevel.REQUIRE_ACK
    with pytest.raises(ValueError, match="FORCE_FLATTEN"):
        wd.request_flatten_approval(alerts[0])


def test_request_flatten_approval_end_to_end(tmp_path: Path) -> None:
    """End-to-end: watchdog -> JarvisAdmin (engine-backed) -> audit JSONL."""
    from eta_engine.brain.jarvis_admin import (  # noqa: PLC0415
        JarvisAdmin,
        SubsystemId,
        Verdict,
    )
    from eta_engine.brain.jarvis_context import (  # noqa: PLC0415
        EquitySnapshot,
        JarvisContextBuilder,
        JarvisContextEngine,
        JournalSnapshot,
        MacroSnapshot,
        RegimeSnapshot,
    )

    # Constant providers -> TRADE-tier context every tick
    macro = MacroSnapshot(vix_level=17.0, macro_bias="neutral")
    equity = EquitySnapshot(
        account_equity=50_000.0,
        daily_pnl=0.0,
        daily_drawdown_pct=0.0,
        open_positions=1,
        open_risk_r=1.0,
    )
    regime = RegimeSnapshot(regime="TREND_UP", confidence=0.7)
    journal = JournalSnapshot()

    class _P:
        def get_macro(self) -> MacroSnapshot:
            return macro

        def get_equity(self) -> EquitySnapshot:
            return equity

        def get_regime(self) -> RegimeSnapshot:
            return regime

        def get_journal_snapshot(self) -> JournalSnapshot:
            return journal

    providers = _P()
    builder = JarvisContextBuilder(
        macro_provider=providers,
        equity_provider=providers,
        regime_provider=providers,
        journal_provider=providers,
    )
    engine = JarvisContextEngine(builder=builder)
    audit = tmp_path / "admin_audit.jsonl"
    admin = JarvisAdmin(engine=engine, audit_path=audit)

    wd = AutopilotWatchdog(
        clock=lambda: _T0 + timedelta(seconds=7300),
        admin=admin,
    )
    wd.register_position(_pos())
    alerts = wd.check_all()
    assert alerts[0].level == WatchdogAlertLevel.FORCE_FLATTEN

    resp = wd.request_flatten_approval(alerts[0])
    # POSITION_FLATTEN is exit-only -- always approved
    assert resp.verdict == Verdict.APPROVED

    records = admin.audit_tail(10)
    assert len(records) == 1
    assert records[0]["request"]["subsystem"] == SubsystemId.AUTOPILOT_WATCHDOG.value
    assert records[0]["request"]["action"] == "POSITION_FLATTEN"
    assert records[0]["response"]["verdict"] == "APPROVED"

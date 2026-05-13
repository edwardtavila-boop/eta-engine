"""Tests for the 24/7 framework (2026-05-06).

Covers:
* Per-venue scope-aware order-entry hold (futures vs crypto split).
* Watchdog relaunch on stale heartbeat.
* Watchdog opt-out via supervisor_disabled.txt.
* IbgConnectionMonitor port-refused -> hold.set(scope=ibkr).
* IbgConnectionMonitor recovery -> hold.clear.
* Alpaca retry decorator for transient errors.
* Alpaca retry decorator skipping deterministic broker rejects.
* Uptime telemetry start event.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


# --------------------------------------------------------------------------- #
# 1. Scope-aware order-entry hold
# --------------------------------------------------------------------------- #
def test_order_entry_hold_scope_ibkr_allows_alpaca_bots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scope=ibkr hold must NOT block a bot resolved to Alpaca."""
    from eta_engine.scripts.runtime_order_hold import (
        load_order_entry_hold,
        write_order_entry_hold,
    )

    hold_path = tmp_path / "order_entry_hold.json"
    monkeypatch.setenv("ETA_ORDER_ENTRY_HOLD_PATH", str(hold_path))
    monkeypatch.delenv("ETA_ORDER_ENTRY_HOLD", raising=False)

    write_order_entry_hold(
        active=True,
        reason="ibgateway_waiting_for_manual_login_or_2fa",
        scope="ibkr",
        path=hold_path,
    )

    hold = load_order_entry_hold(hold_path)
    assert hold.active is True
    assert hold.scope == "ibkr"

    # Alpaca crypto bot -> NOT blocked.
    assert hold.blocks(venue="alpaca", asset_class="crypto") is False
    # IBKR futures bot -> blocked.
    assert hold.blocks(venue="ibkr", asset_class="futures") is True
    # Tastytrade bot (also non-IBKR) -> NOT blocked by scope=ibkr.
    assert hold.blocks(venue="tastytrade", asset_class="futures") is False


def test_order_entry_hold_scope_all_blocks_everything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back-compat: scope='all' (or missing) blocks every venue."""
    from eta_engine.scripts.runtime_order_hold import load_order_entry_hold

    hold_path = tmp_path / "order_entry_hold.json"
    monkeypatch.setenv("ETA_ORDER_ENTRY_HOLD_PATH", str(hold_path))
    monkeypatch.delenv("ETA_ORDER_ENTRY_HOLD", raising=False)

    # Write a hold without a scope field (legacy contract).
    hold_path.write_text(
        json.dumps(
            {
                "active": True,
                "reason": "operator_hold_legacy",
                "ts": "2026-05-06T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    hold = load_order_entry_hold(hold_path)
    assert hold.active is True
    assert hold.scope == "all"  # missing scope normalises to "all"

    # Both lanes are blocked.
    assert hold.blocks(venue="alpaca", asset_class="crypto") is True
    assert hold.blocks(venue="ibkr", asset_class="futures") is True


# --------------------------------------------------------------------------- #
# 2. Watchdog relaunch + opt-out
# --------------------------------------------------------------------------- #
def test_watchdog_relaunches_when_heartbeat_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale heartbeat triggers relaunch_fn."""
    from eta_engine.scripts import eta_watchdog

    hb_path = tmp_path / "supervisor_heartbeat.json"
    # Heartbeat dated 1 hour ago.
    hb_path.write_text(
        json.dumps({"ts": "2020-01-01T00:00:00+00:00", "tick_count": 1}),
        encoding="utf-8",
    )

    relaunch_calls: list[dict[str, Any]] = []

    def fake_relaunch(*, task_name: str, wrapper_cmd: str | None) -> tuple[bool, str]:
        relaunch_calls.append({"task_name": task_name, "wrapper_cmd": wrapper_cmd})
        return True, "fake_relaunched"

    decision = eta_watchdog.watchdog_tick(
        component="supervisor",
        heartbeat_path=hb_path,
        keepalive_path=None,  # ignore keepalive
        process_substring="never_match_anything_xyz123",  # process is gone
        stale_s=60.0,
        disabled_flag_path=tmp_path / "supervisor_disabled.txt",  # missing => not disabled
        watchdog_heartbeat_path=tmp_path / "watchdog_heartbeat.json",
        relaunch_fn=fake_relaunch,
        pid_fn=lambda _sub: [],  # no live processes
        kill_fn=lambda _pids: [],
    )

    assert decision.action == "relaunched"
    assert decision.stale is True
    assert decision.process_alive is False
    assert relaunch_calls, "relaunch_fn should have been invoked"
    # Watchdog heartbeat is written so operators know the watchdog ran.
    assert (tmp_path / "watchdog_heartbeat.json").exists()


def test_watchdog_trusts_fresh_heartbeat_when_pid_scan_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh heartbeat is stronger evidence than a flaky Windows PID scan."""
    from eta_engine.scripts import eta_watchdog

    hb_path = tmp_path / "supervisor_heartbeat.json"
    hb_path.write_text(
        json.dumps({"ts": datetime.now(UTC).isoformat(), "tick_count": 7}),
        encoding="utf-8",
    )

    relaunch_calls: list[Any] = []
    decision = eta_watchdog.watchdog_tick(
        component="supervisor",
        heartbeat_path=hb_path,
        keepalive_path=None,
        process_substring="jarvis_strategy_supervisor.py",
        stale_s=60.0,
        disabled_flag_path=tmp_path / "supervisor_disabled.txt",
        watchdog_heartbeat_path=tmp_path / "watchdog_heartbeat.json",
        relaunch_fn=lambda **_kw: relaunch_calls.append(_kw) or (True, "bad"),
        pid_fn=lambda _sub: [],
        kill_fn=lambda _pids: [],
    )

    assert decision.action == "noop"
    assert decision.stale is False
    assert decision.process_alive is False
    assert decision.reason == "fresh_heartbeat_process_unobserved"
    assert relaunch_calls == []


def test_watchdog_noop_when_supervisor_disabled_file_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator opt-out via supervisor_disabled.txt blocks every relaunch."""
    from eta_engine.scripts import eta_watchdog

    hb_path = tmp_path / "supervisor_heartbeat.json"
    # Heartbeat dated 1 hour ago — would normally trigger relaunch.
    hb_path.write_text(
        json.dumps({"ts": "2020-01-01T00:00:00+00:00", "tick_count": 1}),
        encoding="utf-8",
    )
    disabled = tmp_path / "supervisor_disabled.txt"
    disabled.write_text("operator opt-out reason=manual_review\n", encoding="utf-8")

    relaunch_calls: list[Any] = []

    decision = eta_watchdog.watchdog_tick(
        component="supervisor",
        heartbeat_path=hb_path,
        keepalive_path=None,
        process_substring="never_match_xyz",
        stale_s=60.0,
        disabled_flag_path=disabled,
        watchdog_heartbeat_path=tmp_path / "watchdog_heartbeat.json",
        relaunch_fn=lambda **_kw: relaunch_calls.append(_kw) or (True, "should_not_be_called"),
        pid_fn=lambda _sub: [],
        kill_fn=lambda _pids: [],
    )

    assert decision.action == "skipped_disabled"
    assert decision.disabled_opt_out is True
    assert relaunch_calls == [], "relaunch must NOT be invoked when opt-out is active"
    # Watchdog still records its own heartbeat so the operator can verify
    # the watchdog itself ran.
    assert (tmp_path / "watchdog_heartbeat.json").exists()


def test_watchdog_powershell_pid_fallback_uses_env_needle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-psutil Windows fallback must not match its own command line."""
    from eta_engine.scripts import eta_watchdog

    calls: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append({"cmd": cmd, "env": kwargs.get("env")})
        return SimpleNamespace(stdout="123\nnot-a-pid\n456\n")

    monkeypatch.setattr(eta_watchdog.os, "name", "nt")
    monkeypatch.setattr(eta_watchdog.subprocess, "run", fake_run)

    pids = eta_watchdog._find_pids_with_powershell("broker_router.py")

    assert pids == [123, 456]
    assert calls
    assert "broker_router.py" not in " ".join(calls[0]["cmd"])
    assert calls[0]["env"]["_ETA_WATCHDOG_PROCESS_NEEDLE"] == "broker_router.py"


# --------------------------------------------------------------------------- #
# 3. IbgConnectionMonitor — sets hold on port refused / clears on recovery
# --------------------------------------------------------------------------- #
def test_ibg_connection_monitor_sets_hold_on_port_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the IB Gateway port is unreachable, the monitor must set
    ``order_entry_hold.json`` with scope=ibkr."""
    from eta_engine.scripts.runtime_order_hold import load_order_entry_hold
    from eta_engine.venues.connection import IbgConnectionMonitor

    hold_path = tmp_path / "order_entry_hold.json"
    monkeypatch.setenv("ETA_ORDER_ENTRY_HOLD_PATH", str(hold_path))
    monkeypatch.delenv("ETA_ORDER_ENTRY_HOLD", raising=False)

    monitor = IbgConnectionMonitor(
        venue=None,
        host="127.0.0.1",
        port=4002,
        probe_fn=lambda _h, _p, _t: False,  # port refused
    )
    state = monitor.tick()

    assert state.port_reachable is False
    assert state.last_action == "set_hold"
    assert state.last_reason.startswith("ibgateway_unreachable_port_")

    hold = load_order_entry_hold(hold_path)
    assert hold.active is True
    assert hold.scope == "ibkr"
    # Confirm scope correctly leaves alpaca/crypto unblocked.
    assert hold.blocks(venue="alpaca", asset_class="crypto") is False
    assert hold.blocks(venue="ibkr", asset_class="futures") is True


def test_ibg_connection_monitor_clears_hold_on_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the port comes back AND venue.connect() succeeds, the
    scope=ibkr hold must be cleared."""
    from eta_engine.scripts.runtime_order_hold import (
        load_order_entry_hold,
        write_order_entry_hold,
    )
    from eta_engine.venues.base import ConnectionStatus, VenueConnectionReport
    from eta_engine.venues.connection import IbgConnectionMonitor

    hold_path = tmp_path / "order_entry_hold.json"
    monkeypatch.setenv("ETA_ORDER_ENTRY_HOLD_PATH", str(hold_path))
    monkeypatch.delenv("ETA_ORDER_ENTRY_HOLD", raising=False)

    # Pre-set a scope=ibkr hold (simulating the previous outage tick).
    write_order_entry_hold(
        active=True,
        reason="ibgateway_unreachable_port_4002",
        scope="ibkr",
        path=hold_path,
    )
    pre = load_order_entry_hold(hold_path)
    assert pre.active is True

    class _FakeVenue:
        async def connect(self) -> VenueConnectionReport:
            return VenueConnectionReport(
                venue="ibkr",
                status=ConnectionStatus.READY,
                creds_present=True,
                details={"endpoint": "127.0.0.1:4002"},
            )

    monitor = IbgConnectionMonitor(
        venue=_FakeVenue(),
        host="127.0.0.1",
        port=4002,
        probe_fn=lambda _h, _p, _t: True,  # port up
    )
    state = monitor.tick()

    assert state.port_reachable is True
    assert state.venue_connect_ok is True
    assert state.last_action == "cleared_hold"

    post = load_order_entry_hold(hold_path)
    assert post.active is False


# --------------------------------------------------------------------------- #
# 4. Alpaca retry decorator
# --------------------------------------------------------------------------- #
def test_alpaca_retry_succeeds_after_one_transient_failure() -> None:
    """First call raises a transient error, second succeeds."""
    from eta_engine.venues.connection import with_transient_retry

    attempts = {"n": 0}

    @with_transient_retry(attempts=3, base_delay_s=0.0)
    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ConnectionRefusedError("boom transient")
        return "ok"

    result = asyncio.run(flaky())
    assert result == "ok"
    assert attempts["n"] == 2


def test_alpaca_retry_does_not_retry_on_deterministic_reject() -> None:
    """Deterministic broker rejects (e.g. 403 cost-basis) skip retries."""
    from eta_engine.venues.connection import (
        DeterministicBrokerReject,
        with_transient_retry,
    )

    attempts = {"n": 0}

    @with_transient_retry(attempts=3, base_delay_s=0.0)
    async def cost_basis_too_low() -> str:
        attempts["n"] += 1
        raise DeterministicBrokerReject("alpaca POST status=403 cost basis")

    with pytest.raises(DeterministicBrokerReject):
        asyncio.run(cost_basis_too_low())
    assert attempts["n"] == 1, "deterministic rejects must not retry"


# --------------------------------------------------------------------------- #
# 5. Uptime telemetry
# --------------------------------------------------------------------------- #
def test_uptime_events_jsonl_writes_start_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """record_uptime_event must append a JSON line we can read back."""
    from eta_engine.scripts.uptime_events import (
        read_recent_events,
        record_uptime_event,
    )

    events_path = tmp_path / "uptime_events.jsonl"
    monkeypatch.setenv("ETA_UPTIME_EVENTS_PATH", str(events_path))

    record_uptime_event(
        component="supervisor",
        event="start",
        reason="run_forever_entered",
        extra={"mode": "paper_live", "feed": "composite"},
    )

    events = read_recent_events(n=10, path=events_path)
    assert events, "expected at least one event"
    last = events[-1]
    assert last["component"] == "supervisor"
    assert last["event"] == "start"
    assert last["reason"] == "run_forever_entered"
    assert last["extra"]["mode"] == "paper_live"
    assert "ts" in last and last["ts"]

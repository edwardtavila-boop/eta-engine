from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eta_engine.brain.jarvis_v3.vps import (
    DEFAULT_CATALOG,
    ServiceState,
    VPSActionRequest,
    VPSActionType,
    VPSSnapshot,
    assess_vps,
    vps_action_to_shell,
)


def test_assess_vps_proposes_restart_start_and_disk_prune_actions() -> None:
    now = datetime(2026, 4, 29, tzinfo=UTC)
    snapshot = VPSSnapshot(
        ts=now,
        cpu_pct=91,
        mem_pct=96,
        disk_pct=97,
        load_1m=4.0,
        load_5m=3.0,
        load_15m=2.0,
    )

    report = assess_vps(
        snapshot,
        {
            "mnq-bot.service": ServiceState.FAILED,
            "trading-dashboard.service": ServiceState.STOPPED,
        },
        DEFAULT_CATALOG,
        now=now,
    )

    assert report.overall == "RED"
    assert "CPU 91% >= critical 90%" in report.alerts
    assert [action.action for action in report.proposed_actions] == [
        VPSActionType.RESTART,
        VPSActionType.START,
        VPSActionType.DISK_PRUNE,
    ]


def test_vps_action_to_shell_maps_supported_actions() -> None:
    assert vps_action_to_shell(
        VPSActionRequest(action=VPSActionType.RESTART, service="mnq-bot.service", rationale="failed")
    ) == ["systemctl", "restart", "mnq-bot.service"]
    assert vps_action_to_shell(
        VPSActionRequest(action=VPSActionType.TAIL_LOG, service="mnq-bot.service", rationale="inspect")
    ) == ["journalctl", "-u", "mnq-bot.service", "-n", "200", "--no-pager"]
    assert vps_action_to_shell(VPSActionRequest(action=VPSActionType.KILL_PID, pid=1234, rationale="runaway")) == [
        "kill",
        "1234",
    ]


def test_vps_action_to_shell_rejects_incomplete_request() -> None:
    with pytest.raises(ValueError, match="unsupported action"):
        vps_action_to_shell(VPSActionRequest(action=VPSActionType.START, rationale="missing service"))

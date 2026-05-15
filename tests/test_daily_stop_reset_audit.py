from __future__ import annotations

import json
from pathlib import Path


def _killswitch(
    *,
    tripped: bool,
    reset_in_s: int = 3600,
    reason: str = "day_pnl=$-925.50 <= limit=$-900.00",
) -> dict[str, object]:
    return {
        "status": "tripped" if tripped else "clear",
        "tripped": tripped,
        "disabled": False,
        "reason": reason if tripped else "day_pnl=$+0.00 (limit=$-900.00)",
        "date": "2026-05-15",
        "timezone": "America/New_York",
        "reset_at": "2026-05-16T00:00:00-04:00",
        "reset_display": "2026-05-16 00:00 EDT",
        "reset_in_s": reset_in_s,
        "today_pnl_usd": -925.50 if tripped else 0.0,
        "limit_usd": -900.0,
    }


def _transition(
    *,
    status: str,
    critical_ready: bool,
    failed_gate: str = "",
) -> dict[str, object]:
    gates: list[dict[str, object]] = [
        {
            "name": "tws_api_4002",
            "passed": True,
            "critical": True,
            "detail": "paper-live order routing is ready through TWS 4002",
            "next_action": "",
        }
    ]
    if failed_gate:
        gates.append(
            {
                "name": failed_gate,
                "passed": False,
                "critical": True,
                "detail": "gateway release guard is still blocked",
                "next_action": "python -m eta_engine.scripts.ibgateway_release_guard",
            }
        )
    return {
        "status": status,
        "critical_ready": critical_ready,
        "paper_ready_bots": 11,
        "operator_queue_effective_launch_blocked_count": 0 if critical_ready else 1,
        "operator_queue_first_launch_blocker_op_id": "OP-19" if failed_gate else None,
        "operator_queue_first_launch_next_action": "repair gateway" if failed_gate else None,
        "gates": gates,
        "launch_command": (
            "$env:ETA_SUPERVISOR_MODE='paper_live'; "
            "python eta_engine/scripts/jarvis_strategy_supervisor.py"
        ),
    }


def test_reset_audit_marks_active_daily_stop_as_wait_until_reset() -> None:
    from eta_engine.scripts import daily_stop_reset_audit as mod

    payload = mod.build_reset_audit(
        killswitch_provider=lambda: _killswitch(tripped=True, reset_in_s=600),
        transition_provider=lambda: _transition(status="ready_to_launch_paper_live", critical_ready=True),
    )

    assert payload["status"] == "held_until_reset"
    assert payload["post_reset_ready"] is False
    assert payload["read_only"] is True
    assert payload["safe_to_trade_mutation"] is False
    assert payload["daily_loss_killswitch"]["tripped"] is True
    assert payload["paper_live_transition"]["status"] == "ready_to_launch_paper_live"
    assert "Wait for the automatic daily-loss reset" in payload["operator_next_action"]


def test_reset_audit_marks_clear_daily_stop_and_ready_transition() -> None:
    from eta_engine.scripts import daily_stop_reset_audit as mod

    payload = mod.build_reset_audit(
        killswitch_provider=lambda: _killswitch(tripped=False, reset_in_s=86400),
        transition_provider=lambda: _transition(status="ready_to_launch_paper_live", critical_ready=True),
    )

    assert payload["status"] == "reset_cleared_ready"
    assert payload["post_reset_ready"] is True
    assert payload["daily_loss_killswitch"]["status"] == "clear"
    assert payload["paper_live_transition"]["critical_ready"] is True
    assert payload["first_failed_gate"] == {}
    assert "Watch the first supervisor tick" in payload["operator_next_action"]


def test_reset_audit_marks_clear_daily_stop_but_blocked_transition() -> None:
    from eta_engine.scripts import daily_stop_reset_audit as mod

    payload = mod.build_reset_audit(
        killswitch_provider=lambda: _killswitch(tripped=False, reset_in_s=86400),
        transition_provider=lambda: _transition(
            status="blocked",
            critical_ready=False,
            failed_gate="ibgateway_release_guard",
        ),
    )

    assert payload["status"] == "reset_cleared_blocked"
    assert payload["post_reset_ready"] is False
    assert payload["first_failed_gate"]["name"] == "ibgateway_release_guard"
    assert payload["operator_next_action"] == "python -m eta_engine.scripts.ibgateway_release_guard"


def test_reset_audit_write_round_trips_json(tmp_path: Path) -> None:
    from eta_engine.scripts import daily_stop_reset_audit as mod

    output = tmp_path / "daily_stop_reset_audit_latest.json"
    payload = mod.build_reset_audit(
        killswitch_provider=lambda: _killswitch(tripped=False),
        transition_provider=lambda: _transition(status="ready_to_launch_paper_live", critical_ready=True),
    )

    written = mod.write_reset_audit(payload, output)
    loaded = json.loads(written.read_text(encoding="utf-8"))

    assert written == output
    assert loaded["schema_version"] == 1
    assert loaded["status"] == "reset_cleared_ready"
    assert loaded["source"] == "daily_stop_reset_audit"

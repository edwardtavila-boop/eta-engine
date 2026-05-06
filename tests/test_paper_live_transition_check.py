from __future__ import annotations

import json
from pathlib import Path


def _surface(*, ready: bool) -> dict[str, object]:
    return {
        "summary": {
            "paper_live_ready": ready,
            "operator_action": (
                "paper_live order routing is ready through TWS 4002"
                if ready
                else "Keep supervisor in paper_sim until TWS/IB Gateway API 4002 is running"
            ),
        },
    }


def _release(*, ready: bool, hold_reason: str = "") -> dict[str, object]:
    return {
        "status": "ready_to_release" if ready else "blocked_watchdog_unhealthy",
        "operator_action_required": not ready,
        "reason": "fresh healthy watchdog" if ready else "watchdog is unhealthy",
        "hold": {
            "active": bool(hold_reason),
            "reason": hold_reason,
            "scope": "ibkr" if hold_reason else "all",
        },
    }


def _queue(*, first_op: str | None, blocked: int, paper_ready: int) -> dict[str, object]:
    return {
        "blocked_count": blocked,
        "first_blocker_op_id": first_op,
        "first_next_action": "install gateway" if first_op == "OP-19" else "review warnings",
        "bot_strategy_paper_ready": paper_ready,
    }


def test_transition_check_blocks_when_gateway_op19_is_top_blocker(monkeypatch) -> None:
    from eta_engine.scripts import paper_live_transition_check as mod

    monkeypatch.setattr(mod.ibkr_surface_status, "build_status", lambda **_kwargs: _surface(ready=False))
    monkeypatch.setattr(mod.ibgateway_release_guard, "run_guard", lambda **_kwargs: _release(ready=False))
    monkeypatch.setattr(
        mod.operator_queue_snapshot,
        "build_snapshot",
        lambda **_kwargs: _queue(first_op="OP-19", blocked=5, paper_ready=10),
    )

    result = mod.build_transition_check()

    assert result["status"] == "blocked"
    assert result["critical_ready"] is False
    assert result["launch_command"] == ""
    assert result["operator_queue_first_blocker_op_id"] == "OP-19"
    assert [gate["name"] for gate in result["gates"]] == [
        "tws_api_4002",
        "ibgateway_release_guard",
        "op19_gateway_runtime",
        "paper_ready_bots",
    ]


def test_transition_check_prioritizes_visible_gateway_login_hold(monkeypatch) -> None:
    from eta_engine.scripts import paper_live_transition_check as mod

    monkeypatch.setattr(mod.ibkr_surface_status, "build_status", lambda **_kwargs: _surface(ready=False))
    monkeypatch.setattr(
        mod.ibgateway_release_guard,
        "run_guard",
        lambda **_kwargs: _release(
            ready=False,
            hold_reason="ibgateway_waiting_for_manual_login_or_2fa",
        ),
    )
    monkeypatch.setattr(
        mod.operator_queue_snapshot,
        "build_snapshot",
        lambda **_kwargs: _queue(first_op="OP-19", blocked=4, paper_ready=22),
    )

    result = mod.build_transition_check()
    op19_gate = next(gate for gate in result["gates"] if gate["name"] == "op19_gateway_runtime")

    assert result["operator_queue_first_next_action"].startswith(
        "Complete the visible IBKR Gateway login/2FA"
    )
    assert op19_gate["next_action"] == result["operator_queue_first_next_action"]
    assert "tws_watchdog" in op19_gate["next_action"]


def test_transition_check_ready_with_warnings_when_critical_gates_clear(monkeypatch) -> None:
    from eta_engine.scripts import paper_live_transition_check as mod

    monkeypatch.setattr(mod.ibkr_surface_status, "build_status", lambda **_kwargs: _surface(ready=True))
    monkeypatch.setattr(mod.ibgateway_release_guard, "run_guard", lambda **_kwargs: _release(ready=True))
    monkeypatch.setattr(
        mod.operator_queue_snapshot,
        "build_snapshot",
        lambda **_kwargs: _queue(first_op="OP-16", blocked=1, paper_ready=10),
    )

    result = mod.build_transition_check()

    assert result["status"] == "ready_with_operator_queue_warnings"
    assert result["critical_ready"] is True
    assert "ETA_SUPERVISOR_MODE='paper_live'" in result["launch_command"]
    assert result["operator_queue_blocked_count"] == 1
    assert result["paper_ready_bots"] == 10


def test_transition_check_writes_canonical_payload(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import paper_live_transition_check as mod

    monkeypatch.setattr(mod.ibkr_surface_status, "build_status", lambda **_kwargs: _surface(ready=True))
    monkeypatch.setattr(mod.ibgateway_release_guard, "run_guard", lambda **_kwargs: _release(ready=True))
    monkeypatch.setattr(
        mod.operator_queue_snapshot,
        "build_snapshot",
        lambda **_kwargs: _queue(first_op=None, blocked=0, paper_ready=10),
    )

    output = tmp_path / "paper_live_transition_check.json"
    rc = mod.main(["--out", str(output)])

    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "ready_to_launch_paper_live"
    assert payload["critical_ready"] is True

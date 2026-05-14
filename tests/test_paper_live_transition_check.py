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


def _queue(
    *,
    first_op: str | None,
    blocked: int,
    paper_ready: int,
    launch_blocked: int | None = None,
    first_launch_op: str | None = None,
    first_detail: str | None = None,
    first_launch_detail: str | None = None,
) -> dict[str, object]:
    launch_count = blocked if launch_blocked is None else launch_blocked
    launch_op = first_op if first_launch_op is None else first_launch_op
    detail = first_detail or ("install gateway blocker" if first_op == "OP-19" else "review warnings")
    launch_detail = first_launch_detail or (detail if launch_op == first_op else "launch blocker")
    operator_queue: list[dict[str, object]] = []
    if first_op:
        operator_queue.append(
            {
                "op_id": first_op,
                "detail": detail,
                "next_actions": [
                    "install gateway" if first_op == "OP-19" else "review warnings",
                ],
            }
        )
    if launch_op and launch_op != first_op:
        operator_queue.append(
            {
                "op_id": launch_op,
                "detail": launch_detail,
                "next_actions": [
                    "install gateway" if launch_op == "OP-19" else "launch is clear",
                ],
            }
        )
    return {
        "blocked_count": blocked,
        "first_blocker_op_id": first_op,
        "first_next_action": "install gateway" if first_op == "OP-19" else "review warnings",
        "launch_blocked_count": launch_count,
        "first_launch_blocker_op_id": launch_op,
        "first_launch_next_action": ("install gateway" if launch_op == "OP-19" else "launch is clear"),
        "bot_strategy_paper_ready": paper_ready,
        "operator_queue": operator_queue,
    }


def test_transition_check_blocks_when_gateway_op19_is_top_blocker(monkeypatch) -> None:
    from eta_engine.scripts import paper_live_transition_check as mod

    observed: dict[str, object] = {}
    monkeypatch.setattr(mod.ibkr_surface_status, "build_status", lambda **_kwargs: _surface(ready=False))
    monkeypatch.setattr(mod.ibgateway_release_guard, "run_guard", lambda **_kwargs: _release(ready=False))

    def _build_snapshot(**kwargs: object) -> dict[str, object]:
        observed["kwargs"] = kwargs
        return _queue(first_op="OP-19", blocked=5, paper_ready=10)

    monkeypatch.setattr(mod.operator_queue_snapshot, "build_snapshot", _build_snapshot)

    result = mod.build_transition_check()

    assert observed["kwargs"]["refresh_readiness"] is True
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
    op19_gate = next(gate for gate in result["gates"] if gate["name"] == "op19_gateway_runtime")
    assert op19_gate["detail"] == "install gateway blocker"


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

    assert result["operator_queue_first_launch_next_action"].startswith("Complete the visible IBKR Gateway login/2FA")
    assert op19_gate["next_action"] == result["operator_queue_first_launch_next_action"]
    assert "tws_watchdog" in op19_gate["next_action"]


def test_transition_check_scopes_gateway_actions_to_vps_when_host_is_not_authority(monkeypatch) -> None:
    from eta_engine.scripts import paper_live_transition_check as mod

    monkeypatch.setattr(mod.ibkr_surface_status, "build_status", lambda **_kwargs: _surface(ready=False))
    monkeypatch.setattr(
        mod.ibgateway_release_guard,
        "run_guard",
        lambda **_kwargs: {
            "status": "blocked_non_authoritative_gateway_host",
            "operator_action_required": True,
            "reason": "fresh watchdog exists, but this host is not the VPS Gateway authority",
            "gateway_authority": {"allowed": False, "computer_name": "ETA"},
            "hold": {
                "active": True,
                "reason": "ibgateway_waiting_for_manual_login_or_2fa",
                "scope": "ibkr",
            },
        },
    )
    monkeypatch.setattr(
        mod.operator_queue_snapshot,
        "build_snapshot",
        lambda **_kwargs: {
            "blocked_count": 1,
            "first_blocker_op_id": "OP-19",
            "first_next_action": "install gateway",
            "launch_blocked_count": 1,
            "first_launch_blocker_op_id": "OP-19",
            "first_launch_next_action": "install gateway",
            "bot_strategy_paper_ready": 10,
            "operator_queue": {
                "top_launch_blockers": [
                    {
                        "op_id": "OP-19",
                        "detail": "This host is not the VPS Gateway authority",
                        "evidence": {
                            "non_authoritative_gateway_host": True,
                            "gateway_authority": {"allowed": False},
                        },
                        "next_actions": ["install gateway"],
                    }
                ]
            },
        },
    )

    result = mod.build_transition_check()
    gates = {gate["name"]: gate for gate in result["gates"]}

    assert result["non_authoritative_gateway_host"] is True
    assert result["operator_queue_first_launch_next_action"].startswith("On the VPS only:")
    assert gates["tws_api_4002"]["next_action"].startswith("On the VPS only:")
    assert gates["ibgateway_release_guard"]["next_action"].startswith("On the VPS only:")
    assert gates["op19_gateway_runtime"]["next_action"].startswith("On the VPS only:")
    assert "visible IBKR Gateway login" not in gates["op19_gateway_runtime"]["next_action"]


def test_transition_check_reads_op19_detail_from_snapshot_top_launch_blockers(monkeypatch) -> None:
    from eta_engine.scripts import paper_live_transition_check as mod

    monkeypatch.setattr(mod.ibkr_surface_status, "build_status", lambda **_kwargs: _surface(ready=False))
    monkeypatch.setattr(mod.ibgateway_release_guard, "run_guard", lambda **_kwargs: _release(ready=True))
    monkeypatch.setattr(
        mod.operator_queue_snapshot,
        "build_snapshot",
        lambda **_kwargs: {
            "blocked_count": 1,
            "first_blocker_op_id": "OP-19",
            "first_next_action": "repair gateway",
            "launch_blocked_count": 1,
            "first_launch_blocker_op_id": "OP-19",
            "first_launch_next_action": "repair gateway",
            "bot_strategy_paper_ready": 10,
            "operator_queue": {
                "top_launch_blockers": [
                    {
                        "op_id": "OP-19",
                        "detail": "live IBC is healthy but boot-task drift remains",
                        "next_actions": ["repair gateway"],
                    }
                ]
            },
        },
    )

    result = mod.build_transition_check()
    op19_gate = next(gate for gate in result["gates"] if gate["name"] == "op19_gateway_runtime")

    assert op19_gate["detail"] == "live IBC is healthy but boot-task drift remains"


def test_transition_check_clears_stale_op19_when_gateway_runtime_is_healthy(monkeypatch) -> None:
    from eta_engine.scripts import paper_live_transition_check as mod

    monkeypatch.setattr(mod.ibkr_surface_status, "build_status", lambda **_kwargs: _surface(ready=True))
    monkeypatch.setattr(mod.ibgateway_release_guard, "run_guard", lambda **_kwargs: _release(ready=True))
    monkeypatch.setattr(
        mod.operator_queue_snapshot,
        "build_snapshot",
        lambda **_kwargs: _queue(
            first_op="OP-19",
            blocked=1,
            paper_ready=10,
            launch_blocked=1,
            first_launch_op="OP-19",
            first_launch_detail="stale Gateway blocker from the previous recovery tick",
        ),
    )

    result = mod.build_transition_check()
    op19_gate = next(gate for gate in result["gates"] if gate["name"] == "op19_gateway_runtime")

    assert result["status"] == "ready_to_launch_paper_live"
    assert result["critical_ready"] is True
    assert result["operator_queue_stale_op19_cleared"] is True
    assert result["operator_queue_launch_blocked_count"] == 1
    assert result["operator_queue_effective_launch_blocked_count"] == 0
    assert result["operator_queue_first_launch_blocker_op_id"] is None
    assert op19_gate["passed"] is True
    assert "OP-19 stale" in op19_gate["detail"]


def test_transition_check_ready_when_only_non_launch_operator_warnings_remain(monkeypatch) -> None:
    from eta_engine.scripts import paper_live_transition_check as mod

    monkeypatch.setattr(mod.ibkr_surface_status, "build_status", lambda **_kwargs: _surface(ready=True))
    monkeypatch.setattr(mod.ibgateway_release_guard, "run_guard", lambda **_kwargs: _release(ready=True))
    monkeypatch.setattr(
        mod.operator_queue_snapshot,
        "build_snapshot",
        lambda **_kwargs: _queue(
            first_op="OP-5",
            blocked=1,
            paper_ready=10,
            launch_blocked=0,
            first_launch_op=None,
        ),
    )

    result = mod.build_transition_check()

    assert result["status"] == "ready_to_launch_paper_live"
    assert result["critical_ready"] is True
    assert "ETA_SUPERVISOR_MODE='paper_live'" in result["launch_command"]
    assert result["operator_queue_blocked_count"] == 1
    assert result["operator_queue_launch_blocked_count"] == 0
    assert result["operator_queue_warning_blocked_count"] == 1
    assert result["operator_queue_first_launch_blocker_op_id"] is None
    assert result["operator_queue_first_launch_next_action"] is None
    op19_gate = next(gate for gate in result["gates"] if gate["name"] == "op19_gateway_runtime")
    assert op19_gate["next_action"] == ""
    assert result["paper_ready_bots"] == 10


def test_transition_check_blocks_when_gateway_api_is_read_only(monkeypatch) -> None:
    from eta_engine.scripts import paper_live_transition_check as mod

    monkeypatch.setattr(mod.ibkr_surface_status, "build_status", lambda **_kwargs: _surface(ready=True))
    monkeypatch.setattr(mod.ibgateway_release_guard, "run_guard", lambda **_kwargs: _release(ready=True))
    monkeypatch.setattr(
        mod.operator_queue_snapshot,
        "build_snapshot",
        lambda **_kwargs: _queue(
            first_op="OP-16",
            blocked=1,
            paper_ready=10,
            launch_blocked=0,
            first_launch_op=None,
        ),
    )
    monkeypatch.setattr(
        mod,
        "_latest_order_api_status",
        lambda: {
            "ready": False,
            "status": "read_only",
            "detail": "IB Gateway API is in Read-Only mode; paper_live order entry would be rejected.",
            "operator_action": "Uncheck Read-Only API in IB Gateway API settings.",
        },
    )

    result = mod.build_transition_check()

    assert result["status"] == "blocked"
    assert result["critical_ready"] is False
    assert result["launch_command"] == ""
    assert result["operator_queue_launch_blocked_count"] == 0
    gate = next(gate for gate in result["gates"] if gate["name"] == "ibkr_order_api")
    assert gate["passed"] is False
    assert "Read-Only mode" in gate["detail"]


def test_transition_check_accepts_already_released_guard(monkeypatch) -> None:
    from eta_engine.scripts import paper_live_transition_check as mod

    monkeypatch.setattr(mod.ibkr_surface_status, "build_status", lambda **_kwargs: _surface(ready=True))
    monkeypatch.setattr(
        mod.ibgateway_release_guard,
        "run_guard",
        lambda **_kwargs: {
            "status": "already_released",
            "operator_action_required": False,
            "reason": "fresh healthy watchdog; order-entry hold already clear",
            "hold": {
                "active": False,
                "reason": "ibkr_gateway_recovered_20260506",
                "scope": "all",
            },
        },
    )
    monkeypatch.setattr(
        mod.operator_queue_snapshot,
        "build_snapshot",
        lambda **_kwargs: _queue(first_op="OP-16", blocked=1, paper_ready=10),
    )

    result = mod.build_transition_check()
    release_gate = next(gate for gate in result["gates"] if gate["name"] == "ibgateway_release_guard")

    assert release_gate["passed"] is True
    assert result["critical_ready"] is True


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

"""
EVOLUTIONARY TRADING ALGO  //  tests.test_rental_client_contract
====================================================
Schema + validator tests for the downloadable-client WS contract.
"""

from __future__ import annotations

from eta_engine.rental.client_contract import (
    ClientCommand,
    ClientCommandKind,
    ServerMessage,
    ServerMessageKind,
    make_error,
    make_hello,
    make_status_update,
    validate_command,
)

# ---------------------------------------------------------------------------
# ClientCommand serialization
# ---------------------------------------------------------------------------


def test_client_command_to_dict_round_trip() -> None:
    cmd = ClientCommand(
        kind=ClientCommandKind.BOT_START,
        session_token="abc",
        tenant_id="tenant_1",
        params={"sku": "BTC_SEED", "mode": "paper"},
    )
    d = cmd.to_dict()
    assert d["kind"] == "BOT_START"
    assert d["session_token"] == "abc"
    assert d["tenant_id"] == "tenant_1"
    assert d["params"] == {"sku": "BTC_SEED", "mode": "paper"}


# ---------------------------------------------------------------------------
# Validation: success cases
# ---------------------------------------------------------------------------


def test_validate_hello_ok() -> None:
    cmd = make_hello(tenant_id="t1", session_token="tok", client_version="0.1.0")
    ok, reason = validate_command(cmd)
    assert ok
    assert reason == "ok"


def test_validate_bot_start_with_optional_mode() -> None:
    cmd = ClientCommand(
        kind=ClientCommandKind.BOT_START,
        session_token="tok",
        tenant_id="t1",
        params={"sku": "BTC_SEED", "mode": "paper"},
    )
    ok, _ = validate_command(cmd)
    assert ok


def test_validate_ping_no_params() -> None:
    cmd = ClientCommand(
        kind=ClientCommandKind.PING,
        session_token="tok",
        tenant_id="t1",
    )
    ok, _ = validate_command(cmd)
    assert ok


def test_validate_fetch_logs_accepts_any_optional_subset() -> None:
    cmd = ClientCommand(
        kind=ClientCommandKind.FETCH_LOGS,
        session_token="tok",
        tenant_id="t1",
        params={"since_utc": "2026-04-17T00:00:00Z"},
    )
    ok, _ = validate_command(cmd)
    assert ok


# ---------------------------------------------------------------------------
# Validation: failures
# ---------------------------------------------------------------------------


def test_missing_required_param_rejected() -> None:
    cmd = ClientCommand(
        kind=ClientCommandKind.BOT_START,
        session_token="tok",
        tenant_id="t1",
        params={},  # missing sku
    )
    ok, reason = validate_command(cmd)
    assert not ok
    assert "missing required params" in reason
    assert "sku" in reason


def test_unexpected_param_rejected() -> None:
    cmd = ClientCommand(
        kind=ClientCommandKind.BOT_STOP,
        session_token="tok",
        tenant_id="t1",
        params={"sku": "BTC_SEED", "debug": True},
    )
    ok, reason = validate_command(cmd)
    assert not ok
    assert "unexpected params" in reason
    assert "debug" in reason


def test_forbidden_strategy_param_rejected() -> None:
    # Attack: tenant tries to override reward weights via a bot_start params bag.
    cmd = ClientCommand(
        kind=ClientCommandKind.BOT_START,
        session_token="tok",
        tenant_id="t1",
        params={"sku": "BTC_SEED", "reward_weights": "1,2,3"},
    )
    ok, reason = validate_command(cmd)
    assert not ok
    # Because reward_weights is also "unexpected" for BOT_START, the first
    # failure surfaces; either message would be fine, but it must block.
    assert "reward_weights" in reason


def test_forbidden_param_blocked_even_if_also_optional_kind() -> None:
    # FETCH_LOGS tolerates since_utc + limit + sku; add a forbidden key that
    # bypasses the "unexpected" check must still get caught by forbidden filter.
    # We simulate by monkey-patching allowed keys via a kind that accepts many
    # keys -- easier: use QUERY_JARVIS with question + injection.
    cmd = ClientCommand(
        kind=ClientCommandKind.QUERY_JARVIS,
        session_token="tok",
        tenant_id="t1",
        params={"question": "what is confidence?", "pine_source": "..."},
    )
    ok, reason = validate_command(cmd)
    assert not ok
    # pine_source is forbidden AND unexpected; either fails the check.
    assert "pine_source" in reason


def test_empty_session_token_rejected() -> None:
    cmd = ClientCommand(
        kind=ClientCommandKind.PING,
        session_token="",
        tenant_id="t1",
    )
    ok, reason = validate_command(cmd)
    assert not ok
    assert "session_token" in reason


def test_empty_tenant_id_rejected() -> None:
    cmd = ClientCommand(
        kind=ClientCommandKind.PING,
        session_token="tok",
        tenant_id="",
    )
    ok, reason = validate_command(cmd)
    assert not ok
    assert "tenant_id" in reason


def test_unknown_kind_rejected() -> None:
    # Bypass the enum by constructing directly with an invalid kind via
    # dataclass.replace trick: use ClientCommand but cast a fake kind.
    # Easiest: fabricate via object.__setattr__ since it's frozen.
    cmd = ClientCommand(
        kind=ClientCommandKind.PING,
        session_token="tok",
        tenant_id="t1",
    )
    object.__setattr__(cmd, "kind", "MAKE_ME_ADMIN")  # type: ignore[arg-type]
    ok, reason = validate_command(cmd)
    assert not ok
    assert "unknown command kind" in reason


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def test_make_status_update_rounds_numbers() -> None:
    msg = make_status_update(
        tenant_id="t1",
        sku="BTC_SEED",
        equity=10000.123456,
        daily_pnl=-123.4567,
        regime="NORMAL",
        session_phase="RTH",
        confidence=0.876543,
        kill_switch_active=False,
    )
    assert msg.kind is ServerMessageKind.STATUS
    assert msg.payload["equity"] == 10000.12
    assert msg.payload["daily_pnl"] == -123.46
    assert msg.payload["confidence"] == 0.877
    assert msg.payload["kill_switch_active"] is False


def test_make_error_shape() -> None:
    msg = make_error(tenant_id="t1", code="ERR_RATE_LIMIT", message="too fast")
    assert msg.kind is ServerMessageKind.ERROR
    assert msg.payload == {"code": "ERR_RATE_LIMIT", "message": "too fast"}


def test_server_message_to_dict_round_trip() -> None:
    msg = ServerMessage(
        kind=ServerMessageKind.PONG,
        tenant_id="t1",
        payload={"seq": 42},
    )
    d = msg.to_dict()
    assert d == {"kind": "PONG", "tenant_id": "t1", "payload": {"seq": 42}}

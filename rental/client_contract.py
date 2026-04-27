"""
EVOLUTIONARY TRADING ALGO  //  rental.client_contract
=========================================
WebSocket message contract between the downloadable Electron/Tauri client
and the rental backend.

Client capabilities (strictly limited):
  * Start / Stop / Reset the tenant's bots
  * Subscribe to status updates (PnL, equity, regime, session-phase)
  * Query Jarvis: "what is the current MNQ confidence score?"
  * Pull own logs + daily summary
  * Manage alert rules

Anything that touches strategy internals (reward weights, confluence axes,
regime classifier) is explicitly out-of-scope -- those commands fail with
``ERR_OUT_OF_SCOPE``.

Schema style: every message is a JSON object with a ``kind`` discriminator
and the rest of the payload is kind-specific. The server NEVER echoes
strategy internals; tenants only see their own PnL and high-level scores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ClientCommandKind(StrEnum):
    HELLO = "HELLO"  # auth handshake
    BOT_START = "BOT_START"
    BOT_STOP = "BOT_STOP"
    BOT_RESET = "BOT_RESET"
    SUBSCRIBE_STATUS = "SUBSCRIBE_STATUS"
    UNSUBSCRIBE_STATUS = "UNSUBSCRIBE_STATUS"
    QUERY_JARVIS = "QUERY_JARVIS"
    FETCH_LOGS = "FETCH_LOGS"
    FETCH_DAILY_REPORT = "FETCH_DAILY_REPORT"
    PING = "PING"


class ServerMessageKind(StrEnum):
    HELLO_OK = "HELLO_OK"
    ERROR = "ERROR"
    STATUS = "STATUS"  # periodic bot status update
    JARVIS_ANSWER = "JARVIS_ANSWER"
    LOG_CHUNK = "LOG_CHUNK"
    DAILY_REPORT = "DAILY_REPORT"
    PONG = "PONG"


# ---------------------------------------------------------------------------
# Commands (client -> server)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientCommand:
    kind: ClientCommandKind
    session_token: str  # rotates per connection
    tenant_id: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "session_token": self.session_token,
            "tenant_id": self.tenant_id,
            "params": dict(self.params),
        }


# ---------------------------------------------------------------------------
# Messages (server -> client)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerMessage:
    kind: ServerMessageKind
    tenant_id: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "tenant_id": self.tenant_id,
            "payload": dict(self.payload),
        }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


# Params schemas: kind -> (required_keys, optional_keys)
_COMMAND_SCHEMAS: dict[ClientCommandKind, tuple[frozenset[str], frozenset[str]]] = {
    ClientCommandKind.HELLO: (frozenset({"client_version"}), frozenset({"os"})),
    ClientCommandKind.BOT_START: (frozenset({"sku"}), frozenset({"mode"})),
    ClientCommandKind.BOT_STOP: (frozenset({"sku"}), frozenset()),
    ClientCommandKind.BOT_RESET: (frozenset({"sku"}), frozenset({"confirm"})),
    ClientCommandKind.SUBSCRIBE_STATUS: (frozenset({"sku"}), frozenset({"interval_s"})),
    ClientCommandKind.UNSUBSCRIBE_STATUS: (frozenset({"sku"}), frozenset()),
    ClientCommandKind.QUERY_JARVIS: (frozenset({"question"}), frozenset({"sku"})),
    ClientCommandKind.FETCH_LOGS: (frozenset(), frozenset({"since_utc", "limit", "sku"})),
    ClientCommandKind.FETCH_DAILY_REPORT: (frozenset(), frozenset({"date"})),
    ClientCommandKind.PING: (frozenset(), frozenset()),
}


_FORBIDDEN_PARAMS = frozenset(
    {
        "reward_weights",
        "confluence_axes",
        "regime_weights",
        "pine_source",
        "model_checkpoint",
    }
)


def validate_command(cmd: ClientCommand) -> tuple[bool, str]:
    """Return (ok, reason). Never raises; callers log the reason and ERR back."""
    schema = _COMMAND_SCHEMAS.get(cmd.kind)
    if schema is None:
        return False, f"unknown command kind {cmd.kind!r}"
    required, optional = schema
    keys = set(cmd.params.keys())
    missing = required - keys
    if missing:
        return False, f"missing required params: {sorted(missing)}"
    allowed = required | optional
    extras = keys - allowed
    if extras:
        return False, f"unexpected params: {sorted(extras)}"
    forbidden = keys & _FORBIDDEN_PARAMS
    if forbidden:
        return False, f"ERR_OUT_OF_SCOPE: forbidden params {sorted(forbidden)}"
    if not cmd.session_token or not cmd.tenant_id:
        return False, "session_token and tenant_id are required"
    return True, "ok"


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def make_hello(*, tenant_id: str, session_token: str, client_version: str) -> ClientCommand:
    return ClientCommand(
        kind=ClientCommandKind.HELLO,
        session_token=session_token,
        tenant_id=tenant_id,
        params={"client_version": client_version},
    )


def make_status_update(
    *,
    tenant_id: str,
    sku: str,
    equity: float,
    daily_pnl: float,
    regime: str,
    session_phase: str,
    confidence: float,
    kill_switch_active: bool,
) -> ServerMessage:
    return ServerMessage(
        kind=ServerMessageKind.STATUS,
        tenant_id=tenant_id,
        payload={
            "sku": sku,
            "equity": round(equity, 2),
            "daily_pnl": round(daily_pnl, 2),
            "regime": regime,
            "session_phase": session_phase,
            "confidence": round(confidence, 3),
            "kill_switch_active": kill_switch_active,
        },
    )


def make_error(*, tenant_id: str, code: str, message: str) -> ServerMessage:
    return ServerMessage(
        kind=ServerMessageKind.ERROR,
        tenant_id=tenant_id,
        payload={"code": code, "message": message},
    )

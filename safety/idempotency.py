"""EVOLUTIONARY TRADING ALGO // safety.idempotency.

Order-submission dedup log. Each outbound order carries a
deterministic ``client_order_id`` derived from the request payload
(see ``scripts.live_supervisor.JarvisAwareRouter._ensure_client_order_id``).
A retry of the same intent therefore arrives with the same id; this
module keeps a process-local cache so the second submission never
re-routes to the venue.

Design notes
------------
* The intended production backend is the Supabase ``order_intents``
  table -- a single row per ``client_order_id`` carries the
  authoritative state. The functions below speak that contract
  (``status``, ``broker_order_id``, ``response_payload``) so the
  in-memory backing store here is a drop-in for tests / dev / paper
  while the Supabase wiring matures.
* :class:`IdempotencyError` is a typed marker so the venue can
  fail-closed without conflating "store is unreachable" with
  ordinary "duplicate id".
"""

from __future__ import annotations

import json as _json
import os as _os
import threading
from dataclasses import dataclass, field
from pathlib import Path as _Path
from typing import Any


class IdempotencyError(RuntimeError):
    """Raised when the dedup store cannot service a request.

    The exception's ``.reason`` attribute carries a short stable code
    (``"store_unreachable"`` / ``"corrupt_record"`` / ...).
    """

    def __init__(self, message: str, *, reason: str = "store_unreachable") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """Immutable view of one row in the dedup log.

    ``is_new`` is ``True`` only for the FIRST call to
    :func:`check_or_register` with a given ``client_order_id``.
    Every subsequent call returns the cached record verbatim, so
    a retry skips re-routing and surfaces the cached venue
    response back to the caller.
    """

    client_order_id: str
    venue: str
    symbol: str
    is_new: bool
    status: str = "pending"
    broker_order_id: str | None = None
    note: str = ""
    response_payload: dict[str, Any] | None = None
    intent_payload: dict[str, Any] = field(default_factory=dict)


_LOCK: threading.Lock = threading.Lock()
_STORE: dict[str, IdempotencyRecord] = {}


# ─── Disk persistence ───────────────────────────────────────────
#
# Default-on: every check_or_register / record_result writes a JSONL
# line so the dedup log survives supervisor restarts. Without this,
# a process bounce mid-trade can cause a second submission of the
# same intent (live broker would error on the duplicate
# clientOrderId, but the supervisor's view of "pending" gets lost).
#
# Env var ETA_IDEMPOTENCY_STORE controls the path:
#   * unset / empty   → default workspace path under
#                       C:\EvolutionaryTradingAlgo\var\eta_engine\state\
#   * "disabled"      → genuinely opt out (use in tests that need a
#                       clean slate)
#   * any other value → that path
#
# Per workspace hard rule (CLAUDE.md): all state stays under the
# canonical workspace root — no writes to OneDrive, %LOCALAPPDATA%,
# or legacy paths.

# default-on persistence; set ETA_IDEMPOTENCY_STORE=disabled to opt out
_DEFAULT_PERSIST_PATH: _Path = _Path(
    "C:/EvolutionaryTradingAlgo/var/eta_engine/state/idempotency.jsonl"
)


def _persist_path() -> _Path | None:
    raw = _os.getenv("ETA_IDEMPOTENCY_STORE", "").strip()
    if raw.lower() == "disabled":
        return None
    if not raw:
        # Default path under workspace root. Only ensure the parent
        # exists when the parent is itself inside the workspace root —
        # the hard rule forbids auto-creating dirs elsewhere.
        try:
            _os.makedirs(_DEFAULT_PERSIST_PATH.parent, exist_ok=True)
        except OSError:
            # If the workspace root isn't writable on this machine
            # (CI, sandbox, etc.) silently degrade to in-memory-only
            # rather than crashing the import.
            return None
        return _DEFAULT_PERSIST_PATH
    return _Path(raw)


def _persist_record(rec: IdempotencyRecord) -> None:
    """Append a single record to the JSONL store. Best-effort: a disk
    failure must not break order routing."""
    path = _persist_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = _json.dumps({
            "client_order_id": rec.client_order_id,
            "venue": rec.venue,
            "symbol": rec.symbol,
            "status": rec.status,
            "broker_order_id": rec.broker_order_id,
            "note": rec.note,
            "response_payload": rec.response_payload,
            "intent_payload": rec.intent_payload,
        }) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:  # noqa: BLE001 — disk full, permission, etc.
        pass  # silently degrade — the in-memory store still protects us


def _load_store_from_disk() -> None:
    """Replay the JSONL store on first import after process restart so
    the in-memory cache reflects whatever the previous process committed.
    Each client_order_id is set to its LAST line (most recent status)."""
    path = _persist_path()
    if path is None or not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                cid = obj.get("client_order_id")
                if not cid:
                    continue
                _STORE[cid] = IdempotencyRecord(
                    client_order_id=cid,
                    venue=obj.get("venue", ""),
                    symbol=obj.get("symbol", ""),
                    is_new=False,
                    status=obj.get("status", "unknown"),
                    broker_order_id=obj.get("broker_order_id"),
                    note=obj.get("note", ""),
                    response_payload=obj.get("response_payload"),
                    intent_payload=obj.get("intent_payload") or {},
                )
    except OSError:
        pass


_load_store_from_disk()


def check_or_register(
    *,
    client_order_id: str,
    venue: str,
    symbol: str,
    intent_payload: dict[str, Any],
) -> IdempotencyRecord:
    """Return the existing record for ``client_order_id``, or
    register a fresh one and return it with ``is_new=True``.

    First-call path: inserts a new ``pending`` row and returns
    ``is_new=True`` so the venue should route the order.
    Repeat-call path: returns the cached record with
    ``is_new=False`` so the venue should NOT re-route.
    """
    if not client_order_id:
        raise IdempotencyError(
            "client_order_id must be non-empty", reason="invalid_id"
        )
    with _LOCK:
        existing = _STORE.get(client_order_id)
        if existing is not None:
            return IdempotencyRecord(
                client_order_id=existing.client_order_id,
                venue=existing.venue,
                symbol=existing.symbol,
                is_new=False,
                status=existing.status,
                broker_order_id=existing.broker_order_id,
                note=existing.note or "duplicate",
                response_payload=existing.response_payload,
                intent_payload=existing.intent_payload,
            )
        fresh = IdempotencyRecord(
            client_order_id=client_order_id,
            venue=venue,
            symbol=symbol,
            is_new=True,
            status="pending",
            broker_order_id=None,
            note="",
            response_payload=None,
            intent_payload=dict(intent_payload),
        )
        _STORE[client_order_id] = fresh
        _persist_record(fresh)
        return fresh


def record_result(
    *,
    client_order_id: str,
    status: str,
    broker_order_id: str | None = None,
    response_payload: dict[str, Any] | None = None,
) -> IdempotencyRecord:
    """Update the dedup log with the venue's final verdict.

    Called after :func:`check_or_register` returned ``is_new=True``
    and the order has been routed. Idempotent: writing the same
    terminal status twice is a no-op.
    """
    if not client_order_id:
        raise IdempotencyError(
            "client_order_id must be non-empty", reason="invalid_id"
        )
    with _LOCK:
        existing = _STORE.get(client_order_id)
        if existing is None:
            raise IdempotencyError(
                f"no pending record for {client_order_id!r}",
                reason="missing_record",
            )
        updated = IdempotencyRecord(
            client_order_id=existing.client_order_id,
            venue=existing.venue,
            symbol=existing.symbol,
            is_new=False,
            status=status,
            broker_order_id=broker_order_id or existing.broker_order_id,
            note=existing.note,
            response_payload=response_payload or existing.response_payload,
            intent_payload=existing.intent_payload,
        )
        _STORE[client_order_id] = updated
        _persist_record(updated)
        return updated


def reset_store_for_test() -> None:
    """Test hook: drop every record from the in-memory store."""
    with _LOCK:
        _STORE.clear()


__all__ = [
    "IdempotencyError",
    "IdempotencyRecord",
    "check_or_register",
    "record_result",
    "reset_store_for_test",
]

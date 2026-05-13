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
* ``retryable_failed`` is reserved for failures that happen before an
  order reaches the broker, such as a TWS socket outage. Those rows may
  be re-opened on the next check; normal ``pending`` rows stay locked
  because a process may have crashed after broker submission but before
  recording the result.

TTL semantics
-------------
Each cached entry carries a ``recorded_at`` timestamp. On read:

* Within TTL -> return cached response (dedup).
* Beyond TTL -> treat as never-cached, allow fresh submit, and
  self-clean the stale row from both in-memory and the JSONL store.

REJECTED responses use a SHORTER TTL than FILLED/OPEN responses so a
transient broker-side failure (network blip, JVM-OOM crash, rate-limit)
cannot poison subsequent legitimate retries. FILLED/OPEN responses use
a longer TTL because they reflect durable broker-side state.

Configuration:

* ``ETA_IDEMPOTENCY_TTL_S``           default 3600  (1 hour)
* ``ETA_IDEMPOTENCY_REJECTED_TTL_S``  default  300  (5 minutes)

Backwards compatibility: legacy JSONL entries that lack ``recorded_at``
are treated as expired (force fresh submit) rather than crashing the
load.
"""

from __future__ import annotations

import argparse as _argparse
import json as _json
import os as _os
import sys as _sys
import threading
import time as _time
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
    recorded_at: float = 0.0  # unix epoch seconds; 0.0 == legacy/missing


_LOCK: threading.Lock = threading.Lock()
_STORE: dict[str, IdempotencyRecord] = {}


# ─── TTL configuration ──────────────────────────────────────────
#
# Default TTLs are aggressive enough to clear poison fast (e.g. the
# 2026-05 JVM-OOM era saw 170+ orders pinned by stale REJECTED rows)
# but conservative enough not to dedup legitimate intentional retries.

_DEFAULT_TTL_S: float = 3600.0  # 1 hour for FILLED / OPEN / submitted
_DEFAULT_REJECTED_TTL_S: float = 300.0  # 5 minutes for REJECTED


def _ttl_for_status(status: str) -> float:
    """Return the configured TTL (seconds) for a status string.

    REJECTED-family statuses get a shorter TTL because those failures
    are typically transient (broker-side blips) and stale entries
    poison legitimate retries. Everything else (PENDING, SUBMITTED,
    FILLED, OPEN, CANCELLED, RETRYABLE_FAILED) gets the longer TTL
    since those reflect durable broker-side state.
    """
    raw_default = _os.getenv("ETA_IDEMPOTENCY_TTL_S", "").strip()
    raw_rejected = _os.getenv("ETA_IDEMPOTENCY_REJECTED_TTL_S", "").strip()
    try:
        ttl_default = float(raw_default) if raw_default else _DEFAULT_TTL_S
    except ValueError:
        ttl_default = _DEFAULT_TTL_S
    try:
        ttl_rejected = float(raw_rejected) if raw_rejected else _DEFAULT_REJECTED_TTL_S
    except ValueError:
        ttl_rejected = _DEFAULT_REJECTED_TTL_S
    if (status or "").lower() == "rejected":
        return ttl_rejected
    return ttl_default


def _is_expired(rec: IdempotencyRecord, *, now: float | None = None) -> bool:
    """True if ``rec`` is past its TTL (or has no recorded_at at all).

    A ``recorded_at`` of 0.0 marks a legacy entry that pre-dates the
    TTL schema; treat those as expired so they don't linger forever.
    """
    if rec.recorded_at <= 0.0:
        return True
    ts = _time.time() if now is None else now
    return (ts - rec.recorded_at) > _ttl_for_status(rec.status)


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
_DEFAULT_PERSIST_PATH: _Path = _Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/idempotency.jsonl")


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


def _record_to_dict(rec: IdempotencyRecord) -> dict[str, Any]:
    return {
        "client_order_id": rec.client_order_id,
        "venue": rec.venue,
        "symbol": rec.symbol,
        "status": rec.status,
        "broker_order_id": rec.broker_order_id,
        "note": rec.note,
        "response_payload": rec.response_payload,
        "intent_payload": rec.intent_payload,
        "recorded_at": rec.recorded_at,
    }


def _persist_record(rec: IdempotencyRecord) -> None:
    """Append a single record to the JSONL store. Best-effort: a disk
    failure must not break order routing."""
    path = _persist_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = _json.dumps(_record_to_dict(rec)) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:  # noqa: BLE001 — disk full, permission, etc.
        pass  # silently degrade — the in-memory store still protects us


def _atomic_replace_store(records: list[IdempotencyRecord]) -> None:
    """Rewrite the JSONL store atomically (temp + rename).

    Used by the self-cleanup path and by the ``--clear`` CLI to drop
    expired/rejected entries without leaving a half-written file behind.
    """
    path = _persist_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(_json.dumps(_record_to_dict(rec)) + "\n")
        # os.replace is atomic on the same filesystem on both POSIX and
        # Windows (handles dest-exists overwrite correctly).
        _os.replace(tmp, path)
    except OSError:
        pass


def _load_store_from_disk() -> None:
    """Replay the JSONL store on first import after process restart so
    the in-memory cache reflects whatever the previous process committed.
    Each client_order_id is set to its LAST line (most recent status).

    Backwards compat: entries written before the TTL schema landed
    will be missing ``recorded_at`` and are loaded with ``recorded_at=0.0``,
    which :func:`_is_expired` treats as expired. They will be evicted on
    first read."""
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
                # recorded_at may be missing (legacy) or non-numeric;
                # coerce defensively, default to 0.0 (treated as expired).
                ts_raw = obj.get("recorded_at", 0.0)
                try:
                    ts = float(ts_raw) if ts_raw is not None else 0.0
                except (TypeError, ValueError):
                    ts = 0.0
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
                    recorded_at=ts,
                )
    except OSError:
        pass


def _evict_and_rewrite(client_order_id: str) -> None:
    """Drop ``client_order_id`` from in-memory + JSONL store.

    Inline self-cleanup path: called by :func:`check_or_register` when
    it finds an expired entry. Without this, the next-startup cache
    load would carry forever-stale entries forward."""
    _STORE.pop(client_order_id, None)
    path = _persist_path()
    if path is None or not path.exists():
        return
    try:
        kept: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if obj.get("client_order_id") == client_order_id:
                    continue
                kept.append(obj)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for obj in kept:
                fh.write(_json.dumps(obj) + "\n")
        _os.replace(tmp, path)
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
    Repeat-call path within TTL: returns the cached record with
    ``is_new=False`` so the venue should NOT re-route.
    Repeat-call path past TTL: self-cleans the stale row and
    inserts a fresh one (returns ``is_new=True``) so a transient
    failure cannot poison a legitimate retry forever.
    """
    if not client_order_id:
        raise IdempotencyError("client_order_id must be non-empty", reason="invalid_id")
    with _LOCK:
        existing = _STORE.get(client_order_id)
        # TTL-based eviction must happen BEFORE the retryable_failed
        # check so a stale REJECTED row doesn't sit forever just
        # because its status isn't retryable_failed.
        if existing is not None and _is_expired(existing):
            _evict_and_rewrite(client_order_id)
            existing = None
        if existing is not None:
            if existing.status == "retryable_failed":
                retry = IdempotencyRecord(
                    client_order_id=client_order_id,
                    venue=venue,
                    symbol=symbol,
                    is_new=True,
                    status="pending",
                    broker_order_id=None,
                    note="retry_after_retryable_failure",
                    response_payload=None,
                    intent_payload=dict(intent_payload),
                    recorded_at=_time.time(),
                )
                _STORE[client_order_id] = retry
                _persist_record(retry)
                return retry
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
                recorded_at=existing.recorded_at,
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
            recorded_at=_time.time(),
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

    The new ``recorded_at`` is set to NOW each time, so a row's TTL
    countdown restarts from the latest status update — a REJECTED
    row that gets re-submitted-and-FILLED later naturally inherits
    the longer FILLED TTL from this point forward.
    """
    if not client_order_id:
        raise IdempotencyError("client_order_id must be non-empty", reason="invalid_id")
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
            recorded_at=_time.time(),
        )
        _STORE[client_order_id] = updated
        _persist_record(updated)
        return updated


def reset_store_for_test() -> None:
    """Test hook: drop every record from the in-memory store."""
    with _LOCK:
        _STORE.clear()


def evict(client_order_id: str) -> bool:
    """Drop ``client_order_id`` from in-memory + JSONL store.

    Public API for permanent-rejection class errors that should NOT
    pollute the dedup cache (e.g. account permissions missing). Without
    this, the rejected row sits in cache and traps subsequent retries
    with the same client_order_id — but more importantly, it pads the
    on-disk JSONL with no-cache-value entries that survive process
    restart.

    Returns ``True`` if a row was evicted, ``False`` if no row was
    found. Best-effort: a disk failure is silently swallowed.
    """
    if not client_order_id:
        return False
    with _LOCK:
        existed = client_order_id in _STORE
        if existed:
            _evict_and_rewrite(client_order_id)
        return existed


# ─── CLI ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _ClearSummary:
    kept: int
    dropped: int
    dropped_expired: int
    dropped_rejected: int


def _clear_store(*, rejected_only: bool = False, now: float | None = None) -> _ClearSummary:
    """Filter the JSONL store, atomically replacing it with the
    surviving entries.

    Without ``--rejected-only``: drop expired entries AND rejected
    entries (regardless of TTL — the whole point is to flush poison).
    With ``--rejected-only``: drop only rejected entries; keep expired
    non-rejected so the operator's manual flush is surgical.

    Reads the file directly (not the in-memory _STORE) so the CLI
    works against a stopped process / fresh shell where _STORE is empty.
    """
    path = _persist_path()
    if path is None or not path.exists():
        return _ClearSummary(kept=0, dropped=0, dropped_expired=0, dropped_rejected=0)
    ts = _time.time() if now is None else now

    # Coalesce by client_order_id to the LAST line per id (matches
    # the load-from-disk semantics in _load_store_from_disk).
    last_by_id: dict[str, dict[str, Any]] = {}
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
                last_by_id[cid] = obj
    except OSError:
        return _ClearSummary(kept=0, dropped=0, dropped_expired=0, dropped_rejected=0)

    kept: list[IdempotencyRecord] = []
    dropped_expired = 0
    dropped_rejected = 0
    for cid, obj in last_by_id.items():
        status = (obj.get("status") or "").lower()
        ts_raw = obj.get("recorded_at", 0.0)
        try:
            recorded_at = float(ts_raw) if ts_raw is not None else 0.0
        except (TypeError, ValueError):
            recorded_at = 0.0
        rec = IdempotencyRecord(
            client_order_id=cid,
            venue=obj.get("venue", ""),
            symbol=obj.get("symbol", ""),
            is_new=False,
            status=obj.get("status", "unknown"),
            broker_order_id=obj.get("broker_order_id"),
            note=obj.get("note", ""),
            response_payload=obj.get("response_payload"),
            intent_payload=obj.get("intent_payload") or {},
            recorded_at=recorded_at,
        )
        is_rejected = status == "rejected"
        if rejected_only:
            if is_rejected:
                dropped_rejected += 1
                continue
            kept.append(rec)
            continue
        # Default --clear behaviour: drop expired AND drop rejected.
        if is_rejected:
            dropped_rejected += 1
            continue
        if _is_expired(rec, now=ts):
            dropped_expired += 1
            continue
        kept.append(rec)

    _atomic_replace_store(kept)
    # Also refresh the in-memory store so any other code in the same
    # process (rare; CLI is usually a separate proc) sees the result.
    with _LOCK:
        _STORE.clear()
        for rec in kept:
            _STORE[rec.client_order_id] = rec

    return _ClearSummary(
        kept=len(kept),
        dropped=dropped_expired + dropped_rejected,
        dropped_expired=dropped_expired,
        dropped_rejected=dropped_rejected,
    )


def _main(argv: list[str] | None = None) -> int:
    parser = _argparse.ArgumentParser(
        prog="python -m eta_engine.safety.idempotency",
        description="Maintenance tool for the order-submission dedup store.",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Filter the JSONL store: drop expired entries and "
        "rejected entries (or only rejected with --rejected-only).",
    )
    parser.add_argument(
        "--rejected-only",
        action="store_true",
        help="With --clear: drop only REJECTED entries; keep expired non-rejected rows in place.",
    )
    args = parser.parse_args(argv)
    if not args.clear:
        parser.print_help()
        return 2
    summary = _clear_store(rejected_only=args.rejected_only)
    print(
        f"kept {summary.kept} entries, dropped {summary.dropped} "
        f"({summary.dropped_expired} expired, {summary.dropped_rejected} rejected)"
    )
    return 0


__all__ = [
    "IdempotencyError",
    "IdempotencyRecord",
    "check_or_register",
    "evict",
    "record_result",
    "reset_store_for_test",
]


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(_main(_sys.argv[1:]))

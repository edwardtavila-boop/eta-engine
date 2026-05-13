"""Shared execution lease for live broker order writers.

The lease is intentionally small and file-backed so separate Windows
Scheduled Tasks cannot both believe they are the active order-entry
owner for the same broker account. It is not a substitute for broker
state reconciliation; it is the mutex that prevents two ETA processes
from submitting at the same time through different IBKR client IDs.
"""

from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

_DEFAULT_ROOT = Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/execution_leases")
_DEFAULT_TTL_S = 300.0
_LOCK_WAIT_S = 5.0


class ExecutionLeaseError(RuntimeError):
    """Base class for lease failures."""


class ExecutionLeaseHeld(ExecutionLeaseError):
    """Raised when a fresh lease is already held by another owner."""

    def __init__(self, message: str, *, holder: dict[str, Any]) -> None:
        super().__init__(message)
        self.holder = holder


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    venue: str
    account: str
    owner: str
    client_id: int | None
    ttl_s: float
    path: Path
    acquired_at: float
    updated_at: float
    expires_at: float
    hostname: str
    pid: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "account": self.account,
            "owner": self.owner,
            "client_id": self.client_id,
            "ttl_s": self.ttl_s,
            "acquired_at": self.acquired_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "hostname": self.hostname,
            "pid": self.pid,
        }


def default_execution_lease_root() -> Path:
    raw = os.environ.get("ETA_EXECUTION_LEASE_ROOT", "").strip()
    return Path(raw) if raw else _DEFAULT_ROOT


def _now(value: float | None = None) -> float:
    return time.time() if value is None else float(value)


def _ttl(value: float | None = None) -> float:
    if value is not None:
        return max(1.0, float(value))
    raw = os.environ.get("ETA_EXECUTION_LEASE_TTL_S", "").strip()
    if not raw:
        return _DEFAULT_TTL_S
    try:
        return max(1.0, float(raw))
    except ValueError:
        return _DEFAULT_TTL_S


def _clean_token(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)
    return cleaned.strip("._") or "unknown"


def _lease_path(venue: str, account: str, *, root: Path | None = None) -> Path:
    base = default_execution_lease_root() if root is None else Path(root)
    return base / f"{_clean_token(venue)}_{_clean_token(account)}.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


@contextmanager
def _file_lock(path: Path, *, ttl_s: float) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_WAIT_S
    stale_after = max(30.0, ttl_s * 2.0)
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{socket.gethostname()}:{os.getpid()}:{time.time()}\n".encode())
            break
        except FileExistsError:
            try:
                lock_age = time.time() - lock_path.stat().st_mtime
            except OSError:
                lock_age = 0.0
            if lock_age > stale_after:
                try:
                    lock_path.unlink()
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise ExecutionLeaseError(f"execution lease lock is busy: {lock_path}") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            with os.fdopen(fd, "w"):
                pass
        with suppress(OSError):
            lock_path.unlink()


def _lease_from_payload(path: Path, payload: dict[str, Any]) -> ExecutionLease:
    return ExecutionLease(
        venue=str(payload.get("venue") or "unknown"),
        account=str(payload.get("account") or "unknown"),
        owner=str(payload.get("owner") or "unknown"),
        client_id=payload.get("client_id"),
        ttl_s=float(payload.get("ttl_s") or _DEFAULT_TTL_S),
        path=path,
        acquired_at=float(payload.get("acquired_at") or 0.0),
        updated_at=float(payload.get("updated_at") or 0.0),
        expires_at=float(payload.get("expires_at") or 0.0),
        hostname=str(payload.get("hostname") or ""),
        pid=int(payload.get("pid") or 0),
    )


def _new_lease(
    *,
    venue: str,
    account: str,
    owner: str,
    client_id: int | None,
    ttl_s: float,
    path: Path,
    now: float,
    acquired_at: float | None = None,
) -> ExecutionLease:
    return ExecutionLease(
        venue=venue,
        account=account,
        owner=owner,
        client_id=client_id,
        ttl_s=ttl_s,
        path=path,
        acquired_at=now if acquired_at is None else acquired_at,
        updated_at=now,
        expires_at=now + ttl_s,
        hostname=socket.gethostname(),
        pid=os.getpid(),
    )


def read_execution_lease(
    venue: str,
    account: str,
    *,
    root: Path | None = None,
) -> dict[str, Any] | None:
    return _read_json(_lease_path(venue, account, root=root))


def acquire_execution_lease(
    *,
    venue: str,
    account: str,
    owner: str,
    client_id: int | None = None,
    ttl_s: float | None = None,
    root: Path | None = None,
    now: float | None = None,
) -> ExecutionLease:
    if not owner.strip():
        raise ExecutionLeaseError("execution lease owner must be non-empty")
    current_time = _now(now)
    lease_ttl = _ttl(ttl_s)
    path = _lease_path(venue, account, root=root)
    with _file_lock(path, ttl_s=lease_ttl):
        existing = _read_json(path)
        if existing:
            existing_owner = str(existing.get("owner") or "")
            existing_client_id = existing.get("client_id")
            existing_expires_at = float(existing.get("expires_at") or 0.0)
            is_same_owner = existing_owner == owner and (client_id is None or existing_client_id == client_id)
            if existing_expires_at > current_time and not is_same_owner:
                raise ExecutionLeaseHeld(
                    f"execution lease for {venue}/{account} is held by {existing_owner}",
                    holder=existing,
                )
            acquired_at = float(existing.get("acquired_at") or current_time) if is_same_owner else current_time
        else:
            acquired_at = current_time
        lease = _new_lease(
            venue=venue,
            account=account,
            owner=owner,
            client_id=client_id,
            ttl_s=lease_ttl,
            path=path,
            now=current_time,
            acquired_at=acquired_at,
        )
        _write_json_atomic(path, lease.to_dict())
        return lease


def refresh_execution_lease(
    lease: ExecutionLease,
    *,
    now: float | None = None,
) -> ExecutionLease:
    return acquire_execution_lease(
        venue=lease.venue,
        account=lease.account,
        owner=lease.owner,
        client_id=lease.client_id,
        ttl_s=lease.ttl_s,
        root=lease.path.parent,
        now=now,
    )


def release_execution_lease(lease: ExecutionLease) -> bool:
    with _file_lock(lease.path, ttl_s=lease.ttl_s):
        existing = _read_json(lease.path)
        if not existing:
            return False
        if str(existing.get("owner") or "") != lease.owner or existing.get("client_id") != lease.client_id:
            return False
        try:
            lease.path.unlink()
            return True
        except OSError:
            return False


__all__ = [
    "ExecutionLease",
    "ExecutionLeaseError",
    "ExecutionLeaseHeld",
    "acquire_execution_lease",
    "default_execution_lease_root",
    "read_execution_lease",
    "refresh_execution_lease",
    "release_execution_lease",
]

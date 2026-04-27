"""Dashboard auth helpers (Wave-7, 2026-04-27).

Two-tier auth:
  1. Session: cookie-based, 24h default TTL, bcrypt-hashed password.
  2. Step-up: short-lived (15-min) elevation for irreversible actions
     (kill, flatten, V22 toggle, master actions). Re-prompts the operator
     for a PIN before allowing the action.

Storage:
  * users.json     -- {username: {bcrypt_hash, created_at}}
  * sessions.json  -- {token: {user, created_at, expires_at, step_up_at}}

Both files are read/written via portalocker (cross-platform fcntl/msvcrt)
so concurrent processes don't corrupt them.
"""
from __future__ import annotations

import json
import secrets
import time
from typing import TYPE_CHECKING, Any

import bcrypt
import portalocker

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_SESSION_TTL_SECONDS = 24 * 3600
STEP_UP_TTL_SECONDS = 15 * 60


def _now() -> float:
    """Monkeypatchable clock."""
    return time.time()


def _read_locked(path: Path) -> dict[str, Any]:
    """Read JSON with a shared file lock. Returns {} on missing/corrupt."""
    if not path.exists():
        return {}
    try:
        with portalocker.Lock(str(path), mode="r", timeout=2,
                               flags=portalocker.LOCK_SH) as fh:
            return json.loads(fh.read() or "{}")
    except (portalocker.LockException, json.JSONDecodeError, OSError):
        return {}


def _write_locked(path: Path, data: dict[str, Any]) -> None:
    """Write JSON with an exclusive file lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(path), mode="w", timeout=2,
                           flags=portalocker.LOCK_EX) as fh:
        fh.write(json.dumps(data, indent=2))


def create_user(users_path: Path, username: str, password: str) -> None:
    """Create or replace a user. Bcrypt-hashes the password."""
    users = _read_locked(users_path)
    bhash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    users[username] = {
        "bcrypt_hash": bhash,
        "created_at": _now(),
    }
    _write_locked(users_path, users)


def verify_password(users_path: Path, username: str, password: str) -> bool:
    """Return True if password matches the bcrypt hash for username."""
    users = _read_locked(users_path)
    user = users.get(username)
    if not user:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            user["bcrypt_hash"].encode("ascii"),
        )
    except (ValueError, KeyError):
        return False


def create_session(
    sessions_path: Path,
    user: str,
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
) -> str:
    """Create a new session and return its token."""
    sessions = _read_locked(sessions_path)
    token = secrets.token_urlsafe(32)
    now = _now()
    sessions[token] = {
        "user": user,
        "created_at": now,
        "expires_at": now + ttl_seconds,
        "step_up_at": None,
    }
    _write_locked(sessions_path, sessions)
    return token


def get_session(sessions_path: Path, token: str) -> dict[str, Any] | None:
    """Return the session row for ``token`` or None if missing/expired."""
    sessions = _read_locked(sessions_path)
    s = sessions.get(token)
    if s is None:
        return None
    if _now() > s.get("expires_at", 0):
        return None
    return s


def mark_step_up(sessions_path: Path, token: str) -> None:
    """Mark this session as step-up'd (15-min window starts now)."""
    sessions = _read_locked(sessions_path)
    if token in sessions:
        sessions[token]["step_up_at"] = _now()
        _write_locked(sessions_path, sessions)


def is_stepped_up(sessions_path: Path, token: str) -> bool:
    """True when the session has step-up auth that's still fresh."""
    s = get_session(sessions_path, token)
    if s is None or s.get("step_up_at") is None:
        return False
    return (_now() - s["step_up_at"]) < STEP_UP_TTL_SECONDS


def revoke_session(sessions_path: Path, token: str) -> None:
    """Delete a session (logout)."""
    sessions = _read_locked(sessions_path)
    sessions.pop(token, None)
    _write_locked(sessions_path, sessions)

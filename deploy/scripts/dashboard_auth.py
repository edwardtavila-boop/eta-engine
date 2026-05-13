"""Dashboard auth helpers (Wave-7, 2026-04-27).

Two-tier auth:
  1. Session: cookie-based, 24h default TTL.
  2. Step-up: short-lived (15-min) elevation for irreversible actions
     (kill, flatten, V22 toggle, master actions). Re-prompts the operator
     for a PIN before allowing the action.

Storage:
  * users.json     -- {username: {password_hash, created_at, bcrypt_hash?}}
  * sessions.json  -- {token: {user, created_at, expires_at, step_up_at}}

New users are stored with a stdlib-only PBKDF2 hash so auth keeps working even
if the optional ``bcrypt`` wheel is missing on a runtime host. Legacy
``bcrypt_hash`` records remain readable when ``bcrypt`` is available and are
backfilled to ``password_hash`` after a successful login.

Both files are read/written via portalocker (cross-platform fcntl/msvcrt)
so concurrent processes don't corrupt them.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from typing import TYPE_CHECKING, Any

import portalocker

try:
    import bcrypt as _bcrypt
except ModuleNotFoundError:  # pragma: no cover - exercised via monkeypatch in tests
    _bcrypt = None

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_SESSION_TTL_SECONDS = 24 * 3600
STEP_UP_TTL_SECONDS = 15 * 60
DEFAULT_PASSWORD_HASH_ITERATIONS = 600_000


def _now() -> float:
    """Monkeypatchable clock."""
    return time.time()


def _read_locked(path: Path) -> dict[str, Any]:
    """Read JSON with a shared file lock. Returns {} on missing/corrupt."""
    if not path.exists():
        return {}
    try:
        with portalocker.Lock(str(path), mode="r", timeout=2, flags=portalocker.LOCK_SH) as fh:
            return json.loads(fh.read() or "{}")
    except (portalocker.LockException, json.JSONDecodeError, OSError):
        return {}


def _write_locked(path: Path, data: dict[str, Any]) -> None:
    """Write JSON with an exclusive file lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(path), mode="w", timeout=2, flags=portalocker.LOCK_EX) as fh:
        fh.write(json.dumps(data, indent=2))


def _hash_password(password: str, *, iterations: int = DEFAULT_PASSWORD_HASH_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return (
        "pbkdf2_sha256"
        f"${iterations}"
        f"${base64.b64encode(salt).decode('ascii')}"
        f"${base64.b64encode(digest).decode('ascii')}"
    )


def _verify_pbkdf2_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = encoded.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iterations_raw)
        salt = base64.b64decode(salt_raw.encode("ascii"))
        expected = base64.b64decode(digest_raw.encode("ascii"))
    except (ValueError, binascii.Error):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def create_user(users_path: Path, username: str, password: str) -> None:
    """Create or replace a user with a stdlib-backed password hash."""
    users = _read_locked(users_path)
    users[username] = {
        "password_hash": _hash_password(password),
        "created_at": _now(),
    }
    _write_locked(users_path, users)


def verify_password(users_path: Path, username: str, password: str) -> bool:
    """Return True if password matches the stored hash for username."""
    users = _read_locked(users_path)
    user = users.get(username)
    if not user:
        return False
    password_hash = user.get("password_hash")
    if isinstance(password_hash, str) and _verify_pbkdf2_password(password, password_hash):
        return True

    legacy_bcrypt_hash = user.get("bcrypt_hash")
    if not isinstance(legacy_bcrypt_hash, str) or _bcrypt is None:
        return False
    try:
        ok = _bcrypt.checkpw(
            password.encode("utf-8"),
            legacy_bcrypt_hash.encode("ascii"),
        )
    except ValueError:
        return False
    if not ok:
        return False

    if not isinstance(password_hash, str):
        users[username] = {
            **user,
            "password_hash": _hash_password(password),
        }
        _write_locked(users_path, users)
    return True


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

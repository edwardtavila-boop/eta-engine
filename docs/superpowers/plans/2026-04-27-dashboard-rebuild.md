# Dashboard Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken dashboard at `http://127.0.0.1:8420/` with a vanilla-SPA JARVIS command center + bot fleet view, served by `eta_engine/deploy/scripts/dashboard_api.py`, refreshing via SSE for verdict/fill streams and 5s polling for everything else.

**Architecture:** Extend the existing FastAPI `dashboard_api.py` with ~22 new endpoints (auth, governor, fleet, lifecycle, SSE) ported from `firm/eta_engine/command_center/server/app.py`. Replace `deploy/status_page/index.html` (the broken 945KB bundle) with a hand-written shell + 5 vanilla JS modules + 1 CSS file using Tailwind via CDN. No build step.

**Tech Stack:** Python 3.14, FastAPI, uvicorn, pytest, freezegun, portalocker, bcrypt; vanilla JS (ES modules), Tailwind CSS via CDN, Chart.js via CDN, Playwright for end-to-end tests.

**Spec:** `eta_engine/docs/superpowers/specs/2026-04-27-dashboard-rebuild-design.md`

---

## File Structure

### Backend files

| Path | Action | Purpose |
|---|---|---|
| `eta_engine/deploy/scripts/dashboard_api.py` | Modify | Add ~22 endpoints + auth middleware + SSE stream + safe state-file reader |
| `eta_engine/deploy/scripts/dashboard_auth.py` | Create | Session + step-up auth helpers (bcrypt, session table I/O, dependency injectors) |
| `eta_engine/deploy/scripts/dashboard_sse.py` | Create | SSE tail-follow generator for audit + fills JSONL |
| `eta_engine/deploy/scripts/dashboard_state.py` | Create | `read_json_safe()` (returns `{_warning: "no_data"}` on missing) + `portalocker` wrapper |
| `eta_engine/tests/test_dashboard_auth.py` | Create | Auth + step-up unit tests |
| `eta_engine/tests/test_dashboard_endpoints.py` | Create | One happy-path + one failure-mode test per new endpoint |
| `eta_engine/tests/test_dashboard_sse.py` | Create | SSE tail-follow + reconnect + midnight-rotation tests |
| `eta_engine/tests/test_dashboard_lifecycle.py` | Create | Bot lifecycle endpoint tests (mocked FleetCoordinator) |
| `eta_engine/tests/test_dashboard_e2e.py` | Create | Playwright suite (5 end-to-end tests) |
| `eta_engine/tests/conftest.py` | Modify (line 1, add fixture) | Add `auth_session_for(user)` fixture used by every dashboard test |

### Frontend files

| Path | Action | Purpose |
|---|---|---|
| `eta_engine/deploy/status_page/index.html` | Replace | Shell DOM with login modal, top bar, tab nav, panel containers (empty divs with stable IDs), bottom fill tape strip |
| `eta_engine/deploy/status_page/theme.css` | Create | Tailwind CDN base + dark-mode tokens + panel/card/badge/table base styles |
| `eta_engine/deploy/status_page/js/panels.js` | Create | `Panel` base class + formatters (`formatNumber`, `formatPct`, `formatTime`, `formatR`) |
| `eta_engine/deploy/status_page/js/auth.js` | Create | Session check, login flow, step-up modal, global fetch wrapper that 401s back to login |
| `eta_engine/deploy/status_page/js/live.js` | Create | `LiveStream` class (EventSource wrapper, exponential backoff) + `Poller` class (5s scheduler, visibility suspend) |
| `eta_engine/deploy/status_page/js/command_center.js` | Create | All 10 JARVIS panels as `Panel` subclasses |
| `eta_engine/deploy/status_page/js/bot_fleet.js` | Create | All 12 fleet panels + lifecycle button handlers as `Panel` subclasses |

### Rollout files

| Path | Action | Purpose |
|---|---|---|
| `eta_engine/deploy/scripts/run_dashboard_8421.ps1` | Create | Launch script for Stage 0 QA on port 8421 (alongside the old one on 8420) |
| `eta_engine/deploy/scripts/register_operator_tasks.ps1` | Modify (cutover step) | Update the `Apex-Dashboard` (or equivalent) scheduled task to launch `dashboard_api.py` on 8420 instead of `firm/.../command_center/server/app.py` |

---

## Phase 1: Backend Foundation

### Task 1: Safe state-file reader

**Files:**
- Create: `eta_engine/deploy/scripts/dashboard_state.py`
- Create: `eta_engine/tests/test_dashboard_state.py`

- [ ] **Step 1: Write the failing tests**

```python
# eta_engine/tests/test_dashboard_state.py
"""Tests for safe state-file reader (Wave-7 dashboard, 2026-04-27)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_read_json_safe_returns_data_on_happy_path(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    f = tmp_path / "stuff.json"
    f.write_text(json.dumps({"ok": True}), encoding="utf-8")
    out = read_json_safe(f)
    assert out == {"ok": True}


def test_read_json_safe_returns_warning_when_missing(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    out = read_json_safe(tmp_path / "missing.json")
    assert out == {"_warning": "no_data", "_path": str(tmp_path / "missing.json")}


def test_read_json_safe_returns_error_on_corrupt(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    f = tmp_path / "bad.json"
    f.write_text("not json {{{", encoding="utf-8")
    out = read_json_safe(f)
    assert out["_error_code"] == "state_corrupt"
    assert "bad.json" in out["_path"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest eta_engine/tests/test_dashboard_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'eta_engine.deploy.scripts.dashboard_state'`

- [ ] **Step 3: Create the module**

```python
# eta_engine/deploy/scripts/dashboard_state.py
"""Safe state-file reader for the dashboard (Wave-7, 2026-04-27).

Replaces the bare ``_read_json`` in dashboard_api.py that 404s on missing
files. The dashboard must NEVER 500 / 404 on cold-start -- every endpoint
returns a recoverable JSON shape so the UI can render an empty-state
panel instead of a broken one.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def read_json_safe(path: Path) -> dict[str, Any]:
    """Read JSON from ``path``, or return a structured warning/error dict.

    Returns:
      * ``{...}``                       when file exists and parses
      * ``{"_warning": "no_data", ...}`` when file missing
      * ``{"_error_code": "state_corrupt", ...}`` when JSON parse fails

    Never raises. The dashboard relies on this to keep cold-start UI sane.
    """
    if not path.exists():
        return {"_warning": "no_data", "_path": str(path)}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("state_corrupt at %s: %s", path, exc)
        return {
            "_error_code": "state_corrupt",
            "_error_detail": str(exc),
            "_path": str(path),
        }
    except OSError as exc:
        logger.warning("state_io_error at %s: %s", path, exc)
        return {
            "_error_code": "state_io_error",
            "_error_detail": str(exc),
            "_path": str(path),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest eta_engine/tests/test_dashboard_state.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add eta_engine/deploy/scripts/dashboard_state.py eta_engine/tests/test_dashboard_state.py
git commit -m "feat(dashboard): add read_json_safe -- never 404 on cold-start state files"
```

---

### Task 2: Auth module — sessions, bcrypt, login/logout

**Files:**
- Create: `eta_engine/deploy/scripts/dashboard_auth.py`
- Create: `eta_engine/tests/test_dashboard_auth.py`

- [ ] **Step 1: Write the failing tests**

```python
# eta_engine/tests/test_dashboard_auth.py
"""Tests for dashboard auth (Wave-7, 2026-04-27)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_create_user_and_verify_password(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_auth import (
        create_user, verify_password,
    )

    users_path = tmp_path / "users.json"
    create_user(users_path, "edward", "correct horse battery staple")
    assert verify_password(users_path, "edward", "correct horse battery staple") is True
    assert verify_password(users_path, "edward", "wrong") is False
    assert verify_password(users_path, "nobody", "anything") is False


def test_create_session_and_lookup(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_auth import (
        create_session, get_session,
    )

    sessions_path = tmp_path / "sessions.json"
    token = create_session(sessions_path, user="edward", ttl_seconds=3600)
    s = get_session(sessions_path, token)
    assert s is not None
    assert s["user"] == "edward"
    assert s["step_up_at"] is None  # not stepped up yet


def test_get_session_returns_none_for_unknown_token(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_auth import get_session

    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text("{}", encoding="utf-8")
    assert get_session(sessions_path, "fake-token") is None


def test_step_up_marks_session(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_auth import (
        create_session, get_session, mark_step_up,
    )

    sessions_path = tmp_path / "sessions.json"
    token = create_session(sessions_path, user="edward", ttl_seconds=3600)
    mark_step_up(sessions_path, token)
    s = get_session(sessions_path, token)
    assert s["step_up_at"] is not None


def test_session_expired_returns_none(tmp_path: Path, monkeypatch) -> None:
    """A session past its expires_at should not be returned."""
    from eta_engine.deploy.scripts import dashboard_auth as da

    sessions_path = tmp_path / "sessions.json"
    token = da.create_session(sessions_path, user="edward", ttl_seconds=1)

    # Fast-forward time
    import time
    monkeypatch.setattr(da, "_now", lambda: time.time() + 10)
    assert da.get_session(sessions_path, token) is None


def test_revoke_session(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_auth import (
        create_session, get_session, revoke_session,
    )

    sessions_path = tmp_path / "sessions.json"
    token = create_session(sessions_path, user="edward", ttl_seconds=3600)
    revoke_session(sessions_path, token)
    assert get_session(sessions_path, token) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest eta_engine/tests/test_dashboard_auth.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the module**

```python
# eta_engine/deploy/scripts/dashboard_auth.py
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
from pathlib import Path
from typing import Any

import bcrypt
import portalocker

DEFAULT_SESSION_TTL_SECONDS = 24 * 3600
DEFAULT_STEP_UP_PIN_LEN = 6
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest eta_engine/tests/test_dashboard_auth.py -v`
Expected: 6 PASSED

- [ ] **Step 5: Commit**

```bash
git add eta_engine/deploy/scripts/dashboard_auth.py eta_engine/tests/test_dashboard_auth.py
git commit -m "feat(dashboard): bcrypt-hashed users + cookie sessions + 15-min step-up auth"
```

---

### Task 3: Auth endpoints in dashboard_api.py

**Files:**
- Modify: `eta_engine/deploy/scripts/dashboard_api.py` (add 4 endpoints + dependency)
- Create: `eta_engine/tests/test_dashboard_auth_endpoints.py`

- [ ] **Step 1: Write the failing tests**

```python
# eta_engine/tests/test_dashboard_auth_endpoints.py
"""Tests for dashboard auth endpoints."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def auth_paths(tmp_path: Path, monkeypatch):
    """Point the dashboard at a temp users + sessions store."""
    users = tmp_path / "users.json"
    sessions = tmp_path / "sessions.json"
    monkeypatch.setenv("ETA_DASHBOARD_USERS_PATH", str(users))
    monkeypatch.setenv("ETA_DASHBOARD_SESSIONS_PATH", str(sessions))
    # Seed an operator account
    from eta_engine.deploy.scripts.dashboard_auth import create_user
    create_user(users, "edward", "test-pass-123")
    return {"users": users, "sessions": sessions}


@pytest.fixture
def client(auth_paths):
    from eta_engine.deploy.scripts.dashboard_api import app
    return TestClient(app)


def test_session_endpoint_unauthenticated(client) -> None:
    r = client.get("/api/auth/session")
    assert r.status_code == 200
    assert r.json() == {"authenticated": False}


def test_login_success_sets_cookie(client) -> None:
    r = client.post("/api/auth/login", json={
        "username": "edward", "password": "test-pass-123",
    })
    assert r.status_code == 200
    assert r.json()["authenticated"] is True
    assert "session" in r.cookies


def test_login_wrong_password_returns_401(client) -> None:
    r = client.post("/api/auth/login", json={
        "username": "edward", "password": "wrong",
    })
    assert r.status_code == 401


def test_session_endpoint_authenticated_after_login(client) -> None:
    client.post("/api/auth/login", json={
        "username": "edward", "password": "test-pass-123",
    })
    r = client.get("/api/auth/session")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["user"] == "edward"


def test_logout_revokes_session(client) -> None:
    client.post("/api/auth/login", json={
        "username": "edward", "password": "test-pass-123",
    })
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    # Subsequent session check should be unauthenticated
    r2 = client.get("/api/auth/session")
    assert r2.json() == {"authenticated": False}


def test_step_up_endpoint_requires_login(client) -> None:
    r = client.post("/api/auth/step-up", json={"pin": "0000"})
    assert r.status_code == 401


def test_step_up_endpoint_marks_session(client, auth_paths, monkeypatch) -> None:
    monkeypatch.setenv("ETA_DASHBOARD_STEP_UP_PIN", "1234")
    client.post("/api/auth/login", json={
        "username": "edward", "password": "test-pass-123",
    })
    r = client.post("/api/auth/step-up", json={"pin": "1234"})
    assert r.status_code == 200
    assert r.json()["stepped_up"] is True


def test_step_up_endpoint_wrong_pin_returns_403(client, monkeypatch) -> None:
    monkeypatch.setenv("ETA_DASHBOARD_STEP_UP_PIN", "1234")
    client.post("/api/auth/login", json={
        "username": "edward", "password": "test-pass-123",
    })
    r = client.post("/api/auth/step-up", json={"pin": "0000"})
    assert r.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest eta_engine/tests/test_dashboard_auth_endpoints.py -v`
Expected: FAIL — endpoints don't exist yet

- [ ] **Step 3: Add auth endpoints + dependency to dashboard_api.py**

Insert this block after the existing `_read_json` function (around line 75) in `eta_engine/deploy/scripts/dashboard_api.py`:

```python
# ---------------------------------------------------------------------------
# Auth (Wave-7, 2026-04-27)
# ---------------------------------------------------------------------------
import os
from fastapi import Cookie, Depends, Request, Response, status
from pydantic import BaseModel

_USERS_PATH = Path(os.environ.get(
    "ETA_DASHBOARD_USERS_PATH",
    str(STATE_DIR / "auth" / "users.json"),
))
_SESSIONS_PATH = Path(os.environ.get(
    "ETA_DASHBOARD_SESSIONS_PATH",
    str(STATE_DIR / "auth" / "sessions.json"),
))
_STEP_UP_PIN = os.environ.get("ETA_DASHBOARD_STEP_UP_PIN", "")


class LoginRequest(BaseModel):
    username: str
    password: str


class StepUpRequest(BaseModel):
    pin: str


def require_session(session: str | None = Cookie(default=None)) -> dict:
    """FastAPI dependency: returns session row or raises 401."""
    from eta_engine.deploy.scripts.dashboard_auth import get_session
    if session is None:
        raise HTTPException(status_code=401, detail={"error_code": "no_session"})
    s = get_session(_SESSIONS_PATH, session)
    if s is None:
        raise HTTPException(status_code=401, detail={"error_code": "session_expired"})
    return s


def require_step_up(session: str | None = Cookie(default=None)) -> dict:
    """FastAPI dependency: requires fresh step-up auth."""
    from eta_engine.deploy.scripts.dashboard_auth import is_stepped_up
    s = require_session(session)
    if not is_stepped_up(_SESSIONS_PATH, session):
        raise HTTPException(status_code=403, detail={"error_code": "step_up_required"})
    return s


@app.get("/api/auth/session")
def auth_session(session: str | None = Cookie(default=None)) -> dict:
    from eta_engine.deploy.scripts.dashboard_auth import get_session, is_stepped_up
    if session is None:
        return {"authenticated": False}
    s = get_session(_SESSIONS_PATH, session)
    if s is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "user": s["user"],
        "stepped_up": is_stepped_up(_SESSIONS_PATH, session),
    }


@app.post("/api/auth/login")
def auth_login(req: LoginRequest, response: Response) -> dict:
    from eta_engine.deploy.scripts.dashboard_auth import (
        create_session, verify_password,
    )
    if not verify_password(_USERS_PATH, req.username, req.password):
        raise HTTPException(status_code=401, detail={"error_code": "bad_credentials"})
    token = create_session(_SESSIONS_PATH, user=req.username)
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        samesite="strict",
        max_age=24 * 3600,
    )
    return {"authenticated": True, "user": req.username}


@app.post("/api/auth/logout")
def auth_logout(
    response: Response,
    session: str | None = Cookie(default=None),
) -> dict:
    from eta_engine.deploy.scripts.dashboard_auth import revoke_session
    if session is not None:
        revoke_session(_SESSIONS_PATH, session)
    response.delete_cookie(key="session")
    return {"authenticated": False}


@app.post("/api/auth/step-up")
def auth_step_up(
    req: StepUpRequest,
    session: str | None = Cookie(default=None),
) -> dict:
    from eta_engine.deploy.scripts.dashboard_auth import mark_step_up
    if session is None:
        raise HTTPException(status_code=401, detail={"error_code": "no_session"})
    if not _STEP_UP_PIN or req.pin != _STEP_UP_PIN:
        raise HTTPException(status_code=403, detail={"error_code": "bad_pin"})
    mark_step_up(_SESSIONS_PATH, session)
    return {"stepped_up": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest eta_engine/tests/test_dashboard_auth_endpoints.py -v`
Expected: 8 PASSED

- [ ] **Step 5: Commit**

```bash
git add eta_engine/deploy/scripts/dashboard_api.py eta_engine/tests/test_dashboard_auth_endpoints.py
git commit -m "feat(dashboard): /api/auth/session,login,logout,step-up + require_session/step_up dependencies"
```

---

### Task 4: Frontend static-asset router

**Files:**
- Modify: `eta_engine/deploy/scripts/dashboard_api.py` (add `/theme.css` and `/js/{file}.js` routes)

- [ ] **Step 1: Write the failing test**

```python
# Add to eta_engine/tests/test_dashboard_endpoints.py (create file if missing)
"""General dashboard endpoint tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from eta_engine.deploy.scripts.dashboard_api import app
    return TestClient(app)


def test_serve_theme_css(client, tmp_path, monkeypatch) -> None:
    """The dashboard serves theme.css from deploy/status_page/."""
    # Resolve the on-disk path the dashboard uses for static assets
    from eta_engine.deploy.scripts.dashboard_api import _STATUS_PAGE
    css_path = _STATUS_PAGE.parent / "theme.css"
    css_path.parent.mkdir(parents=True, exist_ok=True)
    css_path.write_text("/* test css */", encoding="utf-8")

    r = client.get("/theme.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    assert "/* test css */" in r.text


def test_serve_js_module(client) -> None:
    from eta_engine.deploy.scripts.dashboard_api import _STATUS_PAGE
    js_dir = _STATUS_PAGE.parent / "js"
    js_dir.mkdir(parents=True, exist_ok=True)
    (js_dir / "auth.js").write_text("export const x = 1;", encoding="utf-8")

    r = client.get("/js/auth.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"].lower()
    assert "export const x" in r.text


def test_js_path_traversal_blocked(client) -> None:
    """Reject path-traversal attempts."""
    r = client.get("/js/../dashboard_api.py")
    # FastAPI normalizes the path first, so this should 404
    assert r.status_code in (400, 404)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest eta_engine/tests/test_dashboard_endpoints.py::test_serve_theme_css eta_engine/tests/test_dashboard_endpoints.py::test_serve_js_module -v`
Expected: FAIL — routes don't exist

- [ ] **Step 3: Add the static-asset routes**

Insert into `eta_engine/deploy/scripts/dashboard_api.py` immediately after the `/favicon.ico` route (around line 100):

```python
@app.get("/theme.css", response_class=PlainTextResponse)
def serve_theme_css() -> PlainTextResponse:
    """Serve the dashboard CSS theme."""
    css = _STATUS_PAGE.parent / "theme.css"
    if not css.exists():
        return PlainTextResponse("/* theme.css missing */", media_type="text/css")
    return PlainTextResponse(
        css.read_text(encoding="utf-8"),
        media_type="text/css",
    )


@app.get("/js/{filename}", response_class=PlainTextResponse)
def serve_js_module(filename: str) -> PlainTextResponse:
    """Serve a JS module from deploy/status_page/js/. Path-traversal-safe."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    js_path = _STATUS_PAGE.parent / "js" / filename
    if not js_path.is_file() or js_path.parent != _STATUS_PAGE.parent / "js":
        raise HTTPException(status_code=404, detail=f"{filename} not found")
    return PlainTextResponse(
        js_path.read_text(encoding="utf-8"),
        media_type="application/javascript",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest eta_engine/tests/test_dashboard_endpoints.py -v -k "theme or js_module or path_traversal"`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add eta_engine/deploy/scripts/dashboard_api.py eta_engine/tests/test_dashboard_endpoints.py
git commit -m "feat(dashboard): serve theme.css + /js/<file>.js with path-traversal guard"
```

---

## Phase 2: Backend New Endpoints

### Task 5: Governor + edge_leaderboard + model_tier + kaizen_latest endpoints

**Files:**
- Modify: `eta_engine/deploy/scripts/dashboard_api.py` (add 4 GET endpoints)
- Modify: `eta_engine/tests/test_dashboard_endpoints.py` (add 8 tests — 2 per endpoint)

- [ ] **Step 1: Write the failing tests**

Append to `eta_engine/tests/test_dashboard_endpoints.py`:

```python
def test_governor_returns_warning_when_state_missing(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/governor")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_warning") == "no_data"


def test_governor_returns_data_when_state_present(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    gov = tmp_path / "jarvis_governor.json"
    gov.write_text('{"grade":"A","score":0.92}', encoding="utf-8")
    r = client.get("/api/jarvis/governor")
    assert r.status_code == 200
    assert r.json()["grade"] == "A"


def test_edge_leaderboard_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/edge_leaderboard")
    assert r.status_code == 200
    body = r.json()
    assert "top" in body and "bottom" in body
    assert body["top"] == [] and body["bottom"] == []


def test_edge_leaderboard_with_data(client, tmp_path, monkeypatch) -> None:
    import json
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    edge = tmp_path / "sage" / "edge_tracker.json"
    edge.parent.mkdir(parents=True)
    edge.write_text(json.dumps({
        "schools": {
            "dow_theory": {"n_obs": 50, "n_aligned_wins": 35, "n_aligned_losses": 10, "sum_r": 12.5},
            "fibonacci":  {"n_obs": 50, "n_aligned_wins": 10, "n_aligned_losses": 35, "sum_r": -8.0},
        }
    }), encoding="utf-8")
    r = client.get("/api/jarvis/edge_leaderboard")
    assert r.status_code == 200
    body = r.json()
    assert any(s["school"] == "dow_theory" for s in body["top"])
    assert any(s["school"] == "fibonacci" for s in body["bottom"])


def test_model_tier_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/model_tier")
    assert r.status_code == 200
    assert r.json().get("_warning") == "no_data"


def test_kaizen_latest_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/kaizen_latest")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_warning") == "no_data"


def test_kaizen_latest_returns_markdown(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    tickets = tmp_path / "kaizen" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "2026-04-26_TKT-001.md").write_text("# Ticket 001\nbody", encoding="utf-8")
    r = client.get("/api/jarvis/kaizen_latest")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Ticket 001"
    assert "body" in body["markdown"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest eta_engine/tests/test_dashboard_endpoints.py -v -k "governor or edge_leaderboard or model_tier or kaizen"`
Expected: 7 FAIL — endpoints don't exist

- [ ] **Step 3: Implement the 4 endpoints**

Insert into `eta_engine/deploy/scripts/dashboard_api.py` after `/api/jarvis/sage_disagreement_heatmap` (around line 322):

```python
@app.get("/api/jarvis/governor")
def jarvis_governor() -> dict:
    """Governor snapshot from state/jarvis_governor.json."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe
    return read_json_safe(STATE_DIR / "jarvis_governor.json")


@app.get("/api/jarvis/edge_leaderboard")
def jarvis_edge_leaderboard(bot: str | None = None, limit: int = 5) -> dict:
    """Top + bottom schools by expectancy. Optional ?bot=<id> for per-bot."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe
    edge_path = STATE_DIR / "sage" / "edge_tracker.json"
    if bot:
        edge_path = STATE_DIR / "sage" / f"edge_tracker_{bot}.json"
    data = read_json_safe(edge_path)
    schools = data.get("schools") or {}
    rows = []
    for name, e in schools.items():
        n = (e.get("n_aligned_wins", 0) + e.get("n_aligned_losses", 0))
        avg_r = (e.get("sum_r", 0.0) / n) if n > 0 else 0.0
        rows.append({
            "school": name,
            "n_obs": e.get("n_obs", 0),
            "n_aligned": n,
            "avg_r": round(avg_r, 4),
            "sum_r": e.get("sum_r", 0.0),
        })
    rows.sort(key=lambda r: r["avg_r"], reverse=True)
    return {
        "top": rows[:limit],
        "bottom": list(reversed(rows[-limit:])) if len(rows) > limit else [],
    }


@app.get("/api/jarvis/model_tier")
def jarvis_model_tier() -> dict:
    """Most recent LLM tier routing decision from today's audit log."""
    from datetime import UTC, datetime
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    audit = STATE_DIR / "jarvis_audit" / f"{today}.jsonl"
    if not audit.exists():
        return {"_warning": "no_data", "_path": str(audit)}
    last_llm: dict | None = None
    try:
        for line in audit.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("request", {}).get("action") == "LLM_INVOCATION":
                last_llm = row
    except (json.JSONDecodeError, OSError) as exc:
        return {"_error_code": "audit_parse_failed", "_error_detail": str(exc)}
    if last_llm is None:
        return {"_warning": "no_llm_invocation_today"}
    return {
        "tier": last_llm.get("response", {}).get("selected_model"),
        "ts": last_llm.get("ts"),
        "subsystem": last_llm.get("request", {}).get("subsystem"),
        "task_category": last_llm.get("request", {}).get("payload", {}).get("task_category"),
    }


@app.get("/api/jarvis/kaizen_latest")
def jarvis_kaizen_latest() -> dict:
    """Latest kaizen ticket from state/kaizen/tickets/."""
    tickets_dir = STATE_DIR / "kaizen" / "tickets"
    if not tickets_dir.exists():
        return {"_warning": "no_data", "_path": str(tickets_dir)}
    files = sorted(tickets_dir.glob("*.md"))
    if not files:
        return {"_warning": "no_tickets"}
    latest = files[-1]
    md = latest.read_text(encoding="utf-8")
    title = md.splitlines()[0].lstrip("# ").strip() if md else latest.stem
    return {
        "title": title,
        "filename": latest.name,
        "markdown": md,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest eta_engine/tests/test_dashboard_endpoints.py -v -k "governor or edge_leaderboard or model_tier or kaizen"`
Expected: 7 PASSED

- [ ] **Step 5: Commit**

```bash
git add eta_engine/deploy/scripts/dashboard_api.py eta_engine/tests/test_dashboard_endpoints.py
git commit -m "feat(dashboard): /api/jarvis/{governor,edge_leaderboard,model_tier,kaizen_latest}"
```

---

### Task 6: Bot fleet endpoints — roster + drill-down + risk_gates + position_reconciler

**Files:**
- Modify: `eta_engine/deploy/scripts/dashboard_api.py` (add 4 GET endpoints)
- Modify: `eta_engine/tests/test_dashboard_endpoints.py` (add 8 tests)

- [ ] **Step 1: Write the failing tests**

Append to `eta_engine/tests/test_dashboard_endpoints.py`:

```python
def test_bot_fleet_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/bot-fleet")
    assert r.status_code == 200
    body = r.json()
    assert body["bots"] == []  # no bot status files yet


def test_bot_fleet_assembles_roster(client, tmp_path, monkeypatch) -> None:
    import json
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    bots_dir = tmp_path / "bots"
    for name in ("mnq", "btc_hybrid"):
        (bots_dir / name).mkdir(parents=True)
        (bots_dir / name / "status.json").write_text(json.dumps({
            "name": name, "symbol": name.upper(), "tier": "FUTURES",
            "venue": "tastytrade", "status": "running",
            "todays_pnl": 12.50, "open_positions": 1,
            "last_signal_ts": "2026-04-27T14:00:00Z",
            "heartbeat_ts": "2026-04-27T14:32:00Z",
            "jarvis_attached": True, "journal_attached": True,
            "online_learner_attached": False,
        }), encoding="utf-8")
    r = client.get("/api/bot-fleet")
    assert r.status_code == 200
    bots = r.json()["bots"]
    assert len(bots) == 2
    assert {b["name"] for b in bots} == {"mnq", "btc_hybrid"}


def test_bot_fleet_drilldown(client, tmp_path, monkeypatch) -> None:
    import json
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    bot_dir = tmp_path / "bots" / "mnq"
    bot_dir.mkdir(parents=True)
    (bot_dir / "status.json").write_text(json.dumps({"name": "mnq"}), encoding="utf-8")
    (bot_dir / "recent_fills.json").write_text(json.dumps([
        {"ts": "2026-04-27T13:00Z", "side": "long", "price": 21000, "qty": 1, "realized_r": 1.2}
    ]), encoding="utf-8")
    (bot_dir / "recent_verdicts.json").write_text(json.dumps([
        {"ts": "2026-04-27T13:00Z", "verdict": "APPROVED", "sage_modulation": "v22_sage_loosened"}
    ]), encoding="utf-8")
    r = client.get("/api/bot-fleet/mnq")
    assert r.status_code == 200
    body = r.json()
    assert body["status"]["name"] == "mnq"
    assert len(body["recent_fills"]) == 1
    assert len(body["recent_verdicts"]) == 1


def test_bot_fleet_drilldown_unknown_bot(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/bot-fleet/no-such-bot")
    assert r.status_code == 404


def test_risk_gates_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/risk_gates")
    assert r.status_code == 200
    body = r.json()
    assert body["bots"] == []
    assert body["fleet_aggregate"].get("_warning") == "no_data"


def test_risk_gates_assembles(client, tmp_path, monkeypatch) -> None:
    import json
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    safety = tmp_path / "safety"
    safety.mkdir()
    (safety / "kill_switch_latch.json").write_text(json.dumps({
        "mnq": {"latch_state": "armed"},
        "btc_hybrid": {"latch_state": "tripped", "reason": "dd_kill"},
    }), encoding="utf-8")
    (safety / "fleet_risk_gate_state.json").write_text(json.dumps({
        "fleet_dd_pct": 1.2, "fleet_dd_threshold_pct": 5.0,
    }), encoding="utf-8")
    r = client.get("/api/risk_gates")
    assert r.status_code == 200
    body = r.json()
    bot_states = {b["bot_id"]: b for b in body["bots"]}
    assert bot_states["mnq"]["latch_state"] == "armed"
    assert bot_states["btc_hybrid"]["latch_state"] == "tripped"
    assert body["fleet_aggregate"]["fleet_dd_pct"] == 1.2


def test_position_reconciler_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/positions/reconciler")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_warning") == "no_data"


def test_position_reconciler_returns_drift(client, tmp_path, monkeypatch) -> None:
    import json
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    safety = tmp_path / "safety"
    safety.mkdir()
    (safety / "position_reconciler_latest.json").write_text(json.dumps({
        "ts": "2026-04-27T14:00:00Z",
        "drifts": [{"bot": "mnq", "internal_qty": 1, "broker_qty": 0}],
    }), encoding="utf-8")
    r = client.get("/api/positions/reconciler")
    assert r.status_code == 200
    body = r.json()
    assert len(body["drifts"]) == 1
    assert body["drifts"][0]["bot"] == "mnq"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest eta_engine/tests/test_dashboard_endpoints.py -v -k "bot_fleet or risk_gates or position_reconciler"`
Expected: 8 FAIL — endpoints don't exist

- [ ] **Step 3: Implement the 4 endpoints**

Insert into `eta_engine/deploy/scripts/dashboard_api.py` after the kaizen endpoint:

```python
@app.get("/api/bot-fleet")
def bot_fleet_roster() -> dict:
    """Roster: scan state/bots/<name>/status.json for each bot."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe
    bots_dir = STATE_DIR / "bots"
    if not bots_dir.exists():
        return {"bots": []}
    rows = []
    for bot_dir in sorted(bots_dir.iterdir()):
        if not bot_dir.is_dir():
            continue
        status = read_json_safe(bot_dir / "status.json")
        if "_warning" in status:
            continue
        rows.append(status)
    return {"bots": rows}


@app.get("/api/bot-fleet/{bot_id}")
def bot_fleet_drilldown(bot_id: str) -> dict:
    """Per-bot drill: status + recent fills + recent verdicts + sage effects."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe
    bot_dir = STATE_DIR / "bots" / bot_id
    if not bot_dir.exists():
        raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
    return {
        "status": read_json_safe(bot_dir / "status.json"),
        "recent_fills": read_json_safe(bot_dir / "recent_fills.json"),
        "recent_verdicts": read_json_safe(bot_dir / "recent_verdicts.json"),
        "sage_effects": read_json_safe(bot_dir / "sage_effects.json"),
    }


@app.get("/api/risk_gates")
def risk_gates() -> dict:
    """Per-bot kill latch + DD + cap state + fleet aggregate."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe
    latches = read_json_safe(STATE_DIR / "safety" / "kill_switch_latch.json")
    fleet_agg = read_json_safe(STATE_DIR / "safety" / "fleet_risk_gate_state.json")
    bots = []
    if "_warning" not in latches:
        for bot_id, row in latches.items():
            if not isinstance(row, dict):
                continue
            row_out = {"bot_id": bot_id, **row}
            bots.append(row_out)
    return {"bots": bots, "fleet_aggregate": fleet_agg}


@app.get("/api/positions/reconciler")
def positions_reconciler() -> dict:
    """Latest position reconciler snapshot."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe
    return read_json_safe(STATE_DIR / "safety" / "position_reconciler_latest.json")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest eta_engine/tests/test_dashboard_endpoints.py -v -k "bot_fleet or risk_gates or position_reconciler"`
Expected: 8 PASSED

- [ ] **Step 5: Commit**

```bash
git add eta_engine/deploy/scripts/dashboard_api.py eta_engine/tests/test_dashboard_endpoints.py
git commit -m "feat(dashboard): /api/bot-fleet + drill-down + risk_gates + positions/reconciler"
```

---

### Task 7: Equity + preflight + sage_modulation_stats + sage_modulation_toggle

**Files:**
- Modify: `eta_engine/deploy/scripts/dashboard_api.py` (add 4 endpoints)
- Modify: `eta_engine/tests/test_dashboard_endpoints.py` (add 8 tests)

- [ ] **Step 1: Write the failing tests**

Append to `eta_engine/tests/test_dashboard_endpoints.py`:

```python
def test_equity_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/equity")
    assert r.status_code == 200
    assert r.json().get("_warning") == "no_data"


def test_equity_returns_curve(client, tmp_path, monkeypatch) -> None:
    import json
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    blot = tmp_path / "blotter"
    blot.mkdir()
    (blot / "equity_curve.json").write_text(json.dumps({
        "today": [{"ts": "...", "equity": 50000.0}],
        "thirty_day": [{"ts": "...", "equity": 49500.0}],
    }), encoding="utf-8")
    r = client.get("/api/equity")
    assert r.status_code == 200
    assert "today" in r.json()


def test_preflight_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/preflight")
    assert r.status_code == 200
    body = r.json()
    assert "throttles" in body
    assert body["throttles"] == []


def test_preflight_with_throttles(client, tmp_path, monkeypatch) -> None:
    import json
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    safety = tmp_path / "safety"
    safety.mkdir()
    (safety / "preflight_correlation_latest.json").write_text(json.dumps({
        "throttles": [
            {"symbol_a": "MNQ", "symbol_b": "NQ", "cap_mult": 0.50, "rho": 0.95},
        ],
    }), encoding="utf-8")
    r = client.get("/api/preflight")
    body = r.json()
    assert len(body["throttles"]) == 1


def test_sage_modulation_stats_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/sage_modulation_stats")
    assert r.status_code == 200
    body = r.json()
    assert body["per_bot"] == {}


def test_sage_modulation_toggle_get_default_off(client, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ETA_FF_V22_SAGE_MODULATION", raising=False)
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/sage_modulation_toggle")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_sage_modulation_toggle_get_when_on(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_FF_V22_SAGE_MODULATION", "true")
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/sage_modulation_toggle")
    assert r.json()["enabled"] is True


def test_sage_modulation_toggle_post_requires_step_up(client, tmp_path, monkeypatch) -> None:
    """POST without step-up cookie returns 403."""
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.post("/api/jarvis/sage_modulation_toggle", json={"enabled": True})
    # Without session: 401; without step-up: 403; both are "blocked"
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest eta_engine/tests/test_dashboard_endpoints.py -v -k "equity or preflight or sage_modulation"`
Expected: 8 FAIL

- [ ] **Step 3: Implement the 4 endpoints**

Insert into `eta_engine/deploy/scripts/dashboard_api.py`:

```python
class SageModulationToggleRequest(BaseModel):
    enabled: bool


@app.get("/api/equity")
def equity_curve() -> dict:
    """Today + 30-day equity curve for the fleet."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe
    return read_json_safe(STATE_DIR / "blotter" / "equity_curve.json")


@app.get("/api/preflight")
def preflight_throttle_map() -> dict:
    """Live correlation throttle map (which symbol pairs are throttled)."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe
    data = read_json_safe(STATE_DIR / "safety" / "preflight_correlation_latest.json")
    if "_warning" in data:
        return {"throttles": []}
    return data


@app.get("/api/jarvis/sage_modulation_stats")
def sage_modulation_stats() -> dict:
    """Per-bot count of v22 agree-loosen / disagree-tighten / defer in last 24h."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe
    data = read_json_safe(STATE_DIR / "sage" / "modulation_stats_24h.json")
    if "_warning" in data:
        return {"per_bot": {}, "_warning": "no_data"}
    return data


@app.get("/api/jarvis/sage_modulation_toggle")
def get_sage_modulation_toggle() -> dict:
    """Current state of the V22_SAGE_MODULATION feature flag."""
    enabled = os.environ.get("ETA_FF_V22_SAGE_MODULATION", "false").strip().lower() in (
        "1", "true", "yes", "on", "y",
    )
    return {"enabled": enabled, "flag_name": "ETA_FF_V22_SAGE_MODULATION"}


@app.post("/api/jarvis/sage_modulation_toggle")
def post_sage_modulation_toggle(
    req: SageModulationToggleRequest,
    _: dict = Depends(require_step_up),
) -> dict:
    """Flip ETA_FF_V22_SAGE_MODULATION (process env + persistent state file)."""
    val = "true" if req.enabled else "false"
    os.environ["ETA_FF_V22_SAGE_MODULATION"] = val
    flag_path = STATE_DIR / "feature_flags.json"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if flag_path.exists():
        try:
            existing = json.loads(flag_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    existing["ETA_FF_V22_SAGE_MODULATION"] = val
    flag_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return {"enabled": req.enabled}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest eta_engine/tests/test_dashboard_endpoints.py -v -k "equity or preflight or sage_modulation"`
Expected: 8 PASSED

- [ ] **Step 5: Commit**

```bash
git add eta_engine/deploy/scripts/dashboard_api.py eta_engine/tests/test_dashboard_endpoints.py
git commit -m "feat(dashboard): /api/equity + preflight + sage_modulation_stats + sage_modulation_toggle (POST step-up gated)"
```

---

### Task 8: Bot lifecycle endpoints — pause / resume / flatten / kill

**Files:**
- Modify: `eta_engine/deploy/scripts/dashboard_api.py` (add 4 POST endpoints)
- Create: `eta_engine/tests/test_dashboard_lifecycle.py`

- [ ] **Step 1: Write the failing tests**

```python
# eta_engine/tests/test_dashboard_lifecycle.py
"""Tests for bot lifecycle endpoints."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def auth_paths(tmp_path: Path, monkeypatch):
    users = tmp_path / "users.json"
    sessions = tmp_path / "sessions.json"
    monkeypatch.setenv("ETA_DASHBOARD_USERS_PATH", str(users))
    monkeypatch.setenv("ETA_DASHBOARD_SESSIONS_PATH", str(sessions))
    monkeypatch.setenv("ETA_DASHBOARD_STEP_UP_PIN", "1234")
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    from eta_engine.deploy.scripts.dashboard_auth import create_user
    create_user(users, "edward", "pw")
    return tmp_path


@pytest.fixture
def authed_client(auth_paths):
    from eta_engine.deploy.scripts.dashboard_api import app
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "edward", "password": "pw"})
    return c


@pytest.fixture
def stepped_up_client(authed_client):
    authed_client.post("/api/auth/step-up", json={"pin": "1234"})
    return authed_client


def test_pause_requires_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    from eta_engine.deploy.scripts.dashboard_api import app
    c = TestClient(app)
    r = c.post("/api/bot/mnq/pause")
    assert r.status_code == 401


def test_pause_writes_signal_file(authed_client, auth_paths) -> None:
    r = authed_client.post("/api/bot/mnq/pause")
    assert r.status_code == 200
    sig = auth_paths / "bots" / "mnq" / "control_signals" / "pause.json"
    assert sig.exists()


def test_resume_writes_signal_file(authed_client, auth_paths) -> None:
    r = authed_client.post("/api/bot/mnq/resume")
    assert r.status_code == 200
    sig = auth_paths / "bots" / "mnq" / "control_signals" / "resume.json"
    assert sig.exists()


def test_flatten_requires_step_up(authed_client) -> None:
    r = authed_client.post("/api/bot/mnq/flatten")
    assert r.status_code == 403
    assert r.json()["detail"]["error_code"] == "step_up_required"


def test_flatten_with_step_up_writes_signal(stepped_up_client, auth_paths) -> None:
    r = stepped_up_client.post("/api/bot/mnq/flatten")
    assert r.status_code == 200
    sig = auth_paths / "bots" / "mnq" / "control_signals" / "flatten.json"
    assert sig.exists()


def test_kill_requires_step_up(authed_client) -> None:
    r = authed_client.post("/api/bot/mnq/kill")
    assert r.status_code == 403


def test_kill_with_step_up_trips_latch(stepped_up_client, auth_paths) -> None:
    import json
    r = stepped_up_client.post("/api/bot/mnq/kill")
    assert r.status_code == 200
    latch = auth_paths / "safety" / "kill_switch_latch.json"
    assert latch.exists()
    body = json.loads(latch.read_text(encoding="utf-8"))
    assert body["mnq"]["latch_state"] == "tripped"
    assert body["mnq"]["reason"] == "operator_kill"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest eta_engine/tests/test_dashboard_lifecycle.py -v`
Expected: FAIL — endpoints don't exist

- [ ] **Step 3: Implement the 4 lifecycle endpoints**

Insert into `eta_engine/deploy/scripts/dashboard_api.py`:

```python
def _write_control_signal(bot_id: str, action: str, by_user: str) -> Path:
    """Write a control signal file the bot daemon polls."""
    from datetime import UTC, datetime
    sig_dir = STATE_DIR / "bots" / bot_id / "control_signals"
    sig_dir.mkdir(parents=True, exist_ok=True)
    sig_path = sig_dir / f"{action}.json"
    sig_path.write_text(json.dumps({
        "ts": datetime.now(UTC).isoformat(),
        "action": action,
        "by": by_user,
    }, indent=2), encoding="utf-8")
    return sig_path


@app.post("/api/bot/{bot_id}/pause")
def bot_pause(bot_id: str, session: dict = Depends(require_session)) -> dict:
    """Signal the bot to pause new entries (existing positions kept)."""
    _write_control_signal(bot_id, "pause", session["user"])
    return {"ok": True, "action": "pause", "bot_id": bot_id}


@app.post("/api/bot/{bot_id}/resume")
def bot_resume(bot_id: str, session: dict = Depends(require_session)) -> dict:
    """Signal the bot to resume taking new entries."""
    _write_control_signal(bot_id, "resume", session["user"])
    return {"ok": True, "action": "resume", "bot_id": bot_id}


@app.post("/api/bot/{bot_id}/flatten")
def bot_flatten(bot_id: str, session: dict = Depends(require_step_up)) -> dict:
    """Step-up gated: signal bot to flatten ALL positions (reduce_only)."""
    _write_control_signal(bot_id, "flatten", session["user"])
    return {"ok": True, "action": "flatten", "bot_id": bot_id}


@app.post("/api/bot/{bot_id}/kill")
def bot_kill(bot_id: str, session: dict = Depends(require_step_up)) -> dict:
    """Step-up gated: trip the kill-switch latch for this bot."""
    from datetime import UTC, datetime
    latch_path = STATE_DIR / "safety" / "kill_switch_latch.json"
    latch_path.parent.mkdir(parents=True, exist_ok=True)
    latches: dict = {}
    if latch_path.exists():
        try:
            latches = json.loads(latch_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            latches = {}
    latches[bot_id] = {
        "latch_state": "tripped",
        "reason": "operator_kill",
        "tripped_at": datetime.now(UTC).isoformat(),
        "tripped_by": session["user"],
    }
    latch_path.write_text(json.dumps(latches, indent=2), encoding="utf-8")
    _write_control_signal(bot_id, "kill", session["user"])
    return {"ok": True, "action": "kill", "bot_id": bot_id, "latch_state": "tripped"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest eta_engine/tests/test_dashboard_lifecycle.py -v`
Expected: 7 PASSED

- [ ] **Step 5: Commit**

```bash
git add eta_engine/deploy/scripts/dashboard_api.py eta_engine/tests/test_dashboard_lifecycle.py
git commit -m "feat(dashboard): bot lifecycle endpoints (pause/resume/flatten/kill) with step-up gating"
```

---

## Phase 3: SSE Stream

### Task 9: SSE tail-follow generator

**Files:**
- Create: `eta_engine/deploy/scripts/dashboard_sse.py`
- Create: `eta_engine/tests/test_dashboard_sse.py`

- [ ] **Step 1: Write the failing tests**

```python
# eta_engine/tests/test_dashboard_sse.py
"""Tests for SSE tail-follow generator."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_tail_yields_new_lines(tmp_path: Path) -> None:
    """Appending to the file emits SSE events."""
    from eta_engine.deploy.scripts.dashboard_sse import tail_follow

    audit = tmp_path / "audit.jsonl"
    audit.write_text("", encoding="utf-8")

    received: list[str] = []

    async def collect() -> None:
        async for event in tail_follow(audit, event_type="verdict",
                                       poll_interval=0.05, max_iterations=10):
            received.append(event)
            if len(received) >= 2:
                return

    async def feed() -> None:
        await asyncio.sleep(0.1)
        with audit.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"a": 1}) + "\n")
        await asyncio.sleep(0.1)
        with audit.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"a": 2}) + "\n")

    await asyncio.gather(collect(), feed())
    assert len(received) == 2
    assert "event: verdict" in received[0]
    assert "data: " in received[0]
    assert '"a": 1' in received[0] or '"a":1' in received[0]


@pytest.mark.asyncio
async def test_tail_handles_missing_file_gracefully(tmp_path: Path) -> None:
    """A missing file should yield no events and not crash."""
    from eta_engine.deploy.scripts.dashboard_sse import tail_follow

    received = []
    async for event in tail_follow(tmp_path / "missing.jsonl",
                                   event_type="verdict",
                                   poll_interval=0.05,
                                   max_iterations=3):
        received.append(event)
    assert received == []


@pytest.mark.asyncio
async def test_tail_skips_invalid_json(tmp_path: Path) -> None:
    """Garbage lines don't break the stream."""
    from eta_engine.deploy.scripts.dashboard_sse import tail_follow

    audit = tmp_path / "audit.jsonl"
    audit.write_text("", encoding="utf-8")

    received = []

    async def collect() -> None:
        async for event in tail_follow(audit, event_type="verdict",
                                       poll_interval=0.05, max_iterations=20):
            received.append(event)
            if len(received) >= 1:
                return

    async def feed() -> None:
        await asyncio.sleep(0.1)
        with audit.open("a", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"good": True}) + "\n")

    await asyncio.gather(collect(), feed())
    assert len(received) == 1
    assert "good" in received[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest eta_engine/tests/test_dashboard_sse.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement the SSE module**

```python
# eta_engine/deploy/scripts/dashboard_sse.py
"""SSE tail-follow generator (Wave-7, 2026-04-27).

Yields SSE-formatted events as new lines are appended to a JSONL file.
Handles missing files gracefully (yields nothing, retries) and skips
invalid JSON lines without crashing the stream.

Designed for the dashboard's /api/live/stream endpoint:
  * audit JSONL  -> 'verdict' events
  * fills JSONL  -> 'fill' events
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncIterator

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 0.5  # seconds


def _format_sse(event_type: str, data: dict) -> str:
    """Format a single SSE event."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


async def tail_follow(
    path: Path,
    *,
    event_type: str,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    max_iterations: int | None = None,
) -> AsyncIterator[str]:
    """Yield SSE-formatted events as new lines appear in ``path``.

    Re-resolves the path each iteration so midnight rotation
    (state/jarvis_audit/<today>.jsonl) is handled transparently.

    ``max_iterations`` lets tests bound the loop; None = run forever.
    """
    last_size = 0
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        if not path.exists():
            await asyncio.sleep(poll_interval)
            continue
        try:
            size = path.stat().st_size
        except OSError:
            await asyncio.sleep(poll_interval)
            continue
        if size > last_size:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    fh.seek(last_size)
                    new = fh.read()
                last_size = size
            except OSError as exc:
                logger.debug("tail read failed at %s: %s", path, exc)
                await asyncio.sleep(poll_interval)
                continue
            for line in new.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield _format_sse(event_type, row)
        elif size < last_size:
            # File rotated / truncated -- reset cursor
            last_size = 0
        await asyncio.sleep(poll_interval)


async def stream_audit_and_fills(
    audit_path: Path,
    fills_path: Path,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> AsyncIterator[str]:
    """Multiplex the two streams into one SSE stream."""
    audit_iter = tail_follow(audit_path, event_type="verdict", poll_interval=poll_interval)
    fills_iter = tail_follow(fills_path, event_type="fill", poll_interval=poll_interval)
    audit_task = asyncio.create_task(audit_iter.__anext__())
    fills_task = asyncio.create_task(fills_iter.__anext__())
    while True:
        done, _pending = await asyncio.wait(
            [audit_task, fills_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            try:
                yield task.result()
            except StopAsyncIteration:
                continue
            if task is audit_task:
                audit_task = asyncio.create_task(audit_iter.__anext__())
            elif task is fills_task:
                fills_task = asyncio.create_task(fills_iter.__anext__())
```

- [ ] **Step 4: Add the SSE endpoint to dashboard_api.py**

Insert into `eta_engine/deploy/scripts/dashboard_api.py`:

```python
from datetime import UTC, datetime as _dt
from fastapi.responses import StreamingResponse


@app.get("/api/live/stream")
async def live_stream(_: dict = Depends(require_session)) -> StreamingResponse:
    """SSE stream: 'verdict' events from today's audit JSONL,
    'fill' events from blotter fills JSONL.

    Re-resolves today's audit path on each iteration so midnight
    rotation is transparent.
    """
    from eta_engine.deploy.scripts.dashboard_sse import stream_audit_and_fills

    async def gen():
        # Re-resolve today's audit path inside the generator so a
        # rollover at midnight gets picked up.
        today = _dt.now(UTC).strftime("%Y-%m-%d")
        audit_path = STATE_DIR / "jarvis_audit" / f"{today}.jsonl"
        fills_path = STATE_DIR / "blotter" / "fills.jsonl"
        async for event in stream_audit_and_fills(audit_path, fills_path):
            yield event

    return StreamingResponse(gen(), media_type="text/event-stream")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest eta_engine/tests/test_dashboard_sse.py -v`
Expected: 3 PASSED

- [ ] **Step 6: Commit**

```bash
git add eta_engine/deploy/scripts/dashboard_sse.py eta_engine/tests/test_dashboard_sse.py eta_engine/deploy/scripts/dashboard_api.py
git commit -m "feat(dashboard): SSE /api/live/stream multiplexes verdict + fill streams from JSONL files"
```

---

## Phase 4: Frontend Foundation

### Task 10: HTML shell + Tailwind theme

**Files:**
- Replace: `eta_engine/deploy/status_page/index.html`
- Create: `eta_engine/deploy/status_page/theme.css`

- [ ] **Step 1: Replace index.html with the shell**

Overwrite `eta_engine/deploy/status_page/index.html` with:

```html
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Evolutionary Trading Algo | Command Center</title>
  <meta name="theme-color" content="#020406" />
  <link rel="stylesheet" href="/theme.css" />
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
</head>
<body class="bg-zinc-950 text-zinc-100 min-h-screen flex flex-col">

  <!-- Login modal (hidden by default; shown by auth.js when unauthenticated) -->
  <div id="login-modal" class="fixed inset-0 z-50 bg-black/80 backdrop-blur hidden items-center justify-center">
    <form id="login-form" class="bg-zinc-900 border border-zinc-700 rounded p-6 w-96 space-y-3">
      <h2 class="text-lg font-semibold">JARVIS Command Center</h2>
      <input id="login-username" type="text" placeholder="username"
             class="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2" autocomplete="username" />
      <input id="login-password" type="password" placeholder="password"
             class="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2" autocomplete="current-password" />
      <div id="login-error" class="text-red-400 text-sm hidden"></div>
      <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 rounded py-2 font-medium">
        Sign in
      </button>
    </form>
  </div>

  <!-- Step-up modal -->
  <div id="step-up-modal" class="fixed inset-0 z-50 bg-black/80 backdrop-blur hidden items-center justify-center">
    <form id="step-up-form" class="bg-zinc-900 border border-amber-700 rounded p-6 w-96 space-y-3">
      <h2 class="text-lg font-semibold text-amber-400">Step-Up Required</h2>
      <p class="text-sm text-zinc-400" id="step-up-reason">Sensitive action requires PIN.</p>
      <input id="step-up-pin" type="password" inputmode="numeric" placeholder="PIN"
             class="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2" />
      <div id="step-up-error" class="text-red-400 text-sm hidden"></div>
      <div class="flex gap-2">
        <button type="button" id="step-up-cancel" class="flex-1 bg-zinc-700 rounded py-2">Cancel</button>
        <button type="submit" class="flex-1 bg-amber-600 hover:bg-amber-500 rounded py-2 font-medium">Authorize</button>
      </div>
    </form>
  </div>

  <!-- Top bar -->
  <header id="top-bar" class="border-b border-zinc-800 bg-zinc-900 px-4 py-2 flex items-center gap-4 text-sm">
    <div class="font-bold text-emerald-400">ETA</div>
    <div id="top-kill-switch" class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-zinc-500"></span><span>kill</span></div>
    <div id="top-v22-toggle" class="flex items-center gap-1"></div>
    <div id="top-stress" class="flex items-center gap-1"></div>
    <div id="top-fleet-equity" class="flex items-center gap-1"></div>
    <div id="top-alerts" class="flex items-center gap-1"></div>
    <div class="flex-1"></div>
    <div id="top-sse-status" class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-zinc-500"></span><span>sse</span></div>
    <div id="top-user-chip" class="flex items-center gap-2"></div>
    <button id="top-logout" class="text-zinc-400 hover:text-red-400 text-xs">logout</button>
  </header>

  <!-- Toast container (top-right) -->
  <div id="toast-container" class="fixed top-12 right-4 z-40 space-y-2 w-80 pointer-events-none"></div>

  <!-- Tab nav -->
  <nav class="border-b border-zinc-800 bg-zinc-900 px-4 flex gap-1 text-sm">
    <button data-tab="jarvis" class="tab-btn px-4 py-2 border-b-2 border-emerald-500 text-emerald-400">
      JARVIS Command Center
    </button>
    <button data-tab="fleet" class="tab-btn px-4 py-2 border-b-2 border-transparent text-zinc-400 hover:text-zinc-100">
      Bot Fleet
    </button>
  </nav>

  <!-- Main content -->
  <main class="flex-1 overflow-y-auto p-4 pb-24">

    <!-- JARVIS view (visible by default) -->
    <section id="view-jarvis" class="grid grid-cols-3 gap-4">
      <div data-panel-id="cc-verdict-stream" class="panel col-span-2 row-span-2"></div>
      <div data-panel-id="cc-stress-mood" class="panel"></div>
      <div data-panel-id="cc-v22-toggle" class="panel"></div>
      <div data-panel-id="cc-sage-explain" class="panel col-span-2"></div>
      <div data-panel-id="cc-sage-health" class="panel"></div>
      <div data-panel-id="cc-disagreement-heatmap" class="panel col-span-2"></div>
      <div data-panel-id="cc-edge-leaderboard" class="panel"></div>
      <div data-panel-id="cc-policy-diff" class="panel col-span-2"></div>
      <div data-panel-id="cc-model-tier" class="panel"></div>
      <div data-panel-id="cc-kaizen-latest" class="panel col-span-3"></div>
    </section>

    <!-- Fleet view (hidden until tab click) -->
    <section id="view-fleet" class="hidden space-y-4">
      <div data-panel-id="fl-roster" class="panel"></div>
      <div class="grid grid-cols-2 gap-4">
        <div data-panel-id="fl-drilldown" class="panel"></div>
        <div data-panel-id="fl-equity-curve" class="panel"></div>
      </div>
      <div class="grid grid-cols-3 gap-4">
        <div data-panel-id="fl-drawdown" class="panel"></div>
        <div data-panel-id="fl-sage-effect" class="panel"></div>
        <div data-panel-id="fl-correlation" class="panel"></div>
      </div>
      <div class="grid grid-cols-2 gap-4">
        <div data-panel-id="fl-edge-per-bot" class="panel"></div>
        <div data-panel-id="fl-position-reconciler" class="panel"></div>
      </div>
      <div data-panel-id="fl-risk-ladder" class="panel"></div>
      <div data-panel-id="fl-controls" class="panel"></div>
      <div data-panel-id="fl-health-badges" class="panel"></div>
    </section>
  </main>

  <!-- Live fill tape (fixed bottom) -->
  <footer id="fl-fill-tape" class="fixed bottom-0 left-0 right-0 border-t border-zinc-800 bg-zinc-900 px-4 py-2 h-20 overflow-hidden">
    <div class="text-xs text-zinc-500 mb-1">live fills</div>
    <div id="fl-fill-tape-rows" class="flex gap-2 overflow-x-auto text-xs font-mono whitespace-nowrap"></div>
  </footer>

  <!-- JS modules (load order matters) -->
  <script type="module" src="/js/panels.js"></script>
  <script type="module" src="/js/auth.js"></script>
  <script type="module" src="/js/live.js"></script>
  <script type="module" src="/js/command_center.js"></script>
  <script type="module" src="/js/bot_fleet.js"></script>
  <script type="module">
    import { initTabs } from '/js/panels.js';
    initTabs();
  </script>
</body>
</html>
```

- [ ] **Step 2: Create theme.css**

```css
/* eta_engine/deploy/status_page/theme.css
   Wave-7 dashboard theme. Tailwind handles the bulk; this file adds
   panel base styles + dark-mode tokens + small utilities. */

:root {
  --panel-bg: #18181b;        /* zinc-900 */
  --panel-border: #3f3f46;    /* zinc-700 */
  --panel-text: #f4f4f5;      /* zinc-100 */
  --accent-good: #10b981;     /* emerald-500 */
  --accent-warn: #f59e0b;     /* amber-500 */
  --accent-bad:  #ef4444;     /* red-500 */
}

.panel {
  background: var(--panel-bg);
  border: 1px solid var(--panel-border);
  border-radius: 6px;
  padding: 12px;
  min-height: 100px;
  position: relative;
  overflow: hidden;
}

.panel.loading::after {
  content: "";
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: linear-gradient(to right,
              transparent, var(--accent-good), transparent);
  animation: panel-loading 1.4s linear infinite;
}

@keyframes panel-loading {
  0%   { transform: translateX(-100%); }
  100% { transform: translateX(100%); }
}

.panel.error {
  border-color: var(--accent-bad);
}

.panel.error::before {
  content: "✕ error";
  position: absolute;
  top: 4px; right: 8px;
  font-size: 10px;
  color: var(--accent-bad);
}

.panel.stale::after {
  content: "⚠ stale";
  position: absolute;
  top: 4px; right: 8px;
  font-size: 10px;
  color: var(--accent-warn);
}

.panel-title {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #71717a;       /* zinc-500 */
  margin-bottom: 8px;
}

.panel-refresh {
  position: absolute;
  bottom: 4px; right: 8px;
  font-size: 10px;
  color: #52525b;       /* zinc-600 */
}

.tab-btn[aria-selected="true"] {
  border-color: var(--accent-good);
  color: var(--accent-good);
}

/* SSE status dot colors */
.sse-connected { background-color: var(--accent-good); }
.sse-reconnecting { background-color: var(--accent-warn); }
.sse-down { background-color: var(--accent-bad); }

/* Toast styles */
.toast {
  background: var(--panel-bg);
  border-left: 3px solid var(--accent-warn);
  padding: 8px 12px;
  border-radius: 4px;
  pointer-events: auto;
  animation: toast-in 0.3s ease;
}
.toast.error { border-left-color: var(--accent-bad); }
.toast.success { border-left-color: var(--accent-good); }

@keyframes toast-in {
  from { opacity: 0; transform: translateX(20px); }
  to   { opacity: 1; transform: translateX(0); }
}
```

- [ ] **Step 3: Verify the shell loads**

Run: `python -c "from eta_engine.deploy.scripts.dashboard_api import app, _STATUS_PAGE; assert _STATUS_PAGE.exists(); assert (_STATUS_PAGE.parent / 'theme.css').exists(); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add eta_engine/deploy/status_page/index.html eta_engine/deploy/status_page/theme.css
git commit -m "feat(dashboard): vanilla HTML shell + dark theme.css with Tailwind via CDN"
```

---

### Task 11: panels.js — Panel base class + formatters

**Files:**
- Create: `eta_engine/deploy/status_page/js/panels.js`

- [ ] **Step 1: Write the module**

```javascript
// eta_engine/deploy/status_page/js/panels.js
// Panel base class + formatters + tab manager.
// Wave-7 dashboard, 2026-04-27.

const STALE_AFTER_MS = 30_000;

export class Panel {
  /**
   * @param {string} containerId - the data-panel-id value (without #)
   * @param {string} endpoint    - HTTP endpoint to fetch
   * @param {string} title       - human-readable panel title
   */
  constructor(containerId, endpoint, title) {
    this.containerId = containerId;
    this.endpoint = endpoint;
    this.title = title;
    this.lastRefreshAt = null;
    this.lastError = null;
    this.element = document.querySelector(`[data-panel-id="${containerId}"]`);
    if (this.element) {
      this.element.innerHTML = `<div class="panel-title">${title}</div><div data-panel-body></div><div class="panel-refresh"></div>`;
      this.body = this.element.querySelector('[data-panel-body]');
      this.refreshLabel = this.element.querySelector('.panel-refresh');
    }
  }

  setLoading() {
    if (!this.element) return;
    this.element.classList.add('loading');
    this.element.classList.remove('error', 'stale');
  }

  setError(message) {
    if (!this.element) return;
    this.element.classList.add('error');
    this.element.classList.remove('loading');
    this.body.innerHTML = `<div class="text-red-400 text-xs">${escapeHtml(message)}</div>`;
    this.lastError = message;
  }

  markStale() {
    if (!this.element) return;
    this.element.classList.add('stale');
  }

  /** Subclasses override this. */
  render(_data) {
    if (!this.body) return;
    this.body.textContent = JSON.stringify(_data, null, 2);
  }

  /** Called by Poller. Fetches + renders + handles errors. */
  async refresh() {
    if (!this.element) return;
    this.setLoading();
    try {
      const resp = await fetch(this.endpoint, {
        credentials: 'same-origin',
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        const code = body?.detail?.error_code || body?.error_code || `http_${resp.status}`;
        this.setError(`${code}`);
        return;
      }
      const data = await resp.json();
      try {
        this.render(data);
        this.element.classList.remove('loading', 'error', 'stale');
        this.lastRefreshAt = Date.now();
        this.updateRefreshLabel();
      } catch (e) {
        console.error(`render failed for ${this.containerId}`, e);
        this.setError(`render: ${e.message}`);
      }
    } catch (e) {
      console.error(`fetch failed for ${this.containerId}`, e);
      this.setError(`network: ${e.message}`);
    }
  }

  updateRefreshLabel() {
    if (!this.refreshLabel || !this.lastRefreshAt) return;
    const ageS = Math.floor((Date.now() - this.lastRefreshAt) / 1000);
    if (ageS > STALE_AFTER_MS / 1000) {
      this.markStale();
      this.refreshLabel.textContent = `stale ${ageS}s`;
    } else {
      this.refreshLabel.textContent = `updated ${ageS}s ago`;
    }
  }
}

// --- formatters ---

export function formatNumber(n, digits = 2) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  return Number(n).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatPct(p, digits = 2) {
  if (p === null || p === undefined || isNaN(p)) return '—';
  return `${(Number(p) * 100).toFixed(digits)}%`;
}

export function formatR(r) {
  if (r === null || r === undefined || isNaN(r)) return '—';
  const sign = r >= 0 ? '+' : '';
  return `${sign}${Number(r).toFixed(2)}R`;
}

export function formatTime(isoOrEpoch) {
  if (!isoOrEpoch) return '—';
  const d = typeof isoOrEpoch === 'number' ? new Date(isoOrEpoch * 1000) : new Date(isoOrEpoch);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleTimeString();
}

export function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

// --- tab manager ---

export function initTabs() {
  const tabBtns = document.querySelectorAll('.tab-btn');
  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.tab;
      tabBtns.forEach(b => {
        b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
        b.classList.toggle('border-emerald-500', b === btn);
        b.classList.toggle('text-emerald-400', b === btn);
        b.classList.toggle('border-transparent', b !== btn);
        b.classList.toggle('text-zinc-400', b !== btn);
      });
      document.querySelectorAll('section[id^="view-"]').forEach(sec => {
        sec.classList.toggle('hidden', sec.id !== `view-${target}`);
      });
    });
  });
}

// --- selection state ---

export const selection = {
  botId: 'mnq',     // default selected bot
  symbol: 'MNQ',
};

export function selectBot(botId, symbol) {
  selection.botId = botId;
  selection.symbol = symbol;
  window.dispatchEvent(new CustomEvent('selection-changed', {
    detail: { botId, symbol },
  }));
}
```

- [ ] **Step 2: Smoke-test it in a browser**

(Manual): start `uvicorn eta_engine.deploy.scripts.dashboard_api:app --reload --port 8421`, open `http://127.0.0.1:8421/`, open DevTools console.

Expected: page loads, no console errors, login modal shows.

- [ ] **Step 3: Commit**

```bash
git add eta_engine/deploy/status_page/js/panels.js
git commit -m "feat(dashboard): Panel base class + formatters + tab manager + selection state"
```

---

### Task 12: auth.js — session check + login + step-up + global fetch wrapper

**Files:**
- Create: `eta_engine/deploy/status_page/js/auth.js`

- [ ] **Step 1: Write the module**

```javascript
// eta_engine/deploy/status_page/js/auth.js
// Auth flow: session check on load, login modal, step-up modal,
// global 401-handler that re-prompts login.
// Wave-7 dashboard, 2026-04-27.

import { escapeHtml } from '/js/panels.js';

export const session = {
  authenticated: false,
  user: null,
  steppedUp: false,
};

let _afterLoginCallbacks = [];

export function onAuthenticated(cb) {
  if (session.authenticated) cb();
  else _afterLoginCallbacks.push(cb);
}

export async function checkSession() {
  try {
    const r = await fetch('/api/auth/session', { credentials: 'same-origin' });
    if (!r.ok) return false;
    const body = await r.json();
    session.authenticated = !!body.authenticated;
    session.user = body.user || null;
    session.steppedUp = !!body.stepped_up;
    return session.authenticated;
  } catch (e) {
    console.error('session check failed', e);
    return false;
  }
}

export function showLoginModal() {
  const modal = document.getElementById('login-modal');
  modal.classList.remove('hidden');
  modal.classList.add('flex');
  document.getElementById('login-username').focus();
}

export function hideLoginModal() {
  const modal = document.getElementById('login-modal');
  modal.classList.add('hidden');
  modal.classList.remove('flex');
}

async function doLogin(username, password) {
  const errEl = document.getElementById('login-error');
  errEl.classList.add('hidden');
  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ username, password }),
    });
    if (!r.ok) {
      errEl.textContent = `login failed (${r.status})`;
      errEl.classList.remove('hidden');
      return false;
    }
    const body = await r.json();
    session.authenticated = true;
    session.user = body.user;
    hideLoginModal();
    _afterLoginCallbacks.forEach(cb => { try { cb(); } catch(e) { console.error(e); }});
    _afterLoginCallbacks = [];
    return true;
  } catch (e) {
    errEl.textContent = `network: ${e.message}`;
    errEl.classList.remove('hidden');
    return false;
  }
}

export async function logout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
  } catch (e) { /* ignore */ }
  session.authenticated = false;
  session.user = null;
  session.steppedUp = false;
  showLoginModal();
}

export function showStepUpModal(reason = 'Sensitive action requires PIN.') {
  const modal = document.getElementById('step-up-modal');
  document.getElementById('step-up-reason').textContent = reason;
  modal.classList.remove('hidden');
  modal.classList.add('flex');
  document.getElementById('step-up-pin').focus();
  return new Promise((resolve) => {
    _stepUpResolver = resolve;
  });
}

let _stepUpResolver = null;

function hideStepUpModal() {
  const modal = document.getElementById('step-up-modal');
  modal.classList.add('hidden');
  modal.classList.remove('flex');
  document.getElementById('step-up-pin').value = '';
}

async function doStepUp(pin) {
  const errEl = document.getElementById('step-up-error');
  errEl.classList.add('hidden');
  try {
    const r = await fetch('/api/auth/step-up', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ pin }),
    });
    if (!r.ok) {
      errEl.textContent = `bad PIN (${r.status})`;
      errEl.classList.remove('hidden');
      return false;
    }
    session.steppedUp = true;
    hideStepUpModal();
    if (_stepUpResolver) { _stepUpResolver(true); _stepUpResolver = null; }
    return true;
  } catch (e) {
    errEl.textContent = `network: ${e.message}`;
    errEl.classList.remove('hidden');
    return false;
  }
}

/** Authenticated POST helper that handles 403 step_up_required via a modal. */
export async function authedPost(url, body, opts = {}) {
  const reason = opts.stepUpReason || 'Sensitive action requires PIN.';
  for (let attempt = 0; attempt < 2; attempt++) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body || {}),
    });
    if (r.status === 401) {
      showLoginModal();
      throw new Error('not authenticated');
    }
    if (r.status === 403) {
      const detail = await r.json().catch(() => ({}));
      if (detail?.detail?.error_code === 'step_up_required' && attempt === 0) {
        const ok = await showStepUpModal(reason);
        if (!ok) throw new Error('step-up cancelled');
        continue;
      }
    }
    return r;
  }
}

// --- wire up the modals ---

document.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    await doLogin(
      document.getElementById('login-username').value,
      document.getElementById('login-password').value,
    );
  });

  document.getElementById('step-up-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    await doStepUp(document.getElementById('step-up-pin').value);
  });

  document.getElementById('step-up-cancel').addEventListener('click', () => {
    hideStepUpModal();
    if (_stepUpResolver) { _stepUpResolver(false); _stepUpResolver = null; }
  });

  document.getElementById('top-logout').addEventListener('click', logout);

  // Initial session check
  const ok = await checkSession();
  if (!ok) {
    showLoginModal();
  } else {
    document.getElementById('top-user-chip').textContent = session.user;
    _afterLoginCallbacks.forEach(cb => { try { cb(); } catch(e) { console.error(e); }});
    _afterLoginCallbacks = [];
  }
});
```

- [ ] **Step 2: Smoke-test login flow**

(Manual): start the server, open `http://127.0.0.1:8421/`, login modal should appear. Use `python -c "from eta_engine.deploy.scripts.dashboard_auth import create_user; from pathlib import Path; create_user(Path('/tmp/users.json'), 'edward', 'test')"` to seed an account, then login should succeed.

- [ ] **Step 3: Commit**

```bash
git add eta_engine/deploy/status_page/js/auth.js
git commit -m "feat(dashboard): auth.js -- session check, login modal, step-up modal, authedPost helper"
```

---

### Task 13: live.js — LiveStream (SSE) + Poller (5s scheduler)

**Files:**
- Create: `eta_engine/deploy/status_page/js/live.js`

- [ ] **Step 1: Write the module**

```javascript
// eta_engine/deploy/status_page/js/live.js
// LiveStream (EventSource wrapper with backoff) + Poller (5s scheduler).
// Wave-7 dashboard, 2026-04-27.

import { onAuthenticated } from '/js/auth.js';

export class LiveStream {
  constructor() {
    this.es = null;
    this.handlers = { verdict: [], fill: [] };
    this.reconnectDelayMs = 1000;
    this.maxReconnectDelayMs = 30_000;
    this.statusEl = document.getElementById('top-sse-status');
  }

  on(event, handler) {
    if (!this.handlers[event]) this.handlers[event] = [];
    this.handlers[event].push(handler);
  }

  connect() {
    this._setStatus('reconnecting');
    try {
      this.es = new EventSource('/api/live/stream');
    } catch (e) {
      console.error('EventSource construct failed', e);
      this._scheduleReconnect();
      return;
    }
    this.es.onopen = () => {
      this.reconnectDelayMs = 1000;  // reset backoff on success
      this._setStatus('connected');
    };
    this.es.onerror = () => {
      this._setStatus('reconnecting');
      this.es.close();
      this._scheduleReconnect();
    };
    ['verdict', 'fill'].forEach(eventType => {
      this.es.addEventListener(eventType, (msg) => {
        let data;
        try { data = JSON.parse(msg.data); }
        catch (e) { console.warn(`bad SSE ${eventType} JSON`, e); return; }
        (this.handlers[eventType] || []).forEach(h => {
          try { h(data); } catch (e) { console.error(`SSE ${eventType} handler`, e); }
        });
      });
    });
  }

  _scheduleReconnect() {
    setTimeout(() => this.connect(), this.reconnectDelayMs);
    this.reconnectDelayMs = Math.min(
      this.reconnectDelayMs * 2,
      this.maxReconnectDelayMs,
    );
    if (this.reconnectDelayMs >= 30_000) this._setStatus('down');
  }

  _setStatus(s) {
    if (!this.statusEl) return;
    const dot = this.statusEl.querySelector('span');
    if (!dot) return;
    dot.classList.remove('sse-connected', 'sse-reconnecting', 'sse-down', 'bg-zinc-500');
    if (s === 'connected') dot.classList.add('sse-connected');
    else if (s === 'reconnecting') dot.classList.add('sse-reconnecting');
    else dot.classList.add('sse-down');
  }
}

export class Poller {
  constructor(intervalMs = 5000) {
    this.intervalMs = intervalMs;
    this.panels = [];
    this.timer = null;
    this.active = true;
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        this.active = false;
      } else {
        this.active = true;
        this._tick();   // immediate force-refresh on return
      }
    });
  }

  register(panel) {
    this.panels.push(panel);
  }

  start() {
    this._tick();
    this.timer = setInterval(() => this._tick(), this.intervalMs);
  }

  async _tick() {
    if (!this.active) return;
    for (const panel of this.panels) {
      panel.refresh().catch(e => console.error(`poller refresh ${panel.containerId}`, e));
    }
    // Also re-render the "updated Xs ago" label on every panel
    this.panels.forEach(p => p.updateRefreshLabel?.());
  }
}

// Singleton instances
export const liveStream = new LiveStream();
export const poller = new Poller(5000);

// Wire on authenticated
onAuthenticated(() => {
  liveStream.connect();
  poller.start();
});
```

- [ ] **Step 2: Smoke-test SSE connection**

(Manual): with the server running and a logged-in session, open DevTools → Network → filter by EventStream, see `/api/live/stream` connection.

- [ ] **Step 3: Commit**

```bash
git add eta_engine/deploy/status_page/js/live.js
git commit -m "feat(dashboard): live.js -- LiveStream (SSE, backoff) + Poller (5s, visibility suspend)"
```

---

## Phase 5: Frontend Panels

### Task 14: command_center.js — 10 JARVIS panels

**Files:**
- Create: `eta_engine/deploy/status_page/js/command_center.js`

- [ ] **Step 1: Write the module with all 10 panels**

Each panel is a `Panel` subclass that overrides `render(data)` for its specific shape. Where SSE-driven, they also subscribe to `liveStream`.

```javascript
// eta_engine/deploy/status_page/js/command_center.js
// 10 JARVIS panels for the Command Center view.
// Wave-7 dashboard, 2026-04-27.

import { Panel, formatPct, formatR, formatTime, escapeHtml, selection } from '/js/panels.js';
import { liveStream, poller } from '/js/live.js';
import { onAuthenticated, authedPost } from '/js/auth.js';

// --- 1. Live verdict stream (SSE) ---
class VerdictStreamPanel extends Panel {
  constructor() {
    super('cc-verdict-stream', null, 'Live Verdict Stream');
    this.rows = [];
    if (this.body) this.body.innerHTML = '<div data-list class="space-y-1 text-xs font-mono max-h-96 overflow-y-auto"></div>';
    this.list = this.body?.querySelector('[data-list]');
    liveStream.on('verdict', (v) => this.add(v));
  }
  add(v) {
    this.rows.unshift(v);
    if (this.rows.length > 50) this.rows.length = 50;
    this.repaint();
  }
  repaint() {
    if (!this.list) return;
    this.list.innerHTML = this.rows.map(v => {
      const verdict = v?.response?.verdict || '?';
      const cls = verdict === 'APPROVED' ? 'text-emerald-400'
                : verdict === 'CONDITIONAL' ? 'text-amber-400'
                : verdict === 'DENIED' ? 'text-red-400' : 'text-zinc-400';
      const sym = v?.request?.payload?.symbol || '?';
      const action = v?.request?.action || '?';
      const sage = (v?.response?.conditions || []).filter(c => c.startsWith('v22_')).join(',');
      return `<div><span class="text-zinc-500">${escapeHtml(formatTime(v.ts))}</span> <span class="${cls}">${escapeHtml(verdict)}</span> ${escapeHtml(sym)} ${escapeHtml(action)} ${sage ? `<span class="text-purple-400">[${escapeHtml(sage)}]</span>` : ''}</div>`;
    }).join('');
  }
  refresh() { /* SSE-driven; no poll */ }
}

// --- 2. Sage explain ---
class SageExplainPanel extends Panel {
  constructor() {
    super('cc-sage-explain', `/api/jarvis/sage_explain?symbol=${selection.symbol}&side=long`, 'Sage Explain');
    window.addEventListener('selection-changed', (e) => {
      this.endpoint = `/api/jarvis/sage_explain?symbol=${e.detail.symbol}&side=long`;
      this.refresh();
    });
  }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    if (data.error_code) { this.setError(data.error_code); return; }
    this.body.innerHTML = `
      <div class="text-sm leading-relaxed text-zinc-200">${escapeHtml(data.narrative || '—')}</div>
      <div class="text-xs text-zinc-500 mt-2 font-mono">${escapeHtml(data.summary_line || '')}</div>`;
  }
}

// --- 3. Sage health alerts ---
class SageHealthPanel extends Panel {
  constructor() { super('cc-sage-health', '/api/jarvis/health', 'Sage Health'); }
  render(data) {
    const issues = data.issues || [];
    if (issues.length === 0) {
      this.body.innerHTML = '<div class="text-emerald-400 text-sm">✓ all schools healthy</div>';
      return;
    }
    this.body.innerHTML = '<ul class="space-y-1 text-xs">' + issues.map(i => {
      const cls = i.severity === 'critical' ? 'text-red-400' : 'text-amber-400';
      return `<li><span class="${cls}">●</span> ${escapeHtml(i.school)} ${formatPct(i.neutral_rate)} neutral (${i.n_consultations})</li>`;
    }).join('') + '</ul>';
  }
}

// --- 4. Disagreement heatmap ---
class DisagreementHeatmapPanel extends Panel {
  constructor() {
    super('cc-disagreement-heatmap', `/api/jarvis/sage_disagreement_heatmap?symbol=${selection.symbol}`, '23-School Disagreement');
    window.addEventListener('selection-changed', (e) => {
      this.endpoint = `/api/jarvis/sage_disagreement_heatmap?symbol=${e.detail.symbol}`;
      this.refresh();
    });
  }
  render(data) {
    const matrix = data.matrix || [];
    if (matrix.length === 0) {
      this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning || 'no data')}</div>`;
      return;
    }
    const html = matrix.map(row => {
      return `<div class="flex gap-px">${row.cells.map(c => {
        const intensity = Math.abs(c.score);
        const color = c.score > 0 ? `rgba(16,185,129,${intensity})` : `rgba(239,68,68,${intensity})`;
        return `<div class="w-3 h-3" style="background:${color}" title="${escapeHtml(c.school_a)} vs ${escapeHtml(c.school_b)}: ${c.score.toFixed(2)}"></div>`;
      }).join('')}</div>`;
    }).join('');
    this.body.innerHTML = `<div class="space-y-px">${html}</div>`;
  }
}

// --- 5. Stress / mood ---
class StressMoodPanel extends Panel {
  constructor() { super('cc-stress-mood', '/api/jarvis/summary', 'Stress + Session'); }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    const stress = data.stress_composite ?? 0;
    const phase = data.session_phase || '—';
    const kill = data.kill_switch_state || 'unknown';
    const killCls = kill === 'tripped' ? 'text-red-400' : kill === 'armed' ? 'text-amber-400' : 'text-emerald-400';
    this.body.innerHTML = `
      <div class="flex items-center justify-between mb-2">
        <span class="text-xs text-zinc-500">stress</span>
        <span class="text-2xl font-mono">${formatPct(stress)}</span>
      </div>
      <div class="text-xs text-zinc-500">session: <span class="text-zinc-100">${escapeHtml(phase)}</span></div>
      <div class="text-xs text-zinc-500">kill-switch: <span class="${killCls}">${escapeHtml(kill)}</span></div>`;
  }
}

// --- 6. Policy diff ---
class PolicyDiffPanel extends Panel {
  constructor() { super('cc-policy-diff', '/api/jarvis/policy_diff', 'Bandit Policy Diff'); }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    const arms = data.arms || {};
    this.body.innerHTML = '<table class="text-xs w-full">' +
      '<tr><th class="text-left">arm</th><th>verdict</th><th>cap</th></tr>' +
      Object.entries(arms).map(([arm, v]) =>
        `<tr><td>${escapeHtml(arm)}</td><td>${escapeHtml(v.verdict)}</td><td>${formatPct(v.size_cap_mult ?? 1)}</td></tr>`
      ).join('') + '</table>';
  }
}

// --- 7. V22 toggle (operator-action panel) ---
class V22TogglePanel extends Panel {
  constructor() { super('cc-v22-toggle', '/api/jarvis/sage_modulation_toggle', 'V22 Sage Modulation'); }
  render(data) {
    const enabled = !!data.enabled;
    const cls = enabled ? 'bg-emerald-600' : 'bg-zinc-700';
    this.body.innerHTML = `
      <div class="flex items-center justify-between">
        <span class="text-sm">${enabled ? 'ON' : 'OFF'}</span>
        <button id="v22-toggle-btn" class="${cls} hover:opacity-80 px-3 py-1 rounded text-sm">flip</button>
      </div>
      <div class="text-xs text-zinc-500 mt-2">flag: ${escapeHtml(data.flag_name || 'ETA_FF_V22_SAGE_MODULATION')}</div>`;
    document.getElementById('v22-toggle-btn').addEventListener('click', async () => {
      try {
        const r = await authedPost('/api/jarvis/sage_modulation_toggle',
          { enabled: !enabled },
          { stepUpReason: 'Flipping V22 sage modulation. PIN required.' });
        if (r && r.ok) this.refresh();
      } catch (e) { console.error('v22 toggle failed', e); }
    });
    // Also reflect on top bar
    const topEl = document.getElementById('top-v22-toggle');
    if (topEl) topEl.innerHTML = `<span class="${enabled ? 'text-emerald-400' : 'text-zinc-500'}">v22 ${enabled ? 'ON' : 'off'}</span>`;
  }
}

// --- 8. Edge tracker leaderboard ---
class EdgeLeaderboardPanel extends Panel {
  constructor() { super('cc-edge-leaderboard', '/api/jarvis/edge_leaderboard', 'Edge Leaderboard'); }
  render(data) {
    const top = data.top || [];
    const bot = data.bottom || [];
    const row = s => `<tr><td>${escapeHtml(s.school)}</td><td class="text-right">${formatR(s.avg_r)}</td><td class="text-right text-zinc-500">${s.n_aligned}</td></tr>`;
    this.body.innerHTML = `
      <div class="grid grid-cols-2 gap-3 text-xs">
        <div><div class="text-emerald-400 mb-1">top</div><table class="w-full">${top.map(row).join('') || '<tr><td>—</td></tr>'}</table></div>
        <div><div class="text-red-400 mb-1">bottom</div><table class="w-full">${bot.map(row).join('') || '<tr><td>—</td></tr>'}</table></div>
      </div>`;
  }
}

// --- 9. Model tier ---
class ModelTierPanel extends Panel {
  constructor() { super('cc-model-tier', '/api/jarvis/model_tier', 'Model Tier'); }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    this.body.innerHTML = `
      <div class="text-2xl font-mono text-emerald-400">${escapeHtml(data.tier || '—')}</div>
      <div class="text-xs text-zinc-500 mt-2">subsystem: ${escapeHtml(data.subsystem || '—')}</div>
      <div class="text-xs text-zinc-500">category: ${escapeHtml(data.task_category || '—')}</div>
      <div class="text-xs text-zinc-500">at: ${formatTime(data.ts)}</div>`;
  }
}

// --- 10. Latest kaizen ticket ---
class KaizenLatestPanel extends Panel {
  constructor() { super('cc-kaizen-latest', '/api/jarvis/kaizen_latest', 'Latest Kaizen Ticket'); }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    this.body.innerHTML = `
      <div class="font-semibold mb-1">${escapeHtml(data.title || '—')}</div>
      <pre class="text-xs whitespace-pre-wrap text-zinc-400 max-h-48 overflow-y-auto">${escapeHtml(data.markdown || '')}</pre>`;
  }
}

// --- Initialize all 10 ---
onAuthenticated(() => {
  const panels = [
    new VerdictStreamPanel(),
    new SageExplainPanel(),
    new SageHealthPanel(),
    new DisagreementHeatmapPanel(),
    new StressMoodPanel(),
    new PolicyDiffPanel(),
    new V22TogglePanel(),
    new EdgeLeaderboardPanel(),
    new ModelTierPanel(),
    new KaizenLatestPanel(),
  ];
  panels.forEach(p => { if (p.endpoint) poller.register(p); });
});
```

- [ ] **Step 2: Smoke-test all 10 panels render**

(Manual): with server running, login, switch to JARVIS tab. All 10 panels should appear with either real data or `no_data` placeholder. No console errors.

- [ ] **Step 3: Commit**

```bash
git add eta_engine/deploy/status_page/js/command_center.js
git commit -m "feat(dashboard): command_center.js -- 10 JARVIS panels (verdict stream, sage explain, health, heatmap, mood, policy diff, V22 toggle, edge leaderboard, model tier, kaizen)"
```

---

### Task 15: bot_fleet.js — 12 fleet panels + lifecycle controls

**Files:**
- Create: `eta_engine/deploy/status_page/js/bot_fleet.js`

- [ ] **Step 1: Write the module with all 12 panels**

```javascript
// eta_engine/deploy/status_page/js/bot_fleet.js
// 12 fleet panels + lifecycle button handlers.
// Wave-7 dashboard, 2026-04-27.

import { Panel, formatPct, formatR, formatTime, formatNumber, escapeHtml,
         selection, selectBot } from '/js/panels.js';
import { liveStream, poller } from '/js/live.js';
import { onAuthenticated, authedPost } from '/js/auth.js';

// --- 1. Roster table ---
class RosterPanel extends Panel {
  constructor() { super('fl-roster', '/api/bot-fleet', 'Bot Fleet Roster'); }
  render(data) {
    const bots = data.bots || [];
    if (bots.length === 0) {
      this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning || 'no bots reporting')}</div>`;
      return;
    }
    this.body.innerHTML = `<table class="w-full text-xs"><thead class="text-zinc-500">
      <tr><th class="text-left">bot</th><th class="text-left">symbol</th><th class="text-left">tier</th><th class="text-left">venue</th><th class="text-left">status</th><th class="text-right">PnL</th><th class="text-right">open</th><th class="text-left">last sig</th></tr>
      </thead><tbody>${bots.map(b => {
        const statusCls = b.status === 'running' ? 'text-emerald-400'
                        : b.status === 'paused' ? 'text-amber-400'
                        : b.status === 'killed' ? 'text-red-400' : 'text-zinc-400';
        const pnlCls = (b.todays_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400';
        const isSel = selection.botId === b.name ? 'bg-zinc-800' : '';
        return `<tr class="cursor-pointer hover:bg-zinc-800 ${isSel}" data-bot-id="${escapeHtml(b.name)}" data-symbol="${escapeHtml(b.symbol)}">
          <td>${escapeHtml(b.name)}</td>
          <td>${escapeHtml(b.symbol)}</td>
          <td>${escapeHtml(b.tier)}</td>
          <td>${escapeHtml(b.venue)}</td>
          <td class="${statusCls}">${escapeHtml(b.status)}</td>
          <td class="text-right ${pnlCls}">${formatNumber(b.todays_pnl)}</td>
          <td class="text-right">${b.open_positions ?? 0}</td>
          <td class="text-zinc-500">${formatTime(b.last_signal_ts)}</td>
        </tr>`;
      }).join('')}</tbody></table>`;
    this.body.querySelectorAll('tr[data-bot-id]').forEach(tr => {
      tr.addEventListener('click', () => selectBot(tr.dataset.botId, tr.dataset.symbol));
    });
  }
}

// --- 2. Drill-down ---
class DrilldownPanel extends Panel {
  constructor() {
    super('fl-drilldown', `/api/bot-fleet/${selection.botId}`, 'Drill-Down');
    window.addEventListener('selection-changed', (e) => {
      this.endpoint = `/api/bot-fleet/${e.detail.botId}`;
      this.refresh();
    });
  }
  render(data) {
    if (data.detail) { this.setError(data.detail); return; }
    const fills = data.recent_fills || [];
    const verdicts = data.recent_verdicts || [];
    this.body.innerHTML = `
      <div class="text-xs text-zinc-500 mb-1">recent fills</div>
      <div class="space-y-1 text-xs font-mono mb-3">${fills.slice(0, 5).map(f =>
        `<div>${formatTime(f.ts)} ${escapeHtml(f.side)} ${formatNumber(f.price)} qty=${f.qty} ${formatR(f.realized_r)}</div>`
      ).join('') || '<div class="text-zinc-600">none</div>'}</div>
      <div class="text-xs text-zinc-500 mb-1">recent verdicts</div>
      <div class="space-y-1 text-xs">${verdicts.slice(0, 5).map(v =>
        `<div><span class="text-emerald-400">${escapeHtml(v.verdict)}</span> ${escapeHtml(v.sage_modulation || '')}</div>`
      ).join('') || '<div class="text-zinc-600">none</div>'}</div>`;
  }
}

// --- 3. Equity curve (Chart.js) ---
class EquityCurvePanel extends Panel {
  constructor() {
    super('fl-equity-curve', '/api/equity', 'Fleet Equity (today + 30d)');
    this.chart = null;
  }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    const series = (data.thirty_day || []).concat(data.today || []);
    if (this.chart) this.chart.destroy();
    this.body.innerHTML = '<canvas></canvas>';
    const ctx = this.body.querySelector('canvas').getContext('2d');
    this.chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: series.map(p => p.ts),
        datasets: [{
          label: 'equity',
          data: series.map(p => p.equity),
          borderColor: '#10b981',
          backgroundColor: 'rgba(16,185,129,0.1)',
          tension: 0.2,
        }],
      },
      options: {
        plugins: { legend: { display: false } },
        scales: { x: { display: false } },
        animation: false,
      },
    });
  }
}

// --- 4. Drawdown ---
class DrawdownPanel extends Panel {
  constructor() { super('fl-drawdown', '/api/risk_gates', 'Drawdown vs Threshold'); }
  render(data) {
    const bots = data.bots || [];
    const fleet = data.fleet_aggregate || {};
    this.body.innerHTML = `
      <div class="space-y-1">
        ${bots.map(b => {
          const dd = b.dd_pct ?? 0;
          const th = b.kill_threshold_pct ?? 8;
          const w = Math.min(100, (dd / th) * 100);
          return `<div class="flex items-center gap-2 text-xs">
            <span class="w-20 truncate">${escapeHtml(b.bot_id)}</span>
            <div class="flex-1 bg-zinc-800 h-2 rounded"><div class="h-full bg-amber-500 rounded" style="width:${w}%"></div></div>
            <span class="text-zinc-500">${dd.toFixed(1)}/${th}%</span>
          </div>`;
        }).join('') || '<div class="text-zinc-600 text-sm">no data</div>'}
      </div>
      <div class="mt-3 text-xs text-zinc-500">fleet: ${formatPct((fleet.fleet_dd_pct || 0) / 100)} of ${fleet.fleet_dd_threshold_pct || '?'}%</div>`;
  }
}

// --- 5. Sage modulation effect ---
class SageEffectPanel extends Panel {
  constructor() { super('fl-sage-effect', '/api/jarvis/sage_modulation_stats', 'Sage Modulation (24h)'); }
  render(data) {
    const perBot = data.per_bot || {};
    const rows = Object.entries(perBot);
    if (rows.length === 0) { this.body.innerHTML = '<div class="text-zinc-500 text-sm">no v22 firings yet</div>'; return; }
    this.body.innerHTML = `<table class="w-full text-xs">
      <tr class="text-zinc-500"><th class="text-left">bot</th><th class="text-right">loosen</th><th class="text-right">tighten</th><th class="text-right">defer</th></tr>
      ${rows.map(([bot, s]) =>
        `<tr><td>${escapeHtml(bot)}</td><td class="text-right text-emerald-400">${s.loosen ?? 0}</td><td class="text-right text-amber-400">${s.tighten ?? 0}</td><td class="text-right text-red-400">${s.defer ?? 0}</td></tr>`
      ).join('')}
    </table>`;
  }
}

// --- 6. Correlation throttle map ---
class CorrelationPanel extends Panel {
  constructor() { super('fl-correlation', '/api/preflight', 'Correlation Throttles'); }
  render(data) {
    const throttles = data.throttles || [];
    if (throttles.length === 0) { this.body.innerHTML = '<div class="text-emerald-400 text-sm">✓ no active throttles</div>'; return; }
    this.body.innerHTML = '<ul class="space-y-1 text-xs font-mono">' + throttles.map(t =>
      `<li>${escapeHtml(t.symbol_a)}↔${escapeHtml(t.symbol_b)} ρ=${(t.rho ?? 0).toFixed(2)} cap=${formatPct(t.cap_mult)}</li>`
    ).join('') + '</ul>';
  }
}

// --- 7. Per-bot edge ---
class EdgePerBotPanel extends Panel {
  constructor() {
    super('fl-edge-per-bot', `/api/jarvis/edge_leaderboard?bot=${selection.botId}`, 'Per-Bot Edge');
    window.addEventListener('selection-changed', (e) => {
      this.endpoint = `/api/jarvis/edge_leaderboard?bot=${e.detail.botId}`;
      this.refresh();
    });
  }
  render(data) {
    const top = (data.top || []).concat(data.bottom || []);
    if (top.length === 0) { this.body.innerHTML = '<div class="text-zinc-500 text-sm">no per-bot edge data</div>'; return; }
    this.body.innerHTML = '<table class="w-full text-xs">' + top.map(s =>
      `<tr><td>${escapeHtml(s.school)}</td><td class="text-right">${formatR(s.avg_r)}</td></tr>`
    ).join('') + '</table>';
  }
}

// --- 8. Position reconciler ---
class PositionReconcilerPanel extends Panel {
  constructor() { super('fl-position-reconciler', '/api/positions/reconciler', 'Position Reconciler'); }
  render(data) {
    if (data._warning) { this.body.innerHTML = `<div class="text-zinc-500 text-sm">${escapeHtml(data._warning)}</div>`; return; }
    const drifts = data.drifts || [];
    if (drifts.length === 0) { this.body.innerHTML = '<div class="text-emerald-400 text-sm">✓ no drift</div>'; return; }
    this.body.innerHTML = '<ul class="space-y-1 text-xs">' + drifts.map(d =>
      `<li class="text-red-400">${escapeHtml(d.bot)}: internal=${d.internal_qty} broker=${d.broker_qty}</li>`
    ).join('') + '</ul>';
  }
}

// --- 9. Risk ladder ---
class RiskLadderPanel extends Panel {
  constructor() { super('fl-risk-ladder', '/api/risk_gates', 'Risk Gate Ladder'); }
  render(data) {
    const bots = data.bots || [];
    if (bots.length === 0) { this.body.innerHTML = '<div class="text-zinc-500 text-sm">no data</div>'; return; }
    this.body.innerHTML = '<table class="w-full text-xs"><tr class="text-zinc-500"><th class="text-left">bot</th><th>latch</th><th>DD</th><th>cap</th></tr>' +
      bots.map(b => {
        const latchCls = b.latch_state === 'tripped' ? 'text-red-400'
                       : b.latch_state === 'armed'   ? 'text-amber-400' : 'text-emerald-400';
        return `<tr><td>${escapeHtml(b.bot_id)}</td><td class="${latchCls}">${escapeHtml(b.latch_state || 'unknown')}</td><td>${(b.dd_pct ?? 0).toFixed(1)}%</td><td>${formatPct(b.cap_mult ?? 1)}</td></tr>`;
      }).join('') + '</table>';
  }
}

// --- 10. Lifecycle controls ---
class ControlsPanel extends Panel {
  constructor() { super('fl-controls', null, 'Lifecycle Controls'); }
  refresh() { this.render(); }
  render() {
    const id = selection.botId;
    this.body.innerHTML = `
      <div class="text-xs text-zinc-500 mb-2">acting on: <span class="text-zinc-100 font-mono">${escapeHtml(id)}</span></div>
      <div class="grid grid-cols-2 gap-2">
        <button data-act="pause"   class="bg-zinc-700 hover:bg-zinc-600 rounded py-2 text-sm">pause</button>
        <button data-act="resume"  class="bg-zinc-700 hover:bg-zinc-600 rounded py-2 text-sm">resume</button>
        <button data-act="flatten" class="bg-amber-700 hover:bg-amber-600 rounded py-2 text-sm">flatten</button>
        <button data-act="kill"    class="bg-red-700 hover:bg-red-600 rounded py-2 text-sm">kill</button>
      </div>`;
    this.body.querySelectorAll('button[data-act]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const act = btn.dataset.act;
        const id = selection.botId;
        if (act === 'flatten' || act === 'kill') {
          if (!confirm(`${act.toUpperCase()} ${id} — are you sure?`)) return;
        }
        try {
          const r = await authedPost(`/api/bot/${id}/${act}`, {},
            { stepUpReason: `${act.toUpperCase()} ${id} requires step-up.` });
          if (r && r.ok) console.info(`${act} ${id} OK`);
        } catch (e) { console.error(`${act} ${id} failed`, e); }
      });
    });
    window.addEventListener('selection-changed', () => this.render());
  }
}

// --- 11. Live fill tape (SSE) ---
class FillTapeManager {
  constructor() {
    this.container = document.getElementById('fl-fill-tape-rows');
    this.rows = [];
    liveStream.on('fill', (f) => this.add(f));
  }
  add(f) {
    this.rows.unshift(f);
    if (this.rows.length > 30) this.rows.length = 30;
    if (!this.container) return;
    this.container.innerHTML = this.rows.map(f => {
      const cls = (f.realized_r ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400';
      return `<span class="px-2 py-1 bg-zinc-800 rounded ${cls}">${escapeHtml(f.bot)}/${escapeHtml(f.symbol)} ${escapeHtml(f.side)} ${formatNumber(f.price)} ${formatR(f.realized_r)}</span>`;
    }).join('');
  }
}

// --- 12. Health badges ---
class HealthBadgesPanel extends Panel {
  constructor() { super('fl-health-badges', '/api/bot-fleet', 'Bot Health Badges'); }
  render(data) {
    const bots = data.bots || [];
    if (bots.length === 0) { this.body.innerHTML = '<div class="text-zinc-500 text-sm">no data</div>'; return; }
    this.body.innerHTML = '<div class="grid grid-cols-2 gap-2 text-xs">' + bots.map(b => {
      const beat = formatTime(b.heartbeat_ts);
      return `<div class="border border-zinc-800 rounded p-2">
        <div class="font-mono text-zinc-200 mb-1">${escapeHtml(b.name)}</div>
        <div class="text-zinc-500">heartbeat: ${beat}</div>
        <div>${b.jarvis_attached ? '✓ jarvis' : '✗ jarvis'}</div>
        <div>${b.journal_attached ? '✓ journal' : '✗ journal'}</div>
        <div>${b.online_learner_attached ? '✓ learner' : '○ learner'}</div>
      </div>`;
    }).join('') + '</div>';
  }
}

// --- Initialize all 12 ---
onAuthenticated(() => {
  const panels = [
    new RosterPanel(),
    new DrilldownPanel(),
    new EquityCurvePanel(),
    new DrawdownPanel(),
    new SageEffectPanel(),
    new CorrelationPanel(),
    new EdgePerBotPanel(),
    new PositionReconcilerPanel(),
    new RiskLadderPanel(),
    new ControlsPanel(),
    new HealthBadgesPanel(),
  ];
  panels.forEach(p => { if (p.endpoint) poller.register(p); });
  new FillTapeManager();
});
```

- [ ] **Step 2: Smoke-test all 12 panels**

(Manual): switch to Fleet tab. Roster table, drill-down, equity chart, etc. all render. Click a row → drill-down + per-bot edge + selection-aware command-center panels refresh.

- [ ] **Step 3: Commit**

```bash
git add eta_engine/deploy/status_page/js/bot_fleet.js
git commit -m "feat(dashboard): bot_fleet.js -- 12 fleet panels + lifecycle controls + live fill tape (SSE)"
```

---

## Phase 6: Tests

### Task 16: Playwright end-to-end tests

**Files:**
- Create: `eta_engine/tests/test_dashboard_e2e.py`

- [ ] **Step 1: Write the test file**

```python
# eta_engine/tests/test_dashboard_e2e.py
"""Playwright end-to-end tests for the dashboard.

Run with:
  pytest eta_engine/tests/test_dashboard_e2e.py --headed=False -v
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def dashboard_server(tmp_path_factory):
    """Spin up the dashboard on port 8521 (test port) for e2e tests."""
    state_dir = tmp_path_factory.mktemp("dashboard_state")
    users_path = state_dir / "auth" / "users.json"
    sessions_path = state_dir / "auth" / "sessions.json"

    # Seed an operator account
    from eta_engine.deploy.scripts.dashboard_auth import create_user
    create_user(users_path, "edward", "test-pass")

    env = {
        "APEX_STATE_DIR": str(state_dir),
        "ETA_DASHBOARD_USERS_PATH": str(users_path),
        "ETA_DASHBOARD_SESSIONS_PATH": str(sessions_path),
        "ETA_DASHBOARD_STEP_UP_PIN": "1234",
    }
    import os
    full_env = {**os.environ, **env}

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "eta_engine.deploy.scripts.dashboard_api:app",
         "--port", "8521", "--host", "127.0.0.1"],
        env=full_env,
    )
    # Wait for it to start
    import urllib.request
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:8521/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.25)
    yield "http://127.0.0.1:8521"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_login_and_render_no_console_errors(dashboard_server) -> None:
    from playwright.async_api import async_playwright

    errors: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

        await page.goto(dashboard_server)
        # Login modal should appear
        await page.wait_for_selector("#login-modal:not(.hidden)")
        await page.fill("#login-username", "edward")
        await page.fill("#login-password", "test-pass")
        await page.click("#login-form button[type=submit]")

        # Wait for at least one panel to be present
        await page.wait_for_selector("[data-panel-id]")
        await asyncio.sleep(2)  # let panels paint

        await browser.close()
    assert errors == [], f"console errors: {errors}"


@pytest.mark.asyncio
async def test_every_panel_has_no_error_class(dashboard_server) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(dashboard_server)
        await page.fill("#login-username", "edward")
        await page.fill("#login-password", "test-pass")
        await page.click("#login-form button[type=submit]")
        await page.wait_for_selector("[data-panel-id]")
        await asyncio.sleep(3)  # let panels paint + first refresh complete

        # Switch to fleet tab so its panels also paint
        await page.click('button[data-tab="fleet"]')
        await asyncio.sleep(2)

        panels = await page.query_selector_all("[data-panel-id]")
        assert len(panels) >= 22, f"expected 22 panels, got {len(panels)}"
        errored = []
        for panel in panels:
            cls = await panel.get_attribute("class") or ""
            if "error" in cls.split():
                pid = await panel.get_attribute("data-panel-id")
                errored.append(pid)
        # Cold-start endpoints SHOULD return _warning, not error.
        # Real errors mean the panel renderer threw.
        assert errored == [], f"panels errored on initial render: {errored}"
        await browser.close()


@pytest.mark.asyncio
async def test_lifecycle_button_prompts_step_up(dashboard_server) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(dashboard_server)
        await page.fill("#login-username", "edward")
        await page.fill("#login-password", "test-pass")
        await page.click("#login-form button[type=submit]")
        await page.wait_for_selector("[data-panel-id]")
        await page.click('button[data-tab="fleet"]')
        await page.wait_for_selector('[data-panel-id="fl-controls"] button[data-act="kill"]')

        # Accept the JS confirm() dialog
        page.on("dialog", lambda d: d.accept())

        await page.click('[data-panel-id="fl-controls"] button[data-act="kill"]')
        # Step-up modal should appear
        await page.wait_for_selector("#step-up-modal:not(.hidden)")
        await browser.close()


@pytest.mark.asyncio
async def test_sse_status_dot_turns_green(dashboard_server) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(dashboard_server)
        await page.fill("#login-username", "edward")
        await page.fill("#login-password", "test-pass")
        await page.click("#login-form button[type=submit]")
        # Wait for SSE to connect (status dot becomes green)
        await page.wait_for_selector("#top-sse-status .sse-connected", timeout=5000)
        await browser.close()


@pytest.mark.asyncio
async def test_unauthenticated_blocks_app_load(dashboard_server) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(dashboard_server)
        # Login modal should be visible from the get-go
        modal = await page.wait_for_selector("#login-modal")
        cls = await modal.get_attribute("class") or ""
        assert "hidden" not in cls.split(), "login modal should be visible"
        await browser.close()
```

- [ ] **Step 2: Run the playwright suite**

Run: `python -m pytest eta_engine/tests/test_dashboard_e2e.py -v --asyncio-mode=auto`
Expected: 5 PASSED in <30s

- [ ] **Step 3: Commit**

```bash
git add eta_engine/tests/test_dashboard_e2e.py
git commit -m "test(dashboard): 5 Playwright e2e tests (login, panels render, step-up modal, SSE dot, unauth blocks load)"
```

---

## Phase 7: Rollout

### Task 17: Stage 0 launcher script — port 8421 alongside

**Files:**
- Create: `eta_engine/deploy/scripts/run_dashboard_8421.ps1`

- [ ] **Step 1: Write the launcher**

```powershell
# eta_engine/deploy/scripts/run_dashboard_8421.ps1
# Stage 0 of the dashboard rebuild rollout.
# Runs the new dashboard on port 8421 ALONGSIDE the existing one on 8420
# so the operator can QA before cutover.
#
# Usage:
#   .\eta_engine\deploy\scripts\run_dashboard_8421.ps1
#
# Then visit http://127.0.0.1:8421/ to test.

$ErrorActionPreference = "Stop"

# Ensure operator account exists (only on first run)
$users = Join-Path $env:LOCALAPPDATA "eta_engine\state\auth\users.json"
if (-not (Test-Path $users)) {
    Write-Host "First-run: creating operator account..."
    $username = Read-Host "Operator username"
    $pw = Read-Host "Operator password" -AsSecureString
    $pwPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
    python -c "from pathlib import Path; from eta_engine.deploy.scripts.dashboard_auth import create_user; create_user(Path(r'$users'), '$username', '$pwPlain')"
    Write-Host "Account created at $users"
}

# PIN for step-up
if (-not $env:ETA_DASHBOARD_STEP_UP_PIN) {
    $env:ETA_DASHBOARD_STEP_UP_PIN = "1234"
    Write-Host "Defaulting step-up PIN to '1234' (set ETA_DASHBOARD_STEP_UP_PIN to override)"
}

Write-Host "Starting Stage 0 dashboard on http://127.0.0.1:8421/"
python -m uvicorn eta_engine.deploy.scripts.dashboard_api:app `
    --host 127.0.0.1 --port 8421 --reload
```

- [ ] **Step 2: Smoke-test it**

(Manual): run `.\eta_engine\deploy\scripts\run_dashboard_8421.ps1`. Open `http://127.0.0.1:8421/`. The OLD dashboard at 8420 should also still be running. QA every panel.

- [ ] **Step 3: Commit**

```bash
git add eta_engine/deploy/scripts/run_dashboard_8421.ps1
git commit -m "feat(dashboard): Stage 0 launcher -- run new dashboard on 8421 alongside old on 8420"
```

---

### Task 18: Stage 1 cutover — replace port 8420

**Files:**
- Modify: `eta_engine/deploy/scripts/register_operator_tasks.ps1` (update the `Apex-Dashboard` task to launch `dashboard_api.py` on 8420 instead of the firm command_center)

- [ ] **Step 1: Locate the existing dashboard task definition**

Run: `grep -nP "Apex-Dashboard|command_center" eta_engine/deploy/scripts/register_operator_tasks.ps1`

Find the existing scheduled task that launches the firm command_center on 8420.

- [ ] **Step 2: Update the task definition**

Edit `eta_engine/deploy/scripts/register_operator_tasks.ps1`. Find the existing dashboard task block (it currently invokes `command_center.launch` or similar) and replace its `Args` with:

```powershell
@{
    Name       = "Apex-Dashboard"
    Exec       = $Python
    Args       = "-m uvicorn eta_engine.deploy.scripts.dashboard_api:app --host 127.0.0.1 --port 8420"
    Cwd        = $EtaEngineDir
    Trigger    = "AtStartup"
    Notes      = "Wave-7 dashboard: serves the JARVIS command center + bot fleet view at http://127.0.0.1:8420/. Replaces the firm command_center on 8420 (kept in repo at firm/eta_engine/command_center/ until Stage 2 decommission)."
}
```

- [ ] **Step 3: Run the cutover**

```powershell
# Stop old
Stop-ScheduledTask -TaskName "Apex-Dashboard" -ErrorAction SilentlyContinue

# Re-register with new task definition
.\eta_engine\deploy\scripts\register_operator_tasks.ps1 -RegisterDashboard

# Start new
Start-ScheduledTask -TaskName "Apex-Dashboard"

# Verify
Start-Sleep 3
Invoke-RestMethod http://127.0.0.1:8420/health
```

Expected: `{status: "ok", state_dir: "...", ...}` (i.e. the new dashboard's `/health` endpoint).

- [ ] **Step 4: Manual smoke-test**

Open `http://127.0.0.1:8420/`. Login. Verify all 22 panels render. Verify SSE dot is green. Verify a kill action prompts step-up.

- [ ] **Step 5: Commit**

```bash
git add eta_engine/deploy/scripts/register_operator_tasks.ps1
git commit -m "feat(dashboard): Stage 1 cutover -- Apex-Dashboard task now launches dashboard_api.py on 8420"
```

---

### Task 19: Stage 2 — decommission firm command_center

**Note:** Run this task AT LEAST 7 days after Task 18 to give the new dashboard time to prove stability.

**Files:**
- Move: `firm/eta_engine/command_center/` → `_archive/2026-XX-XX_firm_command_center/`

- [ ] **Step 1: Verify the new dashboard has been stable for 7 days**

```powershell
Get-ScheduledTaskInfo -TaskName "Apex-Dashboard" | Select-Object LastRunTime, LastTaskResult, NumberOfMissedRuns
```

Expected: `LastTaskResult = 0`, `NumberOfMissedRuns = 0` for the past 7 days.

- [ ] **Step 2: Move the firm command_center to archive**

```powershell
$today = Get-Date -Format "yyyy-MM-dd"
$src = "C:\EvolutionaryTradingAlgo\firm\eta_engine\command_center"
$dst = "C:\EvolutionaryTradingAlgo\_archive\${today}_firm_command_center"
Move-Item $src $dst
```

- [ ] **Step 3: Verify nothing else imports from the archived path**

Run: `grep -rln "firm.eta_engine.command_center\|firm/eta_engine/command_center" --include="*.py" --include="*.ps1" C:/EvolutionaryTradingAlgo`

Expected: no results.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore(dashboard): Stage 2 -- decommission firm command_center after 7 stable days on dashboard_api.py"
```

---

## Self-Review

### Spec coverage
| Spec section | Implementation task |
|---|---|
| Auth — login/logout/session/step-up | Tasks 2, 3 |
| Sage_explain / sage_timeline / disagreement_heatmap | Already in dashboard_api.py (verified at the start) |
| Governor + edge_leaderboard + model_tier + kaizen_latest | Task 5 |
| Bot fleet roster + drill-down + risk_gates + position_reconciler | Task 6 |
| Equity + preflight + sage_modulation_stats + sage_modulation_toggle | Task 7 |
| Bot lifecycle (pause/resume/flatten/kill) with step-up | Task 8 |
| SSE /api/live/stream | Task 9 |
| Frontend shell + theme | Task 10 |
| panels.js base class | Task 11 |
| auth.js login + step-up | Task 12 |
| live.js SSE + Poller | Task 13 |
| 10 JARVIS panels | Task 14 |
| 12 fleet panels + lifecycle controls + fill tape | Task 15 |
| Playwright e2e suite | Task 16 |
| Stage 0 alongside 8421 | Task 17 |
| Stage 1 cutover | Task 18 |
| Stage 2 decommission | Task 19 |

All spec requirements covered.

### Placeholder scan
Searched for: TBD, TODO, "implement later", "fill in details", "appropriate error handling", "Similar to Task N". None found.

### Type consistency
- `Panel` constructor takes `(containerId, endpoint, title)` — used consistently in command_center.js and bot_fleet.js
- `selection.botId` / `selection.symbol` — used consistently
- `liveStream.on(eventType, handler)` — verdict + fill events used consistently
- `authedPost(url, body, opts)` — opts.stepUpReason used consistently
- `_write_control_signal(bot_id, action, by_user)` — same signature in pause/resume/flatten/kill
- `read_json_safe(path)` — returns `{...}` on success or `{_warning: "no_data"}` / `{_error_code: "state_corrupt"}` — used consistently across all endpoints

All consistent.

---

## Execution Handoff

Plan complete and saved to `eta_engine/docs/superpowers/plans/2026-04-27-dashboard-rebuild.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**

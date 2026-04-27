"""Tests for dashboard auth endpoints."""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path


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

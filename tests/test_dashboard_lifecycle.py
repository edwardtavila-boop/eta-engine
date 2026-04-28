"""Tests for bot lifecycle endpoints."""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path


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


@pytest.fixture(autouse=True)
def _reset_rate_limit_between_tests():
    from eta_engine.deploy.scripts.dashboard_api import _LOGIN_FAILURES
    _LOGIN_FAILURES.clear()
    yield
    _LOGIN_FAILURES.clear()


@pytest.fixture
def authed_client(auth_paths):
    from eta_engine.deploy.scripts.dashboard_api import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": "edward", "password": "pw"})
    assert r.status_code == 200
    return c


@pytest.fixture
def stepped_up_client(authed_client):
    r = authed_client.post("/api/auth/step-up", json={"pin": "1234"})
    assert r.status_code == 200
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


def test_pause_rejects_bad_bot_id(authed_client) -> None:
    """Path-traversal guard."""
    r = authed_client.post("/api/bot/..%2Fevil/pause")
    # FastAPI URL normalization may turn this into 404 before reaching handler;
    # OR our regex guard returns 400. Either is acceptable.
    assert r.status_code in (400, 404)

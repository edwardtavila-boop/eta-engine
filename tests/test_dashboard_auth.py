"""Tests for dashboard auth (Wave-7, 2026-04-27)."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def test_create_user_and_verify_password(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_auth import (
        create_user,
        verify_password,
    )

    users_path = tmp_path / "users.json"
    create_user(users_path, "edward", "correct horse battery staple")
    assert verify_password(users_path, "edward", "correct horse battery staple") is True
    assert verify_password(users_path, "edward", "wrong") is False
    assert verify_password(users_path, "nobody", "anything") is False


def test_create_session_and_lookup(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_auth import (
        create_session,
        get_session,
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
        create_session,
        get_session,
        mark_step_up,
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
        create_session,
        get_session,
        revoke_session,
    )

    sessions_path = tmp_path / "sessions.json"
    token = create_session(sessions_path, user="edward", ttl_seconds=3600)
    revoke_session(sessions_path, token)
    assert get_session(sessions_path, token) is None

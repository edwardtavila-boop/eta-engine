"""Tests for agent_registry — T14 inter-agent coordination."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path


def test_register_then_list_round_trip(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    res = agent_registry.register_agent("agent_a", role="researcher", path=path)
    assert res["status"] == "REGISTERED"

    listed = agent_registry.list_agents(path=path)
    assert len(listed) == 1
    assert listed[0]["agent_id"] == "agent_a"
    assert listed[0]["role"] == "researcher"
    assert listed[0]["alive"] is True


def test_register_is_idempotent(tmp_path: Path) -> None:
    """Re-registering preserves registered_at; updates last_heartbeat."""
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    first = agent_registry.register_agent("a", role="r1", path=path)
    second = agent_registry.register_agent("a", role="r1", path=path)
    assert first["registered_at"] == second["registered_at"]


def test_deregister_releases_locks(tmp_path: Path) -> None:
    """Default deregister also drops any locks the agent held."""
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    agent_registry.register_agent("a", role="r1", path=path)
    agent_registry.acquire_lock("a", "bot:vp_mnq", purpose="retire", path=path)

    res = agent_registry.deregister_agent("a", path=path)
    assert res["status"] == "DEREGISTERED"
    assert "bot:vp_mnq" in res["released_locks"]
    # Lock no longer present
    assert agent_registry.check_lock("bot:vp_mnq", path=path) is None


def test_acquire_lock_success(tmp_path: Path) -> None:
    """First-time acquire returns ACQUIRED with proper TTL fields."""
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    res = agent_registry.acquire_lock("a", "bot:x", purpose="retire", path=path)
    assert res["status"] == "ACQUIRED"
    assert res["resource"] == "bot:x"
    assert res["owner_agent_id"] == "a"
    assert "expires_at" in res


def test_acquire_lock_conflict_returns_locked_by_other(tmp_path: Path) -> None:
    """When another agent holds the lock, second agent gets LOCKED_BY_OTHER."""
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    agent_registry.acquire_lock("a", "bot:x", purpose="retire", path=path)
    res = agent_registry.acquire_lock("b", "bot:x", purpose="retire", path=path)
    assert res["status"] == "LOCKED_BY_OTHER"
    assert res["owner_agent_id"] == "a"


def test_reacquire_same_owner_extends_ttl(tmp_path: Path) -> None:
    """Same agent re-acquiring its own lock returns REACQUIRED + new TTL."""
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    res1 = agent_registry.acquire_lock("a", "bot:x", ttl_seconds=60, path=path)
    res2 = agent_registry.acquire_lock("a", "bot:x", ttl_seconds=300, path=path)
    assert res2["status"] == "REACQUIRED"
    # New expires_at is later than the first one
    from datetime import datetime as _dt

    assert _dt.fromisoformat(res2["expires_at"]) > _dt.fromisoformat(res1["expires_at"])


def test_release_lock_only_owner(tmp_path: Path) -> None:
    """Non-owner trying to release gets NOT_OWNER, lock stays."""
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    agent_registry.acquire_lock("a", "bot:x", path=path)
    res = agent_registry.release_lock("b", "bot:x", path=path)
    assert res["status"] == "NOT_OWNER"
    # Lock still held by a
    cur = agent_registry.check_lock("bot:x", path=path)
    assert cur is not None
    assert cur["owner_agent_id"] == "a"


def test_release_lock_voluntarily(tmp_path: Path) -> None:
    """Owner can release, returns RELEASED, lock disappears."""
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    agent_registry.acquire_lock("a", "bot:x", path=path)
    res = agent_registry.release_lock("a", "bot:x", path=path)
    assert res["status"] == "RELEASED"
    assert agent_registry.check_lock("bot:x", path=path) is None


def test_expired_lock_auto_swept_on_next_acquire(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An expired lock doesn't block subsequent acquires."""
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    # Acquire with normal TTL
    agent_registry.acquire_lock("a", "bot:x", ttl_seconds=60, path=path)

    # Move clock forward past expiry by monkeypatching _now() to return
    # the future
    future = datetime.now(UTC) + timedelta(minutes=20)
    monkeypatch.setattr(agent_registry, "_now", lambda: future)

    # Different agent should acquire successfully (expired lock gets swept)
    res = agent_registry.acquire_lock("b", "bot:x", path=path)
    assert res["status"] == "ACQUIRED"
    assert res["owner_agent_id"] == "b"


def test_list_agents_filters_stale_when_only_alive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """only_alive=True hides agents that haven't heartbeat'd recently."""
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    agent_registry.register_agent("a", role="r", path=path)

    # Move clock past liveness window
    future = datetime.now(UTC) + timedelta(minutes=30)
    monkeypatch.setattr(agent_registry, "_now", lambda: future)

    alive = agent_registry.list_agents(only_alive=True, path=path)
    all_ = agent_registry.list_agents(only_alive=False, path=path)
    assert len(alive) == 0
    assert len(all_) == 1
    assert all_[0]["alive"] is False


def test_heartbeat_updates_timestamp(tmp_path: Path) -> None:
    """heartbeat() touches last_heartbeat without disturbing other fields."""
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    reg = agent_registry.register_agent("a", role="r", path=path)
    res = agent_registry.heartbeat("a", path=path)
    assert res["status"] == "OK"

    listed = agent_registry.list_agents(only_alive=False, path=path)
    # last_heartbeat may have advanced (or be equal if same second)
    assert listed[0]["last_heartbeat"] >= reg["last_heartbeat"]


def test_heartbeat_unregistered_agent_rejected(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    res = agent_registry.heartbeat("ghost_agent", path=path)
    assert res["status"] == "NOT_REGISTERED"


def test_corrupt_registry_returns_empty_state(tmp_path: Path) -> None:
    """Garbage JSON → readers see an empty registry, no exception."""
    from eta_engine.brain.jarvis_v3 import agent_registry

    path = tmp_path / "registry.json"
    path.write_text("not json at all", encoding="utf-8")

    assert agent_registry.list_agents(path=path) == []
    assert agent_registry.check_lock("anything", path=path) is None


def test_register_with_missing_agent_id_rejected(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import agent_registry

    res = agent_registry.register_agent("", role="r", path=tmp_path / "r.json")
    assert res["status"] == "REJECTED"


def test_acquire_lock_with_missing_resource_rejected(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import agent_registry

    res = agent_registry.acquire_lock("a", "", path=tmp_path / "r.json")
    assert res["status"] == "REJECTED"

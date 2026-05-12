"""
JARVIS v3 // agent_registry (T14)

Inter-agent coordination layer. Multiple Claude Code sessions /
specialized agents can register with Hermes (research bot, monitor
bot, execution bot, etc.) and acquire scoped LOCKS so two agents
don't take conflicting destructive actions on the same bot.

Pattern
-------

Each "agent" is a logical actor — typically one Claude Code session,
identified by a stable ``agent_id`` (operator picks). On startup the
agent registers itself with a role label. Before any destructive
action (retire / deploy / set_size_modifier <0.3 / clear_override /
kill_switch) the agent acquires a LOCK on the affected resource
(bot_id or fleet). Conflicting concurrent locks fail fast with
``LOCKED_BY_OTHER``.

Locks are TTL-bounded (default 10 min) and auto-release. An agent
that crashes mid-action doesn't strand a lock forever.

Storage
-------

Single JSON sidecar at ``var/eta_engine/state/agent_registry.json``.
Atomic writes via temp + os.replace. Same robustness contract as
hermes_overrides: NEVER raises, always returns clean envelopes.

NOT in scope
------------

* Cross-machine coordination (agents on different VPSes). The registry
  is a single-file source of truth on one VPS.
* Lock priority / preemption. First lock wins until TTL expires.
* Lock renewal — agents can re-acquire to extend, but the registry
  doesn't push notifications.

Public interface
----------------

* ``register_agent(agent_id, role, version)`` — declare presence.
* ``deregister_agent(agent_id)`` — clean shutdown.
* ``heartbeat(agent_id)`` — mark agent as alive (used by liveness check).
* ``list_agents()`` — see who's online.
* ``acquire_lock(agent_id, resource, ttl_seconds)`` — claim a resource.
* ``release_lock(agent_id, resource)`` — voluntarily release.
* ``check_lock(resource)`` — read-only: who owns this resource?
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.agent_registry")

DEFAULT_REGISTRY_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\agent_registry.json",
)
DEFAULT_LOCK_TTL_SECONDS = 600  # 10 minutes
DEFAULT_AGENT_LIVENESS_SECONDS = 600  # 10 min since heartbeat → "stale"

EXPECTED_HOOKS = (
    "register_agent",
    "deregister_agent",
    "heartbeat",
    "list_agents",
    "acquire_lock",
    "release_lock",
    "check_lock",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(value: Any) -> datetime | None:  # noqa: ANN401
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _resolve_path(p: Path | None) -> Path:
    return Path(p) if p is not None else DEFAULT_REGISTRY_PATH


def _empty_state() -> dict[str, Any]:
    return {
        "_doc": (
            "Inter-agent coordination registry (T14). Records online "
            "agents + active resource locks. Stale entries are filtered "
            "automatically by readers; rewrite happens on next mutation."
        ),
        "agents": {},   # {agent_id: {role, version, registered_at, last_heartbeat}}
        "locks": {},    # {resource: {owner_agent_id, acquired_at, expires_at, purpose}}
    }


def _load(path: Path | None = None) -> dict[str, Any]:
    target = _resolve_path(path)
    if not target.exists():
        return _empty_state()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_state()
        data.setdefault("agents", {})
        data.setdefault("locks", {})
        if not isinstance(data["agents"], dict):
            data["agents"] = {}
        if not isinstance(data["locks"], dict):
            data["locks"] = {}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("agent_registry _load failed: %s", exc)
        return _empty_state()


def _atomic_write(target: Path, data: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=2, default=str)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".tmp_agent_registry_", suffix=".json", dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialized)
        os.replace(tmp_name, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _save(data: dict[str, Any], path: Path | None = None) -> bool:
    target = _resolve_path(path)
    try:
        _atomic_write(target, data)
        return True
    except OSError as exc:
        logger.warning("agent_registry _save failed: %s", exc)
        return False


def _is_lock_active(entry: dict[str, Any], now: datetime | None = None) -> bool:
    if not isinstance(entry, dict):
        return False
    expires = _parse_iso(entry.get("expires_at"))
    if expires is None:
        return False
    return expires > (now or _now())


def _is_agent_alive(entry: dict[str, Any], now: datetime | None = None,
                    liveness_s: int = DEFAULT_AGENT_LIVENESS_SECONDS) -> bool:
    if not isinstance(entry, dict):
        return False
    hb = _parse_iso(entry.get("last_heartbeat"))
    if hb is None:
        return False
    return (now or _now()) - hb < timedelta(seconds=liveness_s)


# ---------------------------------------------------------------------------
# Public API — agent lifecycle
# ---------------------------------------------------------------------------


def register_agent(
    agent_id: str,
    role: str,
    version: str = "1.0.0",
    path: Path | None = None,
) -> dict[str, Any]:
    """Register an agent. Idempotent — re-registering refreshes role/version
    and last_heartbeat without disturbing existing locks held by the agent.

    Returns the registered entry on success or ``{"status":"REJECTED",...}``
    on bad input. NEVER raises.
    """
    try:
        if not agent_id:
            return {"status": "REJECTED", "reason": "missing_agent_id"}
        now = _now()
        data = _load(path)
        existing = data["agents"].get(agent_id)
        entry = {
            "role": str(role),
            "version": str(version),
            "registered_at": (existing or {}).get("registered_at") or _iso(now),
            "last_heartbeat": _iso(now),
        }
        data["agents"][agent_id] = entry
        ok = _save(data, path)
        return {
            "status": "REGISTERED" if ok else "WRITE_FAILED",
            "agent_id": agent_id,
            **entry,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_registry.register_agent dropped: %s", exc)
        return {"status": "WRITE_FAILED", "reason": f"unhandled_exception: {exc}"}


def deregister_agent(
    agent_id: str, release_locks: bool = True, path: Path | None = None,
) -> dict[str, Any]:
    """Remove an agent. By default also releases any locks the agent holds —
    clean shutdown protocol. Set ``release_locks=False`` to deregister
    while keeping the agent's locks until their TTL expires (rarely useful).

    NEVER raises.
    """
    try:
        if not agent_id:
            return {"status": "REJECTED", "reason": "missing_agent_id"}
        data = _load(path)
        if agent_id not in data["agents"]:
            return {"status": "NOT_FOUND", "agent_id": agent_id}
        data["agents"].pop(agent_id, None)
        released = []
        if release_locks:
            for resource, lock in list(data["locks"].items()):
                if isinstance(lock, dict) and lock.get("owner_agent_id") == agent_id:
                    data["locks"].pop(resource, None)
                    released.append(resource)
        ok = _save(data, path)
        return {
            "status": "DEREGISTERED" if ok else "WRITE_FAILED",
            "agent_id": agent_id,
            "released_locks": released,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_registry.deregister_agent dropped: %s", exc)
        return {"status": "WRITE_FAILED", "reason": f"unhandled_exception: {exc}"}


def heartbeat(agent_id: str, path: Path | None = None) -> dict[str, Any]:
    """Update the agent's last_heartbeat timestamp.

    Auto-deregistering stale agents is the caller's responsibility
    (typically a scheduled task that calls list_agents and prunes).
    NEVER raises.
    """
    try:
        if not agent_id:
            return {"status": "REJECTED", "reason": "missing_agent_id"}
        data = _load(path)
        if agent_id not in data["agents"]:
            return {"status": "NOT_REGISTERED", "agent_id": agent_id}
        data["agents"][agent_id]["last_heartbeat"] = _iso(_now())
        ok = _save(data, path)
        return {
            "status": "OK" if ok else "WRITE_FAILED",
            "agent_id": agent_id,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_registry.heartbeat dropped: %s", exc)
        return {"status": "WRITE_FAILED", "reason": f"unhandled_exception: {exc}"}


def list_agents(
    only_alive: bool = True, path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return a list of registered agents. By default filters to those with
    a heartbeat within DEFAULT_AGENT_LIVENESS_SECONDS.
    """
    try:
        data = _load(path)
        now = _now()
        out: list[dict[str, Any]] = []
        for agent_id, entry in data["agents"].items():
            if not isinstance(entry, dict):
                continue
            alive = _is_agent_alive(entry, now)
            if only_alive and not alive:
                continue
            out.append({
                "agent_id": agent_id,
                "alive": alive,
                **entry,
            })
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_registry.list_agents dropped: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Public API — locks
# ---------------------------------------------------------------------------


def acquire_lock(
    agent_id: str,
    resource: str,
    *,
    purpose: str = "",
    ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    path: Path | None = None,
) -> dict[str, Any]:
    """Try to claim ``resource`` for ``agent_id``.

    Returns:
      * ``{"status":"ACQUIRED", ...}`` on success — caller proceeds.
      * ``{"status":"LOCKED_BY_OTHER", owner_agent_id, expires_at, ...}``
        when another agent holds an active lock — caller must wait or
        defer the action.
      * ``{"status":"REACQUIRED", ...}`` when the same agent already
        held this resource (idempotent re-acquire extends the TTL).
      * ``{"status":"REJECTED", ...}`` on bad input.

    Auto-cleans expired locks before checking — so a crashed agent's
    abandoned lock doesn't permanently block resource access.
    NEVER raises.
    """
    try:
        if not agent_id or not resource:
            return {"status": "REJECTED", "reason": "missing_agent_id_or_resource"}
        if ttl_seconds <= 0:
            ttl_seconds = DEFAULT_LOCK_TTL_SECONDS
        now = _now()
        expires = now + timedelta(seconds=ttl_seconds)
        data = _load(path)

        # Sweep expired locks
        data["locks"] = {
            r: lock for r, lock in data["locks"].items()
            if _is_lock_active(lock, now)
        }

        existing = data["locks"].get(resource)
        if existing and isinstance(existing, dict):
            if existing.get("owner_agent_id") != agent_id:
                return {
                    "status": "LOCKED_BY_OTHER",
                    "resource": resource,
                    "owner_agent_id": existing.get("owner_agent_id"),
                    "expires_at": existing.get("expires_at"),
                    "purpose": existing.get("purpose"),
                }
            # Re-acquire — extend TTL
            entry = {
                "owner_agent_id": agent_id,
                "acquired_at": existing.get("acquired_at", _iso(now)),
                "expires_at": _iso(expires),
                "purpose": str(purpose) or existing.get("purpose", ""),
            }
            data["locks"][resource] = entry
            ok = _save(data, path)
            return {
                "status": "REACQUIRED" if ok else "WRITE_FAILED",
                "resource": resource,
                **entry,
            }

        # Fresh acquire
        entry = {
            "owner_agent_id": agent_id,
            "acquired_at": _iso(now),
            "expires_at": _iso(expires),
            "purpose": str(purpose),
        }
        data["locks"][resource] = entry
        ok = _save(data, path)
        return {
            "status": "ACQUIRED" if ok else "WRITE_FAILED",
            "resource": resource,
            **entry,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_registry.acquire_lock dropped: %s", exc)
        return {"status": "WRITE_FAILED", "reason": f"unhandled_exception: {exc}"}


def release_lock(
    agent_id: str, resource: str, path: Path | None = None,
) -> dict[str, Any]:
    """Voluntarily release a lock. Only the owner can release.
    Trying to release a lock you don't hold returns NOT_OWNER.
    """
    try:
        if not agent_id or not resource:
            return {"status": "REJECTED", "reason": "missing_agent_id_or_resource"}
        data = _load(path)
        existing = data["locks"].get(resource)
        if not isinstance(existing, dict):
            return {"status": "NOT_FOUND", "resource": resource}
        if existing.get("owner_agent_id") != agent_id:
            return {
                "status": "NOT_OWNER",
                "resource": resource,
                "owner_agent_id": existing.get("owner_agent_id"),
            }
        data["locks"].pop(resource, None)
        ok = _save(data, path)
        return {
            "status": "RELEASED" if ok else "WRITE_FAILED",
            "resource": resource,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_registry.release_lock dropped: %s", exc)
        return {"status": "WRITE_FAILED", "reason": f"unhandled_exception: {exc}"}


def check_lock(
    resource: str, path: Path | None = None,
) -> dict[str, Any] | None:
    """Read-only: return the active lock entry for ``resource``, or ``None``.

    Used by destructive tools to check "can I proceed?" without
    attempting acquire. Filters out expired locks transparently.
    """
    try:
        if not resource:
            return None
        data = _load(path)
        entry = data["locks"].get(resource)
        if not isinstance(entry, dict):
            return None
        if not _is_lock_active(entry):
            return None
        return {"resource": resource, **entry}
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_registry.check_lock dropped: %s", exc)
        return None

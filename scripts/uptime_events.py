"""Append-only uptime event log for the supervisor + watchdog.

The single source of post-mortem truth for crashes, SIGTERM-clean stops,
and watchdog-triggered relaunches. Every line is a self-contained JSON
record keyed by ``ts`` so an operator can ``grep`` or ``jq`` the file
without parsing surrounding state.

Canonical path: ``var/eta_engine/state/uptime_events.jsonl``
(override via ``ETA_UPTIME_EVENTS_PATH`` for tests).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.scripts import workspace_roots

ETA_UPTIME_EVENTS_PATH_ENV = "ETA_UPTIME_EVENTS_PATH"
DEFAULT_UPTIME_EVENTS_PATH: Path = (
    workspace_roots.ETA_RUNTIME_STATE_DIR / "uptime_events.jsonl"
)


def default_uptime_events_path() -> Path:
    """Return the canonical uptime-events path with env override."""
    override = os.getenv(ETA_UPTIME_EVENTS_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_UPTIME_EVENTS_PATH


def record_uptime_event(
    *,
    component: str,
    event: str,
    reason: str = "",
    pid: int | None = None,
    extra: dict[str, Any] | None = None,
    path: Path | None = None,
) -> Path:
    """Append a single uptime event to the JSONL log.

    Parameters
    ----------
    component
        Short tag identifying the writer (e.g. ``"supervisor"``,
        ``"broker_router"``, ``"watchdog"``).
    event
        Lifecycle moment: ``"start"``, ``"stop"``, ``"crash"``,
        ``"relaunch"``, ``"watchdog_noop"``.
    reason
        Free-form note: SIGTERM, last traceback line, port refused,
        operator opt-out detected, etc. Keep under ~200 chars so the
        line stays grep-friendly.
    pid
        Process id when known (the supervisor stamps its own PID; the
        watchdog stamps the supervisor's PID for relaunch events).
    extra
        Optional structured payload for richer post-mortem (recent
        traceback, exit code, etc.). Must be JSON-serializable.
    path
        Override path (tests). Default: canonical workspace path.

    Failure mode
    ------------
    Never raises. Disk full / permission denied / parent missing all
    swallow into a no-op so the caller's main loop never dies on the
    telemetry path.
    """
    target = Path(path) if path is not None else default_uptime_events_path()
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "component": component,
        "event": event,
        "reason": reason or "",
        "pid": int(pid) if pid is not None else os.getpid(),
    }
    if extra:
        try:
            # Round-trip through json to catch non-serializable objects
            # before they corrupt the JSONL file.
            json.loads(json.dumps(extra, default=str))
            record["extra"] = extra
        except Exception:  # noqa: BLE001
            record["extra"] = {"_serialization_error": repr(extra)[:200]}

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:  # noqa: BLE001 — telemetry must never crash the caller
        pass
    return target


def read_recent_events(n: int = 50, path: Path | None = None) -> list[dict[str, Any]]:
    """Return the last ``n`` events for diagnostic surfaces.

    Tolerates malformed lines (older log lines, partial writes) by
    skipping them. Returns the newest event last so callers can
    iterate naturally.
    """
    target = Path(path) if path is not None else default_uptime_events_path()
    if not target.exists():
        return []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in lines[-max(1, int(n)) :]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out

"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.shared_breaker
================================================
File-backed CircuitBreaker that syncs trip state across processes via
``~/.jarvis/breaker.json``.

Why this exists
---------------
The in-memory :class:`CircuitBreaker` is a single-process guard. The
APEX fleet has multiple processes -- the orchestrator, per-persona
daemons, the backtest runner, the dashboard, the cron workers -- each
of which may independently blow up cost or rack up denials. When one
process trips the breaker, all the others must also refuse dispatches
until cooldown elapses. Otherwise a tripped-but-not-broadcast breaker
lets the next process happily burn through the cost cap that the
tripping process was trying to protect.

Design
------
On every state transition (CLOSED->OPEN, OPEN->HALF_OPEN,
HALF_OPEN->CLOSED, manual reset), write an atomic JSON snapshot to
``~/.jarvis/breaker.json``. On every :meth:`pre_dispatch`, re-read the
file and adopt the shared state if it's newer. Atomic writes use the
tempfile + ``os.replace`` dance so a reader never sees a partial file.

Last-writer-wins on contention. This is safe because:

* CLOSED over CLOSED is idempotent.
* OPEN over OPEN keeps the newer trip reason (no data loss).
* OPEN over CLOSED is the "fresh trip" case -- monotonic.
* CLOSED over OPEN is "operator reset" or "probe succeeded in another
  process." Either way, the newer observation wins.

Cost window and consecutive counters remain process-local -- they're
not shared state. Only the trip verdict is.

File schema v1
--------------
::

    {
      "version": 1,
      "state": "OPEN" | "CLOSED",
      "tripped_at":  "ISO-8601 or null",
      "reopen_at":   "ISO-8601 or null",
      "last_reason": "string",
      "written_at":  "ISO-8601",
      "writer_pid":  12345
    }

``HALF_OPEN`` is never persisted -- it's a transient in-process state
during a probe. Each process computes HALF_OPEN locally when it sees
disk state OPEN with ``reopen_at`` in the past.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.brain.avengers.circuit_breaker import (
    BreakerState,
    CircuitBreaker,
)

if TYPE_CHECKING:
    from eta_engine.brain.avengers.base import TaskResult

_LOG = logging.getLogger(__name__)

DEFAULT_BREAKER_PATH = Path.home() / ".jarvis" / "breaker.json"
SCHEMA_VERSION = 1


class SharedCircuitBreaker(CircuitBreaker):
    """CircuitBreaker that persists trip state to ``~/.jarvis/breaker.json``.

    Drop-in replacement for :class:`CircuitBreaker`. On every
    ``pre_dispatch`` and every state transition the breaker syncs with
    the file so multiple processes share a single notion of "is the
    breaker tripped right now."

    Parameters
    ----------
    path
        Override the shared state file. Defaults to
        ``~/.jarvis/breaker.json``.
    rehydrate_on_init
        If True (default), read the file at construction so a freshly
        spawned process inherits an in-flight trip. Disable in tests.
    """

    def __init__(
        self,
        *,
        path: Path | str | None = None,
        rehydrate_on_init: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._path = Path(path) if path else DEFAULT_BREAKER_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if rehydrate_on_init:
            self._refresh_from_disk()

    # --- public API (overridden) ------------------------------------------

    def pre_dispatch(self) -> None:
        """Refresh shared state, then delegate to the in-memory logic."""
        before = self._state
        self._refresh_from_disk()
        super().pre_dispatch()
        after = self._state
        # super().pre_dispatch() may transition OPEN->HALF_OPEN locally,
        # but HALF_OPEN is transient and not persisted -- the transition
        # from disk OPEN will rediscover this on the next read.
        if before != after and after is not BreakerState.HALF_OPEN:
            self._write_shared()

    def record(self, result: TaskResult) -> None:
        """Delegate to parent; persist on any state transition."""
        before = self._state
        super().record(result)
        after = self._state
        if before != after:
            # Transitions we care about: CLOSED->OPEN (fresh trip),
            # HALF_OPEN->OPEN (probe failed), HALF_OPEN->CLOSED (probe
            # succeeded). All require a shared write.
            self._write_shared()

    def reset(self) -> None:
        """Operator manual close. Broadcast to all processes."""
        super().reset()
        self._write_shared()

    # --- disk I/O ---------------------------------------------------------

    def _refresh_from_disk(self) -> None:
        """Adopt state from ``~/.jarvis/breaker.json`` if present and fresh."""
        data = self._read_shared()
        if data is None:
            return  # no shared state, keep in-memory view

        try:
            disk_state = BreakerState(data["state"])
        except (KeyError, ValueError) as exc:
            _LOG.warning("[shared_breaker] unreadable state field: %s", exc)
            return

        if disk_state is BreakerState.OPEN:
            reopen_raw = data.get("reopen_at")
            tripped_raw = data.get("tripped_at")
            if not reopen_raw or not tripped_raw:
                _LOG.warning("[shared_breaker] OPEN missing timestamps; ignoring")
                return
            try:
                reopen_at = datetime.fromisoformat(reopen_raw)
                tripped_at = datetime.fromisoformat(tripped_raw)
            except ValueError as exc:
                _LOG.warning("[shared_breaker] bad timestamps: %s", exc)
                return
            now = self._clock()
            if now >= reopen_at:
                # Cooldown elapsed. Move to HALF_OPEN locally for this
                # process so it can probe. Disk still says OPEN; the
                # first process that completes a probe will overwrite.
                self._state = BreakerState.HALF_OPEN
                self._tripped_at = tripped_at
                self._reopen_at = reopen_at
                self._last_reason = str(data.get("last_reason", ""))
            else:
                self._state = BreakerState.OPEN
                self._tripped_at = tripped_at
                self._reopen_at = reopen_at
                self._last_reason = str(data.get("last_reason", ""))
        elif disk_state is BreakerState.CLOSED:
            # Shared close -- adopt fully, reset counters. This is the
            # operator-reset broadcast case.
            if self._state is not BreakerState.CLOSED:
                self._state = BreakerState.CLOSED
                self._tripped_at = None
                self._reopen_at = None
                self._last_reason = ""
                self._consec_failures = 0
                self._consec_denials = 0

    def _read_shared(self) -> dict[str, object] | None:
        """Return parsed JSON or None if file missing/corrupt."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            _LOG.warning("[shared_breaker] read failed: %s", exc)
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            _LOG.warning("[shared_breaker] bad JSON: %s", exc)
            return None
        if not isinstance(data, dict):
            _LOG.warning("[shared_breaker] root is not an object")
            return None
        ver = data.get("version")
        if ver != SCHEMA_VERSION:
            _LOG.warning("[shared_breaker] schema version %r != %d", ver, SCHEMA_VERSION)
            return None
        return data

    def _write_shared(self) -> None:
        """Atomic write of current breaker snapshot to ``breaker.json``."""
        payload: dict[str, object] = {
            "version": SCHEMA_VERSION,
            "state": self._state.value,
            "tripped_at": self._tripped_at.isoformat() if self._tripped_at else None,
            "reopen_at": self._reopen_at.isoformat() if self._reopen_at else None,
            "last_reason": self._last_reason,
            "written_at": self._clock().isoformat(),
            "writer_pid": os.getpid(),
        }
        # HALF_OPEN is transient and never persists; if the parent put us
        # there (probe in flight), mirror it as OPEN on disk so other
        # processes keep refusing until the probe resolves.
        if self._state is BreakerState.HALF_OPEN:
            payload["state"] = BreakerState.OPEN.value

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Write to temp in same directory (same FS -> os.replace atomic).
            fd, tmp_name = tempfile.mkstemp(
                prefix=".breaker-",
                suffix=".tmp",
                dir=str(self._path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, sort_keys=True, allow_nan=False)
                os.replace(tmp_name, self._path)
            except Exception:
                # Clean up tmp on failure; swallow cleanup errors.
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError as exc:
            # Writing failed but the in-memory breaker is still valid --
            # don't crash the fleet over a dashboard-sync hiccup. Log.
            _LOG.warning("[shared_breaker] write failed: %s", exc)

    # --- introspection ----------------------------------------------------

    @property
    def path(self) -> Path:
        """Path of the shared state file."""
        return self._path


def read_shared_status(path: Path | str | None = None) -> dict[str, object] | None:
    """Read-only helper for the dashboard and CLI.

    Returns ``None`` if the file is missing or unparseable. Callers
    should treat that as "breaker is in an unknown state" (a.k.a. fall
    back to in-memory defaults).
    """
    p = Path(path) if path else DEFAULT_BREAKER_PATH
    try:
        raw = p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def reset_shared(path: Path | str | None = None) -> bool:
    """Operator CLI hook: remove (or mark CLOSED) the shared state file.

    Returns True if the state was changed, False if already clean.
    Writing an explicit CLOSED record is preferred over deleting so
    other processes see "someone just closed this" rather than "the
    file is missing for some reason."
    """
    p = Path(path) if path else DEFAULT_BREAKER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    payload = {
        "version": SCHEMA_VERSION,
        "state": BreakerState.CLOSED.value,
        "tripped_at": None,
        "reopen_at": None,
        "last_reason": "operator_reset",
        "written_at": now,
        "writer_pid": os.getpid(),
    }
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".breaker-",
            suffix=".tmp",
            dir=str(p.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, allow_nan=False)
        os.replace(tmp_name, p)
    except OSError:
        return False
    return True


__all__ = [
    "DEFAULT_BREAKER_PATH",
    "SCHEMA_VERSION",
    "SharedCircuitBreaker",
    "read_shared_status",
    "reset_shared",
]

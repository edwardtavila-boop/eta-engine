"""
EVOLUTIONARY TRADING ALGO  //  core.kill_switch_latch
=========================================
Persistent latch on top of the stateless KillSwitch.

Why this exists
---------------
The stateless ``KillSwitch`` evaluates policy on each tick and returns a
fresh verdict. That means if a global FLATTEN_ALL trips and the process
then restarts (crash, deploy, machine reboot), the kill-switch forgets
and the runtime happily re-arms. For catastrophic verdicts that are
**meant** to require human adjudication (Apex eval preemptive, global
DD/daily-loss kill, operator-pressed e-stop), that's wrong.

The latch closes that hole. It is a small JSON file on disk:

    {
      "state": "TRIPPED",
      "tripped_at_utc": "2026-04-24T17:53:12.194+00:00",
      "reason": "daily loss 6.02% >= cap 6%",
      "scope": "global",
      "action": "FLATTEN_ALL",
      "severity": "CRITICAL",
      "evidence": {"daily_loss_pct": 6.02, "cap_pct": 6.0},
      "cleared_at_utc": null,
      "cleared_by": null
    }

Contract
--------
  * ``boot_allowed()`` -- returns ``(ok, reason)``. On a TRIPPED latch it
    returns ``(False, reason)``. Call this once at runtime startup before
    connecting to the venue.
  * ``record_verdict(v)`` -- idempotent. Called with every ``KillVerdict``.
    If the verdict's action is in ``_LATCHING_ACTIONS`` the latch is set
    TRIPPED (already-tripped stays tripped -- the first trip is
    authoritative). Returns True when this call latched a NEW trip,
    False when the latch was already tripped or the verdict was not
    latch-eligible.
  * ``clear(cleared_by, reason=None)`` -- operator-only reset. Writes
    the latch back to ARMED and stamps ``cleared_at_utc`` /
    ``cleared_by``. Intended to be called from a CLI
    (``python -m eta_engine.scripts.clear_kill_switch``) after the
    operator reviews and confirms.

Why a JSON file, not a DB?
  * The latch MUST survive process crash, not just graceful shutdown.
    A JSON file with ``fsync`` is the simplest possible durable store,
    and a trading runtime does not need higher throughput than that.
  * Atomic writes via rename: write to ``latch.json.tmp`` then
    ``os.replace(tmp, final)``. Avoids leaving a half-written file if
    the process is SIGKILL'd mid-write.

Which verdicts latch?
  * ``FLATTEN_ALL`` (global kill -- portfolio DD / daily-loss cap)
  * ``FLATTEN_TIER_A_PREEMPTIVE`` (apex cushion breached)
  * ``FLATTEN_TIER_B`` (tier-B correlation kill)

The per-bot trip (``FLATTEN_BOT``) is intentionally NOT latched -- it
pauses the offending bot until next session, but other bots keep
trading and the runtime can safely restart. That's a recoverable state,
not an eval-saver.

The soft/info verdicts (``HALVE_SIZE``, ``PAUSE_NEW_ENTRIES``,
``CONTINUE``) are never latched.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eta_engine.core.kill_switch_runtime import KillVerdict

log = logging.getLogger(__name__)


#: Verdict actions that flip the latch to TRIPPED. Anything else is
#: passed through without latching.
_LATCHING_ACTIONS: frozenset[str] = frozenset({
    "FLATTEN_ALL",
    "FLATTEN_TIER_A_PREEMPTIVE",
    "FLATTEN_TIER_B",
})

STATE_ARMED = "ARMED"
STATE_TRIPPED = "TRIPPED"


@dataclass(frozen=True)
class LatchSnapshot:
    """In-memory view of the on-disk latch."""

    state: str
    tripped_at_utc: str | None
    reason: str | None
    scope: str | None
    action: str | None
    severity: str | None
    evidence: dict[str, Any]
    cleared_at_utc: str | None
    cleared_by: str | None

    def is_tripped(self) -> bool:
        return self.state == STATE_TRIPPED

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "tripped_at_utc": self.tripped_at_utc,
            "reason": self.reason,
            "scope": self.scope,
            "action": self.action,
            "severity": self.severity,
            "evidence": dict(self.evidence),
            "cleared_at_utc": self.cleared_at_utc,
            "cleared_by": self.cleared_by,
        }

    @classmethod
    def armed(cls) -> LatchSnapshot:
        return cls(
            state=STATE_ARMED,
            tripped_at_utc=None,
            reason=None,
            scope=None,
            action=None,
            severity=None,
            evidence={},
            cleared_at_utc=None,
            cleared_by=None,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LatchSnapshot:
        return cls(
            state=str(raw.get("state", STATE_ARMED)),
            tripped_at_utc=raw.get("tripped_at_utc"),
            reason=raw.get("reason"),
            scope=raw.get("scope"),
            action=raw.get("action"),
            severity=raw.get("severity"),
            evidence=dict(raw.get("evidence") or {}),
            cleared_at_utc=raw.get("cleared_at_utc"),
            cleared_by=raw.get("cleared_by"),
        )


class KillSwitchLatch:
    """Disk-backed catastrophic-verdict latch.

    Parameters
    ----------
    path:
        File-system path of the latch JSON. Parent directory is
        created if absent. Defaults to ``state/kill_switch_latch.json``
        when callers don't pin a location.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    def snapshot(self) -> LatchSnapshot:
        """Return the current latch state. Missing / unreadable file = ARMED."""
        if not self.path.exists():
            return LatchSnapshot.armed()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning(
                "kill_switch_latch: unreadable file %s (%s); treating as ARMED",
                self.path, exc,
            )
            return LatchSnapshot.armed()
        if not isinstance(raw, dict):
            log.warning(
                "kill_switch_latch: malformed root in %s "
                "(expected object, got %s); treating as ARMED",
                self.path, type(raw).__name__,
            )
            return LatchSnapshot.armed()
        return LatchSnapshot.from_dict(raw)

    def boot_allowed(self) -> tuple[bool, str]:
        """Boot-time gate. Refuse to start when latch is TRIPPED.

        Returns
        -------
        ok:
            True iff the runtime may proceed.
        reason:
            Human-readable reason. Empty string when ``ok`` is True.
        """
        snap = self.snapshot()
        if snap.is_tripped():
            tripped_at = snap.tripped_at_utc or "unknown"
            scope = snap.scope or "unknown"
            action = snap.action or "unknown"
            reason = snap.reason or "no reason recorded"
            msg = (
                f"kill-switch latch TRIPPED at {tripped_at} "
                f"(scope={scope}, action={action}): {reason}. "
                f"Clear with: python -m eta_engine.scripts.clear_kill_switch "
                f"--confirm --operator <your_name>"
            )
            return False, msg
        return True, ""

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    def record_verdict(self, verdict: KillVerdict) -> bool:
        """Idempotently latch a catastrophic verdict.

        Returns True when this call moved the latch from ARMED ->
        TRIPPED; False when the verdict is not latch-eligible OR when
        the latch was already tripped (first trip wins).
        """
        action = getattr(verdict.action, "value", str(verdict.action))
        if action not in _LATCHING_ACTIONS:
            return False
        existing = self.snapshot()
        if existing.is_tripped():
            return False
        severity = getattr(verdict.severity, "value", str(verdict.severity))
        snap = LatchSnapshot(
            state=STATE_TRIPPED,
            tripped_at_utc=datetime.now(UTC).isoformat(),
            reason=str(verdict.reason),
            scope=str(verdict.scope),
            action=action,
            severity=severity,
            evidence=dict(getattr(verdict, "evidence", {}) or {}),
            cleared_at_utc=None,
            cleared_by=None,
        )
        self._write_atomic(snap.to_dict())
        log.critical(
            "kill_switch_latch: TRIPPED action=%s scope=%s reason=%s",
            action, verdict.scope, verdict.reason,
        )
        return True

    def clear(self, cleared_by: str, reason: str | None = None) -> LatchSnapshot:
        """Operator reset. Writes the latch back to ARMED.

        Parameters
        ----------
        cleared_by:
            Operator identifier. Required, non-empty.
        reason:
            Optional clearance reason; preserved alongside the
            cleared-at timestamp for audit.

        Returns
        -------
        LatchSnapshot
            The post-clear ARMED snapshot.
        """
        if not cleared_by or not cleared_by.strip():
            msg = "cleared_by must be a non-empty operator identifier"
            raise ValueError(msg)
        cleared_at = datetime.now(UTC).isoformat()
        snap = LatchSnapshot(
            state=STATE_ARMED,
            tripped_at_utc=None,
            reason=reason,
            scope=None,
            action=None,
            severity=None,
            evidence={},
            cleared_at_utc=cleared_at,
            cleared_by=cleared_by.strip(),
        )
        self._write_atomic(snap.to_dict())
        log.warning(
            "kill_switch_latch: CLEARED by %s at %s",
            snap.cleared_by, cleared_at,
        )
        return snap

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _write_atomic(self, payload: dict[str, Any]) -> None:
        """Write JSON via temp-then-rename so partial writes can't
        corrupt the latch. fsync the data before rename so a crash
        between rename and reboot still surfaces the new state.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(tmp, self.path)


__all__ = [
    "STATE_ARMED",
    "STATE_TRIPPED",
    "KillSwitchLatch",
    "LatchSnapshot",
]

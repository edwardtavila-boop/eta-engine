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
  * ``read()`` -- returns the current :class:`LatchRecord`. Missing
    file = ARMED. Corrupt JSON = **fail-closed TRIPPED** so a trader
    cannot defeat the latch by deleting / mangling the file.
  * ``boot_allowed()`` -- returns ``(ok, reason)``. On a TRIPPED latch
    it returns ``(False, reason)``. Call this once at runtime startup
    before connecting to the venue.
  * ``record_verdict(v)`` -- idempotent. Called with every ``KillVerdict``.
    If the verdict's action is in ``_LATCHING_ACTIONS`` the latch is set
    TRIPPED (already-tripped stays tripped -- the first trip is
    authoritative). Returns True when this call latched a NEW trip,
    False when the latch was already tripped or the verdict was not
    latch-eligible.
  * ``clear(cleared_by, reason=None)`` -- operator-only reset. Writes
    the latch back to ARMED but **preserves the prior trip's audit
    trail** (action / reason / tripped_at_utc / evidence) for post-
    mortem. Stamps ``cleared_at_utc`` / ``cleared_by``. Intended to
    be called from a CLI (``python -m eta_engine.scripts.clear_kill_switch``)
    after the operator reviews and confirms.

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
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eta_engine.core.kill_switch_runtime import KillVerdict

log = logging.getLogger(__name__)


#: Verdict actions that flip the latch to TRIPPED. Anything else is
#: passed through without latching.
_LATCHING_ACTIONS: frozenset[str] = frozenset(
    {
        "FLATTEN_ALL",
        "FLATTEN_TIER_A_PREEMPTIVE",
        "FLATTEN_TIER_B",
    }
)


class LatchState(StrEnum):
    """Discrete latch states. ARMED = trading allowed, TRIPPED = boot refused."""

    ARMED = "ARMED"
    TRIPPED = "TRIPPED"


@dataclass(frozen=True)
class LatchRecord:
    """In-memory view of the on-disk latch.

    The record carries both the current state AND the audit trail of
    the prior trip (when present). This is what makes ``clear()``
    forensically useful: after an operator clears, the record still
    knows what action was taken, when, with what evidence.
    """

    state: LatchState
    tripped_at_utc: str | None
    reason: str | None
    scope: str | None
    action: str | None
    severity: str | None
    evidence: dict[str, Any]
    cleared_at_utc: str | None
    cleared_by: str | None

    def is_tripped(self) -> bool:
        return self.state is LatchState.TRIPPED

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
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
    def armed(cls) -> LatchRecord:
        return cls(
            state=LatchState.ARMED,
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
    def fail_closed_tripped(cls, reason: str) -> LatchRecord:
        """Return a synthetic TRIPPED record for a corrupt/unreadable file.

        Used by ``read()`` to fail-closed when the JSON cannot be
        parsed -- a trader cannot defeat the latch by mangling the
        file; the runtime refuses to boot until the operator either
        repairs the file or runs ``clear()`` to overwrite it cleanly.
        """
        return cls(
            state=LatchState.TRIPPED,
            tripped_at_utc=datetime.now(UTC).isoformat(),
            reason=reason,
            scope="global",
            action="CORRUPT_LATCH",
            severity="CRITICAL",
            evidence={},
            cleared_at_utc=None,
            cleared_by=None,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LatchRecord:
        state_str = str(raw.get("state", LatchState.ARMED.value))
        try:
            state = LatchState(state_str)
        except ValueError:
            state = LatchState.ARMED
        return cls(
            state=state,
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
        created at construction time (mkdir -p).
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        # Eagerly create the parent dir so callers can rely on the
        # path being writable without a separate setup step.
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    def read(self) -> LatchRecord:
        """Return the current latch record.

        Missing file = ARMED (first boot on a clean disk).
        Corrupt JSON or non-object root = **fail-closed TRIPPED**
        with a reason the operator can act on.
        """
        if not self.path.exists():
            return LatchRecord.armed()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error(
                "kill_switch_latch: corrupt latch file %s (%s) -- failing closed (TRIPPED)",
                self.path,
                exc,
            )
            return LatchRecord.fail_closed_tripped(
                f"corrupt latch file at {self.path}: {exc}",
            )
        if not isinstance(raw, dict):
            log.error(
                "kill_switch_latch: malformed root in %s (expected object, got %s) -- failing closed",
                self.path,
                type(raw).__name__,
            )
            return LatchRecord.fail_closed_tripped(
                f"corrupt latch root in {self.path}: not an object",
            )
        return LatchRecord.from_dict(raw)

    def boot_allowed(self) -> tuple[bool, str]:
        """Boot-time gate. Refuse to start when latch is TRIPPED.

        Returns
        -------
        ok:
            True iff the runtime may proceed.
        reason:
            ``"armed"`` on success. On TRIPPED, a multi-line message
            quoting the prior trip's reason + telling the operator
            how to clear via the CLI.
        """
        rec = self.read()
        if rec.is_tripped():
            tripped_at = rec.tripped_at_utc or "unknown"
            scope = rec.scope or "unknown"
            action = rec.action or "unknown"
            reason = rec.reason or "no reason recorded"
            corrupt_hint = "corrupt" if action == "CORRUPT_LATCH" else "TRIPPED"
            msg = (
                f"kill-switch latch {corrupt_hint} at {tripped_at} "
                f"(scope={scope}, action={action}): {reason}. "
                f"Clear with: python -m eta_engine.scripts.clear_kill_switch "
                f"--confirm --operator <your_name>"
            )
            return False, msg
        return True, "armed"

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
        existing = self.read()
        if existing.is_tripped():
            return False
        severity = getattr(verdict.severity, "value", str(verdict.severity))
        rec = LatchRecord(
            state=LatchState.TRIPPED,
            tripped_at_utc=datetime.now(UTC).isoformat(),
            reason=str(verdict.reason),
            scope=str(verdict.scope),
            action=action,
            severity=severity,
            evidence=dict(getattr(verdict, "evidence", {}) or {}),
            cleared_at_utc=None,
            cleared_by=None,
        )
        self._write_atomic(rec.to_dict())
        log.critical(
            "kill_switch_latch: TRIPPED action=%s scope=%s reason=%s",
            action,
            verdict.scope,
            verdict.reason,
        )
        return True

    def clear(self, cleared_by: str, reason: str | None = None) -> LatchRecord:
        """Operator reset. Writes the latch back to ARMED while
        **preserving the prior trip's audit trail** (action, reason,
        tripped_at_utc, evidence) for post-mortem.

        Parameters
        ----------
        cleared_by:
            Operator identifier. Required, non-empty.
        reason:
            Optional clearance reason. Currently unused in the
            persisted record (the audit trail focuses on the trip
            metadata) but accepted for forward-compat.

        Returns
        -------
        LatchRecord
            The post-clear ARMED record. The prior trip's
            ``action`` / ``reason`` / ``tripped_at_utc`` /
            ``evidence`` survive; ``cleared_at_utc`` /
            ``cleared_by`` are stamped.
        """
        _ = reason  # forward-compat; not yet persisted
        if not cleared_by or not cleared_by.strip():
            msg = "cleared_by must be a non-empty operator identifier"
            raise ValueError(msg)

        # Read the prior trip metadata (if any) so we can preserve it.
        prior = self.read()
        cleared_at = datetime.now(UTC).isoformat()
        rec = LatchRecord(
            state=LatchState.ARMED,
            # Preserve trip audit trail across the clear, EXCEPT for
            # the synthetic CORRUPT_LATCH record (those carry no real
            # audit data; clearing a corrupt file should not leave a
            # fake "CORRUPT_LATCH" trip in the post-clear record).
            tripped_at_utc=(prior.tripped_at_utc if prior.action != "CORRUPT_LATCH" else None),
            reason=(prior.reason if prior.action != "CORRUPT_LATCH" else None),
            scope=(prior.scope if prior.action != "CORRUPT_LATCH" else None),
            action=(prior.action if prior.action != "CORRUPT_LATCH" else None),
            severity=(prior.severity if prior.action != "CORRUPT_LATCH" else None),
            evidence=(dict(prior.evidence) if prior.action != "CORRUPT_LATCH" else {}),
            cleared_at_utc=cleared_at,
            cleared_by=cleared_by.strip(),
        )
        self._write_atomic(rec.to_dict())
        log.warning(
            "kill_switch_latch: CLEARED by %s at %s",
            rec.cleared_by,
            cleared_at,
        )
        return rec

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
    "KillSwitchLatch",
    "LatchRecord",
    "LatchState",
]

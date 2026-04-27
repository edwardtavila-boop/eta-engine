"""
EVOLUTIONARY TRADING ALGO  //  core.trailing_dd_tracker
===========================================
Tick-granular trailing-drawdown tracker for Apex Trader Funding eval
accounts.

Why this exists
---------------
The stateless ``KillSwitch._check_apex_preemptive`` consumes an
``ApexEvalSnapshot(trailing_dd_limit_usd, distance_to_limit_usd)``.
Historically that snapshot was built once per bar from the bot-level
``equity_usd`` / ``peak_equity_usd`` pair -- i.e. bar-close only.

Apex's trailing max drawdown does NOT check at bar close. If live
mark-to-market equity prints below the trailing floor at any tick,
the eval is bust. A bar-close-only tracker is therefore one
``high-wick-then-recover`` bar away from a silent eval failure.

This module closes that gap. It:

  * accepts equity updates at arbitrary frequency (tick, second, bar),
  * maintains a peak-equity HWM that only moves up,
  * applies the Apex freeze rule --
    once ``peak >= starting_balance + trailing_dd_cap`` the trailing
    floor LOCKS at ``starting_balance`` permanently,
  * persists ``(peak, frozen, starting_balance, trailing_dd_cap)`` to
    a JSON file (atomic write), so a process crash does not silently
    reset the HWM on restart,
  * emits a fresh ``ApexEvalSnapshot`` on every update, ready to be
    handed to ``KillSwitch.evaluate(apex_eval=...)``.

The tracker is *pure policy*. It does not read from any venue or
submit any order. It is a deterministic function of the equity stream
and its persisted state. Integration with the live runtime is a
one-line swap in ``scripts.run_eta_live.build_apex_eval_snapshot``.

Durability contract
-------------------
  * Atomic writes via ``write-to-tmp + os.replace``. The persisted
    file is never left half-written even under SIGKILL mid-update.
  * Fail-closed on corrupt file -- on JSON decode error, the tracker
    raises ``TrailingDDCorruptError`` rather than silently rebuilding
    from the current tick. If the HWM is gone, the correct move is
    manual operator review, not an implicit reset that could mask a
    prior breach.
  * ``reset(starting_balance_usd)`` is the ONLY path that clears the
    HWM. It's intended for a fresh eval account, never for crash
    recovery.

Usage
-----
    tracker = TrailingDDTracker.load_or_init(
        path=Path("state/apex_trailing_dd.json"),
        starting_balance_usd=50_000.0,
        trailing_dd_cap_usd=2_500.0,
    )
    snapshot = tracker.update(current_equity_usd=51_234.50)
    # -> ApexEvalSnapshot(trailing_dd_limit_usd=2500.0, distance_to_limit_usd=...)
    verdicts = kill_switch.evaluate(apex_eval=snapshot, ...)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.core.kill_switch_runtime import ApexEvalSnapshot

log = logging.getLogger(__name__)


class TrailingDDCorruptError(RuntimeError):
    """Raised when the persisted trailing-DD state file is unparseable."""


class ResetNotAcknowledgedError(RuntimeError):
    """Raised when ``reset()`` is called without ``acknowledge_destruction=True``.

    R3 closure: the reset path is destructive and was previously a single
    function call away from obliterating the frozen-floor invariant. This
    guard forces operator intent to be explicit: the caller must name
    themselves AND pass ``acknowledge_destruction=True`` to proceed.
    """


# ---------------------------------------------------------------------------
# R3 closure -- append-only audit log
# ---------------------------------------------------------------------------
# An append-only JSONL sidecar at ``<state_path>.audit.jsonl`` capturing
# every state-changing event on the tracker: init, load, freeze, breach,
# reset. Survives deletion or re-initialization of the state file so a
# forensic review can reconstruct whether a frozen floor existed and was
# lost. Each event carries a monotonic sequence number; gaps in the
# sequence signal operator tampering.


class TrailingDDAuditLog:
    """Append-only JSONL audit log sidecar for ``TrailingDDTracker``.

    The log is never rotated, compacted, or rewritten by this module.
    Each event is a single JSON object on its own line with keys:

      * ``seq``          -- 1-indexed monotonic sequence number
      * ``ts``           -- ISO UTC timestamp
      * ``event``        -- one of: init, load, freeze, breach, reset
      * ``state``        -- snapshot of tracker state at the moment
      * plus event-specific metadata (operator, reason, etc.)

    Appends are fsynced so an OS crash mid-append does not lose the
    most recent entry.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # R3 chaos-hardening: the write may succeed while fsync fails
        # (read-only mount that allowed the open+write, OneDrive reparse
        # that accepted the buffered write then rejected the sync,
        # network share that silently no-ops fsync). The write is still
        # reflected in the OS buffer cache, so the tracker continues;
        # we just lose the per-append durability guarantee. Counter is
        # exposed so health probes / preflight-followups can detect a
        # degraded volume without parsing logs.
        self.fsync_failure_count: int = 0
        # Public alias used by tests / health probes: last error message
        # from the most recent fsync failure (empty string = clean).
        self.last_fsync_error: str = ""

    def _next_seq(self) -> int:
        if not self.path.exists():
            return 1
        # Count lines cheaply; the log grows at most ~few/day so this is fine.
        try:
            with self.path.open("rb") as fh:
                return sum(1 for _ in fh) + 1
        except OSError:
            log.warning(
                "unable to read audit log %s for seq -- starting at 1",
                self.path,
                exc_info=True,
            )
            return 1

    def append(
        self,
        event: str,
        state: dict[str, Any],
        **extra: object,
    ) -> None:
        record = {
            "seq": self._next_seq(),
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "state": state,
            **extra,
        }
        line = json.dumps(record, sort_keys=True) + "\n"
        # Open in "a" mode to avoid ever truncating existing content.
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                # R3 chaos-hardening: upgrade from DEBUG (invisible) to
                # WARNING + counter. The write has already hit the OS
                # buffer so the tracker can continue; we just lose the
                # per-append durability guarantee. The counter lets a
                # health probe decide whether to pause or alert.
                self.fsync_failure_count += 1
                self.last_fsync_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "audit fsync failed for %s (count=%d, err=%s) -- "
                    "runtime continues but durability of the most "
                    "recent %d audit records is not guaranteed",
                    self.path,
                    self.fsync_failure_count,
                    self.last_fsync_error,
                    self.fsync_failure_count,
                )

    def read_all(self) -> list[dict[str, Any]]:
        """Return every event in order. For forensic inspection / tests."""
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    log.warning("skipping corrupt audit line: %r", ln[:80])
        return out


@dataclass
class TrailingDDState:
    """Full persisted state for the tracker.

    Attributes
    ----------
    starting_balance_usd:
        Account baseline at eval start (e.g. 50_000.0).
    trailing_dd_cap_usd:
        Trailing distance Apex enforces (e.g. 2_500.0).
    peak_equity_usd:
        Running high-water mark. Only ever moves up until ``frozen``.
    frozen:
        True once peak reached ``starting + cap``. The floor then
        locks at ``starting_balance_usd`` and stops trailing upward.
    last_equity_usd:
        Last observed equity mark (diagnostic only).
    last_update_utc:
        ISO timestamp of the last update (diagnostic only).
    breach_count:
        How many updates have observed equity <= floor. Non-zero means
        the runtime should have already flattened; kept for forensics.
    """

    starting_balance_usd: float
    trailing_dd_cap_usd: float
    peak_equity_usd: float
    frozen: bool = False
    last_equity_usd: float | None = None
    last_update_utc: str | None = None
    breach_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class TrailingDDTracker:
    """Tick-granular Apex trailing-DD tracker with durable state."""

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        path: Path,
        state: TrailingDDState,
        *,
        audit_log_path: Path | None = None,
    ) -> None:
        if state.starting_balance_usd <= 0:
            msg = f"starting_balance_usd must be > 0 (got {state.starting_balance_usd})"
            raise ValueError(msg)
        if state.trailing_dd_cap_usd <= 0:
            msg = f"trailing_dd_cap_usd must be > 0 (got {state.trailing_dd_cap_usd})"
            raise ValueError(msg)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = state
        # R3 closure: attach an append-only audit log sidecar. Default
        # colocates the audit file with the state file so a single
        # directory contains everything a forensic reviewer needs.
        if audit_log_path is None:
            audit_log_path = Path(str(self.path) + ".audit.jsonl")
        self._audit_path = Path(audit_log_path)
        self._audit = TrailingDDAuditLog(self._audit_path)

    @classmethod
    def load_or_init(
        cls,
        path: Path,
        starting_balance_usd: float,
        trailing_dd_cap_usd: float,
        *,
        audit_log_path: Path | None = None,
    ) -> TrailingDDTracker:
        """Load state from ``path`` or initialize fresh state.

        On first call the peak is seeded to ``starting_balance_usd``
        (no prior trades, no drawdown yet) and the file is written.

        Raises
        ------
        TrailingDDCorruptError
            If the existing file is unparseable. Operator must
            investigate rather than silently reset.
        ValueError
            If the loaded file's baselines disagree with the provided
            baselines. Protects against accidentally re-pointing a
            $50K eval tracker at $150K-eval state.
        """
        p = Path(path)
        if not p.exists():
            state = TrailingDDState(
                starting_balance_usd=starting_balance_usd,
                trailing_dd_cap_usd=trailing_dd_cap_usd,
                peak_equity_usd=starting_balance_usd,
                frozen=False,
            )
            tracker = cls(p, state, audit_log_path=audit_log_path)
            tracker._write_atomic()
            tracker._audit.append(
                "init",
                tracker._state.as_dict(),
                reason="fresh_state_file_created",
            )
            return tracker

        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            msg = (
                f"trailing-DD state file corrupt at {p}: {exc}. "
                f"Manual operator review required -- do NOT delete this "
                f"file without verifying the eval is not already bust."
            )
            raise TrailingDDCorruptError(msg) from exc

        loaded_start = float(raw.get("starting_balance_usd", 0.0))
        loaded_cap = float(raw.get("trailing_dd_cap_usd", 0.0))
        if abs(loaded_start - starting_balance_usd) > 1e-6:
            msg = (
                f"trailing-DD baseline mismatch at {p}: "
                f"loaded starting_balance={loaded_start}, "
                f"requested={starting_balance_usd}. "
                f"Refusing to silently re-point tracker at new eval."
            )
            raise ValueError(msg)
        if abs(loaded_cap - trailing_dd_cap_usd) > 1e-6:
            msg = (
                f"trailing-DD cap mismatch at {p}: "
                f"loaded cap={loaded_cap}, requested={trailing_dd_cap_usd}. "
                f"Refusing to silently re-point tracker at new eval."
            )
            raise ValueError(msg)

        state = TrailingDDState(
            starting_balance_usd=loaded_start,
            trailing_dd_cap_usd=loaded_cap,
            peak_equity_usd=float(raw.get("peak_equity_usd", loaded_start)),
            frozen=bool(raw.get("frozen", False)),
            last_equity_usd=(float(raw["last_equity_usd"]) if raw.get("last_equity_usd") is not None else None),
            last_update_utc=raw.get("last_update_utc"),
            breach_count=int(raw.get("breach_count", 0)),
        )
        tracker = cls(p, state, audit_log_path=audit_log_path)
        tracker._audit.append(
            "load",
            tracker._state.as_dict(),
            reason="existing_state_file_loaded",
        )
        return tracker

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _write_atomic(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(self._state.as_dict(), indent=2, sort_keys=True)
        tmp.write_text(payload, encoding="utf-8")
        try:
            with tmp.open("rb") as fh:
                os.fsync(fh.fileno())
        except OSError:  # pragma: no cover - platform-dependent
            log.debug("fsync failed for %s (continuing)", tmp, exc_info=True)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #
    # Read accessors
    # ------------------------------------------------------------------ #
    def state(self) -> TrailingDDState:
        """Return the full persisted state (copy-free; do not mutate)."""
        return self._state

    def floor_usd(self) -> float:
        """Trailing floor that live equity must stay above.

        While not frozen:
            floor = peak - cap

        After freeze:
            floor = starting_balance_usd  (Apex rule)
        """
        s = self._state
        if s.frozen:
            return s.starting_balance_usd
        return s.peak_equity_usd - s.trailing_dd_cap_usd

    def freeze_threshold_usd(self) -> float:
        """Peak level at which the trailing floor locks.

        For a standard 50K/2500 eval this is 52_500. Once peak reaches
        this level, the floor freezes at starting_balance (50_000) and
        stops trailing upward.
        """
        s = self._state
        return s.starting_balance_usd + s.trailing_dd_cap_usd

    def snapshot(self) -> ApexEvalSnapshot:
        """Emit an ``ApexEvalSnapshot`` reflecting the *current* state.

        ``distance_to_limit_usd`` is measured against the *last observed*
        equity -- i.e. the most recent ``update()`` call. If ``update()``
        has not yet been called, distance is measured against the peak
        (equivalent: full cap).
        """
        s = self._state
        mark = s.last_equity_usd if s.last_equity_usd is not None else s.peak_equity_usd
        distance = max(0.0, mark - self.floor_usd())
        return ApexEvalSnapshot(
            trailing_dd_limit_usd=s.trailing_dd_cap_usd,
            distance_to_limit_usd=distance,
        )

    # ------------------------------------------------------------------ #
    # Update path (the tick path)
    # ------------------------------------------------------------------ #
    def update(
        self,
        current_equity_usd: float,
        ts: datetime | None = None,
    ) -> ApexEvalSnapshot:
        """Feed a fresh equity mark, return the resulting snapshot.

        Parameters
        ----------
        current_equity_usd:
            Mark-to-market equity at the tick (not the bar close).
        ts:
            Optional timestamp for diagnostics. Defaults to ``utcnow``.

        Behavior
        --------
          * Updates ``peak_equity_usd`` if ``current_equity_usd`` is a
            new high (and the tracker is not yet frozen).
          * Applies freeze rule: if the new peak reaches the freeze
            threshold, the tracker transitions to ``frozen=True`` and
            the floor locks.
          * Increments ``breach_count`` when ``current_equity_usd`` has
            already breached the floor (forensic counter only; the
            runtime is expected to have already reacted via the
            KillSwitch path).
          * Writes state to disk on every call. Frequency is bounded
            by update cadence; a fsync+rename per tick is cheap
            relative to any venue roundtrip.
        """
        s = self._state
        was_frozen = s.frozen
        if not s.frozen and current_equity_usd > s.peak_equity_usd:
            s.peak_equity_usd = current_equity_usd
        # Evaluate freeze after peak update.
        if not s.frozen and s.peak_equity_usd >= self.freeze_threshold_usd():
            s.frozen = True

        s.last_equity_usd = current_equity_usd
        s.last_update_utc = (ts or datetime.now(UTC)).isoformat()
        breached = current_equity_usd <= self.floor_usd()
        if breached:
            s.breach_count += 1
            log.critical(
                "trailing-DD FLOOR BREACHED: equity=%.2f floor=%.2f peak=%.2f frozen=%s breach_count=%d",
                current_equity_usd,
                self.floor_usd(),
                s.peak_equity_usd,
                s.frozen,
                s.breach_count,
            )
        self._write_atomic()
        # R3 closure: emit audit events AFTER state is durably on disk so
        # the audit entry can never claim a state the state-file didn't
        # reach. freeze is logged exactly once at the transition; breach
        # is logged every tick below floor for forensic reconstruction.
        if not was_frozen and s.frozen:
            self._audit.append(
                "freeze",
                s.as_dict(),
                freeze_threshold_usd=self.freeze_threshold_usd(),
                locked_floor_usd=s.starting_balance_usd,
            )
        if breached:
            self._audit.append(
                "breach",
                s.as_dict(),
                equity_usd=current_equity_usd,
                floor_usd=self.floor_usd(),
            )
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Operator reset
    # ------------------------------------------------------------------ #
    def reset(
        self,
        starting_balance_usd: float,
        *,
        operator: str,
        acknowledge_destruction: bool = False,
        reason: str = "",
    ) -> None:
        """Replace state with a fresh tracker at a new baseline.

        Intended for a new eval account. This is destructive: the
        prior peak and breach count are gone.

        R3 closure: caller MUST set ``acknowledge_destruction=True`` and
        name themselves via ``operator`` so the audit log can attribute
        the reset. A reset without the ack raises
        ``ResetNotAcknowledgedError`` -- prevents accidental clearing of
        a frozen-floor tracker via a stray ``reset()`` call in tooling.

        Parameters
        ----------
        starting_balance_usd:
            New account baseline (e.g. 50_000.0 for a fresh eval).
        operator:
            Name / identifier of the person initiating the reset.
            Recorded verbatim in the audit log. REQUIRED.
        acknowledge_destruction:
            Must be ``True``. Forces the caller to think about the fact
            that prior peak / freeze / breach state is being wiped.
        reason:
            Free-form rationale (e.g. "new 50K eval, prior account
            failed consistency"). Recorded in the audit log.
        """
        if not acknowledge_destruction:
            msg = (
                "TrailingDDTracker.reset() is destructive. "
                "Pass acknowledge_destruction=True explicitly; "
                "this clears the HWM, freeze flag, and breach count. "
                "If the eval is still live, resetting may MASK a prior "
                "breach -- verify with the audit log first."
            )
            raise ResetNotAcknowledgedError(msg)
        if not operator or not operator.strip():
            msg = "reset() requires a non-empty 'operator' identifier."
            raise ValueError(msg)
        if starting_balance_usd <= 0:
            msg = f"starting_balance_usd must be > 0 (got {starting_balance_usd})"
            raise ValueError(msg)
        prior_state = self._state.as_dict()
        self._state = TrailingDDState(
            starting_balance_usd=starting_balance_usd,
            trailing_dd_cap_usd=self._state.trailing_dd_cap_usd,
            peak_equity_usd=starting_balance_usd,
            frozen=False,
            last_equity_usd=None,
            last_update_utc=None,
            breach_count=0,
        )
        self._write_atomic()
        # The audit log is intentionally written AFTER state is durable,
        # and the event carries BOTH the prior and new state so a forensic
        # reviewer can see what was destroyed.
        self._audit.append(
            "reset",
            self._state.as_dict(),
            prior_state=prior_state,
            operator=operator,
            reason=reason,
        )
        log.warning(
            "trailing-DD tracker RESET: operator=%s new starting_balance=%.2f cap=%.2f reason=%r",
            operator,
            starting_balance_usd,
            self._state.trailing_dd_cap_usd,
            reason,
        )


__all__ = [
    "ResetNotAcknowledgedError",
    "TrailingDDAuditLog",
    "TrailingDDCorruptError",
    "TrailingDDState",
    "TrailingDDTracker",
]

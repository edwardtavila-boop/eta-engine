"""
APEX PREDATOR  //  core.trailing_dd_tracker
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
one-line swap in ``scripts.run_apex_live.build_apex_eval_snapshot``.

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

from apex_predator.core.kill_switch_runtime import ApexEvalSnapshot

log = logging.getLogger(__name__)


class TrailingDDCorruptError(RuntimeError):
    """Raised when the persisted trailing-DD state file is unparseable."""


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
    def __init__(self, path: Path, state: TrailingDDState) -> None:
        if state.starting_balance_usd <= 0:
            msg = (
                "starting_balance_usd must be > 0 "
                f"(got {state.starting_balance_usd})"
            )
            raise ValueError(msg)
        if state.trailing_dd_cap_usd <= 0:
            msg = (
                "trailing_dd_cap_usd must be > 0 "
                f"(got {state.trailing_dd_cap_usd})"
            )
            raise ValueError(msg)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = state

    @classmethod
    def load_or_init(
        cls,
        path: Path,
        starting_balance_usd: float,
        trailing_dd_cap_usd: float,
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
            tracker = cls(p, state)
            tracker._write_atomic()
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
            last_equity_usd=(
                float(raw["last_equity_usd"])
                if raw.get("last_equity_usd") is not None
                else None
            ),
            last_update_utc=raw.get("last_update_utc"),
            breach_count=int(raw.get("breach_count", 0)),
        )
        return cls(p, state)

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
        if not s.frozen and current_equity_usd > s.peak_equity_usd:
            s.peak_equity_usd = current_equity_usd
        # Evaluate freeze after peak update.
        if not s.frozen and s.peak_equity_usd >= self.freeze_threshold_usd():
            s.frozen = True

        s.last_equity_usd = current_equity_usd
        s.last_update_utc = (ts or datetime.now(UTC)).isoformat()
        if current_equity_usd <= self.floor_usd():
            s.breach_count += 1
            log.critical(
                "trailing-DD FLOOR BREACHED: equity=%.2f floor=%.2f "
                "peak=%.2f frozen=%s breach_count=%d",
                current_equity_usd, self.floor_usd(), s.peak_equity_usd,
                s.frozen, s.breach_count,
            )
        self._write_atomic()
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Operator reset
    # ------------------------------------------------------------------ #
    def reset(self, starting_balance_usd: float) -> None:
        """Replace state with a fresh tracker at a new baseline.

        Intended for a new eval account. This is destructive: the
        prior peak and breach count are gone.
        """
        if starting_balance_usd <= 0:
            msg = (
                "starting_balance_usd must be > 0 "
                f"(got {starting_balance_usd})"
            )
            raise ValueError(msg)
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
        log.warning(
            "trailing-DD tracker RESET: new starting_balance=%.2f cap=%.2f",
            starting_balance_usd, self._state.trailing_dd_cap_usd,
        )


__all__ = [
    "TrailingDDCorruptError",
    "TrailingDDState",
    "TrailingDDTracker",
]

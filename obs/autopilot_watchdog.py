"""
EVOLUTIONARY TRADING ALGO  //  obs.autopilot_watchdog
=========================================
"Never on autopilot" enforcement, operationally.

Why this exists
---------------
A position left running for 2 hours with no human eyes on it is autopilot.
The watchdog watches every open position and:

  * If no ack for ``ack_ttl_sec`` seconds -> escalate to REQUIRE_ACK.
  * If no ack for ``tighten_after_sec`` seconds -> tighten trailing stop
    by ``tighten_factor``.
  * If no ack for ``max_age_sec`` seconds -> force flatten.

Ack = operator confirmed "I'm still watching this". A one-click action.
Emits WatchdogAlert events, optionally writing to DecisionJournal.

Jarvis admin integration
------------------------
If a ``JarvisAdmin`` is supplied at construction time, the watchdog reports
its FORCE_FLATTEN intentions to Jarvis for approval. This is the "everyone
reports to Jarvis" architecture -- even protective subsystems route through
the central authority so every action is logged in the command audit trail.

Public API
----------
  * ``PositionState``           -- snapshot of an open position
  * ``WatchdogPolicy``          -- tunables
  * ``WatchdogAlertLevel``      -- REQUIRE_ACK / TIGHTEN_STOP / FORCE_FLATTEN
  * ``WatchdogAlert``           -- one alert with suggested action
  * ``AutopilotMode``           -- ACTIVE / REQUIRE_ACK / FROZEN
  * ``AutopilotWatchdog``       -- main watcher
"""

from __future__ import annotations

from datetime import UTC, datetime  # noqa: TC003  -- pydantic needs runtime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.brain.jarvis_admin import ActionResponse, JarvisAdmin
    from eta_engine.obs.decision_journal import DecisionJournal


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class WatchdogAlertLevel(StrEnum):
    REQUIRE_ACK = "REQUIRE_ACK"
    TIGHTEN_STOP = "TIGHTEN_STOP"
    FORCE_FLATTEN = "FORCE_FLATTEN"


class AutopilotMode(StrEnum):
    ACTIVE = "ACTIVE"  # no pending alerts
    REQUIRE_ACK = "REQUIRE_ACK"  # one or more positions need ack
    FROZEN = "FROZEN"  # hard stop -- something was flattened


class PositionState(BaseModel):
    trade_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    opened_at: datetime
    last_ack_at: datetime
    current_stop_distance: float = Field(
        gt=0.0,
        description="Distance of current stop from entry, in R or points.",
    )
    open_r: float = Field(
        default=0.0,
        description="Unrealized R. Negative = underwater.",
    )


class WatchdogPolicy(BaseModel):
    ack_ttl_sec: float = Field(
        default=1800.0,
        gt=0.0,
        description="Seconds without an ack before REQUIRE_ACK triggers.",
    )
    tighten_after_sec: float = Field(
        default=3600.0,
        gt=0.0,
        description="Seconds without an ack before suggest tightening stop.",
    )
    tighten_factor: float = Field(
        default=0.75,
        gt=0.0,
        lt=1.0,
        description="Multiply current stop distance by this -- 0.75 = tighten 25%.",
    )
    max_age_sec: float = Field(
        default=7200.0,
        gt=0.0,
        description="Seconds without an ack before FORCE_FLATTEN.",
    )

    def validate_ordering(self) -> WatchdogPolicy:
        if not (self.ack_ttl_sec < self.tighten_after_sec < self.max_age_sec):
            raise ValueError(
                "policy must satisfy ack_ttl < tighten_after < max_age",
            )
        return self


class WatchdogAlert(BaseModel):
    ts: datetime
    trade_id: str
    symbol: str
    level: WatchdogAlertLevel
    reason: str
    seconds_since_ack: float = Field(ge=0.0)
    suggested_stop_distance: float | None = Field(default=None)


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class AutopilotWatchdog:
    """Tracks open positions, emits escalating alerts as staleness grows.

    Parameters
    ----------
    policy: WatchdogPolicy
    journal: optional DecisionJournal to also append alerts to
    clock: callable returning current datetime (for tests)
    """

    def __init__(
        self,
        *,
        policy: WatchdogPolicy | None = None,
        journal: DecisionJournal | None = None,
        clock: Callable[[], datetime] | None = None,
        admin: JarvisAdmin | None = None,
    ) -> None:
        p = policy or WatchdogPolicy()
        p.validate_ordering()
        self._policy = p
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(UTC))
        self._admin = admin
        self._positions: dict[str, PositionState] = {}
        self._flattened: set[str] = set()

    # --- registration ------------------------------------------------------

    def register_position(self, state: PositionState) -> None:
        self._positions[state.trade_id] = state
        self._flattened.discard(state.trade_id)

    def remove_position(self, trade_id: str) -> None:
        """Call when a position closes naturally (stop or target hit)."""
        self._positions.pop(trade_id, None)
        self._flattened.discard(trade_id)

    def ack(self, trade_id: str) -> None:
        """Operator confirmed they're still watching. Resets staleness clock."""
        if trade_id not in self._positions:
            raise KeyError(f"unknown position {trade_id!r}")
        cur = self._positions[trade_id]
        self._positions[trade_id] = cur.model_copy(
            update={"last_ack_at": self._clock()},
        )

    # --- checking ----------------------------------------------------------

    def check_all(self) -> list[WatchdogAlert]:
        """Walk all registered positions, emit alerts for any stale ones.
        One alert per position per call (the highest-severity alert).
        """
        now = self._clock()
        alerts: list[WatchdogAlert] = []
        for tid, state in list(self._positions.items()):
            elapsed = (now - state.last_ack_at).total_seconds()
            alert = self._alert_for(state, elapsed, now)
            if alert is None:
                continue
            alerts.append(alert)
            self._record_alert(alert)
            if alert.level == WatchdogAlertLevel.FORCE_FLATTEN:
                self._flattened.add(tid)
        return alerts

    def mode(self) -> AutopilotMode:
        if self._flattened:
            return AutopilotMode.FROZEN
        if not self._positions:
            return AutopilotMode.ACTIVE
        now = self._clock()
        for state in self._positions.values():
            elapsed = (now - state.last_ack_at).total_seconds()
            if elapsed >= self._policy.ack_ttl_sec:
                return AutopilotMode.REQUIRE_ACK
        return AutopilotMode.ACTIVE

    # --- helpers -----------------------------------------------------------

    def _alert_for(
        self,
        state: PositionState,
        elapsed: float,
        now: datetime,
    ) -> WatchdogAlert | None:
        p = self._policy
        if elapsed >= p.max_age_sec:
            return WatchdogAlert(
                ts=now,
                trade_id=state.trade_id,
                symbol=state.symbol,
                level=WatchdogAlertLevel.FORCE_FLATTEN,
                reason=(f"position idle {elapsed:.0f}s >= max_age {p.max_age_sec:.0f}s -- flatten immediately"),
                seconds_since_ack=elapsed,
                suggested_stop_distance=None,
            )
        if elapsed >= p.tighten_after_sec:
            new_dist = state.current_stop_distance * p.tighten_factor
            return WatchdogAlert(
                ts=now,
                trade_id=state.trade_id,
                symbol=state.symbol,
                level=WatchdogAlertLevel.TIGHTEN_STOP,
                reason=(
                    f"position idle {elapsed:.0f}s >= tighten_after "
                    f"{p.tighten_after_sec:.0f}s -- tighten stop by "
                    f"{(1.0 - p.tighten_factor):.0%}"
                ),
                seconds_since_ack=elapsed,
                suggested_stop_distance=round(new_dist, 4),
            )
        if elapsed >= p.ack_ttl_sec:
            return WatchdogAlert(
                ts=now,
                trade_id=state.trade_id,
                symbol=state.symbol,
                level=WatchdogAlertLevel.REQUIRE_ACK,
                reason=(f"position idle {elapsed:.0f}s >= ack_ttl {p.ack_ttl_sec:.0f}s -- operator ack required"),
                seconds_since_ack=elapsed,
                suggested_stop_distance=None,
            )
        return None

    def _record_alert(self, alert: WatchdogAlert) -> None:
        if self._journal is None:
            return
        # Deferred import to avoid cycles at module load.
        from eta_engine.obs.decision_journal import (  # noqa: PLC0415
            Actor,
            Outcome,
        )

        outcome = Outcome.EXECUTED if alert.level == WatchdogAlertLevel.FORCE_FLATTEN else Outcome.NOTED
        self._journal.record(
            actor=Actor.WATCHDOG,
            intent=f"watchdog:{alert.level}:{alert.trade_id}",
            rationale=alert.reason,
            outcome=outcome,
            links=[alert.trade_id],
            metadata={
                "symbol": alert.symbol,
                "seconds_since_ack": str(alert.seconds_since_ack),
                "suggested_stop_distance": (
                    str(alert.suggested_stop_distance) if alert.suggested_stop_distance is not None else "none"
                ),
            },
            ts=alert.ts,
        )

    # --- Jarvis admin integration ------------------------------------------

    def request_flatten_approval(
        self,
        alert: WatchdogAlert,
    ) -> ActionResponse:
        """Ask Jarvis to approve a FORCE_FLATTEN on the position.

        POSITION_FLATTEN is an exit-only action, so Jarvis should always
        approve -- but routing through the admin produces an audit trail entry
        and gives Jarvis a chance to attach conditions (e.g. staged exit).

        Raises
        ------
        RuntimeError
            If no admin was wired at construction time.
        ValueError
            If the alert isn't a FORCE_FLATTEN alert.
        """
        if self._admin is None:
            raise RuntimeError(
                "watchdog has no JarvisAdmin wired; construct with admin=JarvisAdmin(...) first",
            )
        if alert.level != WatchdogAlertLevel.FORCE_FLATTEN:
            raise ValueError(
                f"request_flatten_approval only applies to FORCE_FLATTEN; got {alert.level.value}",
            )
        # Deferred import to avoid cycles at module load.
        from eta_engine.brain.jarvis_admin import (  # noqa: PLC0415
            ActionType,
            SubsystemId,
            make_action_request,
        )

        req = make_action_request(
            subsystem=SubsystemId.AUTOPILOT_WATCHDOG,
            action=ActionType.POSITION_FLATTEN,
            rationale=alert.reason,
            trade_id=alert.trade_id,
            symbol=alert.symbol,
            seconds_since_ack=alert.seconds_since_ack,
        )
        return self._admin.request_approval(req)

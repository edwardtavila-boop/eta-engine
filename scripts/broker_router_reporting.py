from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from eta_engine.obs.decision_journal import Actor, Outcome

if TYPE_CHECKING:
    from collections import deque
    from logging import Logger


class BrokerRouterReporting:
    """Own broker-router recent-event and journal side effects."""

    def __init__(
        self,
        *,
        recent_events: deque[dict[str, Any]],
        journal: Any,
        logger: Logger,
    ) -> None:
        self._recent_events = recent_events
        self._journal = journal
        self._logger = logger

    def record_event(self, filename: str, kind: str, detail: str) -> None:
        self._recent_events.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "file": filename,
                "kind": kind,
                "detail": detail,
            }
        )

    def safe_journal(
        self,
        *,
        actor: Actor,
        intent: str,
        rationale: str = "",
        gate_checks: list[str] | None = None,
        outcome: Outcome = Outcome.NOTED,
        links: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append to the journal; failures are logged, not raised."""
        try:
            self._journal.record(
                actor=actor,
                intent=intent,
                rationale=rationale,
                gate_checks=gate_checks or [],
                outcome=outcome,
                links=links or [],
                metadata=metadata or {},
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("journal append failed (intent=%s): %s", intent, exc)

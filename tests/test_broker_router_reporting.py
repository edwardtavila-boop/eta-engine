from __future__ import annotations

import logging
from collections import deque
from typing import Any

from eta_engine.obs.decision_journal import Actor, Outcome
from eta_engine.scripts.broker_router_reporting import BrokerRouterReporting


class _Journal:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return kwargs


class _BrokenJournal:
    def record(self, **kwargs: Any) -> None:
        raise RuntimeError("boom")


def test_record_event_appends_timestamped_recent_event() -> None:
    recent_events: deque[dict[str, Any]] = deque(maxlen=32)
    helper = BrokerRouterReporting(
        recent_events=recent_events,
        journal=_Journal(),
        logger=logging.getLogger("test_broker_router_reporting"),
    )

    helper.record_event("alpha.pending_order.json", "routing_error", "choose_venue failed")

    assert len(recent_events) == 1
    event = recent_events[0]
    assert event["file"] == "alpha.pending_order.json"
    assert event["kind"] == "routing_error"
    assert event["detail"] == "choose_venue failed"
    assert "ts" in event


def test_safe_journal_records_expected_payload() -> None:
    journal = _Journal()
    helper = BrokerRouterReporting(
        recent_events=deque(maxlen=32),
        journal=journal,
        logger=logging.getLogger("test_broker_router_reporting"),
    )

    helper.safe_journal(
        actor=Actor.STRATEGY_ROUTER,
        intent="pending_order_failed",
        rationale="venue rejected",
        gate_checks=["heartbeat"],
        outcome=Outcome.FAILED,
        links=["signal:sig-1"],
        metadata={"reason": "venue_rejected"},
    )

    assert len(journal.calls) == 1
    call = journal.calls[0]
    assert call["intent"] == "pending_order_failed"
    assert call["rationale"] == "venue rejected"
    assert call["gate_checks"] == ["heartbeat"]
    assert call["outcome"] == Outcome.FAILED
    assert call["links"] == ["signal:sig-1"]
    assert call["metadata"] == {"reason": "venue_rejected"}


def test_safe_journal_swallows_record_failures(caplog) -> None:
    helper = BrokerRouterReporting(
        recent_events=deque(maxlen=32),
        journal=_BrokenJournal(),
        logger=logging.getLogger("test_broker_router_reporting"),
    )

    with caplog.at_level(logging.WARNING):
        helper.safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_failed",
            outcome=Outcome.FAILED,
        )

    assert "journal append failed" in caplog.text

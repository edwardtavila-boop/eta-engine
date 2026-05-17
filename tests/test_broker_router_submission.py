from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from eta_engine.obs.decision_journal import Outcome
from eta_engine.scripts.broker_router_submission import BrokerRouterSubmission
from eta_engine.venues.base import OrderResult, OrderStatus


class _Request:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump_json(self) -> str:
        return json.dumps(self._payload)


def _order() -> Any:
    return SimpleNamespace(signal_id="sig-1", bot_id="alpha", ts="2026-05-17T17:00:00+00:00")


def _result(*, status: OrderStatus, order_id: str = "OID", filled_at: str = "") -> OrderResult:
    return OrderResult(
        order_id=order_id,
        status=status,
        filled_qty=1.0 if status is not OrderStatus.REJECTED else 0.0,
        avg_price=25_000.0,
        filled_at=filled_at,
    )


def _make_helper(tmp_path: Path):
    counts = {"submitted": 0, "rejected": 0, "failed": 0, "filled": 0}
    retry_counts: dict[str, int] = {}
    recorded_events: list[tuple[str, str, str]] = []
    journal_calls: list[dict[str, Any]] = []
    failed_moves: list[tuple[Path, dict[str, Any]]] = []
    retry_meta_saves: list[tuple[Path, dict[str, Any]]] = []
    cleared: list[Path] = []
    moved: list[tuple[Path, Path]] = []
    sidecars: dict[Path, dict[str, Any]] = {}
    routing_errors: list[tuple[Any, Path, str]] = []
    archive_dir = tmp_path / "archive"
    fill_results_dir = tmp_path / "fill_results"

    def _write_sidecar(path: Path, payload: dict[str, Any]) -> None:
        sidecars[path] = dict(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")

    helper = BrokerRouterSubmission(
        counts=counts,
        retry_counts=retry_counts,
        max_retries=3,
        archive_dir=archive_dir,
        fill_results_dir=fill_results_dir,
        place_with_failover_chain=None,  # type: ignore[arg-type]
        handle_routing_error=lambda order, target, reason: routing_errors.append((order, target, reason)),
        write_sidecar=_write_sidecar,
        move_to_failed_with_meta=lambda target, meta: failed_moves.append((target, dict(meta))),
        save_retry_meta=lambda target, meta: retry_meta_saves.append((target, dict(meta))),
        clear_retry_meta=lambda target: cleared.append(target),
        record_event=lambda filename, kind, detail: recorded_events.append((filename, kind, detail)),
        safe_journal=lambda **kwargs: journal_calls.append(dict(kwargs)),
        atomic_move=lambda src, dst: moved.append((src, dst)),
        extract_broker_fill_ts=lambda result: getattr(result, "filled_at", "") or "",
        logger=logging.getLogger("test_broker_router_submission"),
    )
    return (
        helper,
        counts,
        retry_counts,
        recorded_events,
        journal_calls,
        failed_moves,
        retry_meta_saves,
        cleared,
        moved,
        sidecars,
        routing_errors,
    )


def test_submit_and_finalize_rejected_retry_persists_meta_and_journals(tmp_path: Path) -> None:
    (
        helper,
        counts,
        retry_counts,
        recorded_events,
        journal_calls,
        failed_moves,
        retry_meta_saves,
        cleared,
        moved,
        sidecars,
        _routing_errors,
    ) = _make_helper(tmp_path)
    venue = SimpleNamespace(name="ibkr")
    request = _Request({"symbol": "MNQ"})

    async def _place(order, resolved_venue, resolved_request):
        return _result(status=OrderStatus.REJECTED, order_id="OID-REJ"), resolved_venue

    helper._place_with_failover_chain = _place  # type: ignore[attr-defined]
    target = tmp_path / "processing" / "alpha.pending_order.json"

    asyncio.run(
        helper.submit_and_finalize(
            _order(),
            target,
            venue,
            request,
            ["heartbeat"],
            retry_meta={"attempts": 1},
        )
    )

    assert counts["submitted"] == 1
    assert counts["rejected"] == 1
    assert retry_counts["sig-1"] == 2
    assert retry_meta_saves and retry_meta_saves[0][1]["attempts"] == 2
    assert ("alpha.pending_order.json", "rejected_retry", "2") in recorded_events
    assert journal_calls[0]["intent"] == "pending_order_rejected_retry"
    assert journal_calls[0]["outcome"] == Outcome.NOTED
    assert not failed_moves
    assert not cleared
    assert not moved
    assert any(path.name == "sig-1_result.json" for path in sidecars)


def test_submit_and_finalize_filled_archives_and_clears_retry_meta(tmp_path: Path) -> None:
    (
        helper,
        counts,
        retry_counts,
        recorded_events,
        journal_calls,
        _failed_moves,
        _retry_meta_saves,
        cleared,
        moved,
        sidecars,
        _routing_errors,
    ) = _make_helper(tmp_path)
    retry_counts["sig-1"] = 2
    venue = SimpleNamespace(name="ibkr")
    request = _Request({"symbol": "MNQ"})

    async def _place(order, resolved_venue, resolved_request):
        return _result(status=OrderStatus.FILLED, order_id="OID-FILL", filled_at="2026-05-16T13:58:45+00:00"), resolved_venue

    helper._place_with_failover_chain = _place  # type: ignore[attr-defined]
    target = tmp_path / "processing" / "alpha.pending_order.json"

    asyncio.run(
        helper.submit_and_finalize(
            _order(),
            target,
            venue,
            request,
            ["heartbeat"],
            retry_meta={"attempts": 2},
        )
    )

    assert counts["submitted"] == 1
    assert counts["filled"] == 1
    assert "sig-1" not in retry_counts
    assert ("alpha.pending_order.json", "executed", "FILLED") in recorded_events
    assert journal_calls[0]["intent"] == "pending_order_executed"
    assert journal_calls[0]["outcome"] == Outcome.EXECUTED
    assert cleared == [target]
    assert moved and moved[0][0] == target
    assert any(path.name == "sig-1_result.json" for path in sidecars)
    payload = next(iter(sidecars.values()))
    assert payload["broker_fill_ts"] == "2026-05-16T13:58:45+00:00"


def test_submit_and_finalize_routing_exception_fails_closed(tmp_path: Path) -> None:
    (
        helper,
        counts,
        _retry_counts,
        _recorded_events,
        _journal_calls,
        _failed_moves,
        _retry_meta_saves,
        _cleared,
        _moved,
        _sidecars,
        routing_errors,
    ) = _make_helper(tmp_path)
    venue = SimpleNamespace(name="ibkr")
    request = _Request({"symbol": "MNQ"})

    async def _place(order, resolved_venue, resolved_request):
        raise RuntimeError("handshake lost")

    helper._place_with_failover_chain = _place  # type: ignore[attr-defined]
    target = tmp_path / "processing" / "alpha.pending_order.json"

    asyncio.run(
        helper.submit_and_finalize(
            _order(),
            target,
            venue,
            request,
            ["heartbeat"],
            retry_meta={},
        )
    )

    assert counts["submitted"] == 1
    assert routing_errors and "handshake lost" in routing_errors[0][2]

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from eta_engine.obs.decision_journal import Actor, Outcome
from eta_engine.venues.base import OrderStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping
    from logging import Logger


class _OrderLike(Protocol):
    signal_id: str
    bot_id: str
    ts: str


class _VenueLike(Protocol):
    name: str


class _RequestLike(Protocol):
    def model_dump_json(self) -> str: ...


class _ResultLike(Protocol):
    order_id: str
    status: OrderStatus
    filled_qty: float
    avg_price: float | None

    def model_dump_json(self) -> str: ...


class BrokerRouterSubmission:
    """Own broker-router submit/result finalization side effects."""

    def __init__(
        self,
        *,
        counts: MutableMapping[str, int],
        retry_counts: MutableMapping[str, int],
        max_retries: int,
        archive_dir: Path,
        fill_results_dir: Path,
        place_with_failover_chain: Callable[[_OrderLike, _VenueLike, _RequestLike], Awaitable[tuple[_ResultLike, _VenueLike]]],
        handle_routing_error: Callable[[_OrderLike, Path, str], None],
        write_sidecar: Callable[[Path, dict[str, Any]], None],
        move_to_failed_with_meta: Callable[[Path, dict[str, Any]], None],
        save_retry_meta: Callable[[Path, dict[str, Any]], None],
        clear_retry_meta: Callable[[Path], None],
        record_event: Callable[[str, str, str], None],
        safe_journal: Callable[..., None],
        atomic_move: Callable[[Path, Path], None],
        extract_broker_fill_ts: Callable[[_ResultLike], str],
        logger: Logger,
    ) -> None:
        self._counts = counts
        self._retry_counts = retry_counts
        self._max_retries = int(max_retries)
        self._archive_dir = Path(archive_dir)
        self._fill_results_dir = Path(fill_results_dir)
        self._place_with_failover_chain = place_with_failover_chain
        self._handle_routing_error = handle_routing_error
        self._write_sidecar = write_sidecar
        self._move_to_failed_with_meta = move_to_failed_with_meta
        self._save_retry_meta = save_retry_meta
        self._clear_retry_meta = clear_retry_meta
        self._record_event = record_event
        self._safe_journal = safe_journal
        self._atomic_move = atomic_move
        self._extract_broker_fill_ts = extract_broker_fill_ts
        self._logger = logger

    async def submit_and_finalize(
        self,
        order: _OrderLike,
        target: Path,
        venue: _VenueLike,
        request: _RequestLike,
        gate_checks_summary: list[str],
        *,
        retry_meta: dict[str, Any],
    ) -> None:
        self._counts["submitted"] += 1
        try:
            result, venue = await self._place_with_failover_chain(order, venue, request)
        except Exception as exc:  # noqa: BLE001
            self._handle_routing_error(order, target, f"venue.place_order raised: {exc}")
            return

        result_written_ts = datetime.now(UTC).isoformat()
        broker_fill_ts = self._extract_broker_fill_ts(result)
        sidecar_payload = {
            "signal_id": order.signal_id,
            "bot_id": order.bot_id,
            "venue": venue.name,
            "order_ts": order.ts,
            "broker_fill_ts": broker_fill_ts,
            "request": json.loads(request.model_dump_json()),
            "result": json.loads(result.model_dump_json()),
            "result_written_ts": result_written_ts,
            "ts": result_written_ts,
        }
        self._write_sidecar(
            self._fill_results_dir / f"{order.signal_id}_result.json",
            sidecar_payload,
        )
        links = [
            f"signal:{order.signal_id}",
            f"bot:{order.bot_id}",
            f"order:{result.order_id}",
        ]

        if result.status is OrderStatus.REJECTED:
            await self._handle_rejected_result(
                order,
                target,
                venue,
                result,
                gate_checks_summary,
                retry_meta,
                links,
                sidecar_payload,
            )
            return

        self._counts["filled"] += 1
        self._record_event(target.name, "executed", result.status.value)
        self._retry_counts.pop(order.signal_id, None)
        archive_dated = self._archive_dir / datetime.now(UTC).strftime("%Y-%m-%d")
        archive_dated.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self._atomic_move(target, archive_dated / target.name)
        self._clear_retry_meta(target)
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_executed",
            rationale=(
                f"venue={venue.name} status={result.status.value} "
                f"filled={result.filled_qty} avg_price={result.avg_price}"
            ),
            gate_checks=gate_checks_summary,
            outcome=Outcome.EXECUTED,
            links=links,
            metadata=sidecar_payload,
        )

    async def _handle_rejected_result(
        self,
        order: _OrderLike,
        target: Path,
        venue: _VenueLike,
        result: _ResultLike,
        gate_checks_summary: list[str],
        retry_meta: dict[str, Any],
        links: list[str],
        sidecar_payload: dict[str, Any],
    ) -> None:
        self._counts["rejected"] += 1
        attempts = int(retry_meta.get("attempts", 0)) + 1
        reject_reason = getattr(result, "error_message", None) or f"venue={venue.name} rejected order_id={result.order_id}"
        new_meta = {
            "attempts": attempts,
            "last_attempt_ts": datetime.now(UTC).isoformat(),
            "last_reject_reason": str(reject_reason),
        }
        self._retry_counts[order.signal_id] = attempts
        if attempts >= self._max_retries:
            self._counts["failed"] += 1
            self._record_event(target.name, "failed", "max_retries")
            self._move_to_failed_with_meta(target, new_meta)
            self._safe_journal(
                actor=Actor.STRATEGY_ROUTER,
                intent="pending_order_failed",
                rationale=(f"venue={venue.name} rejected {attempts} times; order_id={result.order_id}"),
                gate_checks=gate_checks_summary,
                outcome=Outcome.FAILED,
                links=links,
                metadata=sidecar_payload,
            )
            self._retry_counts.pop(order.signal_id, None)
            return

        self._save_retry_meta(target, new_meta)
        self._logger.info(
            "rejected attempt=%d/%d signal=%s; will retry",
            attempts,
            self._max_retries,
            order.signal_id,
        )
        self._record_event(target.name, "rejected_retry", str(attempts))
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_rejected_retry",
            rationale=(f"venue={venue.name} rejected attempt={attempts}/{self._max_retries}"),
            gate_checks=gate_checks_summary,
            outcome=Outcome.NOTED,
            links=links,
            metadata=sidecar_payload,
        )

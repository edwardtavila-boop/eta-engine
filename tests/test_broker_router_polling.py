from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from eta_engine.scripts.broker_router_polling import BrokerRouterPolling
from eta_engine.scripts.runtime_order_hold import OrderEntryHold


def _hold(
    tmp_path: Path,
    *,
    active: bool,
    reason: str = "",
    scope: str = "all",
) -> OrderEntryHold:
    return OrderEntryHold(
        active=active,
        reason=reason,
        source="test",
        scope=scope,
        path=tmp_path / "order_entry_hold.json",
    )


def test_hold_blocks_file_for_scoped_futures_and_records_event(tmp_path: Path) -> None:
    counts = {"held": 0}
    recorded: list[tuple[str, str, str]] = []
    helper = BrokerRouterPolling(
        pending_dir=tmp_path / "pending",
        processing_dir=tmp_path / "processing",
        dry_run=False,
        counts=counts,
        order_entry_hold=lambda: _hold(
            tmp_path,
            active=True,
            reason="ibkr_incident",
            scope="futures",
        ),
        emit_heartbeat=lambda **kwargs: None,
        record_event=lambda filename, kind, detail: recorded.append((filename, kind, detail)),
        process_pending_file=None,  # type: ignore[arg-type]
        process_retry_file=None,  # type: ignore[arg-type]
        parse_pending_file=lambda path: SimpleNamespace(bot_id="mnq_futures_sage", symbol="MNQ1"),
        routing_venue_for=lambda bot_id, symbol: "ibkr",
        asset_class_for_symbol=lambda symbol: "futures",
        logger=logging.getLogger("test_broker_router_polling"),
    )
    target = tmp_path / "pending" / "mnq_futures_sage.pending_order.json"

    assert helper.hold_blocks_file(target) is True
    assert counts["held"] == 1
    assert recorded == [("mnq_futures_sage.pending_order.json", "order_entry_hold", "ibkr_incident")]


def test_hold_blocks_file_returns_false_when_parse_fails(tmp_path: Path) -> None:
    counts = {"held": 0}
    helper = BrokerRouterPolling(
        pending_dir=tmp_path / "pending",
        processing_dir=tmp_path / "processing",
        dry_run=False,
        counts=counts,
        order_entry_hold=lambda: _hold(tmp_path, active=True, reason="incident", scope="futures"),
        emit_heartbeat=lambda **kwargs: None,
        record_event=lambda filename, kind, detail: None,
        process_pending_file=None,  # type: ignore[arg-type]
        process_retry_file=None,  # type: ignore[arg-type]
        parse_pending_file=lambda path: (_ for _ in ()).throw(ValueError("bad json")),
        routing_venue_for=lambda bot_id, symbol: "ibkr",
        asset_class_for_symbol=lambda symbol: "futures",
        logger=logging.getLogger("test_broker_router_polling"),
    )

    assert helper.hold_blocks_file(tmp_path / "pending" / "alpha.pending_order.json") is False
    assert counts["held"] == 0


def test_tick_short_circuits_on_global_hold_and_emits_single_heartbeat(tmp_path: Path) -> None:
    pending_dir = tmp_path / "pending"
    processing_dir = tmp_path / "processing"
    pending_dir.mkdir(parents=True, exist_ok=True)
    processing_dir.mkdir(parents=True, exist_ok=True)

    counts = {"held": 0}
    recorded: list[tuple[str, str, str]] = []
    heartbeats: list[dict[str, Any]] = []
    pending_calls: list[Path] = []
    retry_calls: list[Path] = []

    async def _process_pending(path: Path) -> None:
        pending_calls.append(path)

    async def _process_retry(path: Path) -> None:
        retry_calls.append(path)

    helper = BrokerRouterPolling(
        pending_dir=pending_dir,
        processing_dir=processing_dir,
        dry_run=False,
        counts=counts,
        order_entry_hold=lambda: _hold(tmp_path, active=True, reason="broker_incident", scope="all"),
        emit_heartbeat=lambda **kwargs: heartbeats.append(dict(kwargs)),
        record_event=lambda filename, kind, detail: recorded.append((filename, kind, detail)),
        process_pending_file=_process_pending,
        process_retry_file=_process_retry,
        parse_pending_file=lambda path: SimpleNamespace(bot_id="alpha", symbol="MNQ1"),
        routing_venue_for=lambda bot_id, symbol: "ibkr",
        asset_class_for_symbol=lambda symbol: "futures",
        logger=logging.getLogger("test_broker_router_polling"),
    )

    asyncio.run(helper.tick(stopped=lambda: False))

    assert counts["held"] == 1
    assert recorded == [("runtime", "order_entry_hold", "broker_incident")]
    assert len(heartbeats) == 1
    assert heartbeats[0]["hold"].active is True
    assert pending_calls == []
    assert retry_calls == []


def test_tick_processes_pending_then_retry_and_emits_two_heartbeats(tmp_path: Path) -> None:
    pending_dir = tmp_path / "pending"
    processing_dir = tmp_path / "processing"
    pending_dir.mkdir(parents=True, exist_ok=True)
    processing_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "b.pending_order.json").write_text("{}", encoding="utf-8")
    (pending_dir / "a.pending_order.json").write_text("{}", encoding="utf-8")
    (processing_dir / "retry.pending_order.json").write_text("{}", encoding="utf-8")

    heartbeats: list[dict[str, Any]] = []
    pending_calls: list[str] = []
    retry_calls: list[str] = []

    async def _process_pending(path: Path) -> None:
        pending_calls.append(path.name)

    async def _process_retry(path: Path) -> None:
        retry_calls.append(path.name)

    helper = BrokerRouterPolling(
        pending_dir=pending_dir,
        processing_dir=processing_dir,
        dry_run=False,
        counts={"held": 0},
        order_entry_hold=lambda: _hold(tmp_path, active=False),
        emit_heartbeat=lambda **kwargs: heartbeats.append(dict(kwargs)),
        record_event=lambda filename, kind, detail: None,
        process_pending_file=_process_pending,
        process_retry_file=_process_retry,
        parse_pending_file=lambda path: SimpleNamespace(bot_id="alpha", symbol="MNQ1"),
        routing_venue_for=lambda bot_id, symbol: "ibkr",
        asset_class_for_symbol=lambda symbol: "futures",
        logger=logging.getLogger("test_broker_router_polling"),
    )

    asyncio.run(helper.tick(stopped=lambda: False))

    assert len(heartbeats) == 2
    assert pending_calls == ["a.pending_order.json", "b.pending_order.json"]
    assert retry_calls == ["retry.pending_order.json"]

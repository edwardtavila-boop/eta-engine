from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import broker_router_pending


def _write_pending(
    tmp_path: Path,
    *,
    bot_id: str = "alpha",
    symbol: str = "MNQ",
    extra: dict[str, object] | None = None,
) -> Path:
    payload: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "signal_id": "sig-001",
        "side": "BUY",
        "qty": 1.0,
        "symbol": symbol,
        "limit_price": 25_000.0,
        "stop_price": 24_900.0,
        "target_price": 25_100.0,
    }
    if extra:
        payload.update(extra)
    path = tmp_path / f"{bot_id}.pending_order.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_parse_pending_file_normalizes_bad_single_digit_futures_suffix(tmp_path: Path) -> None:
    path = _write_pending(tmp_path, symbol="MNQ1")
    order = broker_router_pending.parse_pending_file(path)
    assert order.bot_id == "alpha"
    assert order.symbol == "MNQ"


def test_parse_pending_file_preserves_gate_metadata(tmp_path: Path) -> None:
    path = _write_pending(
        tmp_path,
        extra={
            "execution_lane": "paper",
            "capital_gate_scope": "global",
            "daily_loss_gate_mode": "hard",
            "daily_loss_gate_active": True,
            "daily_loss_gate_reason": "tripped",
        },
    )
    order = broker_router_pending.parse_pending_file(path)
    assert order.execution_lane == "paper"
    assert order.capital_gate_scope == "global"
    assert order.daily_loss_gate_mode == "hard"
    assert order.daily_loss_gate_active is True
    assert order.daily_loss_gate_reason == "tripped"


def test_pending_order_sanity_denial_requires_brackets_for_new_entries() -> None:
    order = broker_router_pending.PendingOrder(
        ts=datetime.now(UTC).isoformat(),
        signal_id="sig-002",
        side="BUY",
        qty=1.0,
        symbol="MNQ",
        limit_price=25_000.0,
        bot_id="alpha",
    )
    reason = broker_router_pending.pending_order_sanity_denial(order)
    assert "missing bracket fields" in reason


def test_pending_order_sanity_denial_allows_reduce_only_without_brackets() -> None:
    order = broker_router_pending.PendingOrder(
        ts=datetime.now(UTC).isoformat(),
        signal_id="sig-003",
        side="SELL",
        qty=1.0,
        symbol="MNQ",
        limit_price=25_000.0,
        bot_id="alpha",
        reduce_only=True,
    )
    assert broker_router_pending.pending_order_sanity_denial(order) == ""


def test_pending_order_sanity_denial_blocks_stale_orders() -> None:
    stale_ts = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    order = broker_router_pending.PendingOrder(
        ts=stale_ts,
        signal_id="sig-004",
        side="BUY",
        qty=1.0,
        symbol="MNQ",
        limit_price=25_000.0,
        bot_id="alpha",
        stop_price=24_900.0,
        target_price=25_100.0,
    )
    reason = broker_router_pending.pending_order_sanity_denial(order)
    assert "stale pending order" in reason

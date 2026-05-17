from __future__ import annotations

import json
import logging
import sqlite3
from collections import deque
from pathlib import Path

from eta_engine.scripts.broker_router_ops import BrokerRouterOpsSurface
from eta_engine.scripts.runtime_order_hold import OrderEntryHold


def _make_hold(tmp_path: Path, *, active: bool, reason: str = "", scope: str = "all") -> OrderEntryHold:
    return OrderEntryHold(
        active=active,
        reason=reason,
        source="test",
        scope=scope,
        path=tmp_path / "order_entry_hold.json",
    )


def _make_ops(
    tmp_path: Path,
    *,
    counts: dict[str, int] | None = None,
    recent_events: deque[dict[str, object]] | None = None,
    hold: OrderEntryHold | None = None,
    venue_circuits: dict[str, str] | None = None,
) -> BrokerRouterOpsSurface:
    state_root = tmp_path / "state"
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    def _write_sidecar(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")

    resolved_hold = hold if hold is not None else _make_hold(tmp_path, active=False)
    return BrokerRouterOpsSurface(
        pending_dir=pending_dir,
        state_root=state_root,
        heartbeat_path=state_root / "broker_router_heartbeat.json",
        gate_pre_trade_path=state_root / "pre_trade_gate.json",
        gate_heat_state_path=state_root / "heat_state.json",
        gate_journal_path=state_root / "gate_journal.sqlite",
        dry_run=False,
        interval_s=5.0,
        max_retries=3,
        counts=counts or {"parsed": 0, "held": 0},
        recent_events=recent_events or deque(maxlen=32),
        order_entry_hold=lambda: resolved_hold,
        venue_circuit_states=lambda: dict(venue_circuits or {}),
        write_sidecar=_write_sidecar,
        env_int=lambda name, default: 4 if name == "ETA_BROKER_ROUTER_GATE_MAX_CONCURRENT" else default,
        env_float=lambda name, default: 0.5 if name == "ETA_BROKER_ROUTER_GATE_BUDGET" else default,
        logger=logging.getLogger("test_broker_router_ops"),
    )


def test_heat_state_snapshot_uses_nonzero_positions_and_budget(tmp_path: Path) -> None:
    ops = _make_ops(tmp_path)

    payload = ops.heat_state_snapshot(
        now_iso="2026-05-17T16:50:00+00:00",
        open_positions={"MNQ": 1, "ES": 0, "BTC": -2},
    )

    assert payload["positions"] == 2
    assert payload["max_concurrent"] == 4
    assert payload["budget"] == 0.5
    assert payload["current_heat"] == 0.5
    assert payload["utilization_pct"] == 100.0
    assert payload["open_positions"] == {"MNQ": 1, "BTC": -2}


def test_sync_gate_state_writes_sidecars_and_gate_journal(tmp_path: Path) -> None:
    hold = _make_hold(tmp_path, active=True, reason="broker_incident", scope="futures")
    ops = _make_ops(tmp_path, hold=hold)

    ops.sync_gate_state(hold=hold, open_positions={"MNQ": 1, "ES": 0})

    pre_trade = json.loads((tmp_path / "state" / "pre_trade_gate.json").read_text(encoding="utf-8"))
    heat_state = json.loads((tmp_path / "state" / "heat_state.json").read_text(encoding="utf-8"))
    journal_path = tmp_path / "state" / "gate_journal.sqlite"

    assert pre_trade["state"] == "HOT"
    assert pre_trade["reason"] == "broker_incident"
    assert pre_trade["scope"] == "futures"
    assert heat_state["positions"] == 1
    assert journal_path.exists()
    with sqlite3.connect(journal_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
    assert row == ("events",)


def test_emit_heartbeat_writes_counts_events_and_circuits(tmp_path: Path) -> None:
    hold = _make_hold(tmp_path, active=False)
    ops = _make_ops(
        tmp_path,
        counts={"parsed": 2, "held": 1},
        recent_events=deque([{"kind": "held", "detail": "incident"}], maxlen=32),
        hold=hold,
        venue_circuits={"ibkr": "closed", "tastytrade": "half-open"},
    )

    ops.emit_heartbeat()

    payload = json.loads((tmp_path / "state" / "broker_router_heartbeat.json").read_text(encoding="utf-8"))
    assert payload["order_entry_hold"]["active"] is False
    assert payload["counts"]["parsed"] == 2
    assert payload["recent_events"][0]["kind"] == "held"
    assert payload["venue_circuits"] == {"ibkr": "closed", "tastytrade": "half-open"}

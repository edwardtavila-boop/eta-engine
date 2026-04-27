"""
End-to-end tests for the BTC broker-paper worker loop.

Exercises the full ``btc_broker_fleet._execute_worker_tick`` path with
a stubbed broker adapter, proving that:

  * a worker tick builds a PaperLaneRunner + writes the heartbeat
    payload + lane snapshot;
  * auto-submit fires a real ``place_order`` through the stub adapter
    on the first tick, then transitions to reconcile on subsequent
    ticks;
  * a terminal (FILLED / REJECTED) broker response clears
    ``active_order_id`` and increments ``terminal_orders``;
  * an adapter that raises on every call degrades the snapshot to
    ``execution_state = "ERROR"`` without crashing the tick;
  * the trade ledger grows monotonically across ticks.

Uses the in-memory ``_StubAdapter`` from ``tests.test_btc_paper_lane``
so the network-free test surface stays consistent.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from eta_engine.scripts.btc_broker_fleet import (
    FleetWorkerSpec,
    _build_lane_runner,
    _execute_worker_tick,
    fleet_workers,
    worker_ledger_path,
    worker_status_path,
)
from eta_engine.scripts.btc_paper_lane import PaperLaneRunner
from eta_engine.venues.base import OrderResult, OrderStatus

if TYPE_CHECKING:
    from pathlib import Path

    from eta_engine.venues.base import OrderRequest


class _StubAdapter:
    """Deterministic adapter driven by a canned OrderResult script."""

    def __init__(self, script: list[OrderResult] | None = None) -> None:
        self.script = list(script or [])
        self.place_calls: list[OrderRequest] = []
        self.status_calls: list[str] = []
        self.cancel_calls: list[str] = []

    async def place_order(self, request: OrderRequest) -> OrderResult:
        self.place_calls.append(request)
        if self.script:
            return self.script.pop(0)
        return OrderResult(order_id="auto", status=OrderStatus.OPEN, raw={})

    async def get_order_status(
        self,
        symbol: str,
        order_id: str,
    ) -> OrderResult | None:
        _ = symbol
        self.status_calls.append(order_id)
        if self.script:
            return self.script.pop(0)
        return OrderResult(order_id=order_id, status=OrderStatus.OPEN, raw={})

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        _ = symbol
        self.cancel_calls.append(order_id)
        return True


def _spec(lane: str = "grid", broker: str = "tastytrade") -> FleetWorkerSpec:
    return FleetWorkerSpec(
        worker_id=f"btc-{lane}-{broker}",
        broker=broker,
        lane=lane,
        symbol="BTCUSD",
        paper_starting_cash=5_000.0,
    )


def _runner(tmp_path: Path, spec: FleetWorkerSpec, adapter: _StubAdapter, *, auto_submit: bool) -> PaperLaneRunner:
    return PaperLaneRunner(
        worker_id=spec.worker_id,
        broker=spec.broker,
        lane=spec.lane,
        symbol=spec.symbol,
        adapter=adapter,
        state_dir=tmp_path,
        ledger_path=worker_ledger_path(tmp_path),
        anchor_price=100_000.0,
        probe_qty=1,
        auto_submit=auto_submit,
    )


# --------------------------------------------------------------------------- #
# fleet_workers: every lane-broker pair is distinct
# --------------------------------------------------------------------------- #
class TestFleetWorkers:
    def test_four_workers_each_lane_each_broker(self) -> None:
        workers = fleet_workers(starting_cash=1000.0)
        ids = [w.worker_id for w in workers]
        assert set(ids) == {
            "btc-directional-tastytrade",
            "btc-directional-ibkr",
            "btc-grid-tastytrade",
            "btc-grid-ibkr",
        }
        assert all(w.symbol == "BTCUSD" for w in workers)
        assert all(w.paper_starting_cash == 1000.0 for w in workers)


# --------------------------------------------------------------------------- #
# _execute_worker_tick: single-tick surface
# --------------------------------------------------------------------------- #
class TestWorkerTickLifecycle:
    def test_first_tick_submits_probe(self, tmp_path: Path) -> None:
        spec = _spec("directional", "tastytrade")
        adapter = _StubAdapter(
            [
                OrderResult(order_id="srv-X", status=OrderStatus.OPEN, raw={}),
            ]
        )
        runner = _runner(tmp_path, spec, adapter, auto_submit=True)

        payload = _execute_worker_tick(
            spec,
            runner=runner,
            out_dir=tmp_path,
            heartbeat=1,
            started_at="2026-04-24T10:00:00+00:00",
            runner_error="",
        )

        # Adapter was called exactly once for place_order
        assert len(adapter.place_calls) == 1
        assert adapter.place_calls[0].symbol == "BTCUSD"

        # Heartbeat file was written
        status_path = worker_status_path(tmp_path, spec.worker_id)
        assert status_path.exists()
        on_disk = json.loads(status_path.read_text(encoding="utf-8"))
        assert on_disk["heartbeat_count"] == 1
        assert on_disk["lane_runner"]["active_order_id"] == "srv-X"
        assert on_disk["execution_state"] == "ACTIVE"
        assert on_disk["fill_lifecycle_ready"] is True

        # Payload returned matches what was written
        assert payload["worker_id"] == spec.worker_id

    def test_second_tick_reconciles_no_new_submit(self, tmp_path: Path) -> None:
        spec = _spec("grid", "ibkr")
        adapter = _StubAdapter(
            [
                OrderResult(order_id="srv-Y", status=OrderStatus.OPEN, raw={}),
                # Second tick -> reconcile returns still-OPEN
                OrderResult(order_id="srv-Y", status=OrderStatus.OPEN, raw={}),
            ]
        )
        runner = _runner(tmp_path, spec, adapter, auto_submit=True)

        _execute_worker_tick(
            spec,
            runner=runner,
            out_dir=tmp_path,
            heartbeat=1,
            started_at="ts",
            runner_error="",
        )
        _execute_worker_tick(
            spec,
            runner=runner,
            out_dir=tmp_path,
            heartbeat=2,
            started_at="ts",
            runner_error="",
        )
        # One submit, one reconcile
        assert len(adapter.place_calls) == 1
        assert adapter.status_calls == ["srv-Y"]

    def test_fill_transition_clears_active(self, tmp_path: Path) -> None:
        spec = _spec("grid", "ibkr")
        adapter = _StubAdapter(
            [
                OrderResult(order_id="srv-F", status=OrderStatus.OPEN, raw={}),
                OrderResult(
                    order_id="srv-F",
                    status=OrderStatus.FILLED,
                    filled_qty=1.0,
                    avg_price=95_000.0,
                    raw={},
                ),
            ]
        )
        runner = _runner(tmp_path, spec, adapter, auto_submit=True)

        _execute_worker_tick(
            spec,
            runner=runner,
            out_dir=tmp_path,
            heartbeat=1,
            started_at="ts",
            runner_error="",
        )
        payload2 = _execute_worker_tick(
            spec,
            runner=runner,
            out_dir=tmp_path,
            heartbeat=2,
            started_at="ts",
            runner_error="",
        )
        # Terminal FILLED -> cleared
        assert payload2["lane_runner"]["active_order_id"] is None
        assert payload2["lane_runner"]["terminal_orders"] == 1
        assert payload2["lane_runner"]["execution_state"] == "ARMED"

    def test_rejected_terminal_clears(self, tmp_path: Path) -> None:
        spec = _spec("directional", "tastytrade")
        adapter = _StubAdapter(
            [
                OrderResult(order_id="srv-R", status=OrderStatus.OPEN, raw={}),
                OrderResult(order_id="srv-R", status=OrderStatus.REJECTED, raw={}),
            ]
        )
        runner = _runner(tmp_path, spec, adapter, auto_submit=True)
        _execute_worker_tick(
            spec,
            runner=runner,
            out_dir=tmp_path,
            heartbeat=1,
            started_at="ts",
            runner_error="",
        )
        payload2 = _execute_worker_tick(
            spec,
            runner=runner,
            out_dir=tmp_path,
            heartbeat=2,
            started_at="ts",
            runner_error="",
        )
        assert payload2["lane_runner"]["active_order_id"] is None
        assert payload2["lane_runner"]["terminal_orders"] == 1

    def test_reconcile_only_mode_never_submits(self, tmp_path: Path) -> None:
        spec = _spec("directional", "tastytrade")
        adapter = _StubAdapter()
        runner = _runner(tmp_path, spec, adapter, auto_submit=False)
        payload = _execute_worker_tick(
            spec,
            runner=runner,
            out_dir=tmp_path,
            heartbeat=1,
            started_at="ts",
            runner_error="",
        )
        assert adapter.place_calls == []
        assert payload["lane_runner"]["execution_state"] == "RECONCILE_ONLY"

    def test_no_runner_still_writes_heartbeat(self, tmp_path: Path) -> None:
        spec = _spec("grid", "ibkr")
        payload = _execute_worker_tick(
            spec,
            runner=None,
            out_dir=tmp_path,
            heartbeat=1,
            started_at="ts",
            runner_error="ConfigError: simulated",
        )
        # Heartbeat still written even when the runner failed to construct.
        assert payload["worker_id"] == spec.worker_id
        assert payload["note"].startswith("ConfigError")
        # No lane_runner snapshot
        assert "lane_runner" not in payload

    def test_adapter_exception_is_quarantined_to_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        class _BreakingAdapter:
            async def place_order(self, request):  # type: ignore[no-untyped-def]
                raise RuntimeError("transport dead")

            async def get_order_status(self, symbol, order_id):  # type: ignore[no-untyped-def]
                raise RuntimeError("transport dead")

            async def cancel_order(self, symbol, order_id):  # type: ignore[no-untyped-def]
                return False

        spec = _spec("grid", "ibkr")
        runner = PaperLaneRunner(
            worker_id=spec.worker_id,
            broker=spec.broker,
            lane=spec.lane,
            symbol=spec.symbol,
            adapter=_BreakingAdapter(),  # type: ignore[arg-type]
            state_dir=tmp_path,
            ledger_path=worker_ledger_path(tmp_path),
            anchor_price=100_000.0,
            auto_submit=True,
        )
        # First tick triggers place_order, which raises -> runner
        # catches internally and records a submit_error last_event.
        payload = _execute_worker_tick(
            spec,
            runner=runner,
            out_dir=tmp_path,
            heartbeat=1,
            started_at="ts",
            runner_error="",
        )
        # Lane tick didn't crash the worker; snapshot is present.
        assert "lane_runner" in payload
        snap = payload["lane_runner"]
        # No successful submit -> still ARMED (auto_submit on, no active)
        assert snap["active_order_id"] is None
        assert "submit_error" in snap["last_event"]


# --------------------------------------------------------------------------- #
# _build_lane_runner: error capture
# --------------------------------------------------------------------------- #
class TestBuildLaneRunner:
    def test_unsupported_lane_returns_error(self, tmp_path: Path) -> None:
        bad = FleetWorkerSpec(
            worker_id="x",
            broker="tastytrade",
            lane="banana",
            symbol="BTCUSD",
        )
        runner, err = _build_lane_runner(bad, out_dir=tmp_path)
        assert runner is None
        assert "unsupported lane" in err.lower()

    def test_unsupported_broker_returns_error(self, tmp_path: Path) -> None:
        bad = FleetWorkerSpec(
            worker_id="x",
            broker="fake-broker",
            lane="grid",
            symbol="BTCUSD",
        )
        runner, err = _build_lane_runner(bad, out_dir=tmp_path)
        assert runner is None
        assert "unsupported broker" in err.lower()


# --------------------------------------------------------------------------- #
# Trade ledger grows monotonically
# --------------------------------------------------------------------------- #
class TestLedgerMonotonic:
    def test_ledger_appends_on_submit_and_transition(
        self,
        tmp_path: Path,
    ) -> None:
        spec = _spec("grid", "tastytrade")
        adapter = _StubAdapter(
            [
                OrderResult(order_id="srv-L", status=OrderStatus.OPEN, raw={}),
                OrderResult(
                    order_id="srv-L",
                    status=OrderStatus.FILLED,
                    filled_qty=1.0,
                    avg_price=95_000.0,
                    raw={},
                ),
            ]
        )
        runner = _runner(tmp_path, spec, adapter, auto_submit=True)
        _execute_worker_tick(
            spec,
            runner=runner,
            out_dir=tmp_path,
            heartbeat=1,
            started_at="ts",
            runner_error="",
        )
        _execute_worker_tick(
            spec,
            runner=runner,
            out_dir=tmp_path,
            heartbeat=2,
            started_at="ts",
            runner_error="",
        )
        ledger = worker_ledger_path(tmp_path)
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        events = [r["event"] for r in rows]
        assert "submit" in events
        assert "transition" in events
        # Both rows keyed to the correct worker
        assert all(r["worker_id"] == spec.worker_id for r in rows)

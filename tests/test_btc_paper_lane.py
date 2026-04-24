"""
Tests for ``scripts.btc_paper_lane.PaperLaneRunner``.

Covers the full lane lifecycle against a stubbed broker adapter:

  * reconcile-only mode (auto_submit=False) never POSTs
  * auto-submit submits a single probe LIMIT-BUY per lane at the
    lane-specific fraction of the anchor price
  * transitions through OPEN -> FILLED write ledger rows + clear
    ``active_order_id``
  * state persists across runner re-construction so a worker restart
    picks up the existing probe instead of submitting a duplicate
  * ``cancel_active`` cleans up on shutdown
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from eta_engine.scripts.btc_paper_lane import (
    LaneState,
    PaperLaneRunner,
    run_one_tick,
    shutdown,
)
from eta_engine.venues.base import (
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
)

if TYPE_CHECKING:
    from pathlib import Path


class _StubAdapter:
    """Deterministic broker adapter driven by a canned ``OrderResult`` script."""

    def __init__(self, script: list[OrderResult] | None = None) -> None:
        self.script = list(script or [])
        self.place_calls: list[OrderRequest] = []
        self.status_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self._cancel_ok = True

    async def place_order(self, request: OrderRequest) -> OrderResult:
        self.place_calls.append(request)
        if self.script:
            return self.script.pop(0)
        return OrderResult(
            order_id="auto-" + str(len(self.place_calls)),
            status=OrderStatus.OPEN,
            raw={},
        )

    async def get_order_status(
        self, symbol: str, order_id: str,
    ) -> OrderResult | None:
        _ = symbol
        self.status_calls.append(order_id)
        if self.script:
            return self.script.pop(0)
        return OrderResult(order_id=order_id, status=OrderStatus.OPEN, raw={})

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        _ = symbol
        self.cancel_calls.append(order_id)
        return self._cancel_ok


def _runner(
    tmp_path: Path,
    *,
    lane: str = "directional",
    auto_submit: bool = False,
    adapter: _StubAdapter | None = None,
) -> PaperLaneRunner:
    adapter = adapter or _StubAdapter()
    return PaperLaneRunner(
        worker_id=f"btc-{lane}-tastytrade",
        broker="tastytrade",
        lane=lane,
        symbol="BTCUSD",
        adapter=adapter,
        state_dir=tmp_path / "state",
        ledger_path=tmp_path / "trades.jsonl",
        anchor_price=100_000.0,
        probe_qty=1,
        auto_submit=auto_submit,
    )


class TestReconcileOnlyMode:

    def test_no_active_order_no_submission(self, tmp_path: Path) -> None:
        adapter = _StubAdapter()
        runner = _runner(tmp_path, auto_submit=False, adapter=adapter)
        snap = asyncio.run(runner.tick())
        assert snap["execution_state"] == "RECONCILE_ONLY"
        assert snap["active_order_id"] is None
        assert adapter.place_calls == []
        assert adapter.status_calls == []


class TestAutoSubmitLifecycle:

    def test_submits_single_probe_per_lane(self, tmp_path: Path) -> None:
        scripted = [
            OrderResult(order_id="srv-A", status=OrderStatus.OPEN, raw={}),
        ]
        adapter = _StubAdapter(scripted)
        runner = _runner(
            tmp_path, lane="directional",
            auto_submit=True, adapter=adapter,
        )
        snap = asyncio.run(runner.tick())
        assert snap["active_order_id"] == "srv-A"
        assert snap["execution_state"] == "ACTIVE"
        assert snap["submitted_orders"] == 1

        req = adapter.place_calls[0]
        assert req.symbol == "BTCUSD"
        assert req.side is Side.BUY
        assert req.order_type is OrderType.LIMIT
        # Directional lane -> probe price = anchor * 0.90 = 90000
        assert req.price == 90_000.0
        assert req.qty == 1.0
        # client_order_id is stable + includes worker id prefix
        assert req.client_order_id is not None
        assert req.client_order_id.startswith("btc-directional-tastytrade-")

    def test_grid_lane_uses_deeper_probe_price(self, tmp_path: Path) -> None:
        adapter = _StubAdapter([
            OrderResult(order_id="g1", status=OrderStatus.OPEN, raw={}),
        ])
        runner = _runner(tmp_path, lane="grid", auto_submit=True, adapter=adapter)
        asyncio.run(runner.tick())
        # Grid probe = anchor * 0.70 = 70000
        assert adapter.place_calls[0].price == 70_000.0

    def test_reconcile_transitions_fill(self, tmp_path: Path) -> None:
        # Tick 1: submits OPEN. Tick 2: status returns FILLED.
        scripted = [
            OrderResult(order_id="srv-B", status=OrderStatus.OPEN, raw={}),
            OrderResult(
                order_id="srv-B", status=OrderStatus.FILLED,
                filled_qty=1.0, avg_price=99_500.0, raw={},
            ),
        ]
        adapter = _StubAdapter(scripted)
        runner = _runner(tmp_path, auto_submit=True, adapter=adapter)

        asyncio.run(runner.tick())  # submit
        snap2 = asyncio.run(runner.tick())  # reconcile
        assert snap2["active_order_id"] is None  # cleared on terminal
        assert snap2["terminal_orders"] == 1
        assert snap2["execution_state"] == "ARMED"  # auto_submit still on

        # Ledger has two rows: submit + transition
        rows = [
            json.loads(line)
            for line in (tmp_path / "trades.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        events = [r.get("event") for r in rows]
        assert "submit" in events
        assert "transition" in events

    def test_reject_terminal_clears_and_logs(self, tmp_path: Path) -> None:
        scripted = [
            OrderResult(order_id="srv-R", status=OrderStatus.OPEN, raw={}),
            OrderResult(order_id="srv-R", status=OrderStatus.REJECTED, raw={}),
        ]
        adapter = _StubAdapter(scripted)
        runner = _runner(tmp_path, auto_submit=True, adapter=adapter)
        asyncio.run(runner.tick())
        snap2 = asyncio.run(runner.tick())
        assert snap2["active_order_id"] is None
        assert snap2["terminal_orders"] == 1


class TestStatePersistence:

    def test_second_runner_picks_up_existing_order(self, tmp_path: Path) -> None:
        adapter1 = _StubAdapter([
            OrderResult(order_id="srv-P", status=OrderStatus.OPEN, raw={}),
        ])
        runner1 = _runner(tmp_path, auto_submit=True, adapter=adapter1)
        asyncio.run(runner1.tick())

        # Fresh runner (worker restart) — should NOT submit a new probe
        # because the state file still reports srv-P as active.
        adapter2 = _StubAdapter([
            OrderResult(order_id="srv-P", status=OrderStatus.OPEN, raw={}),
        ])
        runner2 = _runner(tmp_path, auto_submit=True, adapter=adapter2)
        snap = asyncio.run(runner2.tick())
        # No new place_order call
        assert adapter2.place_calls == []
        # Just a reconciliation
        assert adapter2.status_calls == ["srv-P"]
        assert snap["active_order_id"] == "srv-P"


class TestCancelOnShutdown:

    def test_cancel_clears_active(self, tmp_path: Path) -> None:
        adapter = _StubAdapter([
            OrderResult(order_id="srv-C", status=OrderStatus.OPEN, raw={}),
        ])
        runner = _runner(tmp_path, auto_submit=True, adapter=adapter)
        asyncio.run(runner.tick())
        assert runner.state.active_order_id == "srv-C"

        ok = asyncio.run(shutdown(runner))  # type: ignore[func-returns-value]
        assert ok is None  # shutdown() wraps cancel_active; returns None
        # The cancel was issued
        assert adapter.cancel_calls == ["srv-C"]
        # State cleared
        assert runner.state.active_order_id is None


class TestLaneStateDefaults:

    def test_new_lane_state_empty(self) -> None:
        state = LaneState(
            worker_id="x", broker="tastytrade", lane="grid",
        )
        assert state.active_order_id is None
        assert state.active_order_status == "NONE"
        assert state.submitted_orders == 0

    def test_rejects_unknown_lane(self, tmp_path: Path) -> None:
        adapter = _StubAdapter()
        try:
            PaperLaneRunner(
                worker_id="x", broker="tastytrade", lane="banana",
                adapter=adapter,
                state_dir=tmp_path, ledger_path=tmp_path / "t.jsonl",
            )
        except ValueError as exc:
            assert "unsupported lane" in str(exc)
        else:
            msg = "expected ValueError for unsupported lane"
            raise AssertionError(msg)


class TestRunOneTickWrapper:

    def test_wrapper_returns_snapshot(self, tmp_path: Path) -> None:
        adapter = _StubAdapter()
        runner = _runner(tmp_path, auto_submit=False, adapter=adapter)
        snap = asyncio.run(run_one_tick(runner))
        assert snap["worker_id"] == "btc-directional-tastytrade"
        assert snap["execution_state"] == "RECONCILE_ONLY"

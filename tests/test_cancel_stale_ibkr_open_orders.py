from __future__ import annotations

from pathlib import Path

from eta_engine.scripts import cancel_stale_ibkr_open_orders as cancel_stale


class _FakeContract:
    def __init__(self, symbol: str, local_symbol: str = "") -> None:
        self.symbol = symbol
        self.localSymbol = local_symbol


class _FakeOrder:
    def __init__(self, order_id: int) -> None:
        self.orderId = order_id
        self.action = "BUY"
        self.orderType = "LMT"
        self.totalQuantity = 1.0
        self.permId = 0


class _FakeStatus:
    def __init__(self, status: str = "Submitted") -> None:
        self.status = status


class _FakeTrade:
    def __init__(self, contract: _FakeContract, order: _FakeOrder, order_status: _FakeStatus) -> None:
        self.contract = contract
        self.order = order
        self.orderStatus = order_status


class _FakeIB:
    def __init__(self, trades: list[_FakeTrade]) -> None:
        self.trades = trades
        self.cancelled: list[int] = []
        self.connected = False
        self.disconnected = False

    def connect(self, _host: str, _port: int, *, clientId: int, timeout: int) -> None:  # noqa: N803
        self.connected = True
        assert clientId > 0
        assert timeout > 0

    def openTrades(self) -> list[_FakeTrade]:  # noqa: N802
        return self.trades

    def cancelOrder(self, order: _FakeOrder) -> None:  # noqa: N802
        self.cancelled.append(order.orderId)

    def sleep(self, _seconds: float) -> None:
        return None

    def disconnect(self) -> None:
        self.disconnected = True


def _trade(symbol: str, order_id: int, *, local_symbol: str = "") -> _FakeTrade:
    return _FakeTrade(
        contract=_FakeContract(symbol=symbol, local_symbol=local_symbol),
        order=_FakeOrder(order_id=order_id),
        order_status=_FakeStatus(),
    )


def test_select_cancel_candidates_matches_only_audit_stale_symbols() -> None:
    trades = [
        _trade("MNQ", 101, local_symbol="MNQM6"),
        _trade("MCL", 102, local_symbol="MCLM6"),
        _trade("MYM", 103, local_symbol="MYMM6"),
    ]

    candidates = cancel_stale.select_cancel_candidates(
        trades,
        stale_flat_open_orders=[
            {"symbol": "MCLM6"},
            {"symbol": "MYMM6"},
        ],
    )

    assert [row.local_symbol for row in candidates] == ["MCLM6", "MYMM6"]
    assert [row.order_id for row in candidates] == [102, 103]


def test_cancel_stale_orders_dry_run_does_not_call_cancel(tmp_path: Path) -> None:
    fake_ib = _FakeIB([_trade("MNQ", 101, local_symbol="MNQM6"), _trade("MCL", 102, local_symbol="MCLM6")])
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        '{"stale_flat_open_orders":[{"symbol":"MCLM6"}]}',
        encoding="utf-8",
    )

    results = cancel_stale.cancel_stale_ibkr_open_orders(
        host="127.0.0.1",
        port=4002,
        client_id=9031,
        confirm=False,
        audit_path=audit_path,
        ib_factory=lambda: fake_ib,
    )

    assert [row.status for row in results] == ["dry_run"]
    assert fake_ib.cancelled == []
    assert fake_ib.disconnected is True


def test_cancel_stale_orders_confirm_cancels_only_candidates(tmp_path: Path) -> None:
    fake_ib = _FakeIB(
        [
            _trade("MNQ", 101, local_symbol="MNQM6"),
            _trade("MCL", 102, local_symbol="MCLM6"),
            _trade("MYM", 103, local_symbol="MYMM6"),
        ],
    )
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        '{"stale_flat_open_orders":[{"symbol":"MCLM6"},{"symbol":"MYMM6"}]}',
        encoding="utf-8",
    )

    results = cancel_stale.cancel_stale_ibkr_open_orders(
        host="127.0.0.1",
        port=4002,
        client_id=9031,
        confirm=True,
        audit_path=audit_path,
        ib_factory=lambda: fake_ib,
    )

    assert [row.status for row in results] == ["cancel_submitted", "cancel_submitted"]
    assert fake_ib.cancelled == [102, 103]
    assert fake_ib.disconnected is True

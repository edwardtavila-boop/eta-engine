"""Interactive Brokers LIVE paper venue — routes through TWS API (port 4002).

Replaces the mock HttpIbkrVenue with real order execution via ib_insync.
Uses the same safety gates (live_gate, fleet_risk_gate, position_cap, idempotency)
and env var names from the original venue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from eta_engine.venues.base import (
    ConnectionStatus,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    VenueBase,
    VenueConnectionReport,
)

logger = logging.getLogger(__name__)

# ─── SYMBOL → CONTRACT MAP ───────────────────────────────────────
# Futures: (symbol_root, exchange, multiplier)
FUTURES_MAP: dict[str, tuple[str, str, str]] = {
    "MNQ":  ("MNQ", "CME", "2"),
    "MNQ1": ("MNQ", "CME", "2"),
    "NQ":   ("NQ",  "CME", "20"),
    "NQ1":  ("NQ",  "CME", "20"),
    "ES":   ("ES",  "CME", "50"),
    "ES1":  ("ES",  "CME", "50"),
    "MES":  ("MES", "CME", "5"),
    "MBT":  ("MBT", "CME", "0.1"),
    "MET":  ("MET", "CME", "0.1"),
    "NG":   ("NG",  "NYMEX", "10000"),
    "CL":   ("CL",  "NYMEX", "1000"),
    "ZN":   ("ZN",  "CBOT", "1000"),
    "ZB":   ("ZB",  "CBOT", "1000"),
    "6E":   ("6E",  "CME", "125000"),
    "M6E":  ("M6E", "CME", "12500"),
}

# Crypto symbols — these use PAXOS spot or crypto contracts
CRYPTO_MAP: dict[str, tuple[str, str, str]] = {
    "BTC":   ("BTC", "PAXOS", "0.001"),
    "BTCUSD": ("BTC", "PAXOS", "0.001"),
    "ETH":   ("ETH", "PAXOS", "0.01"),
    "ETHUSD": ("ETH", "PAXOS", "0.01"),
    "SOL":   ("SOL", "PAXOS", "1"),
    "SOLUSD": ("SOL", "PAXOS", "1"),
}

# Current contract month (June 2026 → 202606)
# TODO: auto-roll detection
CONTRACT_MONTH = "202606"


def _make_contract(symbol: str) -> Any | None:  # noqa: ANN401 — ib_insync Contract is dynamically typed
    """Build an ib_insync Contract for the given symbol."""
    from ib_insync import Contract, Future, Stock

    sym = symbol.upper().strip()

    # Futures — must explicitly set exchange BEFORE creating Future object
    if sym in FUTURES_MAP:
        root, exchange, mult = FUTURES_MAP[sym]
        contract = Future(symbol=root, exchange=exchange, currency="USD")
        contract.lastTradeDateOrContractMonth = CONTRACT_MONTH
        contract.multiplier = mult
        contract.includeExpired = False
        return contract

    # Crypto (Paxos spot at IBKR)
    if sym in CRYPTO_MAP:
        root, exchange, mult = CRYPTO_MAP[sym]
        contract = Contract()
        contract.symbol = root
        contract.secType = "CRYPTO"
        contract.exchange = exchange
        contract.currency = "USD"
        return contract

    # Try as stock
    if sym in ("SPY", "QQQ", "AAPL", "TSLA", "NVDA"):
        contract = Stock(sym, "SMART", "USD")
        contract.exchange = "SMART"
        return contract

    return None


class LiveIbkrVenue(VenueBase):
    """Live IBKR execution venue through TWS API (ib_insync → port 4002)."""

    name: str = "ibkr"
    _ib: Any | None = None
    _client_id: int = 99
    _connected: bool = False
    _orders: dict[str, Any] = {}
    _lock: asyncio.Lock | None = None

    def __init__(self, config: Any | None = None) -> None:  # noqa: ANN401 — passthrough config
        super().__init__("DUQ319869", "")
        self.config = config  # kept for API compatibility, not used by TWS

    def connection_endpoint(self) -> str:
        return "127.0.0.1:4002"

    def has_credentials(self) -> bool:
        return True

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _ensure_connected(self) -> bool:
        """Connect to TWS API if not already connected. Returns True if ready."""
        # Fast path: existing connection still alive
        if self._ib is not None:
            try:
                if self._ib.isConnected():
                    return True
            except Exception:  # noqa: BLE001
                pass

        async with self._get_lock():
            if self._ib is not None:
                try:
                    if self._ib.isConnected():
                        return True
                except Exception:  # noqa: BLE001
                    pass

                # Old instance is dead — release the clientId at TWS before
                # asking for a new one, otherwise the next connectAsync hits
                # 'Error 326: clientId already in use' and times out.
                with contextlib.suppress(Exception):
                    self._ib.disconnect()
                await asyncio.sleep(0.5)
                self._ib = None

            from ib_insync import IB
            # Retry up to 3 times to handle cross-process clientId stickiness:
            # when the supervisor is bounced, TWS can hold the previous PID's
            # clientId for several seconds before releasing it, so the first
            # connect then hits Error 326 ("client id is already in use").
            # Backing off and retrying lets TWS finish the cleanup.
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    self._ib = IB()
                    await self._ib.connectAsync(
                        "127.0.0.1", 4002,
                        clientId=self._client_id, timeout=5,
                    )
                    self._connected = True
                    logger.info(
                        "LiveIbkrVenue connected to TWS on port 4002 "
                        "(clientId=%d, attempt=%d)",
                        self._client_id, attempt + 1,
                    )
                    return True
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    with contextlib.suppress(Exception):
                        if self._ib is not None:
                            self._ib.disconnect()
                    self._ib = None
                    if attempt < 2:
                        # Exponential backoff: 1.0s, 2.5s
                        await asyncio.sleep(1.0 + 1.5 * attempt)

            logger.warning(
                "LiveIbkrVenue could not connect to TWS after 3 attempts: %s",
                last_exc,
            )
            self._connected = False
            return False

    async def connect(self) -> VenueConnectionReport:
        ok = await self._ensure_connected()
        status = ConnectionStatus.READY if ok else ConnectionStatus.DEGRADED
        return VenueConnectionReport(
            venue=self.name,
            status=status,
            creds_present=True,
            details={
                "mode": "paper_live",
                "endpoint": "127.0.0.1:4002",
                "connected": ok,
                "operator_action": "Ensure IBKR CP Gateway is running on port 4002" if not ok else "ready",
            },
        )

    async def place_order(self, request: OrderRequest) -> OrderResult:
        # ── SAFETY GATES (same as mock venue) ──────────────────────
        from eta_engine.safety.live_gate import assert_live_allowed
        assert_live_allowed()

        from eta_engine.safety.fleet_risk_gate import assert_fleet_within_budget
        assert_fleet_within_budget(bot_id=getattr(request, "bot_id", None))

        from eta_engine.safety.position_cap import assert_within_caps
        signed_qty = float(getattr(request, "qty", 0) or 0)
        side_str = str(getattr(request, "side", "buy")).lower()
        signed_qty = -abs(signed_qty) if side_str in ("sell", "short") else abs(signed_qty)
        assert_within_caps(side="mnq", venue="ibkr", symbol=request.symbol, requested_delta=signed_qty)

        # ── CONTRACT RESOLUTION ────────────────────────────────────
        contract = _make_contract(request.symbol)
        if contract is None:
            return OrderResult(
                order_id=self.idempotency_key(request),
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": f"unknown IBKR contract for {request.symbol}"},
            )

        order_id = self.idempotency_key(request)

        # ── IDEMPOTENCY GUARD ─────────────────────────────────────
        try:
            from eta_engine.safety.idempotency import (
                IdempotencyError,
                check_or_register,
                record_result,
            )
            intent = {
                "symbol": request.symbol,
                "side": str(getattr(request, "side", "?")),
                "quantity": float(getattr(request, "qty", 0) or 0),
                "venue": "ibkr_live",
            }
            idem = check_or_register(
                client_order_id=order_id,
                venue="ibkr",
                symbol=request.symbol,
                intent_payload=intent,
            )
            if not idem.is_new:
                return OrderResult(
                    order_id=idem.broker_order_id or order_id,
                    status=OrderStatus.OPEN if idem.status == "submitted" else OrderStatus.REJECTED,
                    raw={
                        "venue": self.name,
                        "deduped": True,
                        "note": idem.note,
                        "cached_response": idem.response_payload or {},
                    },
                )
        except IdempotencyError as exc:
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": f"idempotency unavailable: {exc!r}"},
            )

        # ── CONNECT ───────────────────────────────────────────────
        if not await self._ensure_connected():
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": "TWS API connection on port 4002 failed"},
            )

        # ── BUILD ORDER ───────────────────────────────────────────
        from ib_insync import LimitOrder, MarketOrder

        action = "BUY" if str(getattr(request, "side", "BUY")).upper() == "BUY" else "SELL"
        qty = int(abs(float(getattr(request, "qty", 1) or 1)))
        reduce_only = bool(getattr(request, "reduce_only", False))
        stop_price = getattr(request, "stop_price", None)
        target_price = getattr(request, "target_price", None)
        order_type = getattr(request, "order_type", OrderType.MARKET)

        # ── BRACKET REQUIREMENT ───────────────────────────────────
        # An entry MUST attach a bracket (parent MKT + STP child + LMT
        # child).  Naked entries are physically refused at this layer:
        # a process crash mid-position would otherwise leave an
        # unprotected position at the broker.  reduce_only exits keep
        # the simple single-leg path (closing a position should never
        # be gated by a bracket requirement).
        is_entry = not reduce_only
        if is_entry and (stop_price is None or target_price is None):
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                raw={
                    "venue": self.name,
                    "reason": "entry order missing bracket (stop_price + target_price required)",
                    "stop_price": stop_price,
                    "target_price": target_price,
                },
            )

        try:
            if is_entry:
                # ib_insync.bracketOrder returns [parent, takeProfit,
                # stopLoss] with transmit chained (parent.transmit=False,
                # tp.transmit=False, sl.transmit=True) so the broker sees
                # the OCO group atomically.
                bracket = self._ib.bracketOrder(
                    action,
                    qty,
                    limitPrice=float(target_price),
                    takeProfitPrice=float(target_price),
                    stopLossPrice=float(stop_price),
                )
                trades = []
                for ib_order in bracket:
                    trades.append(self._ib.placeOrder(contract, ib_order))
                trade = trades[0]
                self._orders[order_id] = trade
                logger.info(
                    "LiveIbkrVenue BRACKET: %s %s %d MKT entry sl=%.4f tp=%.4f → parentId=%s",
                    action, request.symbol, qty, float(stop_price), float(target_price),
                    trade.order.orderId,
                )
            else:
                # Reduce-only exit — single MKT/LMT, no bracket
                if order_type == OrderType.LIMIT and getattr(request, "price", None):
                    ib_order = LimitOrder(action, qty, float(request.price))
                else:
                    ib_order = MarketOrder(action, qty)
                trade = self._ib.placeOrder(contract, ib_order)
                self._orders[order_id] = trade
                logger.info(
                    "LiveIbkrVenue EXIT: %s %s %d @ %s → orderId=%s",
                    action, request.symbol, qty, order_type.value, trade.order.orderId,
                )

            # ── RECORD RESULT ─────────────────────────────────────
            with contextlib.suppress(Exception):
                record_result(
                    client_order_id=order_id,
                    status="submitted",
                    broker_order_id=str(trade.order.orderId),
                    response_payload={
                        "venue": self.name,
                        "mode": "paper_live",
                        "ibkr_order_id": trade.order.orderId,
                        "symbol": request.symbol,
                        "action": action,
                        "qty": qty,
                        "is_bracket": is_entry,
                        "stop_price": stop_price,
                        "target_price": target_price,
                    },
                )

            return OrderResult(
                order_id=order_id,
                status=OrderStatus.OPEN,
                raw={
                    "venue": self.name,
                    "mode": "paper_live",
                    "ibkr_order_id": trade.order.orderId,
                    "symbol": request.symbol,
                    "action": action,
                    "qty": qty,
                    "is_bracket": is_entry,
                    "stop_price": stop_price,
                    "target_price": target_price,
                },
            )
        except Exception as exc:
            logger.error("LiveIbkrVenue order failed: %s", exc)
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": str(exc)},
            )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        trade = self._orders.pop(order_id, None)
        if trade is None:
            return False
        try:
            self._ib.cancelOrder(trade.order)
            return True
        except Exception:
            return False

    async def get_positions(self) -> list[dict[str, Any]]:
        if not await self._ensure_connected():
            return []
        try:
            positions = list(self._ib.positions())
            return [
                {
                    "symbol": p.contract.symbol,
                    "exchange": p.contract.exchange,
                    "position": float(p.position),
                    "avg_cost": float(p.avgCost) if p.avgCost else 0.0,
                }
                for p in positions
            ]
        except Exception:
            return []

    async def get_balance(self) -> dict[str, float]:
        if not await self._ensure_connected():
            return {}
        try:
            summary = list(self._ib.accountSummary())
            out: dict[str, float] = {}
            for s in summary:
                if s.tag == "NetLiquidation":
                    out["net_liquidation"] = float(s.value)
                elif s.tag == "AvailableFunds":
                    out["available_funds"] = float(s.value)
                elif s.tag == "TotalCashValue":
                    out["total_cash"] = float(s.value)
            return out
        except Exception:
            return {}

    async def get_order_status(self, symbol: str, order_id: str) -> OrderResult | None:
        trade = self._orders.get(order_id)
        if trade is None:
            return None
        status_map = {
            "Submitted": OrderStatus.OPEN,
            "Filled": OrderStatus.FILLED,
            "Cancelled": OrderStatus.REJECTED,
        }
        ib_status = str(trade.orderStatus.status) if trade.orderStatus else "Unknown"
        return OrderResult(
            order_id=order_id,
            status=status_map.get(ib_status, OrderStatus.OPEN),
            raw={
                "venue": self.name,
                "ib_status": ib_status,
                "filled": trade.orderStatus.filled if trade.orderStatus else 0,
            },
        )

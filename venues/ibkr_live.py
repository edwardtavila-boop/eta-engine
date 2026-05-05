"""Interactive Brokers LIVE paper venue — routes through TWS API (port 4002).

Replaces the mock HttpIbkrVenue with real order execution via ib_insync.
Uses the same safety gates (live_gate, fleet_risk_gate, position_cap, idempotency)
and env var names from the original venue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
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

_IBKR_REJECTED_STATUSES = {"ApiCancelled", "Cancelled", "Inactive"}
_IBKR_CONFIRMED_STATUSES = {"Filled", "PreSubmitted", "Submitted"}

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
    "RTY":  ("RTY", "CME", "50"),
    "M2K":  ("M2K", "CME", "5"),
    "MBT":  ("MBT", "CME", "0.1"),
    "MET":  ("MET", "CME", "0.1"),
    "NG":   ("NG",  "NYMEX", "10000"),
    "CL":   ("CL",  "NYMEX", "1000"),
    "MCL":  ("MCL", "NYMEX", "100"),
    "GC":   ("GC",  "COMEX", "100"),
    "MGC":  ("MGC", "COMEX", "10"),
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


def _build_futures_bracket_orders(
    ib: Any,  # noqa: ANN401 - ib_insync IB instance is dynamic
    *,
    action: str,
    qty: float | int,
    order_type: OrderType,
    entry_price: float | None,
    stop_price: float,
    target_price: float,
) -> list[Any]:
    """Build a parent + take-profit + stop-loss bracket for futures.

    ``IB.bracketOrder`` always creates a limit parent. The supervisor's
    paper_live path sends market entries by default, so build the bracket
    manually when the parent should be MKT.
    """
    from ib_insync import LimitOrder, MarketOrder, StopOrder

    reverse_action = "BUY" if action == "SELL" else "SELL"
    if order_type == OrderType.LIMIT and entry_price is not None:
        parent = LimitOrder(
            action,
            qty,
            float(entry_price),
            orderId=ib.client.getReqId(),
            transmit=False,
        )
    else:
        parent = MarketOrder(
            action,
            qty,
            orderId=ib.client.getReqId(),
            transmit=False,
        )
    take_profit = LimitOrder(
        reverse_action,
        qty,
        float(target_price),
        orderId=ib.client.getReqId(),
        transmit=False,
        parentId=parent.orderId,
    )
    stop_loss = StopOrder(
        reverse_action,
        qty,
        float(stop_price),
        orderId=ib.client.getReqId(),
        transmit=True,
        parentId=parent.orderId,
    )
    return [_apply_futures_session_defaults(order) for order in (parent, take_profit, stop_loss)]


def _apply_futures_session_defaults(order: Any) -> Any:  # noqa: ANN401 - ib_insync orders are dynamic
    """Apply futures-safe execution defaults to IBKR order objects.

    CME/NYMEX futures trade through the evening Globex session. Without
    outsideRth enabled, TWS can accept an order as Submitted while holding it
    out of execution until liquid/RTH hours.
    """
    order.tif = "GTC"
    order.outsideRth = True
    order.conditionsIgnoreRth = True
    return order


def _submit_confirm_seconds() -> float:
    raw = os.environ.get("ETA_IBKR_SUBMIT_CONFIRM_SECONDS", "2.0").strip()
    try:
        seconds = float(raw)
    except ValueError:
        logger.warning("ETA_IBKR_SUBMIT_CONFIRM_SECONDS=%r is invalid; using 2.0", raw)
        return 2.0
    return max(0.0, min(seconds, 10.0))


def _trade_submit_snapshot(trade: Any) -> dict[str, Any]:  # noqa: ANN401 - ib_insync Trade is dynamic
    order = getattr(trade, "order", None)
    status = getattr(trade, "orderStatus", None)
    return {
        "order_id": getattr(order, "orderId", None),
        "perm_id": getattr(order, "permId", None)
        or getattr(status, "permId", None)
        or 0,
        "status": str(getattr(status, "status", "") or "Unknown"),
        "filled": float(getattr(status, "filled", 0.0) or 0.0),
        "remaining": float(getattr(status, "remaining", 0.0) or 0.0),
        "avg_fill_price": float(getattr(status, "avgFillPrice", 0.0) or 0.0),
    }


def _ibkr_submission_reject_reason(statuses: list[dict[str, Any]]) -> str:
    rejected = [
        item for item in statuses
        if str(item.get("status") or "") in _IBKR_REJECTED_STATUSES
    ]
    if rejected:
        return f"IBKR rejected/cancelled submitted order legs: {rejected}"
    confirmed = [
        item for item in statuses
        if (
            str(item.get("status") or "") in _IBKR_CONFIRMED_STATUSES
            or int(item.get("perm_id") or 0) > 0
        )
    ]
    if statuses and not confirmed:
        return f"IBKR submission unconfirmed after confirm window: {statuses}"
    return ""

# Per-process cache of resolved front-month YYYYMM strings keyed by
# (root, exchange). Populated lazily on first contract build via an IB
# qualifyContracts() call against ContFuture, then reused for the rest
# of the session — auto-rolls happen at process restart, not mid-run.
_FRONT_MONTH_CACHE: dict[tuple[str, str], str] = {}


async def _resolve_front_month_mnq(ib: Any, root: str = "MNQ", exchange: str = "CME") -> str:  # noqa: ANN401 — ib_insync IB instance
    """Resolve the active front-month YYYYMM for a futures root via IB.

    Uses ``ib_insync.ContFuture`` qualified through
    ``ib.qualifyContractsAsync``; IB returns the active front-month
    contract automatically. The result is cached per (root, exchange)
    so subsequent calls are free.

    Async because the broker_router runs inside an asyncio event loop;
    calling the sync ``qualifyContracts`` from inside an event loop
    raises ``RuntimeError('This event loop is already running')``.

    Fails closed: if IB cannot resolve the contract, raises a
    RuntimeError. Hardcoded fallback was the production-breaking
    footgun this function exists to eliminate.
    """
    cache_key = (root, exchange)
    cached = _FRONT_MONTH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    from ib_insync import ContFuture

    cont = ContFuture(symbol=root, exchange=exchange, currency="USD")
    try:
        qualified = await ib.qualifyContractsAsync(cont)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Failed to resolve front-month {root} via IB qualifyContractsAsync: {exc!r}",
        ) from exc

    if not qualified:
        raise RuntimeError(
            f"IB returned no qualified contract for ContFuture({root}, {exchange})",
        )

    last_trade = getattr(qualified[0], "lastTradeDateOrContractMonth", "") or ""
    yyyymm = last_trade[:6]
    if len(yyyymm) != 6 or not yyyymm.isdigit():
        raise RuntimeError(
            f"IB returned invalid lastTradeDateOrContractMonth={last_trade!r} "
            f"for {root}/{exchange} — cannot derive YYYYMM",
        )

    _FRONT_MONTH_CACHE[cache_key] = yyyymm
    logger.info(
        "Resolved front-month %s/%s = %s (cached for session)",
        root, exchange, yyyymm,
    )
    return yyyymm


async def _make_contract(symbol: str, ib: Any | None = None) -> Any | None:  # noqa: ANN401 — ib_insync types are dynamic
    """Build an ib_insync Contract for the given symbol.

    For futures, an active IB connection (``ib``) is required so the
    front-month contract month can be resolved at first use. Passing
    ``ib=None`` for a futures symbol raises a RuntimeError — fail closed
    rather than silently targeting a stale month.

    Async because front-month resolution must use the async
    qualifyContracts variant when called from inside an event loop
    (which the broker_router is).
    """
    from ib_insync import Contract, Future, Stock

    sym = symbol.upper().strip()

    # Futures — must explicitly set exchange BEFORE creating Future object
    if sym in FUTURES_MAP:
        root, exchange, mult = FUTURES_MAP[sym]
        if ib is None:
            raise RuntimeError(
                f"_make_contract({sym}) requires an IB connection to resolve "
                f"the front-month contract; got ib=None",
            )
        contract_month = await _resolve_front_month_mnq(ib, root=root, exchange=exchange)
        contract = Future(symbol=root, exchange=exchange, currency="USD")
        contract.lastTradeDateOrContractMonth = contract_month
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
        self._client_id = type(self)._client_id
        # Per-process unique clientId. Multiple consumers (supervisor + broker_router)
        # collide on the hardcoded 99 → IB Error 326. Read ETA_IBKR_CLIENT_ID; if 0,
        # let TWS auto-assign. Default 99 preserves legacy behavior.
        env_cid = os.environ.get("ETA_IBKR_CLIENT_ID", "").strip()
        if env_cid:
            try:
                self._client_id = int(env_cid)
            except ValueError:
                logger.warning(
                    "ETA_IBKR_CLIENT_ID=%r is not an int; falling back to default %d",
                    env_cid, self._client_id,
                )

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

        # ── SYMBOL VALIDATION (cheap, no IB connection needed) ────
        # Reject unknown symbols early so we don't burn an idempotency
        # slot or open a TWS connection just to find out the symbol map
        # has no entry for this ticker. The full contract is built below
        # after _ensure_connected() so the front-month resolver has an
        # active IB instance to query.
        _sym_norm = request.symbol.upper().strip()
        if _sym_norm not in FUTURES_MAP and _sym_norm not in CRYPTO_MAP and _sym_norm not in (
            "SPY", "QQQ", "AAPL", "TSLA", "NVDA",
        ):
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

        def _record_idempotency_status(
            status: str,
            reason: str,
            **extra: Any,  # noqa: ANN401 - payload values are broker diagnostics
        ) -> None:
            with contextlib.suppress(Exception):
                payload = {"venue": self.name, "reason": reason, **extra}
                record_result(
                    client_order_id=order_id,
                    status=status,
                    response_payload=payload,
                )

        # ── CONNECT ───────────────────────────────────────────────
        if not await self._ensure_connected():
            reason = "TWS API connection on port 4002 failed"
            _record_idempotency_status("retryable_failed", reason)
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": reason},
            )

        # ── CONTRACT RESOLUTION (needs live IB for front-month) ───
        # Built here, AFTER _ensure_connected, so _resolve_front_month_mnq
        # can query IB. Result is cached per (root, exchange) so this hits
        # IB only on the first order per process.
        try:
            contract = await _make_contract(request.symbol, self._ib)
        except Exception as exc:  # noqa: BLE001 — surface the resolver failure as a reject
            logger.error("LiveIbkrVenue contract resolution failed: %s", exc)
            reason = f"contract resolution failed: {exc!r}"
            _record_idempotency_status("rejected", reason, symbol=request.symbol)
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": reason},
            )
        if contract is None:
            reason = f"unknown IBKR contract for {request.symbol}"
            _record_idempotency_status("rejected", reason, symbol=request.symbol)
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": reason},
            )

        # ── BUILD ORDER ───────────────────────────────────────────
        from ib_insync import LimitOrder, MarketOrder

        action = "BUY" if str(getattr(request, "side", "BUY")).upper() == "BUY" else "SELL"
        reduce_only = bool(getattr(request, "reduce_only", False))
        stop_price = getattr(request, "stop_price", None)
        target_price = getattr(request, "target_price", None)
        order_type = getattr(request, "order_type", OrderType.MARKET)
        # PAXOS crypto rejects bracketOrder (Error 10052 'Invalid time in
        # force' on the LimitOrder parent, Error 321 'size value cannot
        # be zero' on the children). Skip the bracket apparatus for
        # crypto and submit a plain market entry; the supervisor's exit
        # logic must own stop/target management for crypto positions.
        is_crypto = getattr(contract, "secType", "") == "CRYPTO"

        # Account-level crypto trading must be explicitly opted-in.
        # The paper account DUQ319869 returns Cryptocurrency=0 in the
        # account summary — orders submit but are silently rejected at
        # IBKR before producing fills. Honor an env opt-in so a future
        # account upgrade flips this on without code changes.
        if is_crypto and os.getenv("ETA_IBKR_CRYPTO", "").lower() not in {"1", "true", "yes", "on"}:
            reason = (
                "crypto disabled - account lacks crypto permissions; "
                "set ETA_IBKR_CRYPTO=1 once enabled at IBKR"
            )
            _record_idempotency_status("rejected", reason, symbol=request.symbol)
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                raw={
                    "venue": self.name,
                    "reason": reason,
                    "legacy_reason": (
                        "crypto disabled — account lacks crypto permissions; "
                        "set ETA_IBKR_CRYPTO=1 once enabled at IBKR"
                    ),
                    "symbol": request.symbol,
                },
            )
        # Futures contracts trade in whole-lot quanta; crypto on PAXOS
        # accepts fractional qty down to 1e-3 BTC, 1e-2 ETH, etc. so we
        # must NOT floor crypto qty to int.
        _raw_qty = abs(float(getattr(request, "qty", 1) or 1))
        qty: float | int = _raw_qty if is_crypto else int(_raw_qty)
        if qty <= 0:
            reason = f"order quantity rounds to zero for {request.symbol}: raw_qty={_raw_qty}"
            _record_idempotency_status(
                "rejected",
                reason,
                symbol=request.symbol,
                raw_qty=_raw_qty,
            )
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": reason, "raw_qty": _raw_qty},
            )

        # ── BRACKET REQUIREMENT (futures only) ────────────────────
        # Futures entries MUST attach a bracket (parent MKT + STP child
        # + LMT child).  Naked entries are physically refused at this
        # layer: a process crash mid-position would otherwise leave an
        # unprotected position at the broker.  reduce_only exits keep
        # the simple single-leg path (closing a position should never
        # be gated by a bracket requirement).
        is_entry = not reduce_only
        if is_entry and not is_crypto and (stop_price is None or target_price is None):
            reason = "entry order missing bracket (stop_price + target_price required)"
            _record_idempotency_status(
                "rejected",
                reason,
                stop_price=stop_price,
                target_price=target_price,
            )
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                raw={
                    "venue": self.name,
                    "reason": reason,
                    "stop_price": stop_price,
                    "target_price": target_price,
                },
            )

        try:
            submitted_trades = []
            if is_entry and is_crypto:
                # PAXOS doesn't accept ib_insync.bracketOrder, so we build
                # the bracket ourselves: market entry → wait for fill →
                # standing stop + target with OCO simulated by fillEvent
                # callbacks (when one fires, the other is cancelled).
                ib_order = MarketOrder(action, qty)
                ib_order.tif = "GTC"
                trade = self._ib.placeOrder(contract, ib_order)
                self._orders[order_id] = trade
                submitted_trades = [trade]
                logger.info(
                    "LiveIbkrVenue CRYPTO ENTRY: %s %s %.6f MKT → orderId=%s",
                    action, request.symbol, float(qty), trade.order.orderId,
                )

                if stop_price is not None and target_price is not None:
                    # Wait for entry fill before placing protective siblings.
                    # PAXOS market orders typically fill in under a second;
                    # 10s ceiling protects against a stuck-pending entry
                    # leaving the position naked while we sleep forever.
                    for _ in range(100):
                        await asyncio.sleep(0.1)
                        if trade.orderStatus.status == "Filled":
                            break

                    if trade.orderStatus.status == "Filled":
                        opposite = "SELL" if action == "BUY" else "BUY"
                        # Trailing stop opt-in: ETA_CRYPTO_TRAILING_PCT > 0
                        # uses an IB trailing-stop order instead of a fixed
                        # stop. The trail amount is a percentage of the
                        # entry fill price, so as price moves favorably
                        # the broker ratchets the stop with it. Unlike the
                        # fixed StopOrder, this locks in profit. Default
                        # 0 (off) — operators opt in when they're ready.
                        trail_pct_raw = os.getenv("ETA_CRYPTO_TRAILING_PCT", "0")
                        try:
                            trail_pct = float(trail_pct_raw or 0)
                        except ValueError:
                            trail_pct = 0.0
                        from ib_insync import Order, StopOrder
                        if trail_pct > 0:
                            entry_fill = float(
                                trade.orderStatus.avgFillPrice or stop_price
                            )
                            trail_amount = round(entry_fill * (trail_pct / 100.0), 4)
                            stop_order = Order(
                                action=opposite,
                                totalQuantity=qty,
                                orderType="TRAIL",
                                trailingPercent=trail_pct,
                                # auxPrice = trailing $ amount (alternative
                                # to trailingPercent; we set both to be
                                # explicit and TWS picks one).
                                auxPrice=trail_amount,
                                tif="GTC",
                            )
                        else:
                            stop_order = StopOrder(opposite, qty, float(stop_price))
                            stop_order.tif = "GTC"
                        target_order = LimitOrder(opposite, qty, float(target_price))
                        target_order.tif = "GTC"

                        stop_trade = self._ib.placeOrder(contract, stop_order)
                        target_trade = self._ib.placeOrder(contract, target_order)

                        # OCO emulation: when either fills, cancel the
                        # sibling. Wrapped in suppress so a race (sibling
                        # already terminal) doesn't propagate.
                        # ib_insync Trade is dynamically typed.
                        def _make_canceler(other_trade):  # noqa: ANN001, ANN202
                            def _cb(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
                                with contextlib.suppress(Exception):
                                    if other_trade.orderStatus.status not in (
                                        "Filled", "Cancelled", "ApiCancelled",
                                    ):
                                        self._ib.cancelOrder(other_trade.order)
                            return _cb

                        stop_trade.filledEvent += _make_canceler(target_trade)
                        target_trade.filledEvent += _make_canceler(stop_trade)

                        self._orders[order_id + ":stop"] = stop_trade
                        self._orders[order_id + ":target"] = target_trade
                        logger.info(
                            "LiveIbkrVenue CRYPTO BRACKET: filled @ %.4f, "
                            "stop=%.4f (id=%s) target=%.4f (id=%s) — OCO via callback",
                            float(trade.orderStatus.avgFillPrice or 0.0),
                            float(stop_price), stop_trade.order.orderId,
                            float(target_price), target_trade.order.orderId,
                        )
                    else:
                        logger.warning(
                            "LiveIbkrVenue CRYPTO entry not filled in 10s "
                            "(status=%s) — naked position, no stop/target placed",
                            trade.orderStatus.status,
                        )
            elif is_entry:
                # Build the bracket manually so MARKET entries remain
                # market parents. ib_insync.bracketOrder always creates a
                # limit parent, which made the old "MKT entry" log line lie.
                bracket = _build_futures_bracket_orders(
                    self._ib,
                    action=action,
                    qty=qty,
                    order_type=order_type,
                    entry_price=getattr(request, "price", None),
                    stop_price=float(stop_price),
                    target_price=float(target_price),
                )
                trades = []
                for ib_order in bracket:
                    trades.append(self._ib.placeOrder(contract, ib_order))
                trade = trades[0]
                self._orders[order_id] = trade
                submitted_trades = trades
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
                if not is_crypto:
                    _apply_futures_session_defaults(ib_order)
                trade = self._ib.placeOrder(contract, ib_order)
                self._orders[order_id] = trade
                submitted_trades = [trade]
                logger.info(
                    "LiveIbkrVenue EXIT: %s %s %d @ %s → orderId=%s",
                    action, request.symbol, qty, order_type.value, trade.order.orderId,
                )

            submit_confirm_seconds = _submit_confirm_seconds()
            if submitted_trades and submit_confirm_seconds > 0:
                await asyncio.sleep(submit_confirm_seconds)
            ib_statuses = [_trade_submit_snapshot(item) for item in submitted_trades]
            reject_reason = _ibkr_submission_reject_reason(ib_statuses)
            if reject_reason:
                logger.warning(
                    "LiveIbkrVenue submission not accepted: %s",
                    reject_reason,
                )
                with contextlib.suppress(Exception):
                    record_result(
                        client_order_id=order_id,
                        status="rejected",
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
                            "ib_statuses": ib_statuses,
                            "reason": reject_reason,
                        },
                    )
                return OrderResult(
                    order_id=order_id,
                    status=OrderStatus.REJECTED,
                    raw={
                        "venue": self.name,
                        "reason": reject_reason,
                        "ibkr_order_id": trade.order.orderId,
                        "symbol": request.symbol,
                        "action": action,
                        "qty": qty,
                        "is_bracket": is_entry,
                        "stop_price": stop_price,
                        "target_price": target_price,
                        "ib_statuses": ib_statuses,
                    },
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
                        "ib_statuses": ib_statuses,
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
                    "ib_statuses": ib_statuses,
                },
            )
        except Exception as exc:
            logger.error("LiveIbkrVenue order failed: %s", exc)
            _record_idempotency_status("failed_unknown", str(exc), symbol=request.symbol)
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

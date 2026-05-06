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
from datetime import UTC, datetime, time
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

# ─── Crypto guard startup-log latch ──────────────────────────────
#
# The local crypto pre-check (see place_order) emits one of two startup
# notices the FIRST time it runs in a process:
#
#   * INFO when ETA_IBKR_CRYPTO=1 — local guard disabled, IBKR-side
#     permissions decide the order's fate.
#   * WARN when ETA_IBKR_CRYPTO is unset/0 AND a crypto symbol is
#     observed for the first time — actionable hint, fired once so
#     subsequent crypto orders don't spam the log.
#
# Per-order rejection still emits a structured rejection result so the
# router/sidecar still see "what happened" — only the *log line* is
# deduplicated. The latch is process-local; a supervisor restart resets
# it intentionally so each new process logs its current crypto posture
# at least once.
_CRYPTO_GUARD_LOG_EMITTED: bool = False


def _crypto_env_enabled() -> bool:
    """True iff ETA_IBKR_CRYPTO is set to a truthy value (case-insensitive)."""
    return os.getenv("ETA_IBKR_CRYPTO", "").strip().lower() in {"1", "true", "yes", "on"}


def _emit_crypto_guard_startup_log() -> None:
    """Fire the once-per-process startup log for the crypto guard.

    Called from the per-order path the first time a crypto order is
    seen. Subsequent crypto orders in the same process are no-ops.
    Tests can reset the latch via ``_reset_crypto_guard_log_latch``.
    """
    global _CRYPTO_GUARD_LOG_EMITTED
    if _CRYPTO_GUARD_LOG_EMITTED:
        return
    _CRYPTO_GUARD_LOG_EMITTED = True
    if _crypto_env_enabled():
        logger.info(
            "ETA_IBKR_CRYPTO=1 — local crypto guard disabled; "
            "relying on IBKR-side permissions"
        )
    else:
        logger.warning(
            "crypto guard active — local pre-check will reject crypto orders. "
            "Enable IBKR-side crypto permissions then `setx ETA_IBKR_CRYPTO 1 /M` "
            "and restart the broker_router task."
        )


def _reset_crypto_guard_log_latch() -> None:
    """Test hook: clear the once-per-process latch so the next call to
    :func:`_emit_crypto_guard_startup_log` re-emits."""
    global _CRYPTO_GUARD_LOG_EMITTED
    _CRYPTO_GUARD_LOG_EMITTED = False

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
    # CME Euro FX (full size): IB indexes the standard contract under
    # symbol "EUR", not the operator-friendly "6E" trading code that
    # appears on charts and in the supervisor. ContFuture(6E, CME)
    # returns "no security definition"; ContFuture(EUR, CME) resolves
    # cleanly. Keep "6E" as the supervisor-facing key so routing yaml
    # stays readable, but use "EUR" as the IB symbol. Caught by smoke
    # harness 2026-05-05.
    "6E":   ("EUR", "CME", "125000"),
    # Micro Euro FX: IB does index the micro under "M6E" (verified by
    # the same smoke harness — qualified cleanly with month=20260615).
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

# ─── ASSET-CLASS PRIMARY SESSION MAP (America/New_York local time) ──
# Microstructure review (2026-05-05) flagged that MARKET entries during
# low-liquidity globex windows on CL/NG/6E/M6E/ZN/ZB/GC took 5-10 ticks
# of slippage. These windows describe each contract's PRIMARY (deepest
# liquidity) session — outside this window, MKT is converted to a
# marketable LIMIT with a small tick buffer to bound slippage.
#
# Window keys are the IB ROOT symbol (as stored in FUTURES_MAP[sym][0]),
# not the operator-facing trading code. CME Euro FX trades under "EUR"
# at IB; supervisor sees "6E" but FUTURES_MAP rewrites to "EUR" before
# the lookup hits this table.
_ASSET_PRIMARY_SESSION_ET: dict[str, tuple[time, time]] = {
    # CME equity-index futures: RTH 09:30 - 16:00 ET
    "MNQ": (time(9, 30), time(16, 0)),
    "NQ":  (time(9, 30), time(16, 0)),
    "ES":  (time(9, 30), time(16, 0)),
    "MES": (time(9, 30), time(16, 0)),
    "RTY": (time(9, 30), time(16, 0)),
    "M2K": (time(9, 30), time(16, 0)),
    # CME crypto micros (24x5 by venue, but primary liquidity tracks RTH)
    "MBT": (time(9, 30), time(16, 0)),
    "MET": (time(9, 30), time(16, 0)),
    # NYMEX energy: 09:00 - 14:30 ET pit/RTH window
    "CL":  (time(9, 0),  time(14, 30)),
    "MCL": (time(9, 0),  time(14, 30)),
    "NG":  (time(9, 0),  time(14, 30)),
    # COMEX metals: 08:20 - 13:30 ET
    "GC":  (time(8, 20), time(13, 30)),
    "MGC": (time(8, 20), time(13, 30)),
    # CME FX: 08:20 - 15:00 ET (note IB indexes Euro FX as "EUR", not "6E")
    "EUR": (time(8, 20), time(15, 0)),
    "M6E": (time(8, 20), time(15, 0)),
    # CBOT rates: 08:20 - 15:00 ET
    "ZN":  (time(8, 20), time(15, 0)),
    "ZB":  (time(8, 20), time(15, 0)),
}


def _in_primary_session(symbol: str, now_utc: datetime | None = None) -> bool:
    """Return True iff ``symbol`` is currently inside its primary
    (deep-liquidity) session.

    - Looks the symbol up via ``FUTURES_MAP`` to obtain the IB root
      symbol (e.g. ``"6E"`` -> ``"EUR"``), then checks
      ``_ASSET_PRIMARY_SESSION_ET``.
    - Converts ``now_utc`` (or ``datetime.now(UTC)``) to America/New_York
      via ``zoneinfo`` from the stdlib.
    - Weekend (Saturday/Sunday) returns False — futures globex weekend
      breaks should never see MARKET entries.
    - Unknown symbols default permissive (True) so an unmapped contract
      doesn't accidentally block trading.
    - Defensive: any failure (zoneinfo unavailable on the host build,
      lookup error, anything else) returns True so a misconfigured prod
      box doesn't suddenly stop trading.
    """
    try:
        sym_norm = symbol.upper().strip()
        # Map supervisor-facing symbol -> IB root symbol via FUTURES_MAP.
        ib_root: str | None = None
        if sym_norm in FUTURES_MAP:
            ib_root = FUTURES_MAP[sym_norm][0]
        else:
            # Bare root ("CL", "ZN") not in FUTURES_MAP keys but in the
            # session table directly — treat as a valid lookup.
            if sym_norm in _ASSET_PRIMARY_SESSION_ET:
                ib_root = sym_norm

        if ib_root is None or ib_root not in _ASSET_PRIMARY_SESSION_ET:
            # Unknown symbol — fail permissive so we don't block trading
            # on a contract we haven't classified yet.
            return True

        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            # Some Python builds ship without zoneinfo (mostly slim
            # containers). Fail permissive rather than blocking trades.
            logger.warning(
                "zoneinfo unavailable; skipping primary-session check for %s",
                sym_norm,
            )
            return True

        now = now_utc if now_utc is not None else datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        et = now.astimezone(ZoneInfo("America/New_York"))

        # Weekday gate: Mon=0 ... Fri=4. Saturday/Sunday → not in session.
        if et.weekday() >= 5:
            return False

        start, end = _ASSET_PRIMARY_SESSION_ET[ib_root]
        return start <= et.time() <= end
    except Exception as exc:  # noqa: BLE001 — must never raise
        logger.warning(
            "primary-session check failed for %s (%s); defaulting permissive",
            symbol, exc,
        )
        return True


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
    """Resolve the active front-month contract date for a futures root via IB.

    Returns the full date string IB returned for the qualified ContFuture
    (e.g. ``"20260619"`` for MNQ Jun 2026, ``"20260519"`` for CL May
    2026). Truncating this to ``YYYYMM`` works for products that IB
    indexes by month (CME equity-index futures: MNQ/NQ/ES/MES/RTY/M2K)
    but BREAKS for products IB indexes by exact expiry (NYMEX energy:
    CL/MCL/NG and certain metals). Returning the full date works for
    both cases — a more-specific ``lastTradeDateOrContractMonth`` is
    always accepted by ``qualifyContractsAsync``.

    Caught by the per-ticker smoke harness (2026-05-05): NYMEX CL/MCL/NG
    failed with HTTP "No security definition" when this function
    returned ``"202605"`` even though the ContFuture qualifier had
    returned ``"20260519"`` (or similar). The previous truncation to 6
    chars discarded the discriminating expiry day.

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
    if not last_trade or not last_trade[:6].isdigit() or len(last_trade) not in (6, 8):
        raise RuntimeError(
            f"IB returned invalid lastTradeDateOrContractMonth={last_trade!r} "
            f"for {root}/{exchange} — expected YYYYMM or YYYYMMDD",
        )

    _FRONT_MONTH_CACHE[cache_key] = last_trade
    logger.info(
        "Resolved front-month %s/%s = %s (cached for session)",
        root, exchange, last_trade,
    )
    return last_trade


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


async def _qualify_order_contract(
    ib: Any,  # noqa: ANN401 — ib_insync IB instance is dynamic
    contract: Any,  # noqa: ANN401 — ib_insync contract is dynamic
    symbol: str,
) -> Any:  # noqa: ANN401 — ib_insync contract is dynamic
    """Return the fully qualified contract that should be sent to placeOrder.

    Resolving the active front month gives us the right expiry, but Gateway
    still expects the final order contract to be qualified so the payload
    carries the exact conId/localSymbol. Sending the unqualified Future has
    produced broker-side PendingSubmit rows with no permId.
    """
    try:
        qualified = await ib.qualifyContractsAsync(contract)
    except Exception as exc:  # noqa: BLE001 - broker diagnostics belong upstream
        raise RuntimeError(
            f"failed to qualify IBKR order contract for {symbol}: {exc!r}",
        ) from exc
    if not qualified:
        raise RuntimeError(f"IBKR returned no qualified order contract for {symbol}")
    return qualified[0]


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
        # collide on the hardcoded 99 → IB Error 326. Client id 0 is reserved-ish
        # in TWS and has shown stuck PendingSubmit behavior for order-entry flows,
        # so treat <=0 as invalid and require a positive dedicated id.
        env_cid = os.environ.get("ETA_IBKR_CLIENT_ID", "").strip()
        if env_cid:
            try:
                parsed_client_id = int(env_cid)
            except ValueError:
                logger.warning(
                    "ETA_IBKR_CLIENT_ID=%r is not an int; falling back to default %d",
                    env_cid, self._client_id,
                )
            else:
                if parsed_client_id <= 0:
                    logger.warning(
                        "ETA_IBKR_CLIENT_ID=%r is not safe for order entry; "
                        "falling back to default %d",
                        env_cid, self._client_id,
                    )
                else:
                    self._client_id = parsed_client_id

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
        try:
            contract = await _qualify_order_contract(
                self._ib,
                contract,
                request.symbol,
            )
        except Exception as exc:  # noqa: BLE001 — fail closed with the broker reason
            logger.error("LiveIbkrVenue order contract qualification failed: %s", exc)
            reason = f"contract qualification failed: {exc!r}"
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
        #
        # The first crypto order seen in this process emits a startup
        # log line (INFO when bypassed, WARN when guard active) so the
        # operator's posture is loud-once, not loud-every-order.
        if is_crypto:
            _emit_crypto_guard_startup_log()
            if not _crypto_env_enabled():
                reason = (
                    "crypto disabled - account lacks crypto permissions; "
                    "set ETA_IBKR_CRYPTO=1 once enabled at IBKR"
                )
                # Permanent-class rejection: do NOT cache. If we recorded
                # this through the idempotency store, the row would sit
                # in the JSONL forever (well, until short-TTL eviction)
                # and the broker_router would burn its retry budget on a
                # rejection that can only be cleared by an out-of-band
                # operator action (enable crypto at IBKR, set env var).
                # Evict the pending row so a fresh client_order_id can
                # be tried immediately once the env var flips.
                from eta_engine.safety.idempotency import evict as _idem_evict
                with contextlib.suppress(Exception):
                    _idem_evict(order_id)
                logger.info(
                    "LiveIbkrVenue crypto pre-reject signal=%s symbol=%s "
                    "(set ETA_IBKR_CRYPTO=1 to bypass once IBKR perms enabled)",
                    order_id, request.symbol,
                )
                return OrderResult(
                    order_id=order_id,
                    status=OrderStatus.REJECTED,
                    raw={
                        "venue": self.name,
                        "reason": reason,
                        "reason_code": "crypto_disabled",
                        "no_cache": True,
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

        # ── SESSION-AWARE ORDER TYPE (futures only) ───────────────
        # Microstructure review (2026-05-05): MARKET entries during
        # low-liquidity globex windows took 5-10 ticks adverse on CL/NG
        # /6E/M6E/ZN/ZB/GC. Outside the asset's primary session, convert
        # MKT to a marketable LIMIT bounded by a small tick buffer. If
        # we have no reference price, fail closed so the supervisor can
        # re-issue with a price (preferred over silently sending a wide
        # MKT into a thin book).
        #
        # Crypto orders (PAXOS) are NOT affected — they don't have a
        # globex-style night session and the bracket apparatus differs.
        ref_price = getattr(request, "price", None)
        if (
            order_type == OrderType.MARKET
            and not is_crypto
            and not _in_primary_session(request.symbol)
        ):
            if ref_price is None:
                reason = "market_order_outside_primary_session_no_ref_price"
                _record_idempotency_status(
                    "rejected",
                    reason,
                    symbol=request.symbol,
                )
                return OrderResult(
                    order_id=order_id,
                    status=OrderStatus.REJECTED,
                    raw={
                        "venue": self.name,
                        "reason": reason,
                        "symbol": request.symbol,
                    },
                )
            # Convert to marketable LIMIT: BUY pays up 3 ticks; SELL hits
            # 3 ticks under. Tick size from instrument_specs when known.
            try:
                from eta_engine.feeds.instrument_specs import get_spec
                tick_size = float(get_spec(request.symbol).tick_size)
                if tick_size <= 0:
                    tick_size = 0.25
            except Exception:  # noqa: BLE001 — defensive default
                tick_size = 0.25
            buffer_ticks = 3
            if action == "BUY":
                limit_price = float(ref_price) + buffer_ticks * tick_size
            else:
                limit_price = float(ref_price) - buffer_ticks * tick_size
            logger.info(
                "LiveIbkrVenue session-aware order: %s %s outside primary "
                "session → MKT → LMT @ %.6f (ref=%.6f, ticks=%d, tick_size=%.6f)",
                action, request.symbol, limit_price, float(ref_price),
                buffer_ticks, tick_size,
            )
            order_type = OrderType.LIMIT
            # Mutate the request's price so downstream paths (bracket /
            # exit single-leg) see the marketable limit and use it as
            # the parent limit price.
            try:
                request.price = limit_price
            except Exception:  # noqa: BLE001 — pydantic may freeze; downstream uses local var
                pass
            ref_price = limit_price

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
                    "LiveIbkrVenue BRACKET: %s %s %d %s entry @ %s sl=%.4f tp=%.4f → parentId=%s",
                    action, request.symbol, qty, order_type.value,
                    getattr(request, "price", None), float(stop_price),
                    float(target_price), trade.order.orderId,
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

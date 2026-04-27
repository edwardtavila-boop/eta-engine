"""
EVOLUTIONARY TRADING ALGO  //  funnel.fiat_to_crypto
========================================
Fiat -> crypto on-ramp pipeline (Coinbase / Kraken / Strike / Gemini).

Why this exists
---------------
The bot portfolio needs a safe, auditable path from USD to the crypto
venues. The operator runs this manually from Tradovate profit, not
frequently, and every move is high-scrutiny. This module encodes the
workflow as a state machine so that:

  * Each step is observable and recorded.
  * Amounts and destinations are pre-checked by policy.
  * Tests can run the whole pipeline without ever calling a real API.

Flow
----
INITIATED
    -> FIAT_DEPOSITED      (operator confirmed bank -> provider)
    -> CONVERTING          (limit/market order placed on provider)
    -> CONVERTED           (order filled; crypto sitting on provider)
    -> WITHDRAWING         (withdraw-to-venue address call submitted)
    -> COMPLETE            (on-chain confirmation observed)
or  -> FAILED              (any step raised)

The pipeline does NOT submit real orders. Callers inject a
``OnrampExecutor`` (two async methods: ``place_order`` and ``withdraw``)
which does the live work. Tests use ``StubOnrampExecutor``.

Public API
----------
  * ``FiatSource`` / ``OnrampProvider`` / ``CryptoTarget`` / ``OnrampStage``
  * ``OnrampRequest`` / ``OnrampState`` / ``OnrampEvent``
  * ``OnrampPolicy`` (allowed source/provider/target triples + fiat limits)
  * ``OnrampExecutor`` Protocol + ``StubOnrampExecutor``
  * ``OnrampPipeline`` -- drives the state machine
  * Error hierarchy rooted at ``OnrampError``
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FiatSource(StrEnum):
    BANK_WIRE = "BANK_WIRE"
    ACH = "ACH"
    CARD = "CARD"
    ZELLE = "ZELLE"
    CASH_APP = "CASH_APP"


class OnrampProvider(StrEnum):
    COINBASE = "COINBASE"
    KRAKEN = "KRAKEN"
    STRIKE = "STRIKE"
    BINANCE_US = "BINANCE_US"
    GEMINI = "GEMINI"


class CryptoTarget(StrEnum):
    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"
    XRP = "XRP"
    USDC = "USDC"
    USDT = "USDT"


class OnrampStage(StrEnum):
    """Linear pipeline stages. See module docstring for the flow."""

    INITIATED = "INITIATED"
    FIAT_DEPOSITED = "FIAT_DEPOSITED"
    CONVERTING = "CONVERTING"
    CONVERTED = "CONVERTED"
    WITHDRAWING = "WITHDRAWING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


_NEXT_STAGE: dict[OnrampStage, OnrampStage] = {
    OnrampStage.INITIATED: OnrampStage.FIAT_DEPOSITED,
    OnrampStage.FIAT_DEPOSITED: OnrampStage.CONVERTING,
    OnrampStage.CONVERTING: OnrampStage.CONVERTED,
    OnrampStage.CONVERTED: OnrampStage.WITHDRAWING,
    OnrampStage.WITHDRAWING: OnrampStage.COMPLETE,
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OnrampError(RuntimeError):
    """Root of all on-ramp failure modes."""


class OnrampPolicyError(OnrampError):
    """Policy forbids this (source, provider, target) or amount."""


class OnrampStageError(OnrampError):
    """Tried to transition out of an invalid stage (e.g. from FAILED)."""


class OnrampExecutorError(OnrampError):
    """Injected executor raised; pipeline transitions to FAILED."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class OnrampRequest(BaseModel):
    """One fiat-to-crypto ticket."""

    fiat_amount_usd: float = Field(gt=0.0)
    source: FiatSource
    provider: OnrampProvider
    target: CryptoTarget
    venue_address: str = Field(
        min_length=4,
        description=(
            "Destination address on the downstream trading venue (Bybit deposit addr, or a bot's hot-wallet addr)."
        ),
    )
    note: str = ""


class OnrampEvent(BaseModel):
    """One transition event recorded on the state."""

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    from_stage: OnrampStage
    to_stage: OnrampStage
    detail: str = ""


class OnrampState(BaseModel):
    """Mutable state for a single in-flight pipeline.

    The caller never mutates these fields directly; the pipeline does.
    """

    request: OnrampRequest
    stage: OnrampStage = OnrampStage.INITIATED
    fiat_ref: str | None = Field(default=None, description="Bank transfer ref")
    order_id: str | None = Field(default=None, description="Provider order id")
    crypto_qty: float | None = Field(default=None, description="Crypto received after fill")
    fill_price_usd: float | None = Field(default=None, description="Effective fiat/crypto price")
    withdraw_tx_id: str | None = Field(default=None, description="On-chain withdrawal tx id")
    completed_at: datetime | None = None
    last_error: str | None = None
    events: list[OnrampEvent] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class OnrampPolicy(BaseModel):
    """Which (source, provider, target) triples are allowed + fiat limits.

    ``allowed_triples`` is a set of (source, provider, target) tuples. An
    empty set means "nothing allowed" (strict by default). The operator
    explicitly whitelists every route.
    """

    allowed_triples: set[tuple[FiatSource, OnrampProvider, CryptoTarget]] = Field(
        default_factory=set,
    )
    per_txn_limit_usd: float = Field(default=10_000.0, gt=0.0)
    monthly_limit_usd: float = Field(default=50_000.0, gt=0.0)
    provider_min_usd: dict[OnrampProvider, float] = Field(
        default_factory=dict,
        description="Per-provider minimum USD size (e.g. Kraken wire = $100)",
    )

    model_config = {"arbitrary_types_allowed": True}

    def check(self, req: OnrampRequest) -> None:
        """Raise ``OnrampPolicyError`` if the request violates policy.

        Does NOT consult monthly totals here (that's the pipeline's job
        via the injected ``running_monthly_usd`` callback at execute time).
        """
        triple = (req.source, req.provider, req.target)
        if triple not in self.allowed_triples:
            raise OnrampPolicyError(
                f"route {triple} not in allowed_triples; add it to policy first",
            )
        if req.fiat_amount_usd > self.per_txn_limit_usd:
            raise OnrampPolicyError(
                f"fiat ${req.fiat_amount_usd:.2f} exceeds per-txn limit ${self.per_txn_limit_usd:.2f}",
            )
        min_req = self.provider_min_usd.get(req.provider)
        if min_req is not None and req.fiat_amount_usd < min_req:
            raise OnrampPolicyError(
                f"provider {req.provider.value} requires minimum ${min_req:.2f}; got ${req.fiat_amount_usd:.2f}",
            )


# ---------------------------------------------------------------------------
# Executor protocol
# ---------------------------------------------------------------------------


class OrderFill(BaseModel):
    """What the executor returns after a successful order."""

    order_id: str
    crypto_qty: float = Field(gt=0.0)
    fill_price_usd: float = Field(gt=0.0)


class WithdrawReceipt(BaseModel):
    """What the executor returns after a withdraw call."""

    tx_id: str


@runtime_checkable
class OnrampExecutor(Protocol):
    """Async boundary between the pipeline and a real provider client."""

    async def place_order(
        self,
        *,
        provider: OnrampProvider,
        target: CryptoTarget,
        fiat_amount_usd: float,
    ) -> OrderFill: ...

    async def withdraw(
        self,
        *,
        provider: OnrampProvider,
        target: CryptoTarget,
        crypto_qty: float,
        address: str,
    ) -> WithdrawReceipt: ...


class StubOnrampExecutor:
    """Offline stand-in. Deterministic fill price per target.

    Useful in tests and in dry-run mode in prod (operator wants to see
    what the pipeline WOULD do without signing anything).
    """

    _DEFAULT_PRICES: dict[CryptoTarget, float] = {
        CryptoTarget.BTC: 68_000.0,
        CryptoTarget.ETH: 3_500.0,
        CryptoTarget.SOL: 180.0,
        CryptoTarget.XRP: 0.60,
        CryptoTarget.USDC: 1.00,
        CryptoTarget.USDT: 1.00,
    }

    def __init__(
        self,
        *,
        prices: dict[CryptoTarget, float] | None = None,
        slippage_bps: float = 10.0,
        fail_orders: bool = False,
        fail_withdrawals: bool = False,
    ) -> None:
        self.prices = {**self._DEFAULT_PRICES, **(prices or {})}
        self.slippage_bps = slippage_bps
        self.fail_orders = fail_orders
        self.fail_withdrawals = fail_withdrawals
        self._order_counter = 0
        self._tx_counter = 0

    async def place_order(
        self,
        *,
        provider: OnrampProvider,
        target: CryptoTarget,
        fiat_amount_usd: float,
    ) -> OrderFill:
        if self.fail_orders:
            raise OnrampExecutorError(
                f"StubOnrampExecutor configured to fail orders ({provider.value})",
            )
        self._order_counter += 1
        base_price = self.prices[target]
        effective_price = base_price * (1.0 + self.slippage_bps / 10_000.0)
        crypto_qty = fiat_amount_usd / effective_price
        return OrderFill(
            order_id=f"{provider.value}-{self._order_counter}",
            crypto_qty=crypto_qty,
            fill_price_usd=effective_price,
        )

    async def withdraw(
        self,
        *,
        provider: OnrampProvider,
        target: CryptoTarget,
        crypto_qty: float,
        address: str,
    ) -> WithdrawReceipt:
        if self.fail_withdrawals:
            raise OnrampExecutorError(
                f"StubOnrampExecutor configured to fail withdrawals ({provider.value})",
            )
        if crypto_qty <= 0:
            raise OnrampExecutorError("withdraw qty must be > 0")
        if not address:
            raise OnrampExecutorError("withdraw address must be non-empty")
        self._tx_counter += 1
        return WithdrawReceipt(tx_id=f"{target.value}-tx-{self._tx_counter}")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

ClockFn = Callable[[], datetime]


class OnrampPipeline:
    """Drives one OnrampRequest through the linear state machine.

    Usage::

        pipeline = OnrampPipeline(
            policy=policy, executor=executor,
            running_monthly_usd=lambda: sum_from_ledger(),
        )
        state = pipeline.start(req)             # validates + INITIATED
        state = pipeline.confirm_fiat(state, ref="ACH-12345")
        state = await pipeline.place_and_record_order(state)
        state = await pipeline.withdraw_to_venue(state)
        # -> state.stage == OnrampStage.COMPLETE
    """

    def __init__(
        self,
        *,
        policy: OnrampPolicy,
        executor: OnrampExecutor,
        running_monthly_usd: Callable[[], float] | None = None,
        clock: ClockFn | None = None,
    ) -> None:
        self.policy = policy
        self.executor = executor
        self._running_monthly = running_monthly_usd or (lambda: 0.0)
        self._clock: ClockFn = clock if clock is not None else (lambda: datetime.now(UTC))

    # -- helpers -----------------------------------------------------------

    def _advance(self, state: OnrampState, detail: str = "") -> OnrampState:
        frm = state.stage
        if frm == OnrampStage.FAILED:
            raise OnrampStageError("cannot advance from FAILED")
        nxt = _NEXT_STAGE.get(frm)
        if nxt is None:
            raise OnrampStageError(f"no forward transition defined from {frm}")
        state.stage = nxt
        state.events.append(
            OnrampEvent(
                ts=self._clock(),
                from_stage=frm,
                to_stage=nxt,
                detail=detail,
            )
        )
        return state

    def _fail(self, state: OnrampState, err: str) -> OnrampState:
        frm = state.stage
        state.stage = OnrampStage.FAILED
        state.last_error = err
        state.events.append(
            OnrampEvent(
                ts=self._clock(),
                from_stage=frm,
                to_stage=OnrampStage.FAILED,
                detail=err,
            )
        )
        return state

    # -- entrypoint --------------------------------------------------------

    def start(self, req: OnrampRequest) -> OnrampState:
        """Policy-check the request and return an INITIATED state."""
        self.policy.check(req)
        running = float(self._running_monthly())
        if running + req.fiat_amount_usd > self.policy.monthly_limit_usd:
            raise OnrampPolicyError(
                f"fiat ${req.fiat_amount_usd:.2f} would push monthly "
                f"total from ${running:.2f} to "
                f"${running + req.fiat_amount_usd:.2f}, "
                f"exceeding ${self.policy.monthly_limit_usd:.2f}",
            )
        state = OnrampState(request=req, stage=OnrampStage.INITIATED)
        # No event; INITIATED is the entry point, not a transition
        return state

    # -- step functions ----------------------------------------------------

    def confirm_fiat(self, state: OnrampState, *, ref: str) -> OnrampState:
        """Operator confirms the bank transfer hit the provider."""
        if state.stage != OnrampStage.INITIATED:
            raise OnrampStageError(
                f"confirm_fiat requires INITIATED; state is {state.stage}",
            )
        if not ref:
            raise ValueError("fiat reference must be non-empty")
        state.fiat_ref = ref
        return self._advance(state, detail=f"fiat_ref={ref}")

    async def place_and_record_order(self, state: OnrampState) -> OnrampState:
        """Transition FIAT_DEPOSITED -> CONVERTING -> CONVERTED."""
        if state.stage != OnrampStage.FIAT_DEPOSITED:
            raise OnrampStageError(
                f"place_and_record_order requires FIAT_DEPOSITED; state is {state.stage}",
            )
        # FIAT_DEPOSITED -> CONVERTING
        self._advance(state, detail="placing order")
        try:
            fill = await self.executor.place_order(
                provider=state.request.provider,
                target=state.request.target,
                fiat_amount_usd=state.request.fiat_amount_usd,
            )
        except OnrampExecutorError as exc:
            return self._fail(state, str(exc))
        except Exception as exc:
            return self._fail(state, f"executor raised: {exc!r}")

        state.order_id = fill.order_id
        state.crypto_qty = fill.crypto_qty
        state.fill_price_usd = fill.fill_price_usd
        return self._advance(
            state,
            detail=(f"order_id={fill.order_id} qty={fill.crypto_qty:.8f} price={fill.fill_price_usd:.2f}"),
        )

    async def withdraw_to_venue(self, state: OnrampState) -> OnrampState:
        """Transition CONVERTED -> WITHDRAWING -> COMPLETE."""
        if state.stage != OnrampStage.CONVERTED:
            raise OnrampStageError(
                f"withdraw_to_venue requires CONVERTED; state is {state.stage}",
            )
        if state.crypto_qty is None:
            raise OnrampStageError("crypto_qty missing; cannot withdraw")
        # CONVERTED -> WITHDRAWING
        self._advance(state, detail="submitting withdraw")
        try:
            receipt = await self.executor.withdraw(
                provider=state.request.provider,
                target=state.request.target,
                crypto_qty=state.crypto_qty,
                address=state.request.venue_address,
            )
        except OnrampExecutorError as exc:
            return self._fail(state, str(exc))
        except Exception as exc:
            return self._fail(state, f"executor raised: {exc!r}")

        state.withdraw_tx_id = receipt.tx_id
        # WITHDRAWING -> COMPLETE
        self._advance(state, detail=f"tx_id={receipt.tx_id}")
        state.completed_at = self._clock()
        return state

    # -- convenience -------------------------------------------------------

    async def run(self, req: OnrampRequest, *, fiat_ref: str) -> OnrampState:
        """Drive start -> confirm -> order -> withdraw in one call.

        Operator path in production is still two-step (manual bank
        transfer between start() and confirm_fiat()); this helper is for
        tests and full-automation modes.
        """
        state = self.start(req)
        state = self.confirm_fiat(state, ref=fiat_ref)
        state = await self.place_and_record_order(state)
        if state.stage == OnrampStage.FAILED:
            return state
        state = await self.withdraw_to_venue(state)
        return state

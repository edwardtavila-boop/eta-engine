"""
EVOLUTIONARY TRADING ALGO  //  transfer
============================
Inter-bot transfers and cold-wallet sweeps.
Every dollar not working is a dollar at risk.

Public surface
--------------
  * ``TransferRequest`` / ``TransferResult`` / ``TransferStatus`` (legacy)
  * ``execute_transfer(req)`` / ``sweep_to_cold(amount, addr, chain)``
    (legacy stubs; retained unchanged for existing callers)
  * ``TransferExecutor`` protocol + ``StubExecutor``, ``DryRunExecutor``,
    ``FailingExecutor`` implementations for testing
  * ``TransferPolicy`` -- whitelist + daily/per-txn limits + approval
    threshold. Enforced by ``TransferManager``
  * ``TransferManager`` -- orchestrates policy-check -> executor route
    -> ledger record. Wire this as the ``transfer_executor`` in
    ``funnel.orchestrator.FunnelOrchestrator``.
  * ``TransferLedger`` -- append-only audit record list
  * Errors: ``TransferLimitExceededError``,
    ``TransferWhitelistError``, ``TransferApprovalRequiredError``
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TransferStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class TransferRequest(BaseModel):
    """Request to move capital between bots or to cold storage."""

    from_bot: str
    to_bot: str  # "cold_wallet" for off-exchange
    amount_usd: float = Field(gt=0)
    reason: str = ""
    requires_approval: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TransferResult(BaseModel):
    """Outcome of an executed transfer."""

    request: TransferRequest
    status: TransferStatus = TransferStatus.PENDING
    tx_id: str | None = None
    fee_usd: float = 0.0
    executed_at: datetime | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Execution stubs
# ---------------------------------------------------------------------------


async def execute_transfer(req: TransferRequest) -> TransferResult:
    """Execute an inter-bot transfer.

    Production implementation:
        - Bybit internal transfer API for on-exchange moves
        - Ledger Live withdraw API for cold-wallet moves
        - Double-entry ledger logging for audit trail

    Currently returns a stub result.
    """
    if req.requires_approval:
        return TransferResult(
            request=req,
            status=TransferStatus.PENDING,
            error="Awaiting manual approval",
        )

    # TODO: Bybit API — POST /v5/asset/transfer/inter-transfer
    # TODO: Ledger withdraw — POST /api/v1/withdraw
    return TransferResult(
        request=req,
        status=TransferStatus.EXECUTED,
        tx_id=f"stub-{int(datetime.now(UTC).timestamp())}",
        fee_usd=0.0,
        executed_at=datetime.now(UTC),
    )


async def sweep_to_cold(
    amount_usd: float,
    wallet_address: str,
    chain: str = "ERC20",
) -> TransferResult:
    """Sweep profits to cold wallet.

    Production implementation:
        - Exchange withdraw API (Bybit/Coinbase)
        - Chain-specific: ERC20 (ETH), SPL (SOL), XRPL (XRP)
        - Requires whitelist check before execution

    Currently returns a stub result.
    """
    if amount_usd <= 0:
        raise ValueError(f"Sweep amount must be positive, got {amount_usd}")
    if not wallet_address:
        raise ValueError("Wallet address required for cold sweep")

    req = TransferRequest(
        from_bot="exchange_hot",
        to_bot="cold_wallet",
        amount_usd=amount_usd,
        reason=f"Cold sweep via {chain} to {wallet_address[:8]}...",
        requires_approval=True,  # cold sweeps always need approval
    )

    # TODO: Bybit — POST /v5/asset/withdraw/create
    # TODO: Coinbase — POST /v2/accounts/:id/transactions
    return TransferResult(
        request=req,
        status=TransferStatus.PENDING,
        error=f"Cold sweep to {chain} awaiting whitelist confirmation",
    )


# ---------------------------------------------------------------------------
# Executor protocol (for injecting venue-specific clients in prod + fakes
# in tests; ``TransferManager`` routes every request through one of these)
# ---------------------------------------------------------------------------


@runtime_checkable
class TransferExecutor(Protocol):
    """Any object with an async ``execute(req) -> TransferResult`` is an executor."""

    async def execute(self, req: TransferRequest) -> TransferResult: ...


class StubExecutor:
    """Returns a deterministic EXECUTED result. Zero external calls."""

    def __init__(self, *, fee_usd: float = 0.0) -> None:
        if fee_usd < 0:
            raise ValueError("fee_usd must be >= 0")
        self.fee_usd = fee_usd
        self._tx_counter = 0

    async def execute(self, req: TransferRequest) -> TransferResult:
        self._tx_counter += 1
        return TransferResult(
            request=req,
            status=TransferStatus.EXECUTED,
            tx_id=f"stub-{self._tx_counter}",
            fee_usd=self.fee_usd,
            executed_at=datetime.now(UTC),
        )


class DryRunExecutor:
    """Records calls without executing. Result is APPROVED, not EXECUTED."""

    def __init__(self) -> None:
        self.calls: list[TransferRequest] = []

    async def execute(self, req: TransferRequest) -> TransferResult:
        self.calls.append(req)
        return TransferResult(
            request=req,
            status=TransferStatus.APPROVED,
            tx_id=None,
            fee_usd=0.0,
            executed_at=None,
            error="dry-run; no tx submitted",
        )


class FailingExecutor:
    """Always returns FAILED. For negative-path tests."""

    def __init__(self, *, message: str = "simulated failure") -> None:
        self.message = message

    async def execute(self, req: TransferRequest) -> TransferResult:
        return TransferResult(
            request=req,
            status=TransferStatus.FAILED,
            error=self.message,
        )


# ---------------------------------------------------------------------------
# Policy & errors
# ---------------------------------------------------------------------------


class TransferLimitExceededError(ValueError):
    """Raised when a transfer breaches per-txn or 24h-volume limits."""


class TransferWhitelistError(ValueError):
    """Raised when from_bot or to_bot is not whitelisted."""


class TransferApprovalRequiredError(RuntimeError):
    """Raised when a transfer requires manual 2FA approval and none was provided."""


class TransferPolicy(BaseModel):
    """Per-Manager limits and whitelist.

    The whitelist controls which (from_bot, to_bot) pairs are legal. An
    empty ``allowed_to`` set on a bot means it cannot send anywhere. An
    empty ``allowed_from`` bot list means NO sender is restricted.

    ``approval_threshold_usd``: transfers at or above this amount require
    a ``TransferRequest.requires_approval=True`` claim. The manager will
    raise ``TransferApprovalRequiredError`` if the caller did not flag it.
    """

    per_txn_limit_usd: float = Field(
        default=25_000.0,
        gt=0.0,
        description="Max USD size of a single transfer",
    )
    daily_limit_usd: float = Field(
        default=100_000.0,
        gt=0.0,
        description="Rolling 24h USD volume limit across all transfers",
    )
    approval_threshold_usd: float = Field(
        default=10_000.0,
        ge=0.0,
        description=("Transfers at or above this amount must carry requires_approval=True (manual 2FA attestation)."),
    )
    whitelist: dict[str, set[str]] = Field(
        default_factory=dict,
        description=(
            "Map of from_bot -> set of allowed to_bot names. Empty map = "
            "permissive (no restriction). Non-empty map = strict: any "
            "pair not in the map is rejected."
        ),
    )

    model_config = {"arbitrary_types_allowed": True}

    def is_whitelisted(self, *, from_bot: str, to_bot: str) -> bool:
        if not self.whitelist:
            return True  # permissive when no whitelist configured
        return to_bot in self.whitelist.get(from_bot, set())


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class TransferLedgerEntry(BaseModel):
    """Append-only audit entry for one manager decision."""

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    request: TransferRequest
    result: TransferResult | None = None
    outcome: str = Field(
        description="EXECUTED | APPROVED | REJECTED | FAILED",
    )
    reason: str = ""


class TransferLedger:
    """Append-only in-memory ledger. Persist downstream if needed."""

    def __init__(self) -> None:
        self._entries: list[TransferLedgerEntry] = []

    def append(self, entry: TransferLedgerEntry) -> None:
        self._entries.append(entry)

    def entries(self) -> list[TransferLedgerEntry]:
        return list(self._entries)

    def total_usd_since(self, since: datetime) -> float:
        """Sum of EXECUTED request amounts since ``since`` (inclusive)."""
        return sum(e.request.amount_usd for e in self._entries if e.outcome == "EXECUTED" and e.ts >= since)

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

ClockFn = Callable[[], datetime]


class TransferManager:
    """Route every transfer through policy + executor + ledger.

    Typical wiring (orchestrator side):
        mgr = TransferManager(policy=policy, executor=StubExecutor())
        orch = FunnelOrchestrator(
            equity_monitor=..., sweep_configs=..., allocator=...,
            transfer_executor=mgr.execute,
        )
    """

    def __init__(
        self,
        *,
        policy: TransferPolicy,
        executor: TransferExecutor,
        ledger: TransferLedger | None = None,
        clock: ClockFn | None = None,
    ) -> None:
        self.policy = policy
        self.executor = executor
        self.ledger = ledger if ledger is not None else TransferLedger()
        self._clock: ClockFn = clock if clock is not None else (lambda: datetime.now(UTC))
        # Per-bot rolling daily sums (best-effort, not persisted on restart)
        self._daily_sent: dict[str, list[tuple[datetime, float]]] = defaultdict(list)

    # -- policy check ------------------------------------------------------

    def _prune_daily(self, bot: str, now: datetime) -> None:
        cutoff = now - timedelta(hours=24)
        self._daily_sent[bot] = [(ts, amt) for ts, amt in self._daily_sent[bot] if ts >= cutoff]

    def _daily_total(self, bot: str, now: datetime) -> float:
        self._prune_daily(bot, now)
        return sum(amt for _, amt in self._daily_sent[bot])

    def _check_policy(self, req: TransferRequest, now: datetime) -> None:
        # Whitelist
        if not self.policy.is_whitelisted(from_bot=req.from_bot, to_bot=req.to_bot):
            raise TransferWhitelistError(
                f"pair {req.from_bot!r} -> {req.to_bot!r} not in whitelist",
            )
        # Per-txn limit
        if req.amount_usd > self.policy.per_txn_limit_usd:
            raise TransferLimitExceededError(
                f"amount ${req.amount_usd:.2f} exceeds per-txn limit ${self.policy.per_txn_limit_usd:.2f}",
            )
        # 24h rolling
        rolling = self._daily_total(req.from_bot, now)
        if rolling + req.amount_usd > self.policy.daily_limit_usd:
            raise TransferLimitExceededError(
                f"amount ${req.amount_usd:.2f} would push 24h total from "
                f"${rolling:.2f} to ${rolling + req.amount_usd:.2f}, "
                f"exceeding ${self.policy.daily_limit_usd:.2f}",
            )
        # Approval gate for large amounts
        if req.amount_usd >= self.policy.approval_threshold_usd and not req.requires_approval:
            raise TransferApprovalRequiredError(
                f"amount ${req.amount_usd:.2f} at/above approval "
                f"threshold ${self.policy.approval_threshold_usd:.2f} -- "
                f"set requires_approval=True on the request",
            )

    # -- public entrypoint -------------------------------------------------

    async def execute(self, req: TransferRequest) -> TransferResult:
        """Policy-check, route to executor, record ledger entry."""
        now = self._clock()
        try:
            self._check_policy(req, now)
        except (
            TransferWhitelistError,
            TransferLimitExceededError,
            TransferApprovalRequiredError,
        ) as exc:
            result = TransferResult(
                request=req,
                status=TransferStatus.FAILED,
                error=str(exc),
            )
            self.ledger.append(
                TransferLedgerEntry(
                    ts=now,
                    request=req,
                    result=result,
                    outcome="REJECTED",
                    reason=str(exc),
                )
            )
            return result

        result = await self.executor.execute(req)

        outcome = {
            TransferStatus.EXECUTED: "EXECUTED",
            TransferStatus.APPROVED: "APPROVED",
            TransferStatus.PENDING: "APPROVED",
            TransferStatus.FAILED: "FAILED",
        }.get(result.status, "FAILED")

        if outcome == "EXECUTED":
            self._daily_sent[req.from_bot].append((now, req.amount_usd))

        self.ledger.append(
            TransferLedgerEntry(
                ts=now,
                request=req,
                result=result,
                outcome=outcome,
                reason=result.error or "",
            )
        )
        return result

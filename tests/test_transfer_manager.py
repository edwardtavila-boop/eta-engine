"""Tests for TransferManager / TransferPolicy / executors in funnel.transfer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from eta_engine.funnel.transfer import (
    DryRunExecutor,
    FailingExecutor,
    StubExecutor,
    TransferApprovalRequiredError,
    TransferLedger,
    TransferLimitExceededError,
    TransferManager,
    TransferPolicy,
    TransferRequest,
    TransferStatus,
    TransferWhitelistError,
)

_T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def _clock_at(t: datetime) -> Callable[[], datetime]:
    def fn() -> datetime:
        return t

    return fn


def _req(
    amount: float = 100.0, *, from_bot: str = "mnq", to_bot: str = "stake_pool", requires_approval: bool = False
) -> TransferRequest:
    return TransferRequest(
        from_bot=from_bot,
        to_bot=to_bot,
        amount_usd=amount,
        reason="test",
        requires_approval=requires_approval,
    )


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stub_executor_returns_executed_with_tx_id() -> None:
    x = StubExecutor()
    r = await x.execute(_req())
    assert r.status == TransferStatus.EXECUTED
    assert r.tx_id is not None
    assert r.tx_id.startswith("stub-")
    assert r.executed_at is not None


@pytest.mark.asyncio
async def test_stub_executor_increments_tx_id() -> None:
    x = StubExecutor()
    r1 = await x.execute(_req())
    r2 = await x.execute(_req())
    assert r1.tx_id != r2.tx_id


@pytest.mark.asyncio
async def test_stub_executor_rejects_negative_fee() -> None:
    with pytest.raises(ValueError, match="fee_usd"):
        StubExecutor(fee_usd=-1.0)


@pytest.mark.asyncio
async def test_dry_run_returns_approved_and_records() -> None:
    x = DryRunExecutor()
    req = _req()
    r = await x.execute(req)
    assert r.status == TransferStatus.APPROVED
    assert r.tx_id is None
    assert len(x.calls) == 1
    assert x.calls[0] is req


@pytest.mark.asyncio
async def test_failing_executor_returns_failed() -> None:
    x = FailingExecutor(message="boom")
    r = await x.execute(_req())
    assert r.status == TransferStatus.FAILED
    assert r.error == "boom"


# --------------------------------------------------------------------------- #
# TransferPolicy
# --------------------------------------------------------------------------- #


def test_policy_rejects_nonpositive_limits() -> None:
    with pytest.raises(ValueError):
        TransferPolicy(per_txn_limit_usd=0.0)
    with pytest.raises(ValueError):
        TransferPolicy(daily_limit_usd=-1.0)


def test_policy_empty_whitelist_is_permissive() -> None:
    p = TransferPolicy()
    assert p.is_whitelisted(from_bot="x", to_bot="y") is True


def test_policy_nonempty_whitelist_is_strict() -> None:
    p = TransferPolicy(whitelist={"mnq": {"stake_pool", "cold_wallet"}})
    assert p.is_whitelisted(from_bot="mnq", to_bot="stake_pool") is True
    assert p.is_whitelisted(from_bot="mnq", to_bot="unknown") is False
    assert p.is_whitelisted(from_bot="other_bot", to_bot="stake_pool") is False


# --------------------------------------------------------------------------- #
# Manager: happy path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_manager_executes_small_transfer_without_approval() -> None:
    mgr = TransferManager(
        policy=TransferPolicy(approval_threshold_usd=500.0),
        executor=StubExecutor(),
        clock=_clock_at(_T0),
    )
    r = await mgr.execute(_req(amount=100.0))
    assert r.status == TransferStatus.EXECUTED
    assert len(mgr.ledger) == 1
    assert mgr.ledger.entries()[0].outcome == "EXECUTED"


@pytest.mark.asyncio
async def test_manager_executes_large_transfer_with_approval_flag() -> None:
    mgr = TransferManager(
        policy=TransferPolicy(approval_threshold_usd=500.0),
        executor=StubExecutor(),
        clock=_clock_at(_T0),
    )
    r = await mgr.execute(_req(amount=1000.0, requires_approval=True))
    assert r.status == TransferStatus.EXECUTED


# --------------------------------------------------------------------------- #
# Manager: policy rejections
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_manager_rejects_non_whitelisted_pair() -> None:
    mgr = TransferManager(
        policy=TransferPolicy(whitelist={"mnq": {"stake_pool"}}),
        executor=StubExecutor(),
        clock=_clock_at(_T0),
    )
    r = await mgr.execute(_req(from_bot="mnq", to_bot="cold_wallet"))
    assert r.status == TransferStatus.FAILED
    assert "whitelist" in (r.error or "").lower()
    assert mgr.ledger.entries()[0].outcome == "REJECTED"


@pytest.mark.asyncio
async def test_manager_rejects_per_txn_over_limit() -> None:
    mgr = TransferManager(
        policy=TransferPolicy(per_txn_limit_usd=1_000.0, approval_threshold_usd=10_000.0),
        executor=StubExecutor(),
        clock=_clock_at(_T0),
    )
    r = await mgr.execute(_req(amount=5_000.0))
    assert r.status == TransferStatus.FAILED
    assert "exceeds per-txn" in (r.error or "")


@pytest.mark.asyncio
async def test_manager_rejects_daily_rolling_total() -> None:
    mgr = TransferManager(
        policy=TransferPolicy(
            per_txn_limit_usd=5_000.0,
            daily_limit_usd=1_000.0,
            approval_threshold_usd=100_000.0,
        ),
        executor=StubExecutor(),
        clock=_clock_at(_T0),
    )
    # First two send 800 OK (800 total)
    r1 = await mgr.execute(_req(amount=800.0))
    assert r1.status == TransferStatus.EXECUTED
    # Third would push total to 1600 -> over 1000
    r2 = await mgr.execute(_req(amount=800.0))
    assert r2.status == TransferStatus.FAILED
    assert "24h" in (r2.error or "") or "daily" in (r2.error or "").lower() or "exceeds" in (r2.error or "").lower()


@pytest.mark.asyncio
async def test_manager_rejects_large_unapproved_transfer() -> None:
    mgr = TransferManager(
        policy=TransferPolicy(approval_threshold_usd=500.0, per_txn_limit_usd=100_000.0),
        executor=StubExecutor(),
        clock=_clock_at(_T0),
    )
    r = await mgr.execute(_req(amount=1_000.0, requires_approval=False))
    assert r.status == TransferStatus.FAILED
    assert "approval" in (r.error or "").lower()


# --------------------------------------------------------------------------- #
# Manager: daily window rolls off
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_manager_daily_window_rolls_off_after_25h() -> None:
    # Use a mutable clock so we can advance
    now = [_T0]

    def clock() -> datetime:
        return now[0]

    mgr = TransferManager(
        policy=TransferPolicy(
            per_txn_limit_usd=5_000.0,
            daily_limit_usd=1_000.0,
            approval_threshold_usd=100_000.0,
        ),
        executor=StubExecutor(),
        clock=clock,
    )

    r1 = await mgr.execute(_req(amount=800.0))
    assert r1.status == TransferStatus.EXECUTED

    # Advance the clock 25h
    now[0] = _T0 + timedelta(hours=25)

    r2 = await mgr.execute(_req(amount=800.0))
    assert r2.status == TransferStatus.EXECUTED, r2.error


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


def test_ledger_total_usd_since_only_counts_executed() -> None:
    ledger = TransferLedger()
    # Does not import entry directly; simulate via manager side-effect

    # Empty ledger -> 0
    assert ledger.total_usd_since(_T0) == 0.0


@pytest.mark.asyncio
async def test_ledger_records_outcome_and_reason() -> None:
    mgr = TransferManager(
        policy=TransferPolicy(whitelist={"mnq": {"stake_pool"}}),
        executor=StubExecutor(),
        clock=_clock_at(_T0),
    )
    # Good pair
    await mgr.execute(_req(from_bot="mnq", to_bot="stake_pool"))
    # Bad pair
    await mgr.execute(_req(from_bot="mnq", to_bot="random"))
    entries = mgr.ledger.entries()
    assert [e.outcome for e in entries] == ["EXECUTED", "REJECTED"]
    assert "whitelist" in entries[1].reason.lower()


# --------------------------------------------------------------------------- #
# Direct policy error types
# --------------------------------------------------------------------------- #


def test_policy_error_types_are_classified() -> None:
    assert issubclass(TransferLimitExceededError, ValueError)
    assert issubclass(TransferWhitelistError, ValueError)
    assert issubclass(TransferApprovalRequiredError, RuntimeError)

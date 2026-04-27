"""Tests for funnel.fiat_to_crypto -- the fiat -> crypto on-ramp pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from pydantic import ValidationError

from eta_engine.funnel.fiat_to_crypto import (
    CryptoTarget,
    FiatSource,
    OnrampEvent,
    OnrampExecutorError,
    OnrampPipeline,
    OnrampPolicy,
    OnrampPolicyError,
    OnrampProvider,
    OnrampRequest,
    OnrampStage,
    OnrampStageError,
    OnrampState,
    OrderFill,
    StubOnrampExecutor,
    WithdrawReceipt,
)

_T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def _clock_at(t: datetime) -> Callable[[], datetime]:
    def fn() -> datetime:
        return t

    return fn


def _default_policy(
    *,
    per_txn: float = 10_000.0,
    monthly: float = 50_000.0,
) -> OnrampPolicy:
    return OnrampPolicy(
        allowed_triples={
            (FiatSource.ACH, OnrampProvider.COINBASE, CryptoTarget.BTC),
            (FiatSource.BANK_WIRE, OnrampProvider.KRAKEN, CryptoTarget.USDC),
        },
        per_txn_limit_usd=per_txn,
        monthly_limit_usd=monthly,
    )


def _req(
    *,
    amount: float = 500.0,
    source: FiatSource = FiatSource.ACH,
    provider: OnrampProvider = OnrampProvider.COINBASE,
    target: CryptoTarget = CryptoTarget.BTC,
    venue_address: str = "bc1qexampleaddress0000",
) -> OnrampRequest:
    return OnrampRequest(
        fiat_amount_usd=amount,
        source=source,
        provider=provider,
        target=target,
        venue_address=venue_address,
    )


# --------------------------------------------------------------------------- #
# Enum completeness
# --------------------------------------------------------------------------- #


def test_fiat_source_enum_members() -> None:
    assert {m.value for m in FiatSource} == {
        "BANK_WIRE",
        "ACH",
        "CARD",
        "ZELLE",
        "CASH_APP",
    }


def test_provider_enum_members() -> None:
    assert {m.value for m in OnrampProvider} == {
        "COINBASE",
        "KRAKEN",
        "STRIKE",
        "BINANCE_US",
        "GEMINI",
    }


def test_crypto_target_enum_members() -> None:
    assert {m.value for m in CryptoTarget} == {
        "BTC",
        "ETH",
        "SOL",
        "XRP",
        "USDC",
        "USDT",
    }


def test_stage_enum_members() -> None:
    assert {m.value for m in OnrampStage} == {
        "INITIATED",
        "FIAT_DEPOSITED",
        "CONVERTING",
        "CONVERTED",
        "WITHDRAWING",
        "COMPLETE",
        "FAILED",
    }


# --------------------------------------------------------------------------- #
# OnrampRequest validation
# --------------------------------------------------------------------------- #


def test_request_rejects_nonpositive_amount() -> None:
    with pytest.raises(ValidationError):
        OnrampRequest(
            fiat_amount_usd=0.0,
            source=FiatSource.ACH,
            provider=OnrampProvider.COINBASE,
            target=CryptoTarget.BTC,
            venue_address="bc1qexampleaddress0000",
        )


def test_request_rejects_short_address() -> None:
    with pytest.raises(ValidationError):
        OnrampRequest(
            fiat_amount_usd=100.0,
            source=FiatSource.ACH,
            provider=OnrampProvider.COINBASE,
            target=CryptoTarget.BTC,
            venue_address="abc",  # too short
        )


# --------------------------------------------------------------------------- #
# OnrampPolicy.check
# --------------------------------------------------------------------------- #


def test_policy_allows_whitelisted_triple() -> None:
    pol = _default_policy()
    pol.check(_req())  # no raise


def test_policy_rejects_non_whitelisted_triple() -> None:
    pol = _default_policy()
    bad = _req(target=CryptoTarget.SOL)
    with pytest.raises(OnrampPolicyError, match="not in allowed_triples"):
        pol.check(bad)


def test_policy_rejects_over_per_txn_limit() -> None:
    pol = _default_policy(per_txn=1_000.0)
    with pytest.raises(OnrampPolicyError, match="exceeds per-txn"):
        pol.check(_req(amount=2_500.0))


def test_policy_rejects_under_provider_min() -> None:
    pol = OnrampPolicy(
        allowed_triples={(FiatSource.ACH, OnrampProvider.COINBASE, CryptoTarget.BTC)},
        per_txn_limit_usd=10_000.0,
        monthly_limit_usd=50_000.0,
        provider_min_usd={OnrampProvider.COINBASE: 100.0},
    )
    with pytest.raises(OnrampPolicyError, match="minimum"):
        pol.check(_req(amount=50.0))


def test_policy_default_allowed_triples_is_empty() -> None:
    pol = OnrampPolicy()
    with pytest.raises(OnrampPolicyError):
        pol.check(_req())


# --------------------------------------------------------------------------- #
# StubOnrampExecutor
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stub_executor_places_order_with_slippage() -> None:
    x = StubOnrampExecutor(slippage_bps=10.0)
    fill = await x.place_order(
        provider=OnrampProvider.COINBASE,
        target=CryptoTarget.BTC,
        fiat_amount_usd=68_000.0,
    )
    # Expected effective price 68_000 * 1.001 = 68_068
    # qty = 68_000 / 68_068 ~= 0.9990
    assert fill.order_id == "COINBASE-1"
    assert 0.998 <= fill.crypto_qty <= 1.0
    assert fill.fill_price_usd == pytest.approx(68_068.0)


@pytest.mark.asyncio
async def test_stub_executor_zero_slippage() -> None:
    x = StubOnrampExecutor(slippage_bps=0.0)
    fill = await x.place_order(
        provider=OnrampProvider.KRAKEN,
        target=CryptoTarget.USDC,
        fiat_amount_usd=1_000.0,
    )
    assert fill.crypto_qty == pytest.approx(1_000.0)
    assert fill.fill_price_usd == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_stub_executor_order_counter_increments() -> None:
    x = StubOnrampExecutor()
    f1 = await x.place_order(
        provider=OnrampProvider.COINBASE,
        target=CryptoTarget.BTC,
        fiat_amount_usd=500.0,
    )
    f2 = await x.place_order(
        provider=OnrampProvider.COINBASE,
        target=CryptoTarget.BTC,
        fiat_amount_usd=500.0,
    )
    assert f1.order_id == "COINBASE-1"
    assert f2.order_id == "COINBASE-2"


@pytest.mark.asyncio
async def test_stub_executor_fail_orders_flag() -> None:
    x = StubOnrampExecutor(fail_orders=True)
    with pytest.raises(OnrampExecutorError, match="configured to fail"):
        await x.place_order(
            provider=OnrampProvider.COINBASE,
            target=CryptoTarget.BTC,
            fiat_amount_usd=100.0,
        )


@pytest.mark.asyncio
async def test_stub_executor_withdraw_happy_path() -> None:
    x = StubOnrampExecutor()
    r = await x.withdraw(
        provider=OnrampProvider.COINBASE,
        target=CryptoTarget.BTC,
        crypto_qty=0.1,
        address="bc1qexampleaddress0000",
    )
    assert r.tx_id == "BTC-tx-1"


@pytest.mark.asyncio
async def test_stub_executor_withdraw_tx_counter_increments() -> None:
    x = StubOnrampExecutor()
    r1 = await x.withdraw(
        provider=OnrampProvider.COINBASE,
        target=CryptoTarget.BTC,
        crypto_qty=0.1,
        address="addr_1234",
    )
    r2 = await x.withdraw(
        provider=OnrampProvider.COINBASE,
        target=CryptoTarget.BTC,
        crypto_qty=0.2,
        address="addr_1234",
    )
    assert r1.tx_id == "BTC-tx-1"
    assert r2.tx_id == "BTC-tx-2"


@pytest.mark.asyncio
async def test_stub_executor_rejects_zero_qty_withdraw() -> None:
    x = StubOnrampExecutor()
    with pytest.raises(OnrampExecutorError, match="qty must be > 0"):
        await x.withdraw(
            provider=OnrampProvider.COINBASE,
            target=CryptoTarget.BTC,
            crypto_qty=0.0,
            address="addr_1234",
        )


@pytest.mark.asyncio
async def test_stub_executor_rejects_empty_address() -> None:
    x = StubOnrampExecutor()
    with pytest.raises(OnrampExecutorError, match="address must be non-empty"):
        await x.withdraw(
            provider=OnrampProvider.COINBASE,
            target=CryptoTarget.BTC,
            crypto_qty=0.1,
            address="",
        )


@pytest.mark.asyncio
async def test_stub_executor_fail_withdrawals_flag() -> None:
    x = StubOnrampExecutor(fail_withdrawals=True)
    with pytest.raises(OnrampExecutorError, match="configured to fail"):
        await x.withdraw(
            provider=OnrampProvider.COINBASE,
            target=CryptoTarget.BTC,
            crypto_qty=0.1,
            address="addr_1234",
        )


# --------------------------------------------------------------------------- #
# OnrampPipeline.start
# --------------------------------------------------------------------------- #


def test_pipeline_start_happy_path() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(),
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req(amount=500.0))
    assert state.stage == OnrampStage.INITIATED
    assert state.request.fiat_amount_usd == 500.0
    assert state.events == []  # INITIATED is the entry state; no transition event


def test_pipeline_start_rejects_policy_violation() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(),
        clock=_clock_at(_T0),
    )
    with pytest.raises(OnrampPolicyError):
        pipe.start(_req(target=CryptoTarget.SOL))  # not in whitelist


def test_pipeline_start_rejects_monthly_breach() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(monthly=1_000.0),
        executor=StubOnrampExecutor(),
        running_monthly_usd=lambda: 800.0,
        clock=_clock_at(_T0),
    )
    # 800 + 500 = 1300 > 1000
    with pytest.raises(OnrampPolicyError, match="monthly"):
        pipe.start(_req(amount=500.0))


def test_pipeline_start_allows_monthly_exactly_at_limit() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(monthly=1_000.0),
        executor=StubOnrampExecutor(),
        running_monthly_usd=lambda: 500.0,
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req(amount=500.0))  # 500+500 = 1000 (not >)
    assert state.stage == OnrampStage.INITIATED


# --------------------------------------------------------------------------- #
# OnrampPipeline.confirm_fiat
# --------------------------------------------------------------------------- #


def test_confirm_fiat_advances_and_records_ref() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(),
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req())
    state = pipe.confirm_fiat(state, ref="ACH-12345")
    assert state.stage == OnrampStage.FIAT_DEPOSITED
    assert state.fiat_ref == "ACH-12345"
    assert len(state.events) == 1
    assert state.events[0].from_stage == OnrampStage.INITIATED
    assert state.events[0].to_stage == OnrampStage.FIAT_DEPOSITED
    assert "ACH-12345" in state.events[0].detail


def test_confirm_fiat_rejects_empty_ref() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(),
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req())
    with pytest.raises(ValueError, match="non-empty"):
        pipe.confirm_fiat(state, ref="")


def test_confirm_fiat_rejects_wrong_stage() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(),
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req())
    state = pipe.confirm_fiat(state, ref="ACH-12345")  # now FIAT_DEPOSITED
    with pytest.raises(OnrampStageError, match="requires INITIATED"):
        pipe.confirm_fiat(state, ref="ACH-67890")


# --------------------------------------------------------------------------- #
# place_and_record_order
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_place_and_record_order_happy_path() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(slippage_bps=0.0),
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req(amount=68_000.0 * 0.01))  # 680 USD to buy ~0.01 BTC
    state = pipe.confirm_fiat(state, ref="ACH-12345")
    state = await pipe.place_and_record_order(state)
    assert state.stage == OnrampStage.CONVERTED
    assert state.order_id is not None
    assert state.order_id.startswith("COINBASE-")
    assert state.crypto_qty is not None
    assert state.crypto_qty == pytest.approx(0.01, rel=1e-6)
    assert state.fill_price_usd == pytest.approx(68_000.0)
    # 2 events: -> CONVERTING and -> CONVERTED (plus confirm_fiat = 3 total)
    assert [e.to_stage for e in state.events] == [
        OnrampStage.FIAT_DEPOSITED,
        OnrampStage.CONVERTING,
        OnrampStage.CONVERTED,
    ]


@pytest.mark.asyncio
async def test_place_and_record_order_rejects_wrong_stage() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(),
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req())  # still INITIATED
    with pytest.raises(OnrampStageError, match="FIAT_DEPOSITED"):
        await pipe.place_and_record_order(state)


@pytest.mark.asyncio
async def test_place_and_record_order_executor_failure_goes_to_failed() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(fail_orders=True),
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req())
    state = pipe.confirm_fiat(state, ref="ACH-12345")
    state = await pipe.place_and_record_order(state)
    assert state.stage == OnrampStage.FAILED
    assert state.last_error is not None
    assert "configured to fail" in state.last_error
    # last event should record the transition into FAILED
    assert state.events[-1].to_stage == OnrampStage.FAILED


@pytest.mark.asyncio
async def test_place_and_record_order_wraps_unexpected_exception() -> None:
    class BoomExecutor:
        async def place_order(
            self,
            *,
            provider: OnrampProvider,  # noqa: ARG002
            target: CryptoTarget,  # noqa: ARG002
            fiat_amount_usd: float,  # noqa: ARG002
        ) -> OrderFill:
            raise RuntimeError("network exploded")

        async def withdraw(
            self,
            *,
            provider: OnrampProvider,  # noqa: ARG002
            target: CryptoTarget,  # noqa: ARG002
            crypto_qty: float,  # noqa: ARG002
            address: str,  # noqa: ARG002
        ) -> WithdrawReceipt:
            raise RuntimeError("unreachable")

    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=BoomExecutor(),
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req())
    state = pipe.confirm_fiat(state, ref="ACH-12345")
    state = await pipe.place_and_record_order(state)
    assert state.stage == OnrampStage.FAILED
    assert state.last_error is not None
    assert "executor raised" in state.last_error
    assert "network exploded" in state.last_error


# --------------------------------------------------------------------------- #
# withdraw_to_venue
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_withdraw_to_venue_happy_path() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(slippage_bps=0.0),
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req(amount=680.0))
    state = pipe.confirm_fiat(state, ref="ACH-12345")
    state = await pipe.place_and_record_order(state)
    state = await pipe.withdraw_to_venue(state)
    assert state.stage == OnrampStage.COMPLETE
    assert state.withdraw_tx_id is not None
    assert state.withdraw_tx_id.startswith("BTC-tx-")
    assert state.completed_at == _T0


@pytest.mark.asyncio
async def test_withdraw_rejects_wrong_stage() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(),
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req())  # INITIATED
    with pytest.raises(OnrampStageError, match="CONVERTED"):
        await pipe.withdraw_to_venue(state)


@pytest.mark.asyncio
async def test_withdraw_failure_goes_to_failed() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(fail_withdrawals=True),
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req())
    state = pipe.confirm_fiat(state, ref="ACH-12345")
    state = await pipe.place_and_record_order(state)
    assert state.stage == OnrampStage.CONVERTED  # order succeeded
    state = await pipe.withdraw_to_venue(state)
    assert state.stage == OnrampStage.FAILED
    assert state.last_error is not None
    assert "configured to fail" in state.last_error


# --------------------------------------------------------------------------- #
# run() convenience helper
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_end_to_end_complete() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(slippage_bps=0.0),
        clock=_clock_at(_T0),
    )
    state = await pipe.run(_req(amount=500.0), fiat_ref="ACH-AUTOPILOT")
    assert state.stage == OnrampStage.COMPLETE
    assert state.fiat_ref == "ACH-AUTOPILOT"
    # 5 transitions: INITIATED -> FIAT_DEPOSITED -> CONVERTING ->
    #                CONVERTED -> WITHDRAWING -> COMPLETE
    assert [e.to_stage for e in state.events] == [
        OnrampStage.FIAT_DEPOSITED,
        OnrampStage.CONVERTING,
        OnrampStage.CONVERTED,
        OnrampStage.WITHDRAWING,
        OnrampStage.COMPLETE,
    ]


@pytest.mark.asyncio
async def test_run_short_circuits_on_order_failure() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(fail_orders=True),
        clock=_clock_at(_T0),
    )
    state = await pipe.run(_req(), fiat_ref="ACH-12345")
    assert state.stage == OnrampStage.FAILED
    # Should not have attempted withdraw
    assert state.withdraw_tx_id is None


@pytest.mark.asyncio
async def test_pipeline_cannot_advance_from_failed() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(fail_orders=True),
        clock=_clock_at(_T0),
    )
    state = pipe.start(_req())
    state = pipe.confirm_fiat(state, ref="ACH-12345")
    state = await pipe.place_and_record_order(state)
    assert state.stage == OnrampStage.FAILED
    # withdraw from FAILED: wrong-stage error (not FAILED-advance)
    with pytest.raises(OnrampStageError):
        await pipe.withdraw_to_venue(state)


# --------------------------------------------------------------------------- #
# Events and auditability
# --------------------------------------------------------------------------- #


def test_onramp_event_default_ts_is_aware() -> None:
    e = OnrampEvent(from_stage=OnrampStage.INITIATED, to_stage=OnrampStage.FIAT_DEPOSITED)
    assert e.ts.tzinfo is not None


@pytest.mark.asyncio
async def test_events_carry_injected_clock_timestamp() -> None:
    pipe = OnrampPipeline(
        policy=_default_policy(),
        executor=StubOnrampExecutor(slippage_bps=0.0),
        clock=_clock_at(_T0),
    )
    state = await pipe.run(_req(), fiat_ref="ACH-12345")
    # Every transition event should have ts == _T0 (clock is constant)
    for ev in state.events:
        assert ev.ts == _T0


# --------------------------------------------------------------------------- #
# Supporting models
# --------------------------------------------------------------------------- #


def test_order_fill_rejects_nonpositive_qty() -> None:
    with pytest.raises(ValidationError):
        OrderFill(order_id="x", crypto_qty=0.0, fill_price_usd=1.0)


def test_order_fill_rejects_nonpositive_price() -> None:
    with pytest.raises(ValidationError):
        OrderFill(order_id="x", crypto_qty=1.0, fill_price_usd=0.0)


def test_withdraw_receipt_ok() -> None:
    r = WithdrawReceipt(tx_id="abc")
    assert r.tx_id == "abc"


def test_onramp_state_default_stage_is_initiated() -> None:
    req = _req()
    s = OnrampState(request=req)
    assert s.stage == OnrampStage.INITIATED
    assert s.events == []
    assert s.fiat_ref is None


# --------------------------------------------------------------------------- #
# Error hierarchy
# --------------------------------------------------------------------------- #


def test_error_hierarchy() -> None:
    from eta_engine.funnel.fiat_to_crypto import OnrampError

    assert issubclass(OnrampPolicyError, OnrampError)
    assert issubclass(OnrampStageError, OnrampError)
    assert issubclass(OnrampExecutorError, OnrampError)
    assert issubclass(OnrampError, RuntimeError)

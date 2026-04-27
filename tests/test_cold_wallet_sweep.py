"""Cold-wallet sweep verifier tests — P6_FUNNEL cold_wallet."""

from __future__ import annotations

import pytest

from eta_engine.funnel.cold_wallet_sweep import (
    ColdWalletSweep,
    ColdWalletTarget,
    SweepInstruction,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TARGETS = [
    ColdWalletTarget(chain="eth", address="0xCOLD_ETH", label="primary-ledger-eth"),
    ColdWalletTarget(chain="sol", address="COLD_SOL", label="primary-ledger-sol"),
    ColdWalletTarget(chain="btc", address="bc1cold", label="primary-ledger-btc"),
]


def _build_default() -> ColdWalletSweep:
    return ColdWalletSweep(targets=TARGETS, min_sweep_usd=1_000.0, drift_tolerance_pct=1.0)


# ---------------------------------------------------------------------------
# build_sweep_plan
# ---------------------------------------------------------------------------


def test_build_sweep_plan_returns_instruction_above_floor() -> None:
    sweep = _build_default()
    instr = sweep.build_sweep_plan(
        chain="eth",
        asset_symbol="USDC",
        amount=5_000.0,
        source_address="0xHOT",
        price_usd=1.0,
    )
    assert instr is not None
    assert instr.chain == "eth"
    assert instr.asset_symbol == "USDC"
    assert instr.amount == 5_000.0
    assert instr.source_address == "0xHOT"
    assert instr.destination_address == "0xCOLD_ETH"
    assert instr.destination_label == "primary-ledger-eth"


def test_build_sweep_plan_skips_below_floor() -> None:
    sweep = _build_default()
    instr = sweep.build_sweep_plan(
        chain="eth",
        asset_symbol="USDC",
        amount=500.0,  # $500 < $1000 floor
        source_address="0xHOT",
        price_usd=1.0,
    )
    assert instr is None


def test_build_sweep_plan_rejects_non_positive_amount() -> None:
    sweep = _build_default()
    with pytest.raises(ValueError, match="positive"):
        sweep.build_sweep_plan(
            chain="eth",
            asset_symbol="USDC",
            amount=0.0,
            source_address="0xHOT",
        )
    with pytest.raises(ValueError, match="positive"):
        sweep.build_sweep_plan(
            chain="eth",
            asset_symbol="USDC",
            amount=-10.0,
            source_address="0xHOT",
        )


def test_build_sweep_plan_raises_for_unknown_chain() -> None:
    sweep = _build_default()
    with pytest.raises(KeyError, match="no cold-wallet target"):
        sweep.build_sweep_plan(
            chain="flare",  # not in TARGETS
            asset_symbol="FLR",
            amount=200_000.0,  # $200k * 0.03 = $6k above $1k floor
            source_address="flare_hot",
            price_usd=0.03,
        )


def test_build_sweep_plan_flags_high_value_notes() -> None:
    sweep = _build_default()
    instr = sweep.build_sweep_plan(
        chain="btc",
        asset_symbol="BTC",
        amount=2.0,
        source_address="bc1hot",
        price_usd=65_000.0,  # notional $130k > $100k
    )
    assert instr is not None
    assert any("high_value_sweep" in n for n in instr.notes)


def test_build_sweep_plan_omits_high_value_notes_under_threshold() -> None:
    sweep = _build_default()
    instr = sweep.build_sweep_plan(
        chain="btc",
        asset_symbol="BTC",
        amount=0.5,
        source_address="bc1hot",
        price_usd=65_000.0,  # $32.5k < $100k
    )
    assert instr is not None
    assert all("high_value_sweep" not in n for n in instr.notes)


def test_build_sweep_plan_rounds_amount_to_8_dp() -> None:
    sweep = _build_default()
    instr = sweep.build_sweep_plan(
        chain="eth",
        asset_symbol="ETH",
        amount=1.123456789012345,
        source_address="0xHOT",
        price_usd=3_000.0,
    )
    assert instr is not None
    assert instr.amount == round(1.123456789012345, 8)


def test_build_sweep_plan_respects_custom_floor() -> None:
    sweep = ColdWalletSweep(targets=TARGETS, min_sweep_usd=10_000.0)
    below = sweep.build_sweep_plan(
        chain="eth",
        asset_symbol="USDC",
        amount=5_000.0,
        source_address="0xHOT",
    )
    above = sweep.build_sweep_plan(
        chain="eth",
        asset_symbol="USDC",
        amount=15_000.0,
        source_address="0xHOT",
    )
    assert below is None
    assert above is not None


# ---------------------------------------------------------------------------
# verify_sweep
# ---------------------------------------------------------------------------


def _instruction(amount: float = 5_000.0) -> SweepInstruction:
    return SweepInstruction(
        created_utc="2026-04-16T00:00:00+00:00",
        chain="eth",
        asset_symbol="USDC",
        amount=amount,
        source_address="0xHOT",
        destination_address="0xCOLD_ETH",
        destination_label="primary-ledger-eth",
    )


def test_verify_sweep_confirms_exact_match() -> None:
    sweep = _build_default()
    instr = _instruction(amount=5_000.0)
    result = sweep.verify_sweep(
        instruction=instr,
        claimed_tx_hash="0xdeadbeef",
        balance_before=100.0,
        balance_after=5_100.0,
    )
    assert result.verified is True
    assert result.observed_delta == 5_000.0
    assert result.drift_pct == 0.0
    assert result.notes == []


def test_verify_sweep_fails_when_balance_unchanged() -> None:
    sweep = _build_default()
    result = sweep.verify_sweep(
        instruction=_instruction(amount=5_000.0),
        claimed_tx_hash="0xnope",
        balance_before=100.0,
        balance_after=100.0,
    )
    assert result.verified is False
    assert any("observed delta <= 0" in n for n in result.notes)


def test_verify_sweep_flags_drift_above_tolerance() -> None:
    sweep = ColdWalletSweep(targets=TARGETS, drift_tolerance_pct=1.0)
    result = sweep.verify_sweep(
        instruction=_instruction(amount=1_000.0),
        claimed_tx_hash="0xdrift",
        balance_before=100.0,
        balance_after=1_080.0,  # delta=980, drift=2%
    )
    assert result.verified is False
    assert result.drift_pct == pytest.approx(2.0, rel=1e-6)
    assert any("drift" in n for n in result.notes)


def test_verify_sweep_accepts_drift_within_tolerance() -> None:
    sweep = ColdWalletSweep(targets=TARGETS, drift_tolerance_pct=1.0)
    result = sweep.verify_sweep(
        instruction=_instruction(amount=1_000.0),
        claimed_tx_hash="0xtight",
        balance_before=100.0,
        balance_after=1_095.0,  # delta=995, drift=0.5%
    )
    assert result.verified is True
    assert result.drift_pct < 1.0


def test_verify_sweep_records_claimed_tx_hash() -> None:
    sweep = _build_default()
    result = sweep.verify_sweep(
        instruction=_instruction(),
        claimed_tx_hash="0xAUDIT123",
        balance_before=0.0,
        balance_after=5_000.0,
    )
    assert result.claimed_tx_hash == "0xAUDIT123"
    assert result.instruction_id.startswith("eth:")


def test_verify_sweep_handles_zero_expected_amount() -> None:
    sweep = _build_default()
    # Synthesize a zero-amount instruction by bypassing the builder
    instr = SweepInstruction(
        created_utc="2026-04-16T00:00:00+00:00",
        chain="eth",
        asset_symbol="USDC",
        amount=0.0,
        source_address="0xHOT",
        destination_address="0xCOLD_ETH",
        destination_label="primary-ledger-eth",
    )
    result = sweep.verify_sweep(
        instruction=instr,
        claimed_tx_hash="0xzero",
        balance_before=0.0,
        balance_after=100.0,
    )
    # Expected=0 → drift hard-coded to 100.0 (never verified)
    assert result.drift_pct == 100.0
    assert result.verified is False

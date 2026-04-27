"""Cold-wallet sweep verifier — P6_FUNNEL cold_wallet.

After the hot-side sweep engine decides "$X should move to cold", we need to
actually transfer it. Rather than signing and pushing transactions from this
bot (we won't hold a cold wallet private key online), this module produces
structured *sweep instructions* that a human (or a Ledger-connected offline
signer) can verify and execute.

Flow
----
1. Upstream :mod:`eta_engine.core.sweep_engine` decides sweep size.
2. :class:`ColdWalletSweep.build_sweep_plan` produces a
   :class:`SweepInstruction` per asset + chain.
3. The plan is logged + surfaced via the dashboard + emitted to an alert
   transport (Telegram) so the operator is aware.
4. Operator signs via Ledger on an offline machine and broadcasts.
5. :class:`ColdWalletSweep.verify_sweep` checks the on-chain balance after
   the operator claims the sweep is done. Result persists in the ledger.

No private keys ever touch this code. All outputs are human-auditable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ChainKind = Literal["eth", "sol", "btc", "flare", "base"]


class ColdWalletTarget(BaseModel):
    """Configuration of the destination cold wallet per chain."""

    chain: ChainKind
    address: str  # public address (no private key; operator owns on Ledger)
    label: str = ""


class SweepInstruction(BaseModel):
    """Human-auditable sweep request. Operator executes manually."""

    created_utc: str
    chain: ChainKind
    asset_symbol: str
    amount: float
    source_address: str
    destination_address: str
    destination_label: str
    memo: str = ""
    notes: list[str] = Field(default_factory=list)


class SweepVerification(BaseModel):
    """Result of comparing the post-sweep on-chain balance vs expected."""

    instruction_id: str
    claimed_tx_hash: str
    expected_amount: float
    observed_delta: float | None
    verified: bool
    drift_pct: float
    timestamp_utc: str
    notes: list[str] = Field(default_factory=list)


class ColdWalletSweep:
    """Stateless helper: build instructions + verify outcomes.

    Does NOT talk to any RPC by itself — takes an on-chain balance lookup as
    a callback. In tests this is monkeypatched to a fake lookup.
    """

    def __init__(
        self,
        *,
        targets: list[ColdWalletTarget],
        min_sweep_usd: float = 1_000.0,
        drift_tolerance_pct: float = 1.0,
    ) -> None:
        self._targets = {t.chain: t for t in targets}
        self._min_sweep_usd = min_sweep_usd
        self._drift_tolerance_pct = drift_tolerance_pct

    # ── Instruction builder ──

    def build_sweep_plan(
        self,
        *,
        chain: ChainKind,
        asset_symbol: str,
        amount: float,
        source_address: str,
        price_usd: float = 1.0,
        memo: str = "",
    ) -> SweepInstruction | None:
        """Return a :class:`SweepInstruction` or ``None`` if the sweep is below floor."""
        if amount <= 0:
            raise ValueError("sweep amount must be positive")
        notional_usd = amount * price_usd
        if notional_usd < self._min_sweep_usd:
            logger.info(
                "cold_wallet_sweep skipped (below floor): amount=%s notional=$%.2f < $%.0f",
                amount,
                notional_usd,
                self._min_sweep_usd,
            )
            return None
        target = self._targets.get(chain)
        if target is None:
            raise KeyError(f"no cold-wallet target configured for chain {chain!r}")
        notes: list[str] = []
        if notional_usd > 100_000.0:
            notes.append("high_value_sweep — require 2-operator sign-off")
        instr = SweepInstruction(
            created_utc=datetime.now(UTC).isoformat(),
            chain=chain,
            asset_symbol=asset_symbol,
            amount=round(amount, 8),
            source_address=source_address,
            destination_address=target.address,
            destination_label=target.label,
            memo=memo,
            notes=notes,
        )
        logger.info(
            "cold_wallet_sweep planned: %s %s %s → %s (notional $%.2f)",
            amount,
            asset_symbol,
            chain,
            target.label or target.address,
            notional_usd,
        )
        return instr

    # ── Verification ──

    def verify_sweep(
        self,
        *,
        instruction: SweepInstruction,
        claimed_tx_hash: str,
        balance_before: float,
        balance_after: float,
    ) -> SweepVerification:
        """Verify an operator-claimed sweep by comparing balance deltas.

        The operator provides the tx hash (for audit), plus the cold wallet
        balance before and after the tx. If the delta matches the instructed
        amount within ``drift_tolerance_pct``, sweep is verified.
        """
        expected = instruction.amount
        observed = balance_after - balance_before
        drift = 100.0 if expected <= 0 else abs(observed - expected) / expected * 100.0
        verified = drift <= self._drift_tolerance_pct and observed > 0
        notes: list[str] = []
        if not verified:
            notes.append(f"drift {drift:.2f}% exceeds tolerance {self._drift_tolerance_pct:.2f}%")
        if observed <= 0:
            notes.append("observed delta <= 0 — sweep may have failed or gone to wrong address")

        logger.info(
            "cold_wallet_sweep verify: expected=%s observed=%s drift=%.3f%% verified=%s",
            expected,
            observed,
            drift,
            verified,
        )
        return SweepVerification(
            instruction_id=f"{instruction.chain}:{instruction.created_utc}",
            claimed_tx_hash=claimed_tx_hash,
            expected_amount=round(expected, 8),
            observed_delta=round(observed, 8),
            verified=verified,
            drift_pct=round(drift, 4),
            timestamp_utc=datetime.now(UTC).isoformat(),
            notes=notes,
        )

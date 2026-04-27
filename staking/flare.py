"""Flare sFLR adapter — XRP-adjacent staking via FTSO delegation.

Flare Network runs an EVM-compatible L1 (chain id 14 mainnet, 114 testnet).
This adapter reuses :mod:`eta_engine.staking.web3_client` for ERC-20
balance reads against the Flare RPC.

Asset flow (XRP -> sFLR):
    XRP (source) → bridge → FLR (Flare-native) → delegate via FTSO → sFLR
    sFLR represents staked FLR + FTSO delegation rewards + FlareDrops.

Live paths
----------
* ``get_balance`` — ``sFLR.balanceOf(wallet)`` via Flare RPC (lazy web3).
* ``get_apy`` — DefiLlama (pool "flare" + chain "Flare" + symbol "SFLR").
* ``stake/unstake`` — structured payloads against the Flare WNat / sFLR contracts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from eta_engine.staking.apy_tracker import ApyTracker, get_shared_tracker
from eta_engine.staking.base import StakingAdapter
from eta_engine.staking.web3_client import build_contract_call, read_balance

logger = logging.getLogger(__name__)

# Flare mainnet (chain id 14) contract addresses.
SFLR_MAINNET = "0x12e605bc104e93B45e1aD99F9e555f659051c2BB"
WNAT_MAINNET = "0x1D80c49BbBCd1C0911346656B529DF9E5c2F783d"
FLARE_FTSO_MANAGER = "0x1000000000000000000000000000000000000003"


class FlareAdapter(StakingAdapter):
    """Flare Network: FLR -> sFLR (staked FLR via FTSO delegation)."""

    symbol: str = "XRP"
    token: str = "sFLR"
    target_apy: float = 4.5

    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        wallet_address: str | None = None,
        sflr_address: str = SFLR_MAINNET,
        ftso_data_provider: str | None = None,
        apy_tracker: ApyTracker | None = None,
    ) -> None:
        self._balance: float = 0.0
        self._rpc_url = rpc_url
        self._wallet_address = wallet_address
        self._sflr_address = sflr_address
        self._ftso_data_provider = ftso_data_provider
        self._apy_tracker = apy_tracker or get_shared_tracker()

    async def stake(self, amount: float, token: str | None = None) -> str:  # noqa: ARG002 - token kept for parity
        """Stake FLR via WNat deposit + sFLR mint + FTSO delegation."""
        if amount <= 0:
            raise ValueError(f"Stake amount must be positive, got {amount}")
        steps: list[dict[str, Any]] = [
            build_contract_call(WNAT_MAINNET, "deposit"),
            build_contract_call(self._sflr_address, "submit", int(amount * 1e18)),
        ]
        if self._ftso_data_provider:
            steps.append(
                build_contract_call(
                    FLARE_FTSO_MANAGER,
                    "delegate",
                    self._ftso_data_provider,
                    int(amount * 1e18),
                )
            )
        logger.info(
            "Flare stake plan | amount=%.6f FLR delegate=%s steps=%d",
            amount,
            "yes" if self._ftso_data_provider else "no",
            len(steps),
        )
        self._balance += amount
        return f"flare-stake-stub-{amount}"

    async def unstake(self, amount: float) -> str:
        """Unstake sFLR — undelegate + sFLR burn + WNat withdraw + bridge."""
        if amount <= 0 or amount > self._balance:
            raise ValueError(f"Invalid unstake amount: {amount} (balance: {self._balance})")
        steps = [
            build_contract_call(self._sflr_address, "redeem", int(amount * 1e18)),
            build_contract_call(WNAT_MAINNET, "withdraw", int(amount * 1e18)),
        ]
        logger.info("Flare unstake plan | amount=%.6f sFLR steps=%d", amount, len(steps))
        self._balance -= amount
        return f"flare-unstake-stub-{amount}"

    async def get_balance(self) -> float:
        """sFLR balance — on-chain if configured, else in-memory."""
        real = await asyncio.to_thread(
            read_balance,
            self._rpc_url,
            self._wallet_address,
            self._sflr_address,
        )
        if real is not None:
            return real
        return self._balance

    async def get_apy(self) -> float:
        """Live Flare delegation APY (DefiLlama) or target_apy fallback."""
        live = await self._apy_tracker.get_apy("flare")
        return live if live is not None else self.target_apy

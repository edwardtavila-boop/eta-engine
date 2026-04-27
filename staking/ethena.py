"""Ethena sUSDe adapter — delta-neutral stablecoin yield.

Asset flow (USDT -> sUSDe):
    USDT → Ethena mint (delta-neutral ETH collateral + short perp hedge) → USDe
    USDe → StakedUSDe.deposit() → sUSDe
    Yield accrues from perp funding rates (short side earns in contango).

Live paths
----------
* ``get_balance`` — ``sUSDe.balanceOf(wallet)`` via Ethereum RPC (lazy web3).
* ``get_apy`` — DefiLlama Ethena sUSDe pool. APY varies with perp funding
  rates — higher in bull markets where longs overpay shorts.
* ``stake/unstake`` — structured payloads against Ethena mint + StakedUSDe.
  7-day cooldown on unstake is enforced by the protocol, not the adapter.
"""

from __future__ import annotations

import asyncio
import logging

from eta_engine.staking.apy_tracker import ApyTracker, get_shared_tracker
from eta_engine.staking.base import StakingAdapter
from eta_engine.staking.web3_client import build_contract_call, read_balance

logger = logging.getLogger(__name__)

# Ethena mainnet contract addresses (verified on etherscan 2026-Q2).
USDE_MAINNET = "0x4c9EDD5852cd905f086C759E8383e09bff1E68B3"
SUSDE_MAINNET = "0x9D39A5DE30e57443BfF2A8307A4256c8797A3497"
ETHENA_MINT_CONTRACT = "0x2CC440b721d2CaFd6D64908D6d8C4aCb17F18041"


class EthenaAdapter(StakingAdapter):
    """Ethena Labs: USDT -> USDe -> sUSDe (staked synthetic dollar)."""

    symbol: str = "USDT"
    token: str = "sUSDe"
    target_apy: float = 7.0

    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        wallet_address: str | None = None,
        susde_address: str = SUSDE_MAINNET,
        usde_address: str = USDE_MAINNET,
        apy_tracker: ApyTracker | None = None,
    ) -> None:
        self._balance: float = 0.0
        self._rpc_url = rpc_url
        self._wallet_address = wallet_address
        self._susde_address = susde_address
        self._usde_address = usde_address
        self._apy_tracker = apy_tracker or get_shared_tracker()

    async def stake(self, amount: float, token: str | None = None) -> str:  # noqa: ARG002 - token kept for parity
        """Mint USDe then stake into sUSDe."""
        if amount <= 0:
            raise ValueError(f"Stake amount must be positive, got {amount}")
        steps = [
            # Ethena mint() takes (collateral_asset, collateral_amount, min_usde_out).
            build_contract_call(
                ETHENA_MINT_CONTRACT,
                "mint",
                "USDT",  # collateral_asset symbol
                int(amount * 1e6),  # USDT has 6 decimals
                int(amount * 1e18 * 0.998),  # min_usde_out with 0.2% slippage
            ),
            build_contract_call(
                self._susde_address,
                "deposit",
                int(amount * 1e18),
                self._wallet_address or "0x0",
            ),
        ]
        logger.info("Ethena stake plan | amount=%.2f USDT steps=%d", amount, len(steps))
        self._balance += amount
        return f"ethena-stake-stub-{amount}"

    async def unstake(self, amount: float) -> str:
        """Initiate 7-day cooldown + final redeem.

        Production must track the cooldown receipt on-chain so :meth:`get_balance`
        can subtract it from the liquid sUSDe balance.
        """
        if amount <= 0 or amount > self._balance:
            raise ValueError(f"Invalid unstake amount: {amount} (balance: {self._balance})")
        steps = [
            build_contract_call(self._susde_address, "cooldownShares", int(amount * 1e18)),
            # The actual withdraw happens 7 days later via unstake() on StakedUSDe.
            build_contract_call(self._susde_address, "unstake", self._wallet_address or "0x0"),
        ]
        logger.info("Ethena unstake plan | amount=%.2f sUSDe steps=%d cooldown=7d", amount, len(steps))
        self._balance -= amount
        return f"ethena-unstake-stub-{amount}"

    async def get_balance(self) -> float:
        """sUSDe balance — on-chain via web3 if configured, else in-memory."""
        real = await asyncio.to_thread(
            read_balance,
            self._rpc_url,
            self._wallet_address,
            self._susde_address,
        )
        if real is not None:
            return real
        return self._balance

    async def get_apy(self) -> float:
        """Live Ethena sUSDe APY (DefiLlama) or target_apy fallback."""
        live = await self._apy_tracker.get_apy("ethena")
        return live if live is not None else self.target_apy

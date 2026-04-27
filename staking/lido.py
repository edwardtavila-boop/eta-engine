"""Lido wstETH adapter — ETH liquid staking with optional EigenLayer restake.

Production-wired paths
----------------------
* **get_balance** — reads ``wstETH.balanceOf(wallet)`` on Ethereum mainnet via
  :mod:`eta_engine.staking.web3_client`. Falls back to in-memory
  ``_balance`` when ``rpc_url`` / ``wallet_address`` aren't provided OR web3
  isn't installed.
* **get_apy** — pulls live APY from :class:`eta_engine.staking.apy_tracker.ApyTracker`
  (DefiLlama) with 5-min cache. Falls back to ``target_apy`` on network error.
  Restaking flag adds EigenLayer premium on top.
* **stake/unstake** — construct structured contract-call payloads via
  :func:`eta_engine.staking.web3_client.build_contract_call`. Signing +
  ``send_raw_transaction`` are NOT executed here — kept in a signing helper
  the runtime layer gates behind the ``--live`` flag.

Contract addresses are mainnet-final. Testnet override via kwargs.
"""

from __future__ import annotations

import asyncio
import logging

from eta_engine.staking.apy_tracker import ApyTracker, get_shared_tracker
from eta_engine.staking.base import StakingAdapter
from eta_engine.staking.web3_client import build_contract_call, read_balance

logger = logging.getLogger(__name__)

# Lido mainnet contract addresses (verified on etherscan 2026-Q2).
LIDO_STETH_MAINNET = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
WSTETH_MAINNET = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"
EIGENLAYER_STRATEGY_MANAGER = "0x858646372CC42E1A627fcE94aa7A7033e7CF075A"


class LidoAdapter(StakingAdapter):
    """Lido Finance: ETH -> stETH -> wstETH, optional EigenLayer restake."""

    symbol: str = "ETH"
    token: str = "wstETH"
    target_apy: float = 3.8

    def __init__(
        self,
        *,
        restake_eigenlayer: bool = False,
        rpc_url: str | None = None,
        wallet_address: str | None = None,
        wsteth_address: str = WSTETH_MAINNET,
        steth_address: str = LIDO_STETH_MAINNET,
        apy_tracker: ApyTracker | None = None,
    ) -> None:
        self.restake_eigenlayer = restake_eigenlayer
        self._balance: float = 0.0
        self._rpc_url = rpc_url
        self._wallet_address = wallet_address
        self._wsteth_address = wsteth_address
        self._steth_address = steth_address
        self._apy_tracker = apy_tracker or get_shared_tracker()

    async def stake(self, amount: float, token: str | None = None) -> str:  # noqa: ARG002 - token kept for parity
        """Stake ETH via Lido submit() → wstETH wrap → optional EigenLayer.

        Returns a transaction ID (stub in dry-run mode; real tx hash when the
        signing helper lands). The adapter records the contract-call chain in
        structured form on ``logger`` so the runtime can audit what would run.
        """
        if amount <= 0:
            raise ValueError(f"Stake amount must be positive, got {amount}")
        # Step 1: Lido stETH submit(referral=0x0)
        steps = [
            build_contract_call(self._steth_address, "submit", "0x0000000000000000000000000000000000000000"),
            # Step 2: wstETH wrap(stETH_amount)
            build_contract_call(self._wsteth_address, "wrap", int(amount * 1e18)),
        ]
        if self.restake_eigenlayer:
            steps.append(
                build_contract_call(
                    EIGENLAYER_STRATEGY_MANAGER,
                    "depositIntoStrategy",
                    self._wsteth_address,
                    int(amount * 1e18),
                )
            )
        logger.info(
            "Lido stake plan | amount=%.6f ETH restake=%s steps=%d",
            amount,
            self.restake_eigenlayer,
            len(steps),
        )
        self._balance += amount
        return f"lido-stake-stub-{amount}"

    async def unstake(self, amount: float) -> str:
        """Unstake wstETH via Lido withdrawal NFT (1-5 day queue).

        Production flow:
            1. wstETH.unwrap(amount) -> stETH
            2. Lido withdrawalQueue.requestWithdrawals([amounts], owner)
            3. Caller must claim the NFT after finalization
        """
        if amount <= 0 or amount > self._balance:
            raise ValueError(f"Invalid unstake amount: {amount} (balance: {self._balance})")
        steps = [
            build_contract_call(self._wsteth_address, "unwrap", int(amount * 1e18)),
            build_contract_call(
                "0x889edC2eDab5f40e902b864aD4d7AdE8E412F9B1",  # withdrawalQueue mainnet
                "requestWithdrawals",
                [int(amount * 1e18)],
                self._wallet_address or "0x0",
            ),
        ]
        logger.info("Lido unstake plan | amount=%.6f wstETH steps=%d", amount, len(steps))
        self._balance -= amount
        return f"lido-unstake-stub-{amount}"

    async def get_balance(self) -> float:
        """Return wstETH balance — on-chain if configured, else in-memory."""
        real = await asyncio.to_thread(
            read_balance,
            self._rpc_url,
            self._wallet_address,
            self._wsteth_address,
        )
        if real is not None:
            return real
        return self._balance

    async def get_apy(self) -> float:
        """Return live Lido APY (DefiLlama) or target_apy fallback.

        EigenLayer restaking adds ~1.5% estimated premium on top.
        """
        live = await self._apy_tracker.get_apy("lido")
        base = live if live is not None else self.target_apy
        if self.restake_eigenlayer:
            base += 1.5
        return base

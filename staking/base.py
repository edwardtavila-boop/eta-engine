"""
EVOLUTIONARY TRADING ALGO  //  staking.base
===============================
Abstract staking adapter. Every yield source implements this.
"""

from __future__ import annotations

import abc


class StakingAdapter(abc.ABC):
    """Uniform interface for all staking/yield protocols."""

    symbol: str  # underlying asset (ETH, SOL, XRP, USDT)
    token: str  # receipt token (wstETH, JitoSOL, sFLR, sUSDe)
    target_apy: float  # expected annualized yield %

    @abc.abstractmethod
    async def stake(self, amount: float, token: str | None = None) -> str:
        """Stake `amount` of the underlying asset.

        Returns a transaction ID or receipt hash.
        """

    @abc.abstractmethod
    async def unstake(self, amount: float) -> str:
        """Unstake `amount` of the receipt token.

        Returns a transaction ID. May involve unbonding period.
        """

    @abc.abstractmethod
    async def get_balance(self) -> float:
        """Current staked balance in receipt-token units."""

    @abc.abstractmethod
    async def get_apy(self) -> float:
        """Current live APY as a percentage (e.g. 3.8 = 3.8%)."""

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.symbol}->{self.token} target={self.target_apy}%>"

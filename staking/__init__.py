"""
EVOLUTIONARY TRADING ALGO  //  staking
==========================
Yield adapters for the Multiplier layer.
Idle capital is bleeding capital.

Adapters:
    Lido     - wstETH (ETH liquid staking + EigenLayer restaking)
    Jito     - JitoSOL (Solana MEV-boosted staking)
    Flare    - sFLR (XRP-adjacent staking)
    Ethena   - sUSDe (delta-neutral stablecoin yield)
"""

from eta_engine.staking.allocator import AllocationConfig, allocate, rebalance
from eta_engine.staking.base import StakingAdapter
from eta_engine.staking.ethena import EthenaAdapter
from eta_engine.staking.flare import FlareAdapter
from eta_engine.staking.jito import JitoAdapter
from eta_engine.staking.lido import LidoAdapter

__all__ = [
    "StakingAdapter",
    "LidoAdapter",
    "JitoAdapter",
    "FlareAdapter",
    "EthenaAdapter",
    "AllocationConfig",
    "allocate",
    "rebalance",
]

"""EVOLUTIONARY TRADING ALGO 6-Bot Fleet — unified imports."""

from eta_engine.bots.base_bot import (
    BaseBot,
    BotConfig,
    BotState,
    Fill,
    MarginMode,
    Position,
    RegimeType,
    Signal,
    SignalType,
    SweepResult,
    Tier,
)
from eta_engine.bots.crypto_seed.bot import CryptoSeedBot
from eta_engine.bots.eth_perp.bot import EthPerpBot
from eta_engine.bots.mnq.bot import MnqBot
from eta_engine.bots.nq.bot import NqBot
from eta_engine.bots.sol_perp.bot import SolPerpBot
from eta_engine.bots.xrp_perp.bot import XrpPerpBot

ALL_BOTS: list[type[BaseBot]] = [MnqBot, NqBot, CryptoSeedBot, EthPerpBot, SolPerpBot, XrpPerpBot]

__all__ = [
    "ALL_BOTS",
    "BaseBot",
    "BotConfig",
    "BotState",
    "CryptoSeedBot",
    "EthPerpBot",
    "Fill",
    "MarginMode",
    "MnqBot",
    "NqBot",
    "Position",
    "RegimeType",
    "Signal",
    "SignalType",
    "SolPerpBot",
    "SweepResult",
    "Tier",
    "XrpPerpBot",
]

"""
EVOLUTIONARY TRADING ALGO — Runtime Orchestrator
====================================
Boots the 6-bot fleet, wires data feeds, starts funnel + staking monitors.

Usage:
    python -m eta_engine.main --mode paper --bots mnq,eth_perp
    python -m eta_engine.main --mode live --bots all
    python -m eta_engine.main --mode replay --date 2026-03-15

Safety: bots start in PAUSED state; require `--unpause` explicit flag to trade.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "roadmap_state.json"

logger = logging.getLogger("eta_engine")


def load_config() -> dict:
    """Load master config.json."""
    with open(CONFIG_PATH) as f:
        return json.load(f)


class ApexPredator:
    """Master runtime orchestrator for the 6-bot fleet."""

    def __init__(self, mode: str, bots: list[str], unpause: bool = False) -> None:
        self.mode = mode
        self.bot_names = bots
        self.unpause = unpause
        self.config = load_config()
        self.bots: dict[str, object] = {}
        self.running = False

    async def init_bots(self) -> None:
        """Instantiate selected bots from the config."""
        logger.info("Initializing bots: %s", self.bot_names)
        # TODO: wire up actual bot classes once P1-P2 stabilize
        # from eta_engine.bots.mnq.bot import MnqBot
        # from eta_engine.bots.nq.bot import NqBot
        # from eta_engine.bots.crypto_seed.bot import CryptoSeedBot
        # from eta_engine.bots.eth_perp.bot import EthPerpBot
        # from eta_engine.bots.sol_perp.bot import SolPerpBot
        # from eta_engine.bots.xrp_perp.bot import XrpPerpBot
        logger.warning("Bot classes not yet wired — scaffold phase")

    async def init_funnel(self) -> None:
        """Wire equity monitor + sweep engine."""
        logger.info("Initializing funnel: equity monitor + sweep + staking")
        # from eta_engine.funnel.equity_monitor import EquityMonitor
        # from eta_engine.staking.allocator import Allocator
        logger.warning("Funnel/staking not yet wired — scaffold phase")

    async def init_brain(self) -> None:
        """Wire regime classifier + RL agent + multi-agent supervisor."""
        logger.info("Initializing brain: regime + RL + multi-agent")
        # from eta_engine.brain.regime import classify_regime
        # from eta_engine.brain.rl_agent import RLAgent
        # from eta_engine.brain.multi_agent import MultiAgentOrchestrator
        logger.warning("Brain not yet wired — scaffold phase")

    async def start(self) -> None:
        """Boot sequence."""
        logger.info("EVOLUTIONARY TRADING ALGO starting in mode=%s unpause=%s", self.mode, self.unpause)
        await self.init_bots()
        await self.init_funnel()
        await self.init_brain()

        if not self.unpause:
            logger.warning(">>> BOTS STARTED IN PAUSED STATE. Pass --unpause to trade. <<<")

        self.running = True
        try:
            while self.running:
                await asyncio.sleep(30)  # heartbeat
                logger.debug("heartbeat")
        except KeyboardInterrupt:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown — flatten positions, write state, close feeds."""
        logger.warning("EVOLUTIONARY TRADING ALGO shutting down — flattening positions")
        self.running = False
        # TODO: flatten all positions, persist state, close feeds


def main() -> int:
    parser = argparse.ArgumentParser(description="EVOLUTIONARY TRADING ALGO runtime orchestrator")
    parser.add_argument("--mode", choices=["paper", "live", "replay"], default="paper")
    parser.add_argument(
        "--bots",
        default="all",
        help="Comma-separated bot names or 'all'. Options: mnq, nq, crypto_seed, eth_perp, sol_perp, xrp_perp",
    )
    parser.add_argument("--unpause", action="store_true", help="Remove paused state on start (trades allowed)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    all_bots = ["mnq", "nq", "crypto_seed", "eth_perp", "sol_perp", "xrp_perp"]
    bots = all_bots if args.bots == "all" else args.bots.split(",")

    app = ApexPredator(mode=args.mode, bots=bots, unpause=args.unpause)

    # Runtime safety gate: live mode MUST pass preflight or we abort.
    if args.mode == "live":
        from eta_engine.scripts import preflight

        rc = preflight.run()
        if rc != 0:
            logger.error("Preflight FAILED -- refusing to boot in live mode")
            return rc

    try:
        asyncio.run(app.start())
        return 0
    except Exception as e:  # noqa: BLE001
        logger.exception("EVOLUTIONARY TRADING ALGO crashed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())

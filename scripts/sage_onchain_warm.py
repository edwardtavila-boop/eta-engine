"""Periodic on-chain cache warmer (Wave-6 pre-live, 2026-04-27).

Pre-fetches BTC + ETH on-chain metrics so the OnChainSchool sees
warm data when the next sage consult runs. The fetcher itself caches
for 5 minutes -- this script just ensures the cache stays populated
so a cold call from a crypto bot doesn't add an HTTP round-trip to
the order-evaluation path.

Designed to run as ``Eta-Sage-OnChain-Warm`` every 5 minutes via
Task Scheduler. Exits 0 on success, 1 on any failure.

Usage::

    python -m eta_engine.scripts.sage_onchain_warm
    python -m eta_engine.scripts.sage_onchain_warm --symbols BTCUSDT,ETHUSDT
    python -m eta_engine.scripts.sage_onchain_warm --force-refresh
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

# Make the package importable when invoked as a script
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--symbols",
        type=str,
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated symbols to warm. Default: BTCUSDT,ETHUSDT",
    )
    p.add_argument("--force-refresh", action="store_true",
                   help="Bypass the 5-min cache and re-fetch every symbol.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from eta_engine.brain.jarvis_v3.sage.onchain_fetcher import fetch_onchain

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    failed: list[str] = []
    warmed: dict[str, int] = {}

    for sym in symbols:
        try:
            data: dict[str, Any] = fetch_onchain(sym, force_refresh=args.force_refresh)
            n_keys = len([k for k in data if not k.startswith("_")])
            warmed[sym] = n_keys
            logger.info("warmed %s -> %d on-chain fields", sym, n_keys)
        except Exception as exc:  # noqa: BLE001
            logger.warning("warm failed for %s: %s", sym, exc)
            failed.append(sym)

    if failed:
        logger.error("warm finished with failures: %s", failed)
        return 1
    logger.info("warm complete: %s", warmed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

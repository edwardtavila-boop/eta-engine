"""
EVOLUTIONARY TRADING ALGO  //  data.onchain_blockscout
==========================================
Blockscout MCP/REST facade. 5-minute lru_cache for flow aggregates.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache

log = logging.getLogger(__name__)


def _five_min_bucket() -> int:
    return int(time.time() // 300)


class BlockscoutClient:
    """Async facade over the blockscout MCP (or direct REST).

    TODO: wire actual `mcp__blockscout__*` tool calls or direct_api_call.
    """

    def __init__(self, chain_id: int = 1) -> None:
        self.chain_id = chain_id

    # ------------------------------------------------------------------
    # Whale transfers
    # ------------------------------------------------------------------
    async def fetch_whale_transfers(
        self,
        asset: str,
        threshold_usd: float = 1_000_000.0,
    ) -> list[dict]:
        """Return list of {tx_hash, from, to, value_usd, ts} above threshold."""
        return _fetch_whale_transfers_cached(
            asset,
            threshold_usd,
            self.chain_id,
            _five_min_bucket(),
        )

    # ------------------------------------------------------------------
    # Exchange netflow (deposits - withdrawals)
    # ------------------------------------------------------------------
    async def fetch_exchange_netflow(
        self,
        asset: str,
        exchanges: list[str] | None = None,
    ) -> float:
        exchanges = exchanges or ["binance", "coinbase", "bybit"]
        return _fetch_netflow_cached(
            asset,
            tuple(exchanges),
            self.chain_id,
            _five_min_bucket(),
        )

    # ------------------------------------------------------------------
    # Active addresses delta (z-score)
    # ------------------------------------------------------------------
    async def fetch_active_addresses_delta(
        self,
        asset: str,
        window_days: int = 7,
    ) -> float:
        return _fetch_addr_delta_cached(
            asset,
            window_days,
            self.chain_id,
            _five_min_bucket(),
        )


# ---------------------------------------------------------------------------
# Module-level lru_cached stubs — bucket arg forces 5-minute rollover.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=512)
def _fetch_whale_transfers_cached(
    asset: str,
    threshold_usd: float,
    chain_id: int,
    bucket: int,
) -> list[dict]:
    """TODO: call mcp__blockscout__get_token_transfers_by_address for each whale."""
    log.debug("[stub] whale transfers %s >$%.0f chain=%d bucket=%d", asset, threshold_usd, chain_id, bucket)
    _ = (asset, threshold_usd, chain_id, bucket)
    return []


@lru_cache(maxsize=512)
def _fetch_netflow_cached(
    asset: str,
    exchanges: tuple[str, ...],
    chain_id: int,
    bucket: int,
) -> float:
    """TODO: aggregate deposits/withdrawals across known exchange addresses."""
    log.debug("[stub] netflow %s exch=%s chain=%d bucket=%d", asset, exchanges, chain_id, bucket)
    _ = (asset, exchanges, chain_id, bucket)
    return 0.0


@lru_cache(maxsize=512)
def _fetch_addr_delta_cached(
    asset: str,
    window_days: int,
    chain_id: int,
    bucket: int,
) -> float:
    """TODO: call blockscout stats endpoint; compute z-score vs 30d baseline."""
    log.debug("[stub] active-addr delta %s win=%dd chain=%d bucket=%d", asset, window_days, chain_id, bucket)
    _ = (asset, window_days, chain_id, bucket)
    return 0.0

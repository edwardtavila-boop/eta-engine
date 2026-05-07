"""
EVOLUTIONARY TRADING ALGO  //  features.onchain
===================================
On-chain flow signal via Blockscout MCP.
Whale transfers + exchange netflow + active addresses.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from eta_engine.data.onchain_blockscout import BlockscoutClient
from eta_engine.features.base import Feature
from eta_engine.features.mcp_taps import (
    McpTap,
    blockscout_snapshot,
    use_mcp_taps_enabled,
)

if TYPE_CHECKING:
    from eta_engine.core.data_pipeline import BarData

logger = logging.getLogger(__name__)


def whale_delta_score(current: int, baseline: int) -> float:
    """Score whale-transfer count delta.

    Returns 1.0 at +1σ (current ~= 2× baseline),
    0.0 when current <= baseline.
    Assumes sigma ≈ baseline (Poisson-ish).
    """
    if baseline <= 0:
        return 0.5 if current > 0 else 0.0
    delta = current - baseline
    z = delta / max(baseline, 1)  # simple z-proxy
    # Map z in [0, 1] → score [0, 1]
    return max(0.0, min(1.0, z))


def netflow_score(netflow_usd: float) -> float:
    """Exchange netflow: negative (outflow) is bullish accumulation.

    Returns 1.0 at -$50M outflow, 0.0 at +$50M inflow.
    """
    clamped = max(-50_000_000.0, min(50_000_000.0, netflow_usd))
    return 1.0 - (clamped + 50_000_000.0) / 100_000_000.0


def active_addr_score(current: int, baseline: int) -> float:
    """Active addresses delta score.

    Returns 1.0 when active addresses exceed baseline by 20%+.
    """
    if baseline <= 0:
        return 0.5
    ratio = current / baseline
    return max(0.0, min(1.0, (ratio - 1.0) / 0.2))


async def fetch_onchain_snapshot(
    asset: str,
    *,
    client: BlockscoutClient | None = None,
    mcp_client: McpTap | None = None,
    address: str | None = None,
    chain_id: int = 1,
) -> dict[str, Any]:
    """Fetch on-chain metrics via Blockscout.

    Prefers the MCP tap path when ``ETA_USE_MCP_TAPS=1`` and an
    ``mcp_client`` implementing ``McpTap`` is provided with a valid
    ``address``.  Degrades gracefully to the REST ``BlockscoutClient``
    when the MCP client is missing or the flag is off.
    """
    if use_mcp_taps_enabled():
        if mcp_client is None:
            logger.warning(
                "ETA_USE_MCP_TAPS=1 but no mcp_client provided; "
                "falling back to REST BlockscoutClient for %s",
                asset,
            )
        elif address is None:
            logger.warning(
                "ETA_USE_MCP_TAPS=1 but no on-chain address supplied for %s; "
                "falling back to REST BlockscoutClient",
                asset,
            )
        else:
            snap = blockscout_snapshot(
                asset,
                address=address,
                chain_id=chain_id,
                mcp=mcp_client,
            )
            return snap.to_ctx()

    client = client or BlockscoutClient()
    transfers, netflow_usd, active_delta = await asyncio.gather(
        client.fetch_whale_transfers(asset),
        client.fetch_exchange_netflow(asset),
        client.fetch_active_addresses_delta(asset),
    )
    whale_transfers = len(transfers)
    whale_baseline = max(1, whale_transfers // 2 or 1)
    active_baseline = 1_000
    active_current = max(
        0,
        int(round(active_baseline * (1.0 + float(active_delta) * 0.1))),
    )
    return {
        "asset": asset,
        "source": "blockscout",
        "whale_transfers": whale_transfers,
        "whale_transfers_baseline": whale_baseline,
        "exchange_netflow_usd": float(netflow_usd),
        "active_addresses": active_current,
        "active_addresses_baseline": active_baseline,
        "active_addresses_delta": float(active_delta),
        "timestamp": datetime.now(UTC),
    }


class OnchainFeature(Feature):
    """On-chain activity feature.

    Expects in `ctx["onchain"]` a dict matching `fetch_onchain_snapshot`.
    Combines whale delta (50%), netflow (30%), active addrs (20%).
    """

    name: str = "onchain_delta"
    weight: float = 1.5

    def compute(self, bar: BarData, ctx: dict[str, Any]) -> float:
        snap: dict[str, Any] = ctx.get("onchain") or {}
        whale = whale_delta_score(
            int(snap.get("whale_transfers", 0)),
            int(snap.get("whale_transfers_baseline", 0)),
        )
        netflow = netflow_score(float(snap.get("exchange_netflow_usd", 0.0)))
        addrs = active_addr_score(
            int(snap.get("active_addresses", 0)),
            int(snap.get("active_addresses_baseline", 0)),
        )
        return 0.5 * whale + 0.3 * netflow + 0.2 * addrs

"""
EVOLUTIONARY TRADING ALGO  //  features.onchain
===================================
On-chain flow signal via Blockscout MCP.
Whale transfers + exchange netflow + active addresses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from eta_engine.core.data_pipeline import BarData
from eta_engine.features.base import Feature

# TODO: integrate live MCP call
# from mcp_client import blockscout  # pseudo-import, not wired yet


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


async def fetch_onchain_snapshot(asset: str) -> dict[str, Any]:
    """Fetch on-chain metrics via Blockscout MCP.

    TODO: integrate live MCP call via blockscout.get_token_transfers_by_address
    and aggregate into the fields below. Currently returns a zeroed stub so
    downstream pipelines can exercise wiring end-to-end.
    """
    # TODO: integrate live MCP call
    return {
        "asset": asset,
        "whale_transfers": 0,
        "whale_transfers_baseline": 0,
        "exchange_netflow_usd": 0.0,
        "active_addresses": 0,
        "active_addresses_baseline": 0,
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

"""
EVOLUTIONARY TRADING ALGO  //  features.sentiment
=====================================
Social sentiment signal via LunarCrush MCP.
Galaxy Score / AltRank / social volume / Fear & Greed divergence.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from eta_engine.data.sentiment_lunarcrush import LunarCrushClient
from eta_engine.features.base import Feature
from eta_engine.features.mcp_taps import (
    McpTap,
    lunarcrush_snapshot,
    use_mcp_taps_enabled,
)

if TYPE_CHECKING:
    from eta_engine.core.data_pipeline import BarData

logger = logging.getLogger(__name__)


def contrarian_extreme_score(galaxy_score: float, fng: int) -> float:
    """Contrarian signal on social/market divergence.

    Returns 1.0 when Galaxy > 80 but F&G < 25 (hype vs fear split),
    OR when Galaxy < 20 but F&G > 75 (despair vs greed).
    Baseline 0.0 when both aligned.
    """
    g = float(galaxy_score)
    f = float(fng)
    if g > 80 and f < 25:
        return 1.0
    if g < 20 and f > 75:
        return 1.0
    # Partial divergence: larger gap between social & market sentiment = stronger signal.
    # Normalize both to [0,1] and measure distance.
    g_norm = g / 100.0
    f_norm = f / 100.0
    gap = abs(g_norm - f_norm)
    return max(0.0, min(1.0, (gap - 0.2) / 0.4))


def alt_rank_score(alt_rank: int) -> float:
    """Lower AltRank = relatively outperforming. Top-20 = 1.0, 1000+ = 0.0."""
    if alt_rank <= 0:
        return 0.5
    if alt_rank <= 20:
        return 1.0
    if alt_rank >= 1000:
        return 0.0
    return max(0.0, 1.0 - (alt_rank - 20) / 980.0)


def social_volume_score(volume: int, baseline: int) -> float:
    """Social volume spike score.

    Returns 1.0 at 3× baseline, 0.0 when below baseline.
    """
    if baseline <= 0:
        return 0.5 if volume > 0 else 0.0
    ratio = volume / baseline
    return max(0.0, min(1.0, (ratio - 1.0) / 2.0))


async def fetch_sentiment_snapshot(
    asset: str,
    *,
    client: LunarCrushClient | None = None,
    mcp_client: McpTap | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Fetch sentiment metrics via LunarCrush.

    Prefers the MCP tap path when ``ETA_USE_MCP_TAPS=1`` and an
    ``mcp_client`` implementing ``McpTap`` is provided.  Degrades
    gracefully to the REST ``LunarCrushClient`` when the MCP client is
    missing or the flag is off.
    """
    if use_mcp_taps_enabled():
        if mcp_client is None:
            logger.warning(
                "ETA_USE_MCP_TAPS=1 but no mcp_client provided; falling back to REST LunarCrushClient for %s",
                asset,
            )
        else:
            snap = lunarcrush_snapshot(asset, symbol=symbol, mcp=mcp_client)
            return snap.to_ctx()

    client = client or LunarCrushClient()
    galaxy_score, alt_rank, social_volume, fear_greed = await asyncio.gather(
        client.fetch_galaxy_score(asset),
        client.fetch_alt_rank(asset),
        client.fetch_social_volume(asset),
        client.fetch_fear_greed(),
    )
    posts = int(social_volume.get("posts", 0))
    interactions = int(social_volume.get("interactions", 0))
    contributors = int(social_volume.get("contributors", 0))
    volume = max(
        0,
        int(social_volume.get("social_volume", posts + interactions // 10 + contributors * 2)),
    )
    volume_baseline = max(
        1,
        int(social_volume.get("social_volume_baseline", max(1, volume // 2 or 1))),
    )
    return {
        "asset": asset,
        "source": social_volume.get("source", "lunarcrush"),
        "galaxy_score": float(galaxy_score),
        "alt_rank": int(alt_rank),
        "social_volume": volume,
        "social_volume_baseline": volume_baseline,
        "social_volume_raw": social_volume.get(
            "social_volume_raw",
            {
                "posts": posts,
                "interactions": interactions,
                "contributors": contributors,
            },
        ),
        "social_volume_time_series_points": int(social_volume.get("time_series_points", 0)),
        "fear_greed": int(fear_greed),
        "timestamp": datetime.now(UTC),
    }


class SentimentFeature(Feature):
    """Sentiment feature combining Galaxy/AltRank/volume/F&G divergence.

    Expects in `ctx["sentiment"]` a dict matching `fetch_sentiment_snapshot`.
    Weights: divergence (45%), alt_rank (25%), social_volume (30%).
    """

    name: str = "sentiment"
    weight: float = 1.5

    def compute(self, bar: BarData, ctx: dict[str, Any]) -> float:
        snap: dict[str, Any] = ctx.get("sentiment") or {}
        div = contrarian_extreme_score(
            float(snap.get("galaxy_score", 50.0)),
            int(snap.get("fear_greed", 50)),
        )
        rank = alt_rank_score(int(snap.get("alt_rank", 100)))
        vol = social_volume_score(
            int(snap.get("social_volume", 0)),
            int(snap.get("social_volume_baseline", 0)),
        )
        return 0.45 * div + 0.25 * rank + 0.30 * vol

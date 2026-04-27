"""EVOLUTIONARY TRADING ALGO  //  features.mcp_taps.

Optional MCP-backed feature tap for Blockscout + LunarCrush.

Why this module exists
----------------------
`data.onchain_blockscout.BlockscoutClient` and
`data.sentiment_lunarcrush.LunarCrushClient` already talk to the live
REST APIs. But when the runtime runs inside an agent session (or a
CCR remote) that has the `mcp__blockscout__*` and `mcp__lunarcrush__*`
tools attached, we can skip REST entirely and drive features through
the MCP adapters directly -- same typed interface, lower latency,
unified auth, no API-key juggling.

This module exposes a tiny typed seam: ``tap(asset)`` returns an
``OnchainSnapshot`` / ``SentimentSnapshot`` in the exact shape that
``OnchainFeature`` / ``SentimentFeature`` consume in ``ctx["onchain"]``
and ``ctx["sentiment"]``. The actual MCP calls happen in the *caller*
(CCR runtime has tool access; this module cannot call MCP tools
itself). The adapter injection pattern is:

    snap = tap(asset, mcp_client=MyMcpWrapper())

Design
------
* **Pure boundary.** ``McpTap`` is a Protocol. Any object that quacks
  like it works -- the test suite uses an in-memory fake, production
  wires the real MCP client.
* **Feature-flag gated.** ``USE_MCP_TAPS`` defaults False. When True,
  `features.pipeline` prefers the MCP tap; when False, it falls back
  to the REST client.
* **Snapshot shape identical to REST path.** Downstream scorers cannot
  distinguish between sources -- by design.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

__all__ = [
    "OnchainSnapshot",
    "SentimentSnapshot",
    "McpTap",
    "blockscout_snapshot",
    "lunarcrush_snapshot",
    "use_mcp_taps_enabled",
]


def use_mcp_taps_enabled() -> bool:
    """True when the runtime should prefer MCP taps over REST clients."""
    return os.getenv("APEX_USE_MCP_TAPS", "0").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class OnchainSnapshot:
    """Matches the schema that ``OnchainFeature`` consumes via ``ctx[onchain]``."""

    asset: str
    whale_transfers: int
    whale_transfers_baseline: int
    exchange_netflow_usd: float
    active_addresses: int
    active_addresses_baseline: int
    active_addresses_delta: float
    source: str = "mcp_blockscout"
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_ctx(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "source": self.source,
            "whale_transfers": self.whale_transfers,
            "whale_transfers_baseline": self.whale_transfers_baseline,
            "exchange_netflow_usd": self.exchange_netflow_usd,
            "active_addresses": self.active_addresses,
            "active_addresses_baseline": self.active_addresses_baseline,
            "active_addresses_delta": self.active_addresses_delta,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class SentimentSnapshot:
    """Matches the schema that ``SentimentFeature`` consumes via ``ctx[sentiment]``."""

    asset: str
    galaxy_score: float
    alt_rank: int
    social_volume: int
    social_volume_baseline: int
    fear_greed: int
    source: str = "mcp_lunarcrush"
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_ctx(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "source": self.source,
            "galaxy_score": self.galaxy_score,
            "alt_rank": self.alt_rank,
            "social_volume": self.social_volume,
            "social_volume_baseline": self.social_volume_baseline,
            "fear_greed": self.fear_greed,
            "timestamp": self.timestamp,
        }


class McpTap(Protocol):
    """Minimal shape that a wrapper around ``mcp__blockscout__*`` / ``mcp__lunarcrush__*`` provides.

    Only the methods actually called are required; a real implementation
    would live in `eta_engine.runtime.mcp_tap_client` or similar and
    route each method to the corresponding MCP tool call.
    """

    def get_address_info(self, *, address: str, chain_id: int) -> dict[str, Any]: ...

    def get_token_transfers(
        self,
        *,
        address: str,
        chain_id: int,
        age_hours: int = 24,
    ) -> list[dict[str, Any]]: ...

    def get_galaxy_score(self, *, symbol: str) -> float: ...

    def get_alt_rank(self, *, symbol: str) -> int: ...

    def get_social_volume(self, *, symbol: str) -> dict[str, Any]: ...

    def get_fear_greed_index(self) -> int: ...


def blockscout_snapshot(
    asset: str,
    *,
    address: str,
    chain_id: int = 1,
    mcp: McpTap,
    whale_threshold_usd: float = 1_000_000.0,
) -> OnchainSnapshot:
    """Build an on-chain snapshot via the MCP tap.

    The caller supplies a wrapper ``mcp`` that mediates between this
    module and the actual ``mcp__blockscout__*`` tool calls; this keeps
    the module pure and testable without loading MCP schemas.
    """
    transfers = mcp.get_token_transfers(address=address, chain_id=chain_id) or []
    whales = [t for t in transfers if _transfer_value_usd(t) >= whale_threshold_usd]
    whale_count = len(whales)
    whale_baseline = max(1, whale_count // 2 or 1)
    netflow = sum(_signed_netflow_usd(t, target=address) for t in transfers)
    counterparties = {_counterparty(t, target=address) for t in transfers}
    counterparties.discard(None)
    active = max(1, len(counterparties))
    active_baseline = max(1, int(active * 0.7))
    active_delta = (active - active_baseline) / active_baseline
    return OnchainSnapshot(
        asset=asset,
        whale_transfers=whale_count,
        whale_transfers_baseline=whale_baseline,
        exchange_netflow_usd=float(netflow),
        active_addresses=active,
        active_addresses_baseline=active_baseline,
        active_addresses_delta=max(-1.0, min(2.0, float(active_delta))),
    )


def lunarcrush_snapshot(
    asset: str,
    *,
    symbol: str | None = None,
    mcp: McpTap,
) -> SentimentSnapshot:
    """Build a social-sentiment snapshot via the MCP tap."""
    sym = symbol or _normalize_symbol(asset)
    galaxy = float(mcp.get_galaxy_score(symbol=sym))
    alt = int(mcp.get_alt_rank(symbol=sym))
    raw_vol = mcp.get_social_volume(symbol=sym) or {}
    volume = int(raw_vol.get("social_volume", raw_vol.get("posts", 0) or 0))
    baseline = int(raw_vol.get("social_volume_baseline", max(1, volume // 2 or 1)))
    fng = int(mcp.get_fear_greed_index())
    return SentimentSnapshot(
        asset=asset,
        galaxy_score=galaxy,
        alt_rank=alt,
        social_volume=volume,
        social_volume_baseline=baseline,
        fear_greed=fng,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _transfer_value_usd(t: dict[str, Any]) -> float:
    total = t.get("total") if isinstance(t.get("total"), dict) else {}
    token = t.get("token") if isinstance(t.get("token"), dict) else {}
    try:
        value = float(total.get("value") or t.get("value") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    try:
        decimals = int(float(total.get("decimals") or token.get("decimals") or 0))
    except (TypeError, ValueError):
        decimals = 0
    try:
        rate = float(token.get("exchange_rate") or token.get("price_usd") or 1.0)
    except (TypeError, ValueError):
        rate = 1.0
    if decimals > 0:
        value /= 10**decimals
    return max(0.0, value * rate)


def _signed_netflow_usd(t: dict[str, Any], *, target: str) -> float:
    value = _transfer_value_usd(t)
    if value <= 0.0:
        return 0.0
    to_addr = _hash_field(t.get("to")) or ""
    from_addr = _hash_field(t.get("from")) or ""
    tgt = target.lower()
    if to_addr.lower() == tgt:
        return value
    if from_addr.lower() == tgt:
        return -value
    return 0.0


def _counterparty(t: dict[str, Any], *, target: str) -> str | None:
    to_addr = _hash_field(t.get("to"))
    from_addr = _hash_field(t.get("from"))
    tgt = target.lower()
    if to_addr and to_addr.lower() != tgt:
        return to_addr.lower()
    if from_addr and from_addr.lower() != tgt:
        return from_addr.lower()
    return None


def _hash_field(raw: object) -> str | None:
    if isinstance(raw, dict):
        val = raw.get("hash")
        return str(val) if val not in (None, "") else None
    return str(raw) if raw not in (None, "") else None


def _normalize_symbol(asset: str) -> str:
    clean = "".join(ch for ch in str(asset).upper() if ch.isalnum())
    for suffix in ("USDT", "USDC", "USD", "PERP", "USDM"):
        if clean.endswith(suffix) and len(clean) > len(suffix):
            clean = clean[: -len(suffix)]
            break
    return clean or str(asset).upper()

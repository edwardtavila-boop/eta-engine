"""Free-API on-chain metrics fetcher for the OnChainSchool (Wave-6, 2026-04-27).

The OnChainSchool reads ``ctx.onchain`` (a dict). This module provides a
``fetch_onchain(symbol)`` function that hits FREE public APIs to populate
that dict for BTC + ETH:

  * mempool.space          BTC mempool fees, difficulty, hash rate
  * blockchain.info        BTC price, market cap, transactions
  * defillama.com          ETH-side TVL + chain stats (no key required)
  * coingecko (free tier)  market data

NOTE: free APIs don't give us SOPR / MVRV / NUPL / dormancy --
Glassnode requires a paid API key. This fetcher returns the metrics
that ARE freely available and leaves the Glassnode-only fields as None.
The OnChainSchool's logic gracefully ignores missing fields.

Usage::

    from eta_engine.brain.jarvis_v3.sage.onchain_fetcher import fetch_onchain
    onchain = fetch_onchain("BTCUSDT")  # dict suitable for ctx.onchain

    ctx = MarketContext(bars=..., side="long", instrument_class="crypto",
                        onchain=onchain)

A single fetch is cached in-memory for ``CACHE_TTL_SECONDS`` (default
300s) so the school doesn't pound public APIs every consult.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300  # 5 minutes


@dataclass
class _CacheEntry:
    value: dict[str, Any]
    fetched_at: float


_CACHE: dict[str, _CacheEntry] = {}
_LOCK = threading.Lock()


def _http_json(url: str, timeout: float = 5.0) -> dict[str, Any] | None:
    """GET a URL, return parsed JSON or None on any failure."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "eta-engine-onchain-fetcher/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        logger.debug("HTTP fetch failed for %s: %s", url, exc)
        return None
    except json.JSONDecodeError as exc:
        logger.debug("JSON decode failed for %s: %s", url, exc)
        return None


def _btc_onchain() -> dict[str, Any]:
    """BTC on-chain metrics from free APIs."""
    out: dict[str, Any] = {}

    # Mempool fees + difficulty (mempool.space)
    fees = _http_json("https://mempool.space/api/v1/fees/recommended")
    if fees:
        out["fees_fastest_sats_vb"] = fees.get("fastestFee")
        out["fees_economy_sats_vb"] = fees.get("economyFee")

    # Mempool size (transactions awaiting confirmation)
    mempool = _http_json("https://mempool.space/api/mempool")
    if mempool:
        out["mempool_count"] = mempool.get("count")
        out["mempool_vsize"] = mempool.get("vsize")

    # Hash rate / difficulty
    diff = _http_json("https://mempool.space/api/v1/mining/difficulty-adjustments")
    if diff and isinstance(diff, list) and diff:
        latest = diff[0] if isinstance(diff[0], dict) else None
        if latest:
            out["difficulty_change_pct"] = latest.get("difficultyChange")

    # Optional: blockchain.info stats (sometimes 200, sometimes 502)
    chain_stats = _http_json("https://api.blockchain.info/stats")
    if chain_stats:
        out["network_hash_rate"] = chain_stats.get("hash_rate")
        out["next_retarget"] = chain_stats.get("nextretarget")
        out["total_btc"] = chain_stats.get("totalbc")

    return out


def _eth_onchain() -> dict[str, Any]:
    """ETH on-chain metrics from free APIs."""
    out: dict[str, Any] = {}

    # Defillama TVL (chain-level, free, no key)
    tvl = _http_json("https://api.llama.fi/v2/historicalChainTvl/Ethereum")
    if tvl and isinstance(tvl, list) and tvl:
        latest = tvl[-1] if isinstance(tvl[-1], dict) else None
        if latest and "tvl" in latest:
            out["ethereum_tvl_usd"] = latest["tvl"]

    # Coingecko ETH market data
    cg = _http_json("https://api.coingecko.com/api/v3/coins/ethereum?localization=false&tickers=false&community_data=false&developer_data=false")
    if cg and isinstance(cg, dict):
        md = cg.get("market_data", {}) or {}
        out["price_usd"] = (md.get("current_price") or {}).get("usd")
        out["market_cap_usd"] = (md.get("market_cap") or {}).get("usd")
        out["total_volume_usd"] = (md.get("total_volume") or {}).get("usd")
        out["circulating_supply"] = md.get("circulating_supply")

    return out


def fetch_onchain(symbol: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch on-chain metrics for the given symbol. Returns a dict
    suitable for ``MarketContext.onchain``.

    Returns empty dict for unsupported symbols (BTC + ETH only).
    Returns empty dict when offline (every API call failed).
    """
    sym = symbol.upper()
    base = ""
    if sym.startswith("BTC") or sym in ("MBT", "BTC"):
        base = "BTC"
    elif sym.startswith("ETH") or sym in ("MET", "ETH"):
        base = "ETH"
    else:
        return {}

    cache_key = base
    now = time.time()
    if not force_refresh:
        with _LOCK:
            entry = _CACHE.get(cache_key)
            if entry and (now - entry.fetched_at) < CACHE_TTL_SECONDS:
                return dict(entry.value)

    out = _btc_onchain() if base == "BTC" else _eth_onchain()

    out["_source"] = f"free_apis_{base.lower()}"
    out["_fetched_at"] = now

    with _LOCK:
        _CACHE[cache_key] = _CacheEntry(value=out, fetched_at=now)
    return dict(out)


def clear_cache() -> int:
    with _LOCK:
        n = len(_CACHE)
        _CACHE.clear()
        return n

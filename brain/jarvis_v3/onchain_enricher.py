"""On-chain feature enricher for crypto bots (Tier-1 #7, 2026-04-27).

Pulls per-symbol on-chain features into a normalized
``OnchainSnapshot`` that BTC/ETH/SOL/XRP bots can use as confluence
inputs alongside price/volume.

Like the sentiment scorer, the actual MCP calls (Coinbase /
Blockscout / LunarCrush) live in the agent layer; the bots read from
``state/onchain/<symbol>.json`` written by an agent worker every N
minutes.

Features per snapshot:
  * funding_rate_8h    -- annualized perp funding rate (CME futures
                         analog: basis premium)
  * open_interest_usd  -- total OI across major venues
  * net_exchange_flow  -- 24h net flow IN/OUT of exchanges (negative
                         = withdrawals = bullish accumulation)
  * whale_tx_count_24h -- count of $1M+ transfers in 24h
  * btc_dominance_pct  -- BTC dominance for crypto-wide regime
  * realized_vol_30d   -- annualized realized volatility
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
ONCHAIN_DIR = ROOT / "state" / "onchain"


@dataclass(frozen=True)
class OnchainSnapshot:
    symbol: str
    ts: datetime
    funding_rate_8h: float | None  # decimal (0.0001 = 1bp / 8h)
    open_interest_usd: float | None
    net_exchange_flow_usd: float | None  # +inflow / -outflow (24h)
    whale_tx_count_24h: int | None
    btc_dominance_pct: float | None
    realized_vol_30d: float | None  # annualized
    is_stale: bool = False


def current_snapshot(
    symbol: str,
    *,
    max_age_min: float = 30.0,
) -> OnchainSnapshot | None:
    """Most recent on-chain snapshot for symbol; None if missing/stale."""
    path = ONCHAIN_DIR / f"{symbol.upper()}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("can't read %s: %s", path, exc)
        return None

    ts_str = data.get("ts")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (TypeError, ValueError, AttributeError):
        return None

    age = datetime.now(UTC) - ts
    is_stale = age > timedelta(minutes=max_age_min)

    return OnchainSnapshot(
        symbol=str(data.get("symbol", symbol)).upper(),
        ts=ts,
        funding_rate_8h=data.get("funding_rate_8h"),
        open_interest_usd=data.get("open_interest_usd"),
        net_exchange_flow_usd=data.get("net_exchange_flow_usd"),
        whale_tx_count_24h=data.get("whale_tx_count_24h"),
        btc_dominance_pct=data.get("btc_dominance_pct"),
        realized_vol_30d=data.get("realized_vol_30d"),
        is_stale=is_stale,
    )


def confluence_signal(
    snap: OnchainSnapshot | None,
    *,
    direction: str,
) -> dict[str, float]:
    """Per-feature contribution to confluence in [-1.0, +1.0].

    Heuristics tuned for crypto majors:
      * negative funding => crowded short => bullish for longs
      * outflow exchange flow => accumulation => bullish for longs
      * high whale_tx_count => big-money activity, sign-agnostic boost

    Returns a dict so the caller can weight + sum themselves; total
    bias is clamped to +/-1.0 by the consumer.
    """
    if snap is None or snap.is_stale:
        return {}

    is_long = direction.lower() in ("long", "buy", "bull")
    out: dict[str, float] = {}

    # Funding: crowded shorts = -funding (positive for longs)
    if snap.funding_rate_8h is not None:
        f = snap.funding_rate_8h
        # >|0.05% per 8h| is meaningfully crowded
        if abs(f) > 0.0005:
            sign = +1.0 if (f < 0) == is_long else -1.0
            out["funding"] = round(sign * min(1.0, abs(f) / 0.002), 3)

    # Net exchange flow: outflow = accumulation
    if snap.net_exchange_flow_usd is not None:
        flow = snap.net_exchange_flow_usd
        # Materially > $10M moves
        if abs(flow) > 10_000_000:
            sign = +1.0 if (flow < 0) == is_long else -1.0
            out["exch_flow"] = round(sign * min(1.0, abs(flow) / 100_000_000), 3)

    # Whale activity: sign-agnostic confidence boost when active
    if snap.whale_tx_count_24h is not None and snap.whale_tx_count_24h > 50:
        out["whales"] = round(min(0.5, snap.whale_tx_count_24h / 200), 3)

    return out


def write_snapshot(snap: OnchainSnapshot) -> Path:
    """Persist a snapshot. Used by the agent-layer worker."""
    ONCHAIN_DIR.mkdir(parents=True, exist_ok=True)
    path = ONCHAIN_DIR / f"{snap.symbol}.json"
    path.write_text(
        json.dumps(
            {
                "symbol": snap.symbol,
                "ts": snap.ts.isoformat(),
                "funding_rate_8h": snap.funding_rate_8h,
                "open_interest_usd": snap.open_interest_usd,
                "net_exchange_flow_usd": snap.net_exchange_flow_usd,
                "whale_tx_count_24h": snap.whale_tx_count_24h,
                "btc_dominance_pct": snap.btc_dominance_pct,
                "realized_vol_30d": snap.realized_vol_30d,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path

"""Shared BTC market-quality helpers.

The BTC stack now carries several microstructure signals beyond spread and
imbalance. This module keeps the book-depth / freshness / quality math in one
place so live, paper, and strategy code all score the same tape the same way.
"""

# ruff: noqa: ANN401  -- this module deliberately takes dict[str, Any] inputs
from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


def coerce_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_epoch_ms(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
        return dt.timestamp() * 1000.0
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value <= 0.0:
            return None
        # Values above 1e12 are already ms. Smaller unix timestamps are seconds.
        return value if value >= 1_000_000_000_000.0 else value * 1000.0
    try:
        text = str(raw).strip()
        if not text:
            return None
        if text.isdigit():
            value = float(text)
            return value if value >= 1_000_000_000_000.0 else value * 1000.0
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp() * 1000.0


def order_book_age_ms(
    snapshot: Mapping[str, Any],
    *,
    bar_ts: Any | None = None,
) -> float | None:
    raw_age = snapshot.get("order_book_age_ms")
    if raw_age is None:
        raw_age = snapshot.get("book_age_ms")
    if raw_age is None:
        raw_age = snapshot.get("order_book_staleness_ms")
    if raw_age is None:
        raw_age = snapshot.get("book_staleness_ms")
    if raw_age is not None:
        return max(0.0, coerce_float(raw_age, 0.0))

    snapshot_ts = snapshot.get("order_book_ts")
    if snapshot_ts is None:
        snapshot_ts = snapshot.get("order_book_snapshot_ts")
    if snapshot_ts is None:
        snapshot_ts = snapshot.get("ts")

    bar_ms = _parse_epoch_ms(bar_ts)
    snapshot_ms = _parse_epoch_ms(snapshot_ts)
    if bar_ms is None or snapshot_ms is None:
        return None
    return max(0.0, bar_ms - snapshot_ms)


def _spread_bps(snapshot: Mapping[str, Any], *, default: float = 0.0) -> float:
    raw = snapshot.get("spread_bps")
    if raw is None:
        raw = snapshot.get("bid_ask_spread_bps")
    if raw is not None:
        value = coerce_float(raw, default)
        if value > 0.0:
            return value
    spread = coerce_float(snapshot.get("spread"), 0.0)
    close = coerce_float(snapshot.get("close"), 0.0)
    if spread > 0.0 and close > 0.0:
        return max(0.0, (spread / close) * 10_000.0)
    bid = coerce_float(snapshot.get("bid_price"), 0.0) or coerce_float(snapshot.get("best_bid"), 0.0)
    ask = coerce_float(snapshot.get("ask_price"), 0.0) or coerce_float(snapshot.get("best_ask"), 0.0)
    if bid > 0.0 and ask > bid:
        mid = (bid + ask) / 2.0
        if mid > 0.0:
            return max(0.0, ((ask - bid) / mid) * 10_000.0)
    return default


def _book_imbalance(snapshot: Mapping[str, Any]) -> float:
    for key in ("book_imbalance", "order_book_imbalance", "bid_ask_imbalance", "imbalance"):
        raw = snapshot.get(key)
        if raw is not None:
            return clamp(coerce_float(raw, 0.0), -1.0, 1.0)
    bid_depth = coerce_float(snapshot.get("bid_depth"), 0.0)
    ask_depth = coerce_float(snapshot.get("ask_depth"), 0.0)
    if bid_depth > 0.0 and ask_depth > 0.0:
        total = bid_depth + ask_depth
        if total > 0.0:
            return clamp((bid_depth - ask_depth) / total, -1.0, 1.0)
    depth_1 = coerce_float(snapshot.get("depth_1"), 0.0)
    depth_5 = coerce_float(snapshot.get("depth_5"), 0.0)
    if depth_1 != 0.0 or depth_5 != 0.0:
        total = abs(depth_1) + abs(depth_5)
        if total > 0.0:
            return clamp((depth_1 - depth_5) / total, -1.0, 1.0)
    return 0.0


def order_book_depth_score(snapshot: Mapping[str, Any]) -> float:
    for key in ("order_book_depth_score", "book_depth_score", "depth_score"):
        raw = snapshot.get(key)
        if raw is not None:
            return clamp(coerce_float(raw, 5.0), 0.0, 10.0)

    bid_depth = coerce_float(snapshot.get("bid_depth"), 0.0)
    ask_depth = coerce_float(snapshot.get("ask_depth"), 0.0)
    notional_bid = coerce_float(snapshot.get("notional_depth_1"), 0.0)
    notional_ask = coerce_float(snapshot.get("notional_depth_5"), 0.0)
    close = coerce_float(snapshot.get("close"), 0.0)
    spread_bps = _spread_bps(snapshot)
    imbalance = abs(_book_imbalance(snapshot))

    total_depth = bid_depth + ask_depth
    total_notional = notional_bid + notional_ask
    if total_depth <= 0.0 and total_notional <= 0.0:
        return 5.0

    score = 5.0
    if total_depth > 0.0:
        score += min(2.5, math.log1p(total_depth) / 4.5)
    if total_notional > 0.0 and close > 0.0:
        score += min(2.0, math.log1p(total_notional / max(close, 1.0)) / 5.0)
    score += max(-1.2, min(1.2, 1.0 - imbalance * 2.0))
    score += max(-0.9, min(0.9, 1.0 - spread_bps / 20.0))
    return clamp(score, 0.0, 10.0)


def order_book_freshness_score(
    snapshot: Mapping[str, Any],
    *,
    bar_ts: Any | None = None,
    age_ms: float | None = None,
) -> float:
    for key in ("order_book_freshness_score", "book_freshness_score", "freshness_score"):
        raw = snapshot.get(key)
        if raw is not None:
            return clamp(coerce_float(raw, 5.0), 0.0, 10.0)

    if age_ms is None:
        age_ms = order_book_age_ms(snapshot, bar_ts=bar_ts)
    if age_ms is None:
        return 5.0

    if age_ms <= 0.0:
        return 10.0
    if age_ms <= 250.0:
        return 9.5
    if age_ms <= 500.0:
        return 9.0
    if age_ms <= 1_000.0:
        return 8.0
    if age_ms <= 2_500.0:
        return 6.5
    if age_ms <= 5_000.0:
        return 4.5
    if age_ms <= 10_000.0:
        return 2.5
    return 1.0


def order_book_quality(
    snapshot: Mapping[str, Any],
    *,
    bar_ts: Any | None = None,
    age_ms: float | None = None,
    spread_bps: float | None = None,
    book_imbalance: float | None = None,
    depth_score: float | None = None,
    freshness_score: float | None = None,
) -> float:
    for key in ("order_book_quality", "book_quality_score", "depth_quality_score"):
        raw = snapshot.get(key)
        if raw is not None:
            return clamp(coerce_float(raw, 5.0), 0.0, 10.0)

    depth = depth_score if depth_score is not None else order_book_depth_score(snapshot)
    freshness = (
        freshness_score
        if freshness_score is not None
        else order_book_freshness_score(snapshot, bar_ts=bar_ts, age_ms=age_ms)
    )
    spread = spread_bps if spread_bps is not None else _spread_bps(snapshot)
    imbalance = book_imbalance if book_imbalance is not None else _book_imbalance(snapshot)

    spread_score = 0.5 if spread <= 0.0 else clamp(1.0 - spread / 18.0, 0.0, 1.0)
    balance_score = clamp(1.0 - abs(imbalance), 0.0, 1.0)
    quality = 0.36 * (depth / 10.0) + 0.24 * (freshness / 10.0) + 0.22 * spread_score + 0.18 * balance_score
    return clamp(10.0 * quality, 0.0, 10.0)


def order_book_quality_bucket(quality: float) -> str:
    score = clamp(coerce_float(quality, 5.0), 0.0, 10.0)
    if score < 2.0:
        return "Q0_2"
    if score < 4.0:
        return "Q2_4"
    if score < 6.0:
        return "Q4_6"
    if score < 8.0:
        return "Q6_8"
    return "Q8_10"


def derive_order_book_metrics(
    snapshot: Mapping[str, Any],
    *,
    bar_ts: Any | None = None,
    spread_bps: float | None = None,
    book_imbalance: float | None = None,
) -> dict[str, Any]:
    """Return the derived order-book metrics as a stable dictionary."""
    age_ms = order_book_age_ms(snapshot, bar_ts=bar_ts)
    depth_score = order_book_depth_score(snapshot)
    freshness_score = order_book_freshness_score(snapshot, bar_ts=bar_ts, age_ms=age_ms)
    quality = order_book_quality(
        snapshot,
        bar_ts=bar_ts,
        age_ms=age_ms,
        spread_bps=spread_bps,
        book_imbalance=book_imbalance,
        depth_score=depth_score,
        freshness_score=freshness_score,
    )
    return {
        "order_book_age_ms": round(age_ms, 3) if age_ms is not None else 0.0,
        "order_book_depth_score": round(depth_score, 3),
        "order_book_freshness_score": round(freshness_score, 3),
        "order_book_quality": round(quality, 3),
        "order_book_quality_bucket": order_book_quality_bucket(quality),
    }


def _first_present(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return default


def build_market_context_summary(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact market-context summary from a runtime snapshot."""
    external = snapshot.get("market_context")
    external_ctx = external if isinstance(external, Mapping) else {}
    market_regime = str(
        _first_present(
            external_ctx.get("market_regime"),
            snapshot.get("market_context_regime"),
            snapshot.get("market_quality_label"),
            snapshot.get("spread_regime"),
            default="UNKNOWN",
        ),
    )
    market_quality = coerce_float(
        _first_present(
            snapshot.get("market_context_score"),
            snapshot.get("market_quality"),
            external_ctx.get("market_quality"),
            external_ctx.get("external_score"),
            default=0.0,
        ),
        0.0,
    )
    market_quality_label = str(
        _first_present(
            snapshot.get("market_quality_label"),
            external_ctx.get("market_quality_label"),
            snapshot.get("spread_regime"),
            default="UNKNOWN",
        ),
    )
    summary: dict[str, Any] = {
        "market_context_regime": market_regime,
        "market_context_quality": round(market_quality, 4),
        "market_context_quality_label": market_quality_label,
        "market_context_external_score": coerce_float(
            _first_present(
                external_ctx.get("external_score"),
                snapshot.get("market_context_external_score"),
                snapshot.get("market_context_score"),
                default=market_quality,
            ),
            market_quality,
        ),
        "market_context_asset": str(
            _first_present(
                external_ctx.get("asset"),
                snapshot.get("market_context_asset"),
                default="",
            ),
        ),
        "market_context_venue": str(
            _first_present(
                external_ctx.get("venue"),
                snapshot.get("market_context_venue"),
                default="",
            ),
        ),
        "market_context_updated_utc": str(
            _first_present(
                external_ctx.get("updated_utc"),
                snapshot.get("market_context_updated_utc"),
                default="",
            ),
        ),
        "spread_regime": str(_first_present(snapshot.get("spread_regime"), default="UNKNOWN")),
        "spread_bps": round(coerce_float(snapshot.get("spread_bps"), 0.0), 4),
        "book_imbalance": round(coerce_float(snapshot.get("book_imbalance"), 0.0), 4),
        "microstructure_score": round(coerce_float(snapshot.get("microstructure_score"), 0.0), 4),
        "pattern_edge_score": round(coerce_float(snapshot.get("pattern_edge_score"), 0.0), 4),
        "session_phase": str(_first_present(snapshot.get("session_phase"), default="UNKNOWN")),
        "timeframe_label": str(_first_present(snapshot.get("timeframe_label"), default="UNKNOWN")),
        "session_timeframe_key": str(_first_present(snapshot.get("session_timeframe_key"), default="UNKNOWN::UNKNOWN")),
        "order_book_venue": str(_first_present(snapshot.get("order_book_venue"), default="")),
        "order_book_depth": int(coerce_float(snapshot.get("order_book_depth"), 0.0)),
        "order_book_quality": round(coerce_float(snapshot.get("order_book_quality"), 0.0), 4),
        "order_book_quality_bucket": str(_first_present(snapshot.get("order_book_quality_bucket"), default="")),
        "order_book_age_ms": round(coerce_float(snapshot.get("order_book_age_ms"), 0.0), 4),
        "temporal_size_mult": round(coerce_float(snapshot.get("temporal_size_mult"), 0.0), 4),
        "session_size_bias": round(coerce_float(snapshot.get("session_size_bias"), 0.0), 4),
        "timeframe_size_bias": round(coerce_float(snapshot.get("timeframe_size_bias"), 0.0), 4),
        "session_timeframe_size_bias": round(coerce_float(snapshot.get("session_timeframe_size_bias"), 0.0), 4),
        "spread_size_bias": round(coerce_float(snapshot.get("spread_size_bias"), 0.0), 4),
        "market_quality_blocked": bool(snapshot.get("market_quality_blocked")),
    }
    if external_ctx:
        summary["market_context"] = dict(external_ctx)
    return summary


def format_market_context_summary(summary: Mapping[str, Any]) -> str:
    """Return a one-line human-readable market-context rollup.

    The nested summary dict is the canonical artifact, but a stable text
    line is easier to scan in live health output, alerts, and dashboards.
    """
    regime = str(_first_present(summary.get("market_context_regime"), default="UNKNOWN")).upper()
    quality = coerce_float(summary.get("market_context_quality"), 0.0)
    timeframe = str(_first_present(summary.get("session_timeframe_key"), default="UNKNOWN::UNKNOWN"))
    spread = str(_first_present(summary.get("spread_regime"), default="UNKNOWN")).upper()
    parts = [
        f"market_context={regime}",
        f"quality={quality:.2f}",
        f"tf={timeframe}",
        f"spread={spread}",
    ]
    asset = str(_first_present(summary.get("market_context_asset"), default="")).strip()
    if asset and asset.upper() != "UNKNOWN":
        parts.append(f"asset={asset}")
    venue = str(_first_present(summary.get("market_context_venue"), default="")).strip()
    if venue:
        parts.append(f"venue={venue}")
    external_score = coerce_float(summary.get("market_context_external_score"), quality)
    if abs(external_score - quality) > 1e-6:
        parts.append(f"ext={external_score:.2f}")
    ob_bucket = str(_first_present(summary.get("order_book_quality_bucket"), default="")).strip()
    if ob_bucket:
        parts.append(f"ob={ob_bucket}")
    order_book_quality = coerce_float(summary.get("order_book_quality"), 0.0)
    if order_book_quality:
        parts.append(f"obq={order_book_quality:.2f}")
    micro = coerce_float(summary.get("microstructure_score"), 0.0)
    if micro:
        parts.append(f"micro={micro:.2f}")
    pattern = coerce_float(summary.get("pattern_edge_score"), 0.0)
    if pattern:
        parts.append(f"pattern={pattern:.2f}")
    return " ".join(parts)

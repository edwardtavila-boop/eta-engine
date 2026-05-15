"""Shared sentiment-pressure helpers for JARVIS runtime and ops surfaces.

This module turns cached per-asset sentiment snapshots into a consistent
pressure summary that both the dashboard and the live decision layer can use.
It never performs network IO on the hot path; callers read the warmed
``sentiment_overlay`` cache only.
"""

from __future__ import annotations

from typing import Any

from eta_engine.brain.jarvis_v3 import sentiment_overlay

_POSITIVE_TOPICS = {"fomo", "squeeze", "jobs"}
_NEGATIVE_TOPICS = {
    "capitulation",
    "earnings_blowup",
    "geopolitics",
    "hack",
    "inflation",
    "regulation",
    "tariffs",
}
_SYMBOL_ASSET_MAP = {
    "BTC": "BTC",
    "MBT": "BTC",
    "ETH": "ETH",
    "MET": "ETH",
    "SOL": "SOL",
}


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _float_value(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def primary_asset_for_symbol(symbol: str) -> str:
    symbol_text = str(symbol or "").upper().lstrip("/").strip()
    if not symbol_text:
        return ""
    root = symbol_text.rstrip("0123456789")
    for prefix, asset in _SYMBOL_ASSET_MAP.items():
        if root == prefix or symbol_text.startswith(prefix):
            return asset
    return ""


def sentiment_assets_for_symbol(symbol: str, instrument_class: str | None = None) -> list[str]:
    primary = primary_asset_for_symbol(symbol)
    if primary:
        return [primary, "macro"]
    if instrument_class in {"futures", "equity", "fx"}:
        return ["macro"]
    return ["macro"]


def normalize_asset_snapshot(asset: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    topic_flags = snapshot.get("topic_flags") if isinstance(snapshot.get("topic_flags"), dict) else {}
    active_topics = [str(topic).strip() for topic, enabled in topic_flags.items() if enabled and str(topic).strip()]
    extras = snapshot.get("extras") if isinstance(snapshot.get("extras"), dict) else {}
    headlines = extras.get("headlines") if isinstance(extras.get("headlines"), list) else []
    headline_count = int(extras.get("headline_count") or len(headlines) or 0)
    return {
        "asset": str(asset).strip(),
        "fear_greed": _float_value(snapshot.get("fear_greed")),
        "social_volume_z": _float_value(snapshot.get("social_volume_z")),
        "active_topics": active_topics,
        "headline_count": headline_count,
        "headlines": headlines,
        "query": str(extras.get("query") or ""),
        "raw_source": str(snapshot.get("raw_source") or ""),
    }


def asset_pressure_score(asset_summary: dict[str, Any]) -> float:
    fear_greed = _float_value(asset_summary.get("fear_greed"))
    social_volume_z = _float_value(asset_summary.get("social_volume_z"))
    fear_score = _clamp(((fear_greed or 0.5) - 0.5) * 2.0, -1.0, 1.0)
    volume_score = _clamp((social_volume_z or 0.0) / 3.0, -1.0, 1.0)
    active_topics = asset_summary.get("active_topics") if isinstance(asset_summary.get("active_topics"), list) else []
    topic_adjustment = 0.0
    for topic in active_topics:
        topic_name = str(topic).strip().lower()
        if topic_name in _POSITIVE_TOPICS:
            topic_adjustment += 0.05
        elif topic_name in _NEGATIVE_TOPICS:
            topic_adjustment -= 0.05
    return round(_clamp(0.4 * fear_score + 0.6 * volume_score + topic_adjustment, -1.0, 1.0), 4)


def unknown_pressure() -> dict[str, Any]:
    return {
        "status": "unknown",
        "score": 0.0,
        "crypto_score": None,
        "macro_score": None,
        "lead_positive_asset": "",
        "lead_positive_score": None,
        "lead_negative_asset": "",
        "lead_negative_score": None,
        "macro_topics": [],
        "summary_line": "Sentiment pressure unavailable.",
    }


def summarize_pressure(asset_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    if not asset_summaries:
        return unknown_pressure()

    scored_rows: list[dict[str, Any]] = []
    for row in asset_summaries:
        enriched = dict(row)
        enriched["asset"] = str(row.get("asset") or "").strip()
        enriched["pressure_score"] = asset_pressure_score(row)
        scored_rows.append(enriched)

    crypto_rows = [row for row in scored_rows if str(row.get("asset") or "").lower() != "macro"]
    macro_row = next((row for row in scored_rows if str(row.get("asset") or "").lower() == "macro"), None)
    crypto_score = (
        round(sum(float(row["pressure_score"]) for row in crypto_rows) / len(crypto_rows), 4) if crypto_rows else None
    )
    macro_score = float(macro_row["pressure_score"]) if macro_row is not None else None

    weighted_total = 0.0
    weighted_count = 0.0
    for row in scored_rows:
        asset_name = str(row.get("asset") or "").lower()
        weight = 1.25 if asset_name == "macro" else 1.0
        weighted_total += float(row["pressure_score"]) * weight
        weighted_count += weight
    composite_score = round(weighted_total / weighted_count, 4) if weighted_count > 0 else 0.0

    lead_positive = max(scored_rows, key=lambda row: float(row.get("pressure_score") or 0.0))
    lead_negative = min(scored_rows, key=lambda row: float(row.get("pressure_score") or 0.0))
    lead_positive_score = float(lead_positive.get("pressure_score") or 0.0)
    lead_negative_score = float(lead_negative.get("pressure_score") or 0.0)

    macro_topics = []
    if isinstance(macro_row, dict) and isinstance(macro_row.get("active_topics"), list):
        macro_topics = [str(topic).strip() for topic in macro_row.get("active_topics", []) if str(topic).strip()]
    macro_topic_label = ""
    for topic in macro_topics:
        if topic.lower() in _NEGATIVE_TOPICS:
            macro_topic_label = topic
            break
    if not macro_topic_label and macro_topics:
        macro_topic_label = macro_topics[0]

    if lead_positive_score >= 0.45 and composite_score >= 0.05 and (macro_score is None or macro_score > -0.2):
        status = "risk_on"
    elif lead_negative_score <= -0.45 and composite_score <= -0.05 and (macro_score is None or macro_score < 0.2):
        status = "risk_off"
    elif (
        crypto_score is not None
        and macro_score is not None
        and ((crypto_score > 0.1 and macro_score < -0.05) or (crypto_score < -0.1 and macro_score > 0.05))
    ):
        status = "mixed"
    elif abs(composite_score) < 0.1:
        status = "neutral"
    else:
        status = "risk_on" if composite_score > 0 else "risk_off"

    if status == "risk_on":
        if macro_topic_label:
            summary_line = (
                f"Risk-on narrative: {lead_positive.get('asset') or 'the lead asset'} is drawing the strongest upside "
                f"attention, while macro headlines still flag {macro_topic_label}."
            )
        else:
            summary_line = (
                f"Risk-on narrative: {lead_positive.get('asset') or 'the lead asset'} is drawing the strongest upside "
                "attention."
            )
    elif status == "risk_off":
        if macro_topic_label:
            summary_line = (
                f"Risk-off narrative: {lead_negative.get('asset') or 'the weakest asset'} is under the heaviest "
                f"pressure, with macro headlines leaning toward {macro_topic_label}."
            )
        else:
            summary_line = (
                f"Risk-off narrative: {lead_negative.get('asset') or 'the weakest asset'} is under the heaviest "
                "pressure."
            )
    elif status == "mixed":
        summary_line = "Narrative mixed: crypto attention and macro headlines are pulling in different directions."
    else:
        summary_line = "Narrative balanced: no strong outside-of-price pressure is dominating yet."

    return {
        "status": status,
        "score": composite_score,
        "crypto_score": crypto_score,
        "macro_score": macro_score,
        "lead_positive_asset": str(lead_positive.get("asset") or "") if lead_positive_score > 0.05 else "",
        "lead_positive_score": round(lead_positive_score, 4) if lead_positive_score > 0.05 else None,
        "lead_negative_asset": str(lead_negative.get("asset") or "") if lead_negative_score < -0.05 else "",
        "lead_negative_score": round(lead_negative_score, 4) if lead_negative_score < -0.05 else None,
        "macro_topics": macro_topics,
        "summary_line": summary_line,
    }


def build_sentiment_context(symbol: str, instrument_class: str | None = None) -> dict[str, Any] | None:
    assets = sentiment_assets_for_symbol(symbol, instrument_class=instrument_class)
    asset_summaries: list[dict[str, Any]] = []
    active_topics: list[str] = []
    lead_asset = ""
    lead_score = -1.0

    for asset in assets:
        snapshot = sentiment_overlay.current_sentiment(asset)
        if not isinstance(snapshot, dict):
            continue
        summary = normalize_asset_snapshot(asset, snapshot)
        asset_summaries.append(summary)
        for topic in summary["active_topics"]:
            if topic not in active_topics:
                active_topics.append(topic)
        social_volume_z = _float_value(summary.get("social_volume_z"))
        if social_volume_z is not None and abs(social_volume_z) > lead_score:
            lead_score = abs(social_volume_z)
            lead_asset = str(summary.get("asset") or "")

    if not asset_summaries:
        return None

    return {
        "asset_summaries": asset_summaries,
        "active_topics": active_topics,
        "lead_asset": lead_asset,
        "pressure": summarize_pressure(asset_summaries),
        "assets": assets,
    }

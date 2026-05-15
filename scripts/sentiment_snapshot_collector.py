"""Persist live sentiment snapshots into the JARVIS runtime cache.

This keeps ``brain.jarvis_v3.sentiment_overlay`` warm with real public
non-price inputs so regime/classification layers can read sentiment without
doing network IO on the hot path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.brain.jarvis_v3 import sentiment_overlay  # noqa: E402
from eta_engine.data.market_news import (  # noqa: E402
    NewsHeadline,
    fetch_google_news_headlines,
    headline_volume_proxy,
    query_for_symbol,
)
from eta_engine.features.sentiment import fetch_sentiment_snapshot  # noqa: E402
from eta_engine.scripts import workspace_roots  # noqa: E402

DEFAULT_ASSETS: tuple[str, ...] = ("BTC", "ETH", "SOL", "macro")
MACRO_NEWS_QUERY = "stock market OR Federal Reserve OR CPI OR Treasury yields OR recession"
STATUS_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "sentiment_snapshot_collector_latest.json"

_GREED_TERMS = (
    "rally",
    "surge",
    "record high",
    "all-time high",
    "breakout",
    "boom",
    "soar",
    "gain",
)
_FEAR_TERMS = (
    "selloff",
    "slump",
    "recession",
    "inflation",
    "tariff",
    "war",
    "attack",
    "plunge",
    "liquidation",
    "panic",
    "crash",
    "lawsuit",
)
_CRYPTO_TOPIC_RULES = {
    "fomo": ("all-time high", "record high", "moon", "breakout", "surge"),
    "capitulation": ("capitulation", "liquidation", "panic", "selloff", "crash"),
    "regulation": ("sec", "etf", "lawsuit", "regulation", "congress"),
    "hack": ("hack", "exploit", "breach", "stolen"),
    "squeeze": ("short squeeze", "liquidation squeeze", "squeeze"),
}
_MACRO_TOPIC_RULES = {
    "fomc": ("fomc", "federal reserve", "fed", "powell"),
    "inflation": ("cpi", "ppi", "inflation"),
    "jobs": ("jobs report", "payrolls", "unemployment", "jobless claims"),
    "tariffs": ("tariff", "trade war"),
    "earnings_blowup": ("profit warning", "guidance cut", "earnings miss"),
    "geopolitics": ("war", "attack", "sanction", "missile"),
}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _headline_text(rows: list[NewsHeadline]) -> str:
    return " ".join(f"{row.headline} {row.snippet}".lower() for row in rows)


def _topic_flags(rows: list[NewsHeadline], *, macro: bool) -> dict[str, bool]:
    text = _headline_text(rows)
    rules = _MACRO_TOPIC_RULES if macro else _CRYPTO_TOPIC_RULES
    return {
        key: any(term in text for term in terms)
        for key, terms in rules.items()
    }


def _fear_greed_from_headlines(rows: list[NewsHeadline]) -> float:
    text = _headline_text(rows)
    greed_hits = sum(text.count(term) for term in _GREED_TERMS)
    fear_hits = sum(text.count(term) for term in _FEAR_TERMS)
    total = greed_hits + fear_hits
    if total <= 0:
        return 0.5
    signed = (greed_hits - fear_hits) / float(total)
    return round(_clamp01(0.5 + 0.35 * signed), 4)


def _headline_summary(rows: list[NewsHeadline], *, limit: int = 5) -> list[dict[str, Any]]:
    return [
        {
            "headline": row.headline,
            "publisher": row.publisher,
            "published_at_utc": row.published_at_utc.isoformat(),
            "url": row.url,
        }
        for row in rows[: max(limit, 0)]
    ]


def _social_volume_z(current: int, baseline: int) -> float:
    baseline = max(int(baseline), 1)
    return round((float(current) - float(baseline)) / float(baseline), 4)


async def _crypto_overlay(asset: str, *, now: datetime) -> dict[str, Any]:
    live = await fetch_sentiment_snapshot(asset)
    query = query_for_symbol(asset) or asset
    headlines = fetch_google_news_headlines(query, limit=8, max_age_hours=48.0, now=now)
    current = int(live.get("social_volume", 0) or 0)
    baseline = int(live.get("social_volume_baseline", 1) or 1)
    return {
        "fear_greed": round(_clamp01(int(live.get("fear_greed", 50) or 50) / 100.0), 4),
        "social_volume_z": _social_volume_z(current, baseline),
        "topic_flags": _topic_flags(headlines, macro=False),
        "raw_source": str(live.get("source") or "unknown"),
        "extras": {
            "query": query,
            "social_volume": current,
            "social_volume_baseline": baseline,
            "headline_count": len(headlines),
            "headlines": _headline_summary(headlines),
            "social_volume_raw": live.get("social_volume_raw"),
        },
    }


def _macro_overlay(*, now: datetime) -> dict[str, Any]:
    headlines = fetch_google_news_headlines(MACRO_NEWS_QUERY, limit=10, max_age_hours=48.0, now=now)
    proxy = headline_volume_proxy(MACRO_NEWS_QUERY, window_h=24, now=now)
    current = int(proxy.get("social_volume", 0) or 0)
    baseline = int(proxy.get("social_volume_baseline", 1) or 1)
    return {
        "fear_greed": _fear_greed_from_headlines(headlines),
        "social_volume_z": _social_volume_z(current, baseline),
        "topic_flags": _topic_flags(headlines, macro=True),
        "raw_source": "google_news_proxy",
        "extras": {
            "query": MACRO_NEWS_QUERY,
            "social_volume": current,
            "social_volume_baseline": baseline,
            "headline_count": len(headlines),
            "headlines": _headline_summary(headlines),
        },
    }


async def _build_snapshots(assets: list[str], *, now: datetime) -> dict[str, dict[str, Any]]:
    crypto_assets = [asset.upper() for asset in assets if asset.lower() != "macro"]
    built: dict[str, dict[str, Any]] = {}
    if crypto_assets:
        overlays = await asyncio.gather(*(_crypto_overlay(asset, now=now) for asset in crypto_assets))
        built.update(dict(zip(crypto_assets, overlays, strict=False)))
    if any(asset.lower() == "macro" for asset in assets):
        built["macro"] = _macro_overlay(now=now)
    return built


def persist_runtime_sentiment_snapshots(
    *,
    assets: list[str] | tuple[str, ...] = DEFAULT_ASSETS,
    cache_dir: Path | None = None,
    now: datetime | None = None,
    status_path: Path = STATUS_PATH,
) -> dict[str, Any]:
    started = (now or datetime.now(tz=UTC)).astimezone(UTC)
    requested = [str(asset).strip() for asset in assets if str(asset).strip()]
    built = asyncio.run(_build_snapshots(requested, now=started))
    results: dict[str, dict[str, Any]] = {}
    ok_count = 0
    for asset, snapshot in built.items():
        ok = sentiment_overlay.write_sentiment_snapshot(asset, snapshot, cache_dir=cache_dir)
        results[asset] = {
            "ok": ok,
            "fear_greed": snapshot.get("fear_greed"),
            "social_volume_z": snapshot.get("social_volume_z"),
            "raw_source": snapshot.get("raw_source"),
            "topic_flags": snapshot.get("topic_flags"),
        }
        if ok:
            ok_count += 1

    payload = {
        "kind": "eta_sentiment_snapshot_collector",
        "status": "ok" if ok_count == len(requested) else "partial",
        "started_at_utc": started.isoformat(),
        "finished_at_utc": datetime.now(tz=UTC).isoformat(),
        "requested_assets": requested,
        "ok_count": ok_count,
        "cache_dir": str(cache_dir or sentiment_overlay.DEFAULT_CACHE_DIR),
        "results": results,
    }
    workspace_roots.ensure_parent(status_path)
    status_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentiment_snapshot_collector")
    parser.add_argument("--json", action="store_true", help="print JSON status")
    parser.add_argument("--asset", action="append", dest="assets", help="asset to refresh, repeatable")
    args = parser.parse_args(argv)
    payload = persist_runtime_sentiment_snapshots(assets=args.assets or list(DEFAULT_ASSETS))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"sentiment snapshot collector {payload['status']} "
            f"ok={payload['ok_count']}/{len(payload['requested_assets'])}"
        )
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

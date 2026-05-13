"""Sentiment confluence scorer (Tier-1 #4, 2026-04-27).

Wraps the LunarCrush + BigData MCP feeds into a unified sentiment
score that ETA bots can consume as a confluence input. Returns a
single ``SentimentSnapshot`` with bounded scores in ``[-1.0, +1.0]``.

External MCP calls are made by the operator's agent layer because MCP
tool invocations belong outside the deterministic bot loop. Bots read
the normalized JSON state file the agent layer drops at
``state/sentiment/<symbol>.json`` every N minutes.

Bot integration::

    from eta_engine.brain.jarvis_v3.sentiment_score import current_snapshot

    snap = current_snapshot("MNQ")
    if snap and abs(snap.composite) >= 0.40:
        # Material sentiment -- modulate confluence
        if snap.composite > 0 and signal.direction == "long":
            confidence_bonus = +0.5
        elif snap.composite < 0 and signal.direction == "long":
            confidence_penalty = -0.5
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
SENTIMENT_DIR = ROOT / "state" / "sentiment"


@dataclass(frozen=True)
class SentimentSnapshot:
    """A normalized sentiment reading for one symbol."""

    symbol: str
    ts: datetime
    composite: float  # weighted -1..+1 (negative bearish)
    news_score: float  # -1..+1 (BigData / news-NLP)
    social_score: float  # -1..+1 (LunarCrush / X)
    volume_z: float  # social mention z-score (3+ = surge)
    n_news_articles: int
    n_social_mentions: int
    is_stale: bool = False


def current_snapshot(symbol: str, *, max_age_min: float = 30.0) -> SentimentSnapshot | None:
    """Load the most recent sentiment snapshot for symbol from
    ``state/sentiment/<symbol>.json``.

    Returns None if no file or stale beyond ``max_age_min``.
    """
    path = SENTIMENT_DIR / f"{symbol.upper()}.json"
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

    return SentimentSnapshot(
        symbol=str(data.get("symbol", symbol)).upper(),
        ts=ts,
        composite=float(data.get("composite", 0.0)),
        news_score=float(data.get("news_score", 0.0)),
        social_score=float(data.get("social_score", 0.0)),
        volume_z=float(data.get("volume_z", 0.0)),
        n_news_articles=int(data.get("n_news_articles", 0)),
        n_social_mentions=int(data.get("n_social_mentions", 0)),
        is_stale=is_stale,
    )


def write_snapshot(snap: SentimentSnapshot) -> Path:
    """Persist a snapshot. Used by the agent-layer feed worker that
    queries LunarCrush + BigData every N minutes."""
    SENTIMENT_DIR.mkdir(parents=True, exist_ok=True)
    path = SENTIMENT_DIR / f"{snap.symbol}.json"
    path.write_text(
        json.dumps(
            {
                "symbol": snap.symbol,
                "ts": snap.ts.isoformat(),
                "composite": snap.composite,
                "news_score": snap.news_score,
                "social_score": snap.social_score,
                "volume_z": snap.volume_z,
                "n_news_articles": snap.n_news_articles,
                "n_social_mentions": snap.n_social_mentions,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def confluence_modifier(
    snap: SentimentSnapshot | None,
    *,
    direction: str,
    weight: float = 1.0,
) -> float:
    """Return a confluence-score bonus/penalty in [-weight, +weight].

    Positive when sentiment ALIGNS with direction; negative when it
    OPPOSES. Returns 0.0 when no snapshot or stale (no signal).
    """
    if snap is None or snap.is_stale:
        return 0.0
    if abs(snap.composite) < 0.20:
        return 0.0
    sign = 1.0 if (snap.composite > 0) == (direction.lower() in ("long", "buy", "bull")) else -1.0
    return round(sign * abs(snap.composite) * weight, 4)

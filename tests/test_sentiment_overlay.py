"""Tests for sentiment_overlay — T16 external-signal cache."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    """A snapshot written via write_sentiment_snapshot is readable via current_sentiment."""
    from eta_engine.brain.jarvis_v3 import sentiment_overlay

    snap = {
        "fear_greed": 0.32,
        "social_volume_z": 1.8,
        "topic_flags": {"squeeze": True, "capitulation": False},
        "raw_source": "lunarcrush",
        "extras": {"galaxy_score": 64.2},
    }
    ok = sentiment_overlay.write_sentiment_snapshot("BTC", snap, cache_dir=tmp_path)
    assert ok

    out = sentiment_overlay.current_sentiment("BTC", cache_dir=tmp_path)
    assert out is not None
    assert out["fear_greed"] == 0.32
    assert out["topic_flags"]["squeeze"] is True
    # asof was auto-injected
    assert "asof" in out


def test_current_sentiment_returns_none_for_missing(tmp_path: Path) -> None:
    """No cache file for that asset → None, no exception."""
    from eta_engine.brain.jarvis_v3 import sentiment_overlay

    assert sentiment_overlay.current_sentiment("BTC", cache_dir=tmp_path) is None


def test_current_sentiment_returns_none_for_stale_cache(tmp_path: Path) -> None:
    """A snapshot older than STALE_AFTER_MIN minutes is filtered out."""
    from eta_engine.brain.jarvis_v3 import sentiment_overlay

    # Write a snapshot with a 2-hour-old timestamp
    old_asof = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    snap = {"fear_greed": 0.5, "asof": old_asof}
    sentiment_overlay.write_sentiment_snapshot("BTC", snap, cache_dir=tmp_path)

    # Should be filtered as stale (default STALE_AFTER_MIN = 60)
    assert sentiment_overlay.current_sentiment("BTC", cache_dir=tmp_path) is None


def test_current_sentiment_case_insensitive_asset_lookup(tmp_path: Path) -> None:
    """'btc' should resolve to the BTC cache file."""
    from eta_engine.brain.jarvis_v3 import sentiment_overlay

    sentiment_overlay.write_sentiment_snapshot(
        "BTC", {"fear_greed": 0.7}, cache_dir=tmp_path,
    )
    out_lower = sentiment_overlay.current_sentiment("btc", cache_dir=tmp_path)
    out_upper = sentiment_overlay.current_sentiment("BTC", cache_dir=tmp_path)
    assert out_lower is not None
    assert out_upper is not None
    assert out_lower["fear_greed"] == out_upper["fear_greed"]


def test_history_appends_each_write(tmp_path: Path) -> None:
    """Each write appends to the history JSONL alongside the active snapshot."""
    from eta_engine.brain.jarvis_v3 import sentiment_overlay

    for i in range(5):
        sentiment_overlay.write_sentiment_snapshot(
            "BTC", {"fear_greed": 0.5 + i * 0.05}, cache_dir=tmp_path,
        )
    history = sentiment_overlay.sentiment_history("BTC", n=10, cache_dir=tmp_path)
    assert len(history) == 5
    # Chronological order
    values = [h["fear_greed"] for h in history]
    assert values == sorted(values)


def test_history_returns_empty_when_missing(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import sentiment_overlay

    assert sentiment_overlay.sentiment_history("BTC", n=10, cache_dir=tmp_path) == []


def test_history_respects_limit(tmp_path: Path) -> None:
    """n=2 returns only the 2 most recent snapshots."""
    from eta_engine.brain.jarvis_v3 import sentiment_overlay

    for i in range(10):
        sentiment_overlay.write_sentiment_snapshot(
            "BTC", {"fear_greed": 0.1 * i}, cache_dir=tmp_path,
        )
    history = sentiment_overlay.sentiment_history("BTC", n=2, cache_dir=tmp_path)
    assert len(history) == 2


def test_write_handles_unknown_asset_gracefully(tmp_path: Path) -> None:
    """An asset not in the predefined mapping uses a lowercase filename fallback."""
    from eta_engine.brain.jarvis_v3 import sentiment_overlay

    ok = sentiment_overlay.write_sentiment_snapshot(
        "DOGE", {"fear_greed": 0.6}, cache_dir=tmp_path,
    )
    assert ok
    # File written using lowercase fallback
    assert (tmp_path / "doge.json").exists()
    out = sentiment_overlay.current_sentiment("DOGE", cache_dir=tmp_path)
    # Reading the same asset name should find it via the fallback path on write
    # (the read path doesn't have the fallback yet — that's by design, only
    # KNOWN assets are readable). DOGE here returns None until we add it to
    # the registry. Document that this is intentional.
    assert out is None


def test_write_rejects_bad_input() -> None:
    """Non-dict snapshot or empty asset_class → False, no exception."""
    from eta_engine.brain.jarvis_v3 import sentiment_overlay

    assert sentiment_overlay.write_sentiment_snapshot("", {"x": 1}) is False
    assert sentiment_overlay.write_sentiment_snapshot("BTC", "not a dict") is False  # type: ignore[arg-type]


def test_corrupt_cache_returns_none(tmp_path: Path) -> None:
    """Garbage in the cache file → None, no exception."""
    from eta_engine.brain.jarvis_v3 import sentiment_overlay

    (tmp_path / "lunarcrush_btc.json").write_text("not json {{{", encoding="utf-8")
    assert sentiment_overlay.current_sentiment("BTC", cache_dir=tmp_path) is None


def test_atomic_write_no_partial_state_on_failure(tmp_path: Path, monkeypatch) -> None:
    """If the write fails midway, no partial file is left behind."""
    from eta_engine.brain.jarvis_v3 import sentiment_overlay

    def kaboom(*a, **kw):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", kaboom)
    ok = sentiment_overlay.write_sentiment_snapshot(
        "BTC", {"fear_greed": 0.5}, cache_dir=tmp_path,
    )
    assert ok is False
    # The temp file should not be left behind in tmp_path
    leftovers = list(tmp_path.glob(".tmp_sentiment_*"))
    assert leftovers == []

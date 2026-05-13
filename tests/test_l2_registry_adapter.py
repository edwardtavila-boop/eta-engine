"""Tests for l2_registry_adapter — bridges L2 promotion decisions
into the existing supercharge verdict_cache.json."""

# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.strategies import l2_registry_adapter as adapter


def test_verdict_mapping_shadow_to_paper_is_yellow() -> None:
    verdict, reason = adapter._verdict_for("shadow", "paper")
    assert verdict == "YELLOW"
    assert "paper" in reason.lower()


def test_verdict_mapping_paper_to_live_is_green() -> None:
    verdict, _ = adapter._verdict_for("paper", "live")
    assert verdict == "GREEN"


def test_verdict_mapping_live_steady_is_green() -> None:
    verdict, _ = adapter._verdict_for("live", "live")
    assert verdict == "GREEN"


def test_verdict_mapping_retired_is_red() -> None:
    verdict, reason = adapter._verdict_for("shadow", "retired")
    assert verdict == "RED"
    assert "falsification" in reason.lower()


def test_verdict_mapping_shadow_steady_is_yellow() -> None:
    verdict, _ = adapter._verdict_for("shadow", "shadow")
    assert verdict == "YELLOW"


def test_read_latest_promotion_picks_most_recent_per_bot(tmp_path: Path) -> None:
    path = tmp_path / "promotion.jsonl"
    base = datetime.now(UTC)
    records = [
        {
            "ts": (base.isoformat()),
            "bot_id": "mnq_book_imbalance_shadow",
            "current_status": "shadow",
            "recommended_status": "shadow",
        },
        # Later record should win
        {
            "ts": (base.isoformat()),
            "bot_id": "mnq_book_imbalance_shadow",
            "current_status": "shadow",
            "recommended_status": "paper",
        },
        {
            "ts": (base.isoformat()),
            "bot_id": "mnq_footprint_absorption_shadow",
            "current_status": "shadow",
            "recommended_status": "retired",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    latest = adapter._read_latest_promotion_per_bot(_path=path)
    assert latest["mnq_book_imbalance_shadow"]["recommended_status"] == "paper"
    assert latest["mnq_footprint_absorption_shadow"]["recommended_status"] == "retired"


def test_sync_writes_verdict_cache(tmp_path: Path) -> None:
    promotion_path = tmp_path / "promotion.jsonl"
    cache_path = tmp_path / "cache.json"
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "bot_id": "mnq_book_imbalance_shadow",
        "current_status": "shadow",
        "recommended_status": "paper",
        "notes": ["All shadow→paper criteria met"],
    }
    promotion_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    summary = adapter.sync_l2_to_verdict_cache(_promotion_path=promotion_path, _cache_path=cache_path)
    assert summary["n_synced"] == 1
    assert "mnq_book_imbalance_shadow" in summary["bot_ids"]
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    entry = cache["mnq_book_imbalance_shadow"]
    assert entry["verdict"] == "YELLOW"  # shadow → paper
    assert entry["extras"]["source"] == "l2_registry_adapter"


def test_sync_merges_with_existing_cache(tmp_path: Path) -> None:
    """Existing legacy bots in cache should not be wiped."""
    promotion_path = tmp_path / "promotion.jsonl"
    cache_path = tmp_path / "cache.json"
    existing = {"legacy_bot_1": {"verdict": "GREEN", "ts": "2026-05-10T00:00:00+00:00"}}
    cache_path.write_text(json.dumps(existing), encoding="utf-8")
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "bot_id": "mnq_book_imbalance_shadow",
        "current_status": "shadow",
        "recommended_status": "shadow",
    }
    promotion_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    adapter.sync_l2_to_verdict_cache(_promotion_path=promotion_path, _cache_path=cache_path)
    merged = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "legacy_bot_1" in merged
    assert "mnq_book_imbalance_shadow" in merged


def test_sync_no_promotion_log_returns_zero(tmp_path: Path) -> None:
    summary = adapter.sync_l2_to_verdict_cache(
        _promotion_path=tmp_path / "nonexistent.jsonl", _cache_path=tmp_path / "cache.json"
    )
    assert summary["n_synced"] == 0

from datetime import UTC, datetime

from eta_engine.brain.jarvis_v3 import sentiment_overlay
from eta_engine.scripts import seed_sentiment_cache


def test_seed_sentiment_cache_writes_btc_and_eth(tmp_path):
    asof = datetime.now(UTC)

    payload = seed_sentiment_cache.seed_sentiment_cache(cache_dir=tmp_path, asof=asof)

    assert payload["cache_dir"] == str(tmp_path)
    assert payload["written"] == {"BTC": True, "ETH": True}

    btc = sentiment_overlay.current_sentiment("BTC", cache_dir=tmp_path)
    eth = sentiment_overlay.current_sentiment("ETH", cache_dir=tmp_path)
    assert btc is not None
    assert eth is not None
    assert btc["raw_source"] == "lunarcrush_seeded"
    assert eth["raw_source"] == "lunarcrush_seeded"
    assert btc["extras"]["synthetic"] is True
    assert eth["extras"]["seeded_reason"] == seed_sentiment_cache.DEFAULT_SEEDED_REASON
    assert btc["fear_greed"] == 0.58
    assert eth["social_volume_z"] == -0.08


def test_build_seed_snapshot_rejects_unknown_asset():
    try:
        seed_sentiment_cache.build_seed_snapshot("SOL")
    except ValueError as exc:
        assert "unsupported seeded asset" in str(exc)
    else:
        raise AssertionError("expected ValueError for unsupported seeded asset")

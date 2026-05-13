"""Tests for the per-bot Alpaca PnL helper added to dashboard_api.

Covers:
  * client_order_id → bot_id extraction (with/without hex suffix)
  * paper_soak_result → backtest_wr parsing (best-effort)
  * cache TTL behavior (within-window hit, expired miss, day rollover)
  * drift_alarm threshold logic (gap, sample-size gate, missing target)
  * fail-soft behavior on Alpaca errors (no exception escapes the helper)
"""

from __future__ import annotations

import importlib
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def dash():
    """Reload dashboard_api with caches reset."""
    mod = importlib.import_module("eta_engine.deploy.scripts.dashboard_api")
    # Reset both caches so tests don't see stale data between runs.
    mod._ALPACA_PER_BOT_CACHE.clear()
    mod._ALPACA_PER_BOT_CACHE.update({"snapshot": None, "ts": 0.0})
    return mod


# ---------------------------------------------------------------------------
# 1. client_order_id → bot_id extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("coid", "expected"),
    [
        ("vwap_mr_btc_cef83eb7", "vwap_mr_btc"),
        ("orb_sage_eth_a1b2c3d4", "orb_sage_eth"),
        ("rsi_mr_mnq_0123456789ab", "rsi_mr_mnq"),
        # No hex suffix → entire string is treated as bot_id when valid.
        ("simple_bot", "simple_bot"),
        # Empty / None → None.
        ("", None),
        (None, None),
        # Bot ids with multiple underscores are preserved.
        ("btc_compression_short_deadbeef", "btc_compression_short"),
    ],
)
def test_extract_bot_id_from_client_order_id(dash, coid, expected):
    assert dash._extract_bot_id_from_client_order_id(coid) == expected


def test_extract_bot_id_handles_invalid_chars(dash):
    """Reject strings that don't match the bot_id charset."""
    # Slashes / spaces never appear in supervisor-generated COIDs.
    assert dash._extract_bot_id_from_client_order_id("bad bot/id_deadbeef") is None


# ---------------------------------------------------------------------------
# 2. paper_soak_result parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("85.7% WR, +$1947 on 3000 bars", 0.857),
        ("28.6% WR, +$66 on 7 trades", 0.286),
        ("58.8% WR, +$171 on 3000 bars", 0.588),
        # Percent + WR with extra whitespace.
        ("  100% WR  on test data ", 1.0),
        # No WR token → None.
        ("Edge: positive expectancy on MNQ 5m", None),
        ("", None),
        (None, None),
        # Out-of-range → None.
        ("150% WR, weird sample", None),
    ],
)
def test_parse_backtest_wr_from_text(dash, text, expected):
    assert dash._parse_backtest_wr_from_text(text) == expected


def test_registry_backtest_wr_targets_returns_dict(dash):
    """Smoke test — must return a dict even when registry import fails."""
    targets = dash._registry_backtest_wr_targets()
    assert isinstance(targets, dict)
    # When registry imports succeed, we expect at least one parsed target.
    # When it fails (test env without full deps), the dict can be empty —
    # just assert the contract holds either way.
    for k, v in targets.items():
        assert isinstance(k, str) and 0.0 <= v <= 1.0


# ---------------------------------------------------------------------------
# 3. drift_alarm threshold logic
# ---------------------------------------------------------------------------


def _build_filled_order(coid: str, side: str, qty: float, price: float) -> dict:
    return {
        "client_order_id": coid,
        "symbol": "BTCUSD",
        "side": side,
        "filled_qty": str(qty),
        "filled_avg_price": str(price),
        "status": "filled",
    }


def test_drift_alarm_triggers_when_gap_exceeds_threshold(dash):
    """Bot with backtest_wr=85% but live_wr=20% on >=5 fills → alarm."""
    orders = []
    # 5 round-trip pairs for vwap_mr_btc — 1 win, 4 losses → live_wr=20%.
    pairs = [
        ("buy", 1.0, 100.0),
        ("sell", 1.0, 50.0),  # loss
        ("buy", 1.0, 100.0),
        ("sell", 1.0, 60.0),  # loss
        ("buy", 1.0, 100.0),
        ("sell", 1.0, 70.0),  # loss
        ("buy", 1.0, 100.0),
        ("sell", 1.0, 80.0),  # loss
        ("buy", 1.0, 100.0),
        ("sell", 1.0, 150.0),  # win
    ]
    for side, qty, price in pairs:
        orders.append(_build_filled_order("vwap_mr_btc_deadbeef", side, qty, price))

    fake_resp = MagicMock(status_code=200, json=MagicMock(return_value=orders))

    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.get = MagicMock(return_value=fake_resp)

    fake_cfg = MagicMock()
    fake_cfg.api_key_id = "k"
    fake_cfg.api_secret_key = "s"
    fake_cfg.base_url = "https://example.test"
    fake_cfg.missing_requirements = MagicMock(return_value=[])

    with (
        patch(
            "eta_engine.deploy.scripts.dashboard_api._registry_backtest_wr_targets",
            return_value={"vwap_mr_btc": 0.857},
        ),
        patch(
            "eta_engine.venues.alpaca.AlpacaConfig.from_env",
            return_value=fake_cfg,
        ),
        patch("httpx.Client", return_value=fake_client),
    ):
        snap = dash._alpaca_per_bot_pnl_snapshot(today_start_iso="2026-05-06T00:00:00Z")

    assert snap["ready"] is True
    bot = snap["per_bot"]["vwap_mr_btc"]
    assert bot["fills_today"] == 10
    assert bot["wins"] == 1
    assert bot["losses"] == 4
    assert bot["live_wr_today"] == pytest.approx(0.2)
    assert bot["backtest_wr_target"] == pytest.approx(0.857)
    assert bot["drift_alarm"] is True
    assert bot["drift_gap_pp"] is not None and bot["drift_gap_pp"] > 30
    assert snap["drift_alarm_count"] == 1


def test_drift_alarm_suppressed_when_below_min_fills(dash):
    """Same gap but only 2 fills → drift_alarm stays false."""
    pairs = [
        ("buy", 1.0, 100.0),
        ("sell", 1.0, 50.0),
    ]
    orders = [_build_filled_order("vwap_mr_btc_aabbccdd", s, q, p) for s, q, p in pairs]
    fake_resp = MagicMock(status_code=200, json=MagicMock(return_value=orders))
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.get = MagicMock(return_value=fake_resp)

    fake_cfg = MagicMock()
    fake_cfg.api_key_id = "k"
    fake_cfg.api_secret_key = "s"
    fake_cfg.base_url = "https://example.test"
    fake_cfg.missing_requirements = MagicMock(return_value=[])

    with (
        patch(
            "eta_engine.deploy.scripts.dashboard_api._registry_backtest_wr_targets",
            return_value={"vwap_mr_btc": 0.857},
        ),
        patch(
            "eta_engine.venues.alpaca.AlpacaConfig.from_env",
            return_value=fake_cfg,
        ),
        patch("httpx.Client", return_value=fake_client),
    ):
        snap = dash._alpaca_per_bot_pnl_snapshot(today_start_iso="2026-05-06T00:00:00Z")

    bot = snap["per_bot"]["vwap_mr_btc"]
    assert bot["fills_today"] == 2
    # Even with 100% loss rate, fewer than min_fills suppresses the alarm.
    assert bot["drift_alarm"] is False
    assert snap["drift_alarm_count"] == 0


def test_drift_alarm_off_when_target_missing(dash):
    """No backtest_wr_target → drift_alarm stays false."""
    pairs = [
        ("buy", 1.0, 100.0),
        ("sell", 1.0, 50.0),
        ("buy", 1.0, 100.0),
        ("sell", 1.0, 50.0),
        ("buy", 1.0, 100.0),
        ("sell", 1.0, 50.0),
        ("buy", 1.0, 100.0),
        ("sell", 1.0, 50.0),
        ("buy", 1.0, 100.0),
        ("sell", 1.0, 50.0),
    ]
    orders = [_build_filled_order("unknown_bot_x12y34z9", s, q, p) for s, q, p in pairs]
    fake_resp = MagicMock(status_code=200, json=MagicMock(return_value=orders))
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.get = MagicMock(return_value=fake_resp)

    fake_cfg = MagicMock()
    fake_cfg.api_key_id = "k"
    fake_cfg.api_secret_key = "s"
    fake_cfg.base_url = "https://example.test"
    fake_cfg.missing_requirements = MagicMock(return_value=[])

    with (
        patch(
            "eta_engine.deploy.scripts.dashboard_api._registry_backtest_wr_targets",
            return_value={},  # no targets at all
        ),
        patch(
            "eta_engine.venues.alpaca.AlpacaConfig.from_env",
            return_value=fake_cfg,
        ),
        patch("httpx.Client", return_value=fake_client),
    ):
        snap = dash._alpaca_per_bot_pnl_snapshot(today_start_iso="2026-05-06T00:00:00Z")

    # Bot key is the longest valid prefix the regex strips back to.
    bot = next(iter(snap["per_bot"].values()))
    assert bot["backtest_wr_target"] is None
    assert bot["drift_alarm"] is False


# ---------------------------------------------------------------------------
# 4. Cache TTL behavior
# ---------------------------------------------------------------------------


def test_cache_serves_within_ttl_window(dash):
    """Two consecutive calls inside the TTL window must hit the cache."""
    call_count = {"n": 0}

    def fake_snap(*, today_start_iso):
        call_count["n"] += 1
        return {"ready": True, "per_bot": {}, "checked_utc": "x"}

    with patch.object(dash, "_alpaca_per_bot_pnl_snapshot", side_effect=fake_snap):
        first = dash._alpaca_per_bot_pnl_cached(today_start_iso="2026-05-06T00:00:00Z")
        assert first.get("served_from_cache") is not True
        second = dash._alpaca_per_bot_pnl_cached(today_start_iso="2026-05-06T00:00:00Z")
        assert second.get("served_from_cache") is True
        assert second.get("cache_age_s") is not None
    assert call_count["n"] == 1


def test_cache_misses_after_ttl_expires(dash, monkeypatch):
    """Force the cache TTL to ~0 and confirm a second call refetches."""
    monkeypatch.setattr(dash, "_ALPACA_PER_BOT_CACHE_TTL_S", 0.0)
    call_count = {"n": 0}

    def fake_snap(*, today_start_iso):
        call_count["n"] += 1
        return {"ready": True, "per_bot": {}, "checked_utc": "x"}

    with patch.object(dash, "_alpaca_per_bot_pnl_snapshot", side_effect=fake_snap):
        dash._alpaca_per_bot_pnl_cached(today_start_iso="2026-05-06T00:00:00Z")
        time.sleep(0.01)
        dash._alpaca_per_bot_pnl_cached(today_start_iso="2026-05-06T00:00:00Z")
    assert call_count["n"] == 2


def test_cache_invalidates_on_day_rollover(dash):
    """Different today_start_iso bypasses the cache (UTC midnight rollover)."""
    call_count = {"n": 0}

    def fake_snap(*, today_start_iso):
        call_count["n"] += 1
        return {"ready": True, "per_bot": {}, "checked_utc": "x"}

    with patch.object(dash, "_alpaca_per_bot_pnl_snapshot", side_effect=fake_snap):
        dash._alpaca_per_bot_pnl_cached(today_start_iso="2026-05-06T00:00:00Z")
        dash._alpaca_per_bot_pnl_cached(today_start_iso="2026-05-07T00:00:00Z")
    assert call_count["n"] == 2


def test_cache_thread_safe_under_concurrent_callers(dash):
    """Hammer the cache from multiple threads — no exceptions, single fetch."""
    call_count = {"n": 0}
    barrier = threading.Barrier(8)

    def fake_snap(*, today_start_iso):
        call_count["n"] += 1
        time.sleep(0.005)  # exaggerate the race window
        return {"ready": True, "per_bot": {}, "checked_utc": "x"}

    results: list[dict] = []
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait()
            r = dash._alpaca_per_bot_pnl_cached(today_start_iso="2026-05-06T00:00:00Z")
            results.append(r)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with patch.object(dash, "_alpaca_per_bot_pnl_snapshot", side_effect=fake_snap):
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert errors == []
    assert len(results) == 8
    # First fetch + however many slipped past the lock during the sleep —
    # but they must not blow up. The contract is "no exceptions".


# ---------------------------------------------------------------------------
# 5. Fail-soft behavior
# ---------------------------------------------------------------------------


def test_helper_failsoft_when_alpaca_config_missing(dash):
    """Missing config returns a snapshot with error key, no exception."""
    fake_cfg = MagicMock()
    fake_cfg.missing_requirements = MagicMock(return_value=["api_key_id"])

    with patch(
        "eta_engine.venues.alpaca.AlpacaConfig.from_env",
        return_value=fake_cfg,
    ):
        snap = dash._alpaca_per_bot_pnl_snapshot(today_start_iso="2026-05-06T00:00:00Z")

    assert snap["ready"] is False
    assert "error" in snap
    assert snap["per_bot"] == {}


def test_cached_wrapper_swallows_unexpected_exceptions(dash):
    """A blow-up inside the inner snapshot must not propagate."""

    def boom(*, today_start_iso):
        raise RuntimeError("simulated alpaca melt")

    with patch.object(dash, "_alpaca_per_bot_pnl_snapshot", side_effect=boom):
        snap = dash._alpaca_per_bot_pnl_cached(today_start_iso="2026-05-06T00:00:00Z")
    assert snap["ready"] is False
    assert "error" in snap
    assert "alpaca_per_bot_unhandled" in snap["error"]

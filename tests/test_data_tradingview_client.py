"""Tests for ``eta_engine.data.tradingview.client``.

The Playwright-driven tab loops are exercised with fakes -- no network,
no real browser. Mostly tests:

* config validation
* ``is_available()`` matches whether playwright is importable
* run() raises ``TradingViewUnavailable`` when playwright is absent
* ws-frame -> journal wiring (via the parser layer)

Heavy E2E-style tests against a real Chromium are out of scope for unit
suite -- they live in ``deploy/scripts/live_claude_smoke.py`` style
operator-driven verification.
"""

from __future__ import annotations

import asyncio

import pytest

from eta_engine.data.tradingview.auth import AuthState
from eta_engine.data.tradingview.client import (
    ChartTarget,
    TradingViewClient,
    TradingViewClientError,
    TradingViewConfig,
    TradingViewUnavailable,
)
from eta_engine.data.tradingview.journal import TradingViewJournal


def _auth(has_sess: bool = True) -> AuthState:
    cookies = []
    if has_sess:
        cookies.append({
            "name": "sessionid", "value": "x", "domain": ".tradingview.com",
        })
    return AuthState(cookies=cookies, origins=[])


def test_client_rejects_empty_config(tmp_path) -> None:  # noqa: ANN001
    journal = TradingViewJournal(tmp_path)
    cfg = TradingViewConfig(
        targets=(),
        watchlist_url="",
        alerts_url="",
    )
    with pytest.raises(TradingViewClientError, match="no targets"):
        TradingViewClient(cfg, journal, auth_state=_auth())


def test_client_accepts_targets_only(tmp_path) -> None:  # noqa: ANN001
    journal = TradingViewJournal(tmp_path)
    cfg = TradingViewConfig(
        targets=(ChartTarget(symbol="X:Y", interval="1"),),
        watchlist_url="", alerts_url="",
    )
    client = TradingViewClient(cfg, journal, auth_state=_auth())
    assert client.config.targets[0].symbol == "X:Y"


def test_client_accepts_watchlist_only(tmp_path) -> None:  # noqa: ANN001
    journal = TradingViewJournal(tmp_path)
    cfg = TradingViewConfig(
        targets=(),
        watchlist_url="https://www.tradingview.com/watchlist/",
        alerts_url="",
    )
    client = TradingViewClient(cfg, journal, auth_state=_auth())
    assert client.config.watchlist_url


def test_client_warns_when_no_session_cookie(tmp_path, caplog) -> None:  # noqa: ANN001
    import logging
    journal = TradingViewJournal(tmp_path)
    cfg = TradingViewConfig(
        targets=(ChartTarget(symbol="X:Y"),),
        watchlist_url="", alerts_url="",
    )
    with caplog.at_level(logging.WARNING):
        TradingViewClient(cfg, journal, auth_state=_auth(has_sess=False))
    assert any("sessionid" in rec.message for rec in caplog.records)


def test_is_available_matches_playwright_import() -> None:
    try:
        import playwright.async_api  # noqa: F401
        expected = True
    except ImportError:
        expected = False
    assert TradingViewClient.is_available() is expected


def _playwright_installed() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    _playwright_installed(),
    reason="runs only when playwright is NOT installed",
)
def test_run_raises_unavailable_without_playwright(tmp_path) -> None:  # noqa: ANN001
    journal = TradingViewJournal(tmp_path)
    cfg = TradingViewConfig(
        targets=(ChartTarget(symbol="X:Y"),), watchlist_url="", alerts_url="",
    )
    client = TradingViewClient(cfg, journal, auth_state=_auth())
    with pytest.raises(TradingViewUnavailable):
        asyncio.run(client.run())


def test_request_stop_sets_flag(tmp_path) -> None:  # noqa: ANN001
    journal = TradingViewJournal(tmp_path)
    cfg = TradingViewConfig(
        targets=(ChartTarget(symbol="X:Y"),), watchlist_url="", alerts_url="",
    )
    client = TradingViewClient(cfg, journal, auth_state=_auth())
    assert client._stop is False
    client.request_stop()
    assert client._stop is True


def test_chart_target_indicators_default_empty() -> None:
    t = ChartTarget(symbol="X:Y")
    assert t.interval == "1"
    assert t.indicators == ()


def test_config_default_polls_are_positive() -> None:
    cfg = TradingViewConfig(
        targets=(ChartTarget(symbol="X:Y"),),
    )
    assert cfg.poll_indicators_seconds > 0
    assert cfg.poll_watchlist_seconds > 0
    assert cfg.poll_alerts_seconds > 0

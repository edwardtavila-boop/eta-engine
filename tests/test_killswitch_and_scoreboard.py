"""Tests for the daily loss kill switch + bot scoreboard."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# ─── Daily loss kill switch ─────────────────────────────────────


@pytest.fixture()
def tmp_closes(tmp_path: Path):
    """Patch the killswitch's trade_closes path to a tmp file."""
    from eta_engine.scripts import daily_loss_killswitch as ks

    closes = tmp_path / "trade_closes.jsonl"
    with patch.object(ks, "_TRADE_CLOSES_PATH", closes):
        yield closes


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def _write_close(path: Path, *, ts: str, pnl: float, bot_id: str = "bot_a") -> None:
    line = json.dumps(
        {
            "ts": ts,
            "close_ts": ts,
            "signal_id": f"{bot_id}:{ts}:{pnl}",
            "realized_pnl": pnl,
            "bot_id": bot_id,
            "data_source": "live",
        }
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def test_killswitch_passes_when_pnl_above_floor(tmp_closes: Path) -> None:
    """A positive day = killswitch never trips."""
    from eta_engine.scripts.daily_loss_killswitch import is_killswitch_tripped

    _write_close(tmp_closes, ts=_today_iso() + "T12:00:00+00:00", pnl=+50.0)
    os.environ["ETA_KILLSWITCH_DAILY_LIMIT_USD"] = "-300"
    try:
        tripped, reason = is_killswitch_tripped()
    finally:
        os.environ.pop("ETA_KILLSWITCH_DAILY_LIMIT_USD", None)
    assert not tripped
    assert "+50" in reason


def test_killswitch_trips_below_floor(tmp_closes: Path) -> None:
    from eta_engine.scripts.daily_loss_killswitch import is_killswitch_tripped

    _write_close(tmp_closes, ts=_today_iso() + "T09:00:00+00:00", pnl=-200.0)
    _write_close(tmp_closes, ts=_today_iso() + "T11:00:00+00:00", pnl=-150.0)
    os.environ["ETA_KILLSWITCH_DAILY_LIMIT_USD"] = "-300"
    try:
        tripped, reason = is_killswitch_tripped()
    finally:
        os.environ.pop("ETA_KILLSWITCH_DAILY_LIMIT_USD", None)
    assert tripped
    assert "-350" in reason or "-350.0" in reason


def test_killswitch_ignores_yesterday(tmp_closes: Path) -> None:
    """Losses from prior days don't count toward today's limit."""
    from eta_engine.scripts.daily_loss_killswitch import is_killswitch_tripped

    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    _write_close(tmp_closes, ts=yesterday + "T09:00:00+00:00", pnl=-1000.0)
    _write_close(tmp_closes, ts=_today_iso() + "T09:00:00+00:00", pnl=-50.0)
    os.environ["ETA_KILLSWITCH_DAILY_LIMIT_USD"] = "-300"
    try:
        tripped, _ = is_killswitch_tripped()
    finally:
        os.environ.pop("ETA_KILLSWITCH_DAILY_LIMIT_USD", None)
    assert not tripped  # yesterday's -$1k doesn't apply


def test_killswitch_pct_spec_overrides_usd(tmp_closes: Path) -> None:
    """ETA_KILLSWITCH_DAILY_LIMIT_PCT translates via _EQUITY_USD."""
    from eta_engine.scripts.daily_loss_killswitch import is_killswitch_tripped

    _write_close(tmp_closes, ts=_today_iso() + "T09:00:00+00:00", pnl=-200.0)
    os.environ["ETA_KILLSWITCH_DAILY_LIMIT_PCT"] = "5"  # 5%
    os.environ["ETA_KILLSWITCH_EQUITY_USD"] = "10000"  # → -$500 floor
    try:
        tripped, _ = is_killswitch_tripped()
    finally:
        os.environ.pop("ETA_KILLSWITCH_DAILY_LIMIT_PCT", None)
        os.environ.pop("ETA_KILLSWITCH_EQUITY_USD", None)
    assert not tripped  # -$200 above -$500 floor


def test_killswitch_disabled_env_short_circuits(tmp_closes: Path) -> None:
    from eta_engine.scripts.daily_loss_killswitch import is_killswitch_tripped

    _write_close(tmp_closes, ts=_today_iso() + "T09:00:00+00:00", pnl=-50000.0)
    os.environ["ETA_KILLSWITCH_DISABLED"] = "1"
    try:
        tripped, reason = is_killswitch_tripped()
    finally:
        os.environ.pop("ETA_KILLSWITCH_DISABLED", None)
    assert not tripped
    assert reason == "disabled"


def test_killswitch_status_returns_full_snapshot(tmp_closes: Path) -> None:
    from eta_engine.scripts.daily_loss_killswitch import killswitch_status

    _write_close(tmp_closes, ts=_today_iso() + "T09:00:00+00:00", pnl=-100.0)
    os.environ["ETA_KILLSWITCH_DAILY_LIMIT_USD"] = "-300"
    try:
        s = killswitch_status()
    finally:
        os.environ.pop("ETA_KILLSWITCH_DAILY_LIMIT_USD", None)
    assert "tripped" in s
    assert "limit_usd" in s
    assert "today_pnl_usd" in s
    assert "date" in s


# ─── Bot scoreboard ─────────────────────────────────────────────


def test_scoreboard_metrics_compute_from_closes(tmp_path: Path) -> None:
    """_bot_metrics aggregates win_rate and avg_R from closes."""
    from eta_engine.scripts.bot_scoreboard import _bot_metrics

    bot = {
        "bot_id": "btc_test",
        "symbol": "BTC",
        "n_entries": 5,
        "n_exits": 3,
        "open_position": None,
    }
    closes = [
        {"bot_id": "btc_test", "realized_r": +0.5, "realized_pnl": +50},
        {"bot_id": "btc_test", "realized_r": -1.0, "realized_pnl": -100},
        {"bot_id": "btc_test", "realized_r": +1.5, "realized_pnl": +150},
        {"bot_id": "other_bot", "realized_r": -2.0, "realized_pnl": -200},  # ignored
    ]
    m = _bot_metrics(bot, closes)
    assert m["closes"] == 3
    assert abs(m["win_rate"] - 2 / 3) < 1e-6
    assert abs(m["avg_r"] - (0.5 - 1.0 + 1.5) / 3) < 1e-6
    assert m["realized_pnl"] == 100  # +50 - 100 + 150


def test_scoreboard_classifies_assets() -> None:
    from eta_engine.scripts.bot_scoreboard import _asset_class

    assert _asset_class("BTC") == "crypto"
    assert _asset_class("ETHUSD") == "crypto"
    assert _asset_class("MNQ1") == "futures"
    assert _asset_class("AAPL") == "other"


def test_scoreboard_handles_no_closes() -> None:
    """Brand-new bot with zero exits shouldn't divide-by-zero."""
    from eta_engine.scripts.bot_scoreboard import _bot_metrics

    bot = {
        "bot_id": "fresh",
        "symbol": "BTC",
        "n_entries": 1,
        "n_exits": 0,
        "realized_pnl": 0.0,
        "open_position": {"side": "BUY", "qty": 0.001, "entry_price": 80000},
    }
    m = _bot_metrics(bot, [])
    assert m["win_rate"] == 0.0
    assert m["avg_r"] == 0.0
    assert m["realized_pnl"] == 0.0
    assert "BUY" in m["open_pos"]

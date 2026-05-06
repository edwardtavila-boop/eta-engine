"""Tests for the signal validator and _Open invariant.

Locks in the rules that catch the 'wrong-side stop' bug class
(volume_profile, vwap_reversion).  If any of these break, malformed
signals can flow through to the live broker.
"""
from __future__ import annotations

import pytest

from eta_engine.feeds.signal_validator import (
    validate_signal,
)

# ── side ──────────────────────────────────────────────────────────────


def test_long_with_stop_below_entry_passes():
    r = validate_signal(side="LONG", entry=100.0, stop=99.0, target=102.0,
                         qty=1.0, equity=10000.0, point_value=2.0)
    assert r.ok, r.failures


def test_long_with_stop_above_entry_fails():
    r = validate_signal(side="LONG", entry=100.0, stop=101.0, target=102.0,
                         qty=1.0, equity=10000.0, point_value=2.0)
    assert not r.ok
    codes = {f.code for f in r.failures}
    assert "stop_side_inverted" in codes


def test_short_with_stop_below_entry_fails():
    r = validate_signal(side="SHORT", entry=100.0, stop=99.0, target=98.0,
                         qty=1.0, equity=10000.0, point_value=2.0)
    assert not r.ok
    codes = {f.code for f in r.failures}
    assert "stop_side_inverted" in codes


def test_short_with_stop_above_entry_passes():
    r = validate_signal(side="SHORT", entry=100.0, stop=101.0, target=98.0,
                         qty=1.0, equity=10000.0, point_value=2.0)
    assert r.ok, r.failures


def test_long_with_target_below_entry_fails():
    r = validate_signal(side="LONG", entry=100.0, stop=99.0, target=98.0,
                         qty=1.0, equity=10000.0, point_value=2.0)
    assert not r.ok
    codes = {f.code for f in r.failures}
    assert "target_side_inverted" in codes


def test_short_with_target_above_entry_fails():
    r = validate_signal(side="SHORT", entry=100.0, stop=101.0, target=102.0,
                         qty=1.0, equity=10000.0, point_value=2.0)
    assert not r.ok
    codes = {f.code for f in r.failures}
    assert "target_side_inverted" in codes


def test_invalid_side_string_fails():
    r = validate_signal(side="WTF", entry=100.0, stop=99.0, target=102.0,
                         qty=1.0, equity=10000.0, point_value=2.0)
    assert not r.ok
    codes = {f.code for f in r.failures}
    assert "invalid_side" in codes


# ── prices ────────────────────────────────────────────────────────────


def test_zero_prices_rejected():
    for field in ("entry", "stop", "target"):
        kw = {"side": "LONG", "entry": 100.0, "stop": 99.0, "target": 102.0,
              "qty": 1.0, "equity": 10000.0, "point_value": 2.0}
        kw[field] = 0.0
        r = validate_signal(**kw)
        assert not r.ok, f"{field}=0 should have failed"
        assert "non_positive_price" in {f.code for f in r.failures}


def test_negative_prices_rejected():
    r = validate_signal(side="LONG", entry=-100.0, stop=99.0, target=102.0,
                         qty=1.0, equity=10000.0, point_value=2.0)
    assert not r.ok


# ── RR ────────────────────────────────────────────────────────────────


def test_rr_below_floor_rejected():
    # entry=100, stop=99 (risk=1), target=100.05 (reward=0.05 => RR=0.05)
    r = validate_signal(side="LONG", entry=100.0, stop=99.0, target=100.05,
                         qty=1.0, equity=10000.0, point_value=2.0)
    assert not r.ok
    assert "rr_too_small" in {f.code for f in r.failures}


def test_rr_above_ceiling_rejected():
    # entry=100, stop=99.99 (risk=0.01), target=200 (reward=100 => RR=10000)
    r = validate_signal(side="LONG", entry=100.0, stop=99.99, target=200.0,
                         qty=1.0, equity=10000000.0, point_value=2.0)
    assert not r.ok
    assert "rr_absurd" in {f.code for f in r.failures}


def test_rr_normal_passes():
    # RR = 2 (standard 1:2 risk/reward)
    r = validate_signal(side="LONG", entry=100.0, stop=99.0, target=102.0,
                         qty=1.0, equity=10000.0, point_value=2.0)
    assert r.ok


# ── stop too wide ─────────────────────────────────────────────────────


def test_stop_more_than_20_pct_from_entry_rejected():
    """The volume_profile-frozen-profile-drifted-away bug pattern."""
    # entry=100, stop=70 (30% away), target=200
    r = validate_signal(side="LONG", entry=100.0, stop=70.0, target=200.0,
                         qty=1.0, equity=10000.0, point_value=2.0)
    assert not r.ok
    assert "stop_dist_too_wide" in {f.code for f in r.failures}


def test_stop_within_20_pct_passes():
    # entry=100, stop=95 (5% away), target=110
    r = validate_signal(side="LONG", entry=100.0, stop=95.0, target=110.0,
                         qty=1.0, equity=10000.0, point_value=2.0)
    assert r.ok


# ── qty / notional ────────────────────────────────────────────────────


def test_zero_qty_rejected():
    r = validate_signal(side="LONG", entry=100.0, stop=99.0, target=102.0,
                         qty=0.0, equity=10000.0, point_value=2.0)
    assert not r.ok
    assert "non_positive_qty" in {f.code for f in r.failures}


def test_huge_notional_rejected():
    """Catches: tiny stop_dist => qty so big that notional > 50x equity."""
    # 10000 contracts of MNQ @ 20000 * $2/pt = $400,000,000 notional on $10k account
    r = validate_signal(side="LONG", entry=20000.0, stop=19999.0, target=20002.0,
                         qty=10000.0, equity=10000.0, point_value=2.0)
    assert not r.ok
    codes = {f.code for f in r.failures}
    assert "notional_exceeds_cap" in codes


def test_normal_futures_notional_passes():
    # 5 contracts MNQ @ 20000 * $2/pt = $200,000 notional on $50k = 4x — well within cap
    r = validate_signal(side="LONG", entry=20000.0, stop=19990.0, target=20020.0,
                         qty=5.0, equity=50000.0, point_value=2.0)
    assert r.ok, r.failures


def test_typical_retail_futures_size_passes():
    # 10 contracts MNQ @ 27000 * $2/pt = $540,000 notional on $10k = 54x → just over cap.
    # 9 contracts at same price = $486k = 48.6x → just under.  Verify both.
    r_over = validate_signal(side="LONG", entry=27000.0, stop=26990.0, target=27020.0,
                              qty=10.0, equity=10000.0, point_value=2.0)
    r_under = validate_signal(side="LONG", entry=27000.0, stop=26990.0, target=27020.0,
                               qty=9.0, equity=10000.0, point_value=2.0)
    assert not r_over.ok
    assert r_under.ok, r_under.failures


# ── _Open invariant ────────────────────────────────────────────────────


def test_open_rejects_long_stop_above_entry():
    """The volume_profile bug class — _Open MUST refuse to construct
    a position with stop on the wrong side of entry."""
    from datetime import UTC, datetime

    from eta_engine.backtest.engine import _Open
    from eta_engine.core.data_pipeline import BarData

    bar = BarData(
        timestamp=datetime.now(tz=UTC),
        open=100.0, high=101.0, low=99.0, close=100.0, volume=500.0, symbol="TEST",
    )
    with pytest.raises(ValueError, match="LONG stop"):
        _Open(
            entry_bar=bar, side="BUY", qty=1.0,
            entry_price=100.0, stop=101.0, target=105.0,  # stop ABOVE entry — BAD
            risk_usd=100.0, confluence=5.0, leverage=1.0,
        )


def test_open_rejects_short_stop_below_entry():
    from datetime import UTC, datetime

    from eta_engine.backtest.engine import _Open
    from eta_engine.core.data_pipeline import BarData

    bar = BarData(
        timestamp=datetime.now(tz=UTC),
        open=100.0, high=101.0, low=99.0, close=100.0, volume=500.0, symbol="TEST",
    )
    with pytest.raises(ValueError, match="SHORT stop"):
        _Open(
            entry_bar=bar, side="SELL", qty=1.0,
            entry_price=100.0, stop=99.0, target=95.0,  # stop BELOW entry — BAD
            risk_usd=100.0, confluence=5.0, leverage=1.0,
        )


def test_open_rejects_long_target_below_entry():
    from datetime import UTC, datetime

    from eta_engine.backtest.engine import _Open
    from eta_engine.core.data_pipeline import BarData

    bar = BarData(
        timestamp=datetime.now(tz=UTC),
        open=100.0, high=101.0, low=99.0, close=100.0, volume=500.0, symbol="TEST",
    )
    with pytest.raises(ValueError, match="LONG target"):
        _Open(
            entry_bar=bar, side="BUY", qty=1.0,
            entry_price=100.0, stop=99.0, target=95.0,  # target BELOW entry — BAD
            risk_usd=100.0, confluence=5.0, leverage=1.0,
        )


def test_open_accepts_valid_long():
    from datetime import UTC, datetime

    from eta_engine.backtest.engine import _Open
    from eta_engine.core.data_pipeline import BarData

    bar = BarData(
        timestamp=datetime.now(tz=UTC),
        open=100.0, high=101.0, low=99.0, close=100.0, volume=500.0, symbol="TEST",
    )
    op = _Open(
        entry_bar=bar, side="BUY", qty=1.0,
        entry_price=100.0, stop=99.0, target=102.0,
        risk_usd=100.0, confluence=5.0, leverage=1.0,
    )
    assert op.entry_price == 100.0


def test_open_accepts_valid_short():
    from datetime import UTC, datetime

    from eta_engine.backtest.engine import _Open
    from eta_engine.core.data_pipeline import BarData

    bar = BarData(
        timestamp=datetime.now(tz=UTC),
        open=100.0, high=101.0, low=99.0, close=100.0, volume=500.0, symbol="TEST",
    )
    op = _Open(
        entry_bar=bar, side="SELL", qty=1.0,
        entry_price=100.0, stop=101.0, target=98.0,
        risk_usd=100.0, confluence=5.0, leverage=1.0,
    )
    assert op.entry_price == 100.0


def test_open_rejects_zero_qty():
    from datetime import UTC, datetime

    from eta_engine.backtest.engine import _Open
    from eta_engine.core.data_pipeline import BarData

    bar = BarData(
        timestamp=datetime.now(tz=UTC),
        open=100.0, high=101.0, low=99.0, close=100.0, volume=500.0, symbol="TEST",
    )
    with pytest.raises(ValueError, match="qty"):
        _Open(
            entry_bar=bar, side="BUY", qty=0.0,
            entry_price=100.0, stop=99.0, target=102.0,
            risk_usd=100.0, confluence=5.0, leverage=1.0,
        )

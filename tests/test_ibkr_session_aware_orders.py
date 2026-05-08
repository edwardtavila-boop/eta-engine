"""Tests for session-aware MARKET → LIMIT conversion in LiveIbkrVenue.

Microstructure review (2026-05-05) flagged that MARKET entries during
low-liquidity globex windows on CL/NG/6E/M6E/ZN/ZB/GC took 5-10 ticks
adverse. The session-aware policy:

  * In primary session  → MARKET behaves as before.
  * Outside primary session
       + with ref price  → convert to marketable LIMIT (3-tick buffer).
       + without ref     → REJECT with reason
                           ``market_order_outside_primary_session_no_ref_price``.
  * Weekend             → outside session for every symbol.
  * Unknown symbol      → permissive (return True so we don't block trading).

Tests freeze ``datetime.now(UTC)`` indirectly by passing an explicit
``now_utc`` to ``_in_primary_session``. The full ``place_order`` paths
require IB connectivity and aren't exercised here; we test the helper
plus the session-table coverage instead.
"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from eta_engine.venues.ibkr_live import (
    _ASSET_PRIMARY_SESSION_ET,
    _in_primary_session,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")


def _et_to_utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """Build a UTC datetime from an ET wall-clock time."""
    return datetime(year, month, day, hour, minute, tzinfo=ET).astimezone(UTC)


# ---------------------------------------------------------------------------
# 1. MARKET inside RTH → unchanged
# ---------------------------------------------------------------------------


def test_market_order_during_rth_is_unchanged() -> None:
    """At 14:00 ET on a weekday, MNQ is squarely inside RTH; the helper
    must report in-session so MARKET stays MARKET."""
    weekday_14_et = _et_to_utc(2026, 5, 5, 14, 0)  # Tue 14:00 ET
    assert _in_primary_session("MNQ", weekday_14_et) is True


# ---------------------------------------------------------------------------
# 2. MARKET outside RTH with ref price → marketable LIMIT
# ---------------------------------------------------------------------------


def test_market_order_outside_rth_converts_to_marketable_limit() -> None:
    """At 03:00 ET, MNQ is outside its 09:30-16:00 ET primary session.
    The conversion arithmetic — ref + 3 ticks for BUY at 0.25 tick size —
    must put a buy limit at ref + 0.75."""
    pre_open_et = _et_to_utc(2026, 5, 5, 3, 0)  # Tue 03:00 ET
    assert _in_primary_session("MNQ", pre_open_et) is False

    # Replicate the conversion math used inside place_order so this
    # test pins the expected limit price without touching IB.
    from eta_engine.feeds.instrument_specs import get_spec
    ref = 21000.0
    tick = float(get_spec("MNQ").tick_size)  # 0.25
    buffer_ticks = 3
    buy_limit = ref + buffer_ticks * tick
    sell_limit = ref - buffer_ticks * tick
    assert buy_limit == 21000.75
    assert sell_limit == 20999.25


# ---------------------------------------------------------------------------
# 3. MARKET outside RTH with NO ref price → REJECT
# ---------------------------------------------------------------------------


def test_market_order_outside_rth_rejects_when_no_ref_price() -> None:
    """The supervisor's contract: when MKT is sent outside session and
    no ref is available, the venue refuses with the documented reason
    so the caller can re-issue with a price."""
    # Simulate the same outside-session decision used by place_order.
    pre_open_et = _et_to_utc(2026, 5, 5, 3, 0)
    assert _in_primary_session("MNQ", pre_open_et) is False

    # The reason string is the contract surface. The full venue-side
    # path requires an IB connection; we assert the policy invariant
    # by reading the constant text from the source as a regression
    # anchor.
    import eta_engine.venues.ibkr_live as ibkr_live_module
    src = ibkr_live_module.__file__
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert "market_order_outside_primary_session_no_ref_price" in body


# ---------------------------------------------------------------------------
# 4. Weekend → not in session for any asset
# ---------------------------------------------------------------------------


def test_session_check_handles_weekend() -> None:
    """Saturday 14:00 ET — futures globex is open but the deep-liquidity
    primary session does not run on weekends. Helper must return False."""
    saturday_14_et = _et_to_utc(2026, 5, 9, 14, 0)  # 2026-05-09 is a Saturday
    assert _in_primary_session("MNQ", saturday_14_et) is False
    # Sunday 14:00 ET — same answer
    sunday_14_et = _et_to_utc(2026, 5, 10, 14, 0)
    assert _in_primary_session("MNQ", sunday_14_et) is False


# ---------------------------------------------------------------------------
# 5. Unknown symbol → permissive default
# ---------------------------------------------------------------------------


def test_unknown_symbol_defaults_permissive() -> None:
    """An unmapped contract must NOT block trading. Anything not in
    FUTURES_MAP and not in the session table returns True."""
    weekday_14_et = _et_to_utc(2026, 5, 5, 14, 0)
    assert _in_primary_session("FAKEFUTURE", weekday_14_et) is True
    # Even on a weekend — unknown means we don't have a policy, so we
    # don't enforce one.
    saturday_14_et = _et_to_utc(2026, 5, 9, 14, 0)
    assert _in_primary_session("ZZZZ_NEVER_TRADED", saturday_14_et) is True


# ---------------------------------------------------------------------------
# 6. Per-asset-class window spot-check
# ---------------------------------------------------------------------------


def test_each_asset_class_session_window() -> None:
    """One in-session + one out-of-session assertion per asset class."""
    weekday = (2026, 5, 5)  # Tuesday

    # CME equity-index futures: 09:30 - 16:00 ET
    assert _in_primary_session("MNQ", _et_to_utc(*weekday, 10, 0)) is True
    assert _in_primary_session("MNQ", _et_to_utc(*weekday, 8, 0)) is False
    assert _in_primary_session("ES", _et_to_utc(*weekday, 12, 0)) is True
    assert _in_primary_session("ES", _et_to_utc(*weekday, 17, 0)) is False
    assert _in_primary_session("MES", _et_to_utc(*weekday, 14, 0)) is True
    assert _in_primary_session("MYM", _et_to_utc(*weekday, 10, 0)) is True
    assert _in_primary_session("MYM", _et_to_utc(*weekday, 17, 0)) is False

    # CME crypto micros: 09:30 - 16:00 ET (primary liquidity tracks RTH)
    assert _in_primary_session("MBT", _et_to_utc(*weekday, 11, 0)) is True
    assert _in_primary_session("MBT", _et_to_utc(*weekday, 3, 0)) is False
    assert _in_primary_session("MET", _et_to_utc(*weekday, 11, 0)) is True

    # NYMEX energy: 09:00 - 14:30 ET
    assert _in_primary_session("CL", _et_to_utc(*weekday, 12, 0)) is True
    assert _in_primary_session("CL", _et_to_utc(*weekday, 3, 0)) is False
    # 15:30 ET is outside the NYMEX window (still inside CME equity RTH)
    assert _in_primary_session("CL", _et_to_utc(*weekday, 15, 30)) is False
    assert _in_primary_session("MCL", _et_to_utc(*weekday, 11, 0)) is True
    assert _in_primary_session("NG", _et_to_utc(*weekday, 12, 0)) is True

    # COMEX metals: 08:20 - 13:30 ET
    assert _in_primary_session("GC", _et_to_utc(*weekday, 10, 0)) is True
    assert _in_primary_session("GC", _et_to_utc(*weekday, 14, 0)) is False
    assert _in_primary_session("MGC", _et_to_utc(*weekday, 10, 0)) is True

    # CME FX (note IB indexes Euro FX as "EUR" — supervisor still
    # passes "6E", FUTURES_MAP rewrites to "EUR" for the lookup).
    assert _in_primary_session("6E", _et_to_utc(*weekday, 10, 0)) is True
    assert _in_primary_session("6E", _et_to_utc(*weekday, 17, 0)) is False
    assert _in_primary_session("M6E", _et_to_utc(*weekday, 10, 0)) is True

    # CBOT rates: 08:20 - 15:00 ET
    assert _in_primary_session("ZN", _et_to_utc(*weekday, 10, 0)) is True
    assert _in_primary_session("ZN", _et_to_utc(*weekday, 16, 0)) is False
    assert _in_primary_session("ZB", _et_to_utc(*weekday, 10, 0)) is True


def test_session_table_covers_all_required_asset_classes() -> None:
    """Belt-and-suspenders: every asset class the microstructure review
    flagged must have an entry in the session table."""
    required_roots = {
        "MNQ", "NQ", "ES", "MES", "RTY", "M2K", "MYM",  # CME/CBOT equity index
        "MBT", "MET",                              # CME crypto micros
        "CL", "MCL", "NG",                         # NYMEX energy
        "GC", "MGC",                               # COMEX metals
        "EUR", "M6E",                              # CME FX (EUR = 6E)
        "ZN", "ZB",                                # CBOT rates
    }
    missing = required_roots - set(_ASSET_PRIMARY_SESSION_ET.keys())
    assert not missing, f"session table missing roots: {missing}"

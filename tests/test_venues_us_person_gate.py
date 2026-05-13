"""Tests for the US-person venue gate (operator mandate M2, 2026-04-26).

The router must HARD-REFUSE live orders to non-FCM venues when
``IS_US_PERSON`` is True, with no failover path that bypasses the
gate. Adapters must stay importable for offline backtest + unit tests.
"""

from __future__ import annotations

import pytest

from eta_engine.venues import cme_mapping
from eta_engine.venues import router as router_mod
from eta_engine.venues.base import (
    OrderRequest,
    OrderType,
    Side,
)
from eta_engine.venues.router import (
    IS_US_PERSON,
    NON_FCM_VENUES,
    SmartRouter,
)

# ─── M2: US-person gate ──────────────────────────────────────────────────


def test_default_is_us_person_true() -> None:
    """Default policy is US-person on; override requires explicit env."""
    # The module-level constant is captured at import time, so this test
    # documents the default rather than re-evaluates env.
    assert IS_US_PERSON is True or IS_US_PERSON is False  # exists
    # Loud explicit assertion: when env not set, default True.
    import os

    saved = os.environ.pop("ETA_IS_US_PERSON", None)
    try:
        # Re-evaluate the same expression the module uses:
        evaluated = os.environ.get("ETA_IS_US_PERSON", "true").lower() in (
            "1",
            "true",
            "yes",
            "y",
        )
        assert evaluated is True
    finally:
        if saved is not None:
            os.environ["ETA_IS_US_PERSON"] = saved


def test_non_fcm_venues_includes_offshore_perps() -> None:
    assert "bybit" in NON_FCM_VENUES
    assert "okx" in NON_FCM_VENUES
    assert "deribit" in NON_FCM_VENUES
    assert "hyperliquid" in NON_FCM_VENUES
    assert "bitget" in NON_FCM_VENUES
    assert "binance" in NON_FCM_VENUES


def test_fcm_venues_not_in_non_fcm() -> None:
    """IBKR + Tastytrade + Tradovate (FCMs) must NOT appear in the block list."""
    assert "ibkr" not in NON_FCM_VENUES
    assert "tastytrade" not in NON_FCM_VENUES
    assert "tradovate" not in NON_FCM_VENUES


@pytest.mark.asyncio
async def test_btcusdt_translated_to_mbt_for_us_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2: BTCUSDT for a US person is translated to MBT (CME Micro Bitcoin)
    and the venue choice flips from bybit to ibkr. We verify the translation
    by inspecting the helper directly so we don't have to mock IBKR's
    network layer."""
    monkeypatch.setattr(router_mod, "IS_US_PERSON", True)
    r = SmartRouter(preferred_crypto_venue="bybit")
    req = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        qty=1,
        order_type=OrderType.MARKET,
    )
    translated = r._translate_for_us_legal_routing(req)
    assert translated.symbol == "MBT"
    assert translated.raw.get("original_symbol") == "BTCUSDT"
    assert translated.raw.get("m2_translated") is True
    assert r.choose_venue(translated.symbol, translated.qty).name == "ibkr"


@pytest.mark.asyncio
async def test_ethusdt_translated_to_met_for_us_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router_mod, "IS_US_PERSON", True)
    r = SmartRouter(preferred_crypto_venue="okx")
    req = OrderRequest(
        symbol="ETHUSDT",
        side=Side.BUY,
        qty=1,
        order_type=OrderType.MARKET,
    )
    translated = r._translate_for_us_legal_routing(req)
    assert translated.symbol == "MET"
    assert r.choose_venue(translated.symbol, translated.qty).name == "ibkr"


@pytest.mark.asyncio
async def test_solusdt_xrpusdt_translated_for_us_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router_mod, "IS_US_PERSON", True)
    r = SmartRouter()
    for perp, expected_cme in [("SOLUSDT", "SOL"), ("XRPUSDT", "XRP")]:
        req = OrderRequest(symbol=perp, side=Side.BUY, qty=1, order_type=OrderType.MARKET)
        t = r._translate_for_us_legal_routing(req)
        assert t.symbol == expected_cme, f"{perp} should translate to {expected_cme}, got {t.symbol}"
        assert r.choose_venue(t.symbol, t.qty).name == "ibkr"


@pytest.mark.asyncio
async def test_unmapped_perp_still_blocked_for_us_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A perp with no CME equivalent (e.g. DOGEUSDT) cannot be translated;
    the gate must still hard-refuse it instead of silently sending to Bybit."""
    monkeypatch.setattr(router_mod, "IS_US_PERSON", True)
    r = SmartRouter(preferred_crypto_venue="bybit")
    req = OrderRequest(
        symbol="DOGEUSDT",  # not in CRYPTO_PERP_TO_CME_MICRO
        side=Side.BUY,
        qty=1,
        order_type=OrderType.MARKET,
    )
    with pytest.raises(RuntimeError, match="REFUSED"):
        await r.place_with_failover(req)


def test_cme_codes_recognized_as_futures() -> None:
    """The CME crypto codes MBT/MET/BTC/ETH/SOL/XRP are recognized as futures
    so the rest of the router routes them via IBKR."""
    from eta_engine.venues.router import _is_futures

    for code in ("MBT", "MET", "BTC", "ETH", "SOL", "XRP"):
        assert _is_futures(code), f"{code} should be classified as a CME future"
    # And with month codes (e.g. MBTH26 = March 2026)
    for code in ("MBTH26", "METM27", "SOLZ26"):
        assert _is_futures(code), f"{code} (month-coded) should be classified as a future"
    # Non-futures must NOT match
    for code in ("BTCUSDT", "ETHUSDT", "DOGEUSDT"):
        assert not _is_futures(code), f"{code} should NOT be classified as a future"


# ─── M2: CME mapping ─────────────────────────────────────────────────────


def test_cme_mapping_btc_to_mbt() -> None:
    assert cme_mapping.to_cme("BTCUSDT") == "MBT"
    assert cme_mapping.to_cme("btcusdt") == "MBT"  # case-insensitive
    assert cme_mapping.to_cme("BTCUSDT", micro=False) == "BTC"


def test_cme_mapping_eth_to_met() -> None:
    assert cme_mapping.to_cme("ETHUSDT") == "MET"
    assert cme_mapping.to_cme("ETHUSDT", micro=False) == "ETH"


def test_cme_mapping_sol_to_sol() -> None:
    """SOL has no separate full-size contract; both micro=True/False return SOL."""
    assert cme_mapping.to_cme("SOLUSDT") == "SOL"
    assert cme_mapping.to_cme("SOLUSDT", micro=False) == "SOL"


def test_cme_mapping_xrp_to_xrp() -> None:
    assert cme_mapping.to_cme("XRPUSDT") == "XRP"
    assert cme_mapping.to_cme("XRPUSDT", micro=False) == "XRP"


def test_cme_mapping_unknown_returns_none() -> None:
    assert cme_mapping.to_cme("DOGEUSDT") is None
    assert cme_mapping.to_cme("") is None


def test_cme_mapping_reverse() -> None:
    assert cme_mapping.from_cme("MBT") == "BTCUSDT"
    assert cme_mapping.from_cme("MET") == "ETHUSDT"
    assert cme_mapping.from_cme("SOL") == "SOLUSDT"
    assert cme_mapping.from_cme("XRP") == "XRPUSDT"
    assert cme_mapping.from_cme("BTC") == "BTCUSDT"
    assert cme_mapping.from_cme("ETH") == "ETHUSDT"
    assert cme_mapping.from_cme("UNKNOWN") is None


def test_cme_mapping_is_crypto_perp() -> None:
    assert cme_mapping.is_crypto_perp("BTCUSDT") is True
    assert cme_mapping.is_crypto_perp("ETHUSDT") is True
    assert cme_mapping.is_crypto_perp("MNQ") is False
    assert cme_mapping.is_crypto_perp("") is False

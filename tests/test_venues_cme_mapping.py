"""Contract tests for US-legal CME crypto futures mapping."""

from __future__ import annotations

from eta_engine.venues.cme_mapping import (
    CRYPTO_PERP_TO_CME_FULL,
    CRYPTO_PERP_TO_CME_MICRO,
    from_cme,
    is_crypto_perp,
    to_cme,
)


def test_crypto_perp_to_cme_micro_mapping_covers_active_crypto_symbols() -> None:
    assert CRYPTO_PERP_TO_CME_MICRO == {
        "BTCUSDT": "MBT",
        "ETHUSDT": "MET",
        "SOLUSDT": "SOL",
        "XRPUSDT": "XRP",
    }


def test_to_cme_prefers_micro_and_is_case_insensitive() -> None:
    assert to_cme("btcusdt") == "MBT"
    assert to_cme(" ETHUSDT ") == "MET"
    assert to_cme("SOLUSDT") == "SOL"
    assert to_cme("XRPUSDT") == "XRP"


def test_to_cme_full_size_falls_back_for_sol_and_xrp() -> None:
    assert CRYPTO_PERP_TO_CME_FULL == {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
    assert to_cme("BTCUSDT", micro=False) == "BTC"
    assert to_cme("ETHUSDT", micro=False) == "ETH"
    assert to_cme("SOLUSDT", micro=False) == "SOL"
    assert to_cme("XRPUSDT", micro=False) == "XRP"


def test_reverse_mapping_accepts_micro_and_full_codes() -> None:
    assert from_cme("mbt") == "BTCUSDT"
    assert from_cme("MET") == "ETHUSDT"
    assert from_cme("BTC") == "BTCUSDT"
    assert from_cme("ETH") == "ETHUSDT"
    assert from_cme("SOL") == "SOLUSDT"
    assert from_cme("XRP") == "XRPUSDT"


def test_unknown_symbols_are_not_mapped() -> None:
    assert to_cme("DOGEUSDT") is None
    assert from_cme("DOG") is None
    assert is_crypto_perp("DOGEUSDT") is False
    assert is_crypto_perp("BTCUSDT") is True

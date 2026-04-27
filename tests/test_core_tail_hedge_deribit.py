"""Tests for the Deribit BTC put extension in core.tail_hedge."""

from __future__ import annotations

import pytest

from eta_engine.core.tail_hedge import price_otm_put_btc_deribit


class TestPriceOtmPutBtcDeribit:
    def test_returns_positive_premium(self):
        q = price_otm_put_btc_deribit(btc_spot=60_000.0, otm_pct=15.0, days_to_expiry=30, implied_vol=0.65)
        assert q["premium_per_btc"] > 0.0
        assert q["strike"] == pytest.approx(51_000.0)

    def test_lower_iv_means_lower_premium(self):
        high = price_otm_put_btc_deribit(btc_spot=60_000.0, implied_vol=0.90)
        low = price_otm_put_btc_deribit(btc_spot=60_000.0, implied_vol=0.40)
        assert high["premium_per_btc"] > low["premium_per_btc"]

    def test_longer_expiry_means_higher_premium(self):
        short = price_otm_put_btc_deribit(btc_spot=60_000.0, days_to_expiry=7, implied_vol=0.65)
        long = price_otm_put_btc_deribit(btc_spot=60_000.0, days_to_expiry=90, implied_vol=0.65)
        assert long["premium_per_btc"] > short["premium_per_btc"]

    def test_deeper_otm_means_lower_premium(self):
        shallow = price_otm_put_btc_deribit(btc_spot=60_000.0, otm_pct=5.0)
        deep = price_otm_put_btc_deribit(btc_spot=60_000.0, otm_pct=25.0)
        assert deep["premium_per_btc"] < shallow["premium_per_btc"]

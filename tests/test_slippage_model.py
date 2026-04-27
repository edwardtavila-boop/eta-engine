"""tests.test_slippage_model — tiers, symbol defaults, calibration."""

from __future__ import annotations

from eta_engine.data.slippage_model import SlippageModel


class TestTiers:
    def test_normal_uses_half_spread(self) -> None:
        m = SlippageModel()
        # ETHUSDT default spread = 0.5 bps; tiny qty makes impact ~0
        bps = m.estimate("ETHUSDT", "BUY", qty=0.01, price=3500.0, vol_pct=0.01, urgency="NORMAL")
        assert abs(bps - 0.25) < 0.1

    def test_aggressive_greater_than_normal(self) -> None:
        m = SlippageModel()
        n = m.estimate("SOLUSDT", "BUY", qty=100.0, price=200.0, vol_pct=1.0, urgency="NORMAL")
        a = m.estimate("SOLUSDT", "BUY", qty=100.0, price=200.0, vol_pct=1.0, urgency="AGGRESSIVE")
        assert a > n

    def test_passive_negative_bps(self) -> None:
        m = SlippageModel()
        p = m.estimate("ETHUSDT", "SELL", qty=1.0, price=3500.0, urgency="PASSIVE")
        assert p < 0.0

    def test_sqrt_qty_impact_scaling(self) -> None:
        m = SlippageModel()
        low = m.estimate("ETHUSDT", "BUY", qty=100.0, price=3500.0, vol_pct=1.0, urgency="NORMAL")
        high = m.estimate("ETHUSDT", "BUY", qty=10_000.0, price=3500.0, vol_pct=1.0, urgency="NORMAL")
        # 100x qty -> sqrt(100) = 10x impact on variable portion
        assert high > low


class TestCalibration:
    def test_calibrate_adjusts_factor(self) -> None:
        m = SlippageModel()
        theor = [1.0, 1.0, 1.0, 1.0]
        actual = [1.5, 1.5, 1.5, 1.5]
        out = m.calibrate(actual, theor)
        assert out["n"] == 4
        assert out["factor"] == 1.5
        # Subsequent estimate reflects new factor
        # (rough: normal tier for ETH is ~0.25 bps @ tiny qty -> 0.375 after 1.5x)
        bps = m.estimate("ETHUSDT", "BUY", qty=0.01, price=3500.0, vol_pct=0.01, urgency="NORMAL")
        assert bps > 0.3  # inflated by factor

    def test_calibrate_empty_input(self) -> None:
        m = SlippageModel()
        out = m.calibrate([], [])
        assert out["n"] == 0


class TestDefaults:
    def test_mnq_has_narrow_spread(self) -> None:
        m = SlippageModel()
        bps = m.estimate("MNQ", "BUY", qty=1.0, price=20500.0, vol_pct=0.5, urgency="NORMAL")
        assert bps < 1.0

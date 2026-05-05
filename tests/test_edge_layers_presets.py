"""Guard edge-layer presets used by registry bots.

The paper-soak presets should preserve calibration knobs while keeping
trade-veto layers opt-in. Otherwise an edge wrapper can silently starve a
strategy before broker routing ever sees a candidate order.
"""

from __future__ import annotations

from eta_engine.strategies.edge_layers import (
    EdgeAmplifierConfig,
    btc_crypto_preset,
    eth_crypto_preset,
    mnq_futures_preset,
    sol_crypto_preset,
)


def test_edge_amplifier_config_keeps_legacy_threshold_knobs() -> None:
    cfg = EdgeAmplifierConfig(
        absorption_vol_z_min=1.1,
        absorption_range_z_max=0.4,
        rsi_period=9,
        rsi_divergence_lookback=13,
        rsi_peak_tolerance=3,
    )

    assert cfg.absorption_vol_z_min == 1.1
    assert cfg.absorption_range_z_max == 0.4
    assert cfg.rsi_period == 9
    assert cfg.rsi_divergence_lookback == 13
    assert cfg.rsi_peak_tolerance == 3


def test_paper_soak_presets_keep_veto_layers_disabled() -> None:
    for cfg in (
        mnq_futures_preset(),
        btc_crypto_preset(),
        eth_crypto_preset(),
        sol_crypto_preset(),
    ):
        assert cfg.enable_session_gate is False
        assert cfg.enable_exhaustion_gate is False
        assert cfg.enable_absorption_gate is False
        assert cfg.enable_drift_boost is False
        assert cfg.enable_rsi_divergence is False
        assert cfg.enable_rejection_candle is False
        assert cfg.enable_structural_stops is True
        assert cfg.enable_vol_sizing is True
        assert cfg.absorption_vol_z_min > 0
        assert cfg.absorption_range_z_max > 0

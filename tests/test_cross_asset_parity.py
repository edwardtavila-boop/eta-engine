"""Cross-asset parity tests for the foundation strategy presets.

User mandate (2026-04-27): "we need to make sure we have equivalent
strategies for all of the crypto bots and nq".

The foundation strategies (sweep_reclaim, compression_breakout,
regime_gated) ship asset-class preset factories. These tests
guarantee:

1. EVERY supported asset has a preset factory for EVERY foundation
   strategy.
2. The presets DIFFER on volatility-sensitive knobs (no accidental
   cross-asset config leaks).
3. The presets all return valid configs that pass the dataclass
   validation.

This is the safety net — if anyone ever flattens the differences
in a refactor, CI catches it.
"""

from __future__ import annotations

from eta_engine.strategies.compression_breakout_strategy import (
    CompressionBreakoutConfig,
    btc_compression_preset,
    eth_compression_preset,
    mnq_compression_preset,
    nq_compression_preset,
    sol_compression_preset,
)
from eta_engine.strategies.regime_gated_strategy import (
    RegimeGatedConfig,
    btc_daily_preset,
    btc_daily_provider_preset,
    eth_daily_preset,
    mnq_intraday_preset,
    nq_intraday_preset,
    sol_daily_preset,
)
from eta_engine.strategies.sweep_reclaim_strategy import (
    SweepReclaimConfig,
    btc_daily_sweep_preset,
    eth_daily_sweep_preset,
    mnq_intraday_sweep_preset,
    nq_intraday_sweep_preset,
    sol_daily_sweep_preset,
)

# ---------------------------------------------------------------------------
# Sweep+reclaim cross-asset parity
# ---------------------------------------------------------------------------


def test_sweep_reclaim_has_preset_for_every_supported_asset() -> None:
    """MNQ, NQ, BTC, ETH, SOL all have sweep_reclaim presets."""
    presets = {
        "MNQ": mnq_intraday_sweep_preset(),
        "NQ": nq_intraday_sweep_preset(),
        "BTC": btc_daily_sweep_preset(),
        "ETH": eth_daily_sweep_preset(),
        "SOL": sol_daily_sweep_preset(),
    }
    for sym, cfg in presets.items():
        assert isinstance(cfg, SweepReclaimConfig), f"{sym} preset wrong type"
        assert cfg.atr_period > 0, f"{sym} atr_period must be positive"
        assert cfg.rr_target > 0, f"{sym} rr_target must be positive"
        assert 0.0 <= cfg.min_wick_pct <= 1.0, f"{sym} wick_pct out of range"


def test_sweep_reclaim_crypto_presets_differ_by_vol_class() -> None:
    """SOL > BTC > ETH on ATR-stop (DeepSeek-tuned 2026-05-02).

    ETH gets a tighter stop (1.0) than BTC (1.5) because ETH sweeps
    are shallower and faster — wider stops degrade edge on ETH. SOL
    is materially more volatile (~2x BTC) and keeps the widest stop.
    """
    btc = btc_daily_sweep_preset()
    eth = eth_daily_sweep_preset()
    sol = sol_daily_sweep_preset()
    assert eth.atr_stop_mult < btc.atr_stop_mult < sol.atr_stop_mult
    # Wick thresholds: SOL loosest (big fake wicks), ETH tightest
    # (DeepSeek-tuned 0.40 to reduce false reclaims on noisy ETH)
    assert sol.min_wick_pct < btc.min_wick_pct
    assert btc.min_wick_pct < eth.min_wick_pct


def test_sweep_reclaim_futures_vs_crypto_separation() -> None:
    """Intraday futures (MNQ, NQ) configs differ from crypto on
    cooldown + max-trades-per-day (RTH-bounded vs 24/7)."""
    mnq = mnq_intraday_sweep_preset()
    btc = btc_daily_sweep_preset()
    # Intraday cooldown shorter, trades/day higher
    assert mnq.min_bars_between_trades < btc.min_bars_between_trades
    assert mnq.max_trades_per_day > btc.max_trades_per_day


def test_sweep_reclaim_sister_index_presets_match_shape() -> None:
    """MNQ and NQ trade the same Nasdaq-100 mechanic — presets share
    bar-cadence-sensitive knobs (lookback, period, cooldown)."""
    mnq = mnq_intraday_sweep_preset()
    nq = nq_intraday_sweep_preset()
    # Same intraday cadence → same lookback / cooldowns
    assert mnq.level_lookback == nq.level_lookback
    assert mnq.atr_period == nq.atr_period
    assert mnq.min_bars_between_trades == nq.min_bars_between_trades


# ---------------------------------------------------------------------------
# Compression breakout cross-asset parity
# ---------------------------------------------------------------------------


def test_compression_has_preset_for_every_supported_asset() -> None:
    presets = {
        "MNQ": mnq_compression_preset(),
        "NQ": nq_compression_preset(),
        "BTC": btc_compression_preset(),
        "ETH": eth_compression_preset(),
        "SOL": sol_compression_preset(),
    }
    for sym, cfg in presets.items():
        assert isinstance(cfg, CompressionBreakoutConfig), f"{sym} preset wrong type"
        assert cfg.bb_period > 0, f"{sym} bb_period must be positive"
        assert cfg.atr_period > 0, f"{sym} atr_period must be positive"
        assert cfg.rr_target > 0, f"{sym} rr_target must be positive"


def test_compression_crypto_vol_ladder() -> None:
    """ATR-stop: SOL > BTC > ETH (DeepSeek-tuned 2026-05-02).

    ETH gets tighter ATR stop (1.0) and RR (1.5) than BTC because
    its compression breakouts are faster/more shallow — wider stops
    degrade edge. SOL is materially more volatile and keeps widest stop.
    """
    btc = btc_compression_preset()
    eth = eth_compression_preset()
    sol = sol_compression_preset()
    assert eth.atr_stop_mult < btc.atr_stop_mult < sol.atr_stop_mult
    assert eth.rr_target < btc.rr_target < sol.rr_target


def test_compression_futures_vs_crypto_separation() -> None:
    """MNQ/NQ presets target intraday cadence; BTC/ETH/SOL target 1h."""
    mnq = mnq_compression_preset()
    btc = btc_compression_preset()
    # Intraday: shorter warmup, shorter trend EMA, shorter breakout window
    assert mnq.warmup_bars < btc.warmup_bars
    assert mnq.trend_ema_period < btc.trend_ema_period
    assert mnq.breakout_lookback < btc.breakout_lookback


def test_compression_sister_index_presets_match() -> None:
    mnq = mnq_compression_preset()
    nq = nq_compression_preset()
    assert mnq.bb_period == nq.bb_period
    assert mnq.warmup_bars == nq.warmup_bars
    assert mnq.atr_stop_mult == nq.atr_stop_mult


# ---------------------------------------------------------------------------
# Regime-gate cross-asset parity
# ---------------------------------------------------------------------------


def test_regime_gate_has_preset_for_every_supported_asset() -> None:
    presets = {
        "BTC_provider": btc_daily_provider_preset(),
        "BTC_lt": btc_daily_preset(),
        "ETH": eth_daily_preset(),
        "SOL": sol_daily_preset(),
        "MNQ": mnq_intraday_preset(),
        "NQ": nq_intraday_preset(),
    }
    for sym, cfg in presets.items():
        assert isinstance(cfg, RegimeGatedConfig), f"{sym} preset wrong type"
        assert len(cfg.allowed_regimes) >= 1, f"{sym} no regimes allowed"
        assert len(cfg.allowed_biases) >= 1, f"{sym} no biases allowed"


def test_regime_gate_crypto_vol_ladder() -> None:
    """trend_distance_pct ladders BTC <= ETH <= SOL."""
    btc = btc_daily_preset()
    eth = eth_daily_preset()
    sol = sol_daily_preset()
    assert btc.classifier.trend_distance_pct <= eth.classifier.trend_distance_pct
    assert eth.classifier.trend_distance_pct <= sol.classifier.trend_distance_pct
    # ATR cutoff also ladders
    assert (
        btc.classifier.range_atr_pct_max
        <= eth.classifier.range_atr_pct_max
        <= sol.classifier.range_atr_pct_max
    )


def test_regime_gate_futures_vs_crypto_clearly_distinct() -> None:
    """Intraday (MNQ/NQ) classifier has much tighter knobs than
    daily (BTC/ETH/SOL)."""
    mnq = mnq_intraday_preset()
    btc = btc_daily_preset()
    assert mnq.classifier.fast_ema < btc.classifier.fast_ema
    assert mnq.classifier.slow_ema < btc.classifier.slow_ema
    assert mnq.classifier.trend_distance_pct < btc.classifier.trend_distance_pct
    assert mnq.classifier.warmup_bars < btc.classifier.warmup_bars


def test_regime_gate_sister_index_presets_match() -> None:
    mnq = mnq_intraday_preset()
    nq = nq_intraday_preset()
    assert mnq.classifier.fast_ema == nq.classifier.fast_ema
    assert mnq.classifier.slow_ema == nq.classifier.slow_ema
    assert mnq.classifier.trend_distance_pct == nq.classifier.trend_distance_pct
    # Same allowed regime set (both ORB-style mean-rev mechanic)
    assert mnq.allowed_regimes == nq.allowed_regimes
    assert mnq.allowed_modes == nq.allowed_modes


# ---------------------------------------------------------------------------
# Roster-level coverage matrix
# ---------------------------------------------------------------------------


def test_full_asset_coverage_matrix_complete() -> None:
    """Every (asset, strategy) cell in the foundation matrix has
    a preset. If this test fails, a strategy is missing for an
    asset and a bot can't be configured for it.
    """
    coverage = {
        ("MNQ", "sweep"): mnq_intraday_sweep_preset(),
        ("NQ", "sweep"): nq_intraday_sweep_preset(),
        ("BTC", "sweep"): btc_daily_sweep_preset(),
        ("ETH", "sweep"): eth_daily_sweep_preset(),
        ("SOL", "sweep"): sol_daily_sweep_preset(),
        ("MNQ", "compression"): mnq_compression_preset(),
        ("NQ", "compression"): nq_compression_preset(),
        ("BTC", "compression"): btc_compression_preset(),
        ("ETH", "compression"): eth_compression_preset(),
        ("SOL", "compression"): sol_compression_preset(),
        ("MNQ", "regime_gate"): mnq_intraday_preset(),
        ("NQ", "regime_gate"): nq_intraday_preset(),
        ("BTC", "regime_gate"): btc_daily_preset(),
        ("ETH", "regime_gate"): eth_daily_preset(),
        ("SOL", "regime_gate"): sol_daily_preset(),
    }
    # 5 assets × 3 strategies = 15 cells
    assert len(coverage) == 15
    # All non-None
    for cell, cfg in coverage.items():
        assert cfg is not None, f"missing preset for {cell}"

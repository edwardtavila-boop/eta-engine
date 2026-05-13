"""Tests for strategies.regime_gate -- the devils-advocate
grid-trends-die-here gate."""

from __future__ import annotations

import pytest

from eta_engine.strategies.regime_gate import (
    DEFAULT_ADX_MAX,
    DEFAULT_VOL_Z_MAX,
    RegimeGateConfig,
    is_grid_safe,
)

# ---------------------------------------------------------------------------
# RegimeGateConfig.from_extras
# ---------------------------------------------------------------------------


def test_default_config_uses_constants() -> None:
    cfg = RegimeGateConfig()
    assert cfg.adx_max == pytest.approx(DEFAULT_ADX_MAX)
    assert cfg.vol_z_max == pytest.approx(DEFAULT_VOL_Z_MAX)
    assert cfg.fail_shut_on_missing_adx is True


def test_from_extras_returns_defaults_when_missing() -> None:
    assert RegimeGateConfig.from_extras(None) == RegimeGateConfig()
    assert RegimeGateConfig.from_extras({}) == RegimeGateConfig()
    assert RegimeGateConfig.from_extras({"unrelated": 1}) == RegimeGateConfig()


def test_from_extras_parses_full_shape() -> None:
    cfg = RegimeGateConfig.from_extras(
        {
            "regime_gate_config": {
                "adx_max": 30.0,
                "vol_z_max": 3.0,
                "fail_shut_on_missing_adx": False,
            },
        }
    )
    assert cfg.adx_max == pytest.approx(30.0)
    assert cfg.vol_z_max == pytest.approx(3.0)
    assert cfg.fail_shut_on_missing_adx is False


def test_from_extras_returns_defaults_on_garbage() -> None:
    cfg = RegimeGateConfig.from_extras(
        {
            "regime_gate_config": {"adx_max": "not_a_number"},
        }
    )
    assert cfg == RegimeGateConfig()


def test_from_extras_returns_defaults_when_value_not_dict() -> None:
    cfg = RegimeGateConfig.from_extras({"regime_gate_config": "garbage"})
    assert cfg == RegimeGateConfig()


# ---------------------------------------------------------------------------
# is_grid_safe -- ADX gate
# ---------------------------------------------------------------------------


def test_low_adx_allows() -> None:
    """ADX 18 (well below 25) -> ranging -> grid allowed."""
    bar = {"adx_14": 18.0}
    allowed, reason = is_grid_safe(bar)
    assert allowed is True
    assert "ranging" in reason


def test_high_adx_blocks() -> None:
    """ADX 35 (above 25) -> trending -> grid blocked."""
    bar = {"adx_14": 35.0}
    allowed, reason = is_grid_safe(bar)
    assert allowed is False
    assert "trending" in reason
    assert "35" in reason  # surface the value for ops


def test_adx_at_threshold_allows() -> None:
    """ADX exactly equal to threshold -> ranging side -> allowed."""
    bar = {"adx_14": 25.0}
    allowed, _ = is_grid_safe(bar)
    assert allowed is True


def test_adx_just_above_threshold_blocks() -> None:
    bar = {"adx_14": 25.001}
    allowed, _ = is_grid_safe(bar)
    assert allowed is False


def test_missing_adx_fails_shut_by_default() -> None:
    """No adx_14 in bar -> fail-shut so grid doesn't accidentally
    fire in a known-trending tape with incomplete features."""
    bar = {"close": 50_000.0}
    allowed, reason = is_grid_safe(bar)
    assert allowed is False
    assert "no adx_14" in reason


def test_missing_adx_fails_open_when_configured() -> None:
    bar = {"close": 50_000.0}
    cfg = RegimeGateConfig(fail_shut_on_missing_adx=False)
    allowed, _ = is_grid_safe(bar, config=cfg)
    assert allowed is True


def test_garbage_adx_fails_shut() -> None:
    bar = {"adx_14": "not_a_number"}
    allowed, reason = is_grid_safe(bar)
    assert allowed is False
    assert "non-numeric" in reason


# ---------------------------------------------------------------------------
# is_grid_safe -- vol_z gate
# ---------------------------------------------------------------------------


def test_normal_vol_z_allows() -> None:
    bar = {"adx_14": 18.0, "vol_z": 0.5}
    allowed, _ = is_grid_safe(bar)
    assert allowed is True


def test_high_vol_z_blocks() -> None:
    """|vol_z| > 2 -> regime shift -> grid blocked even if ADX is low."""
    bar = {"adx_14": 18.0, "vol_z": 2.5}
    allowed, reason = is_grid_safe(bar)
    assert allowed is False
    assert "vol_z" in reason


def test_negative_vol_z_also_blocks() -> None:
    """|vol_z| above threshold blocks regardless of sign."""
    bar = {"adx_14": 18.0, "vol_z": -2.5}
    allowed, _ = is_grid_safe(bar)
    assert allowed is False


def test_vol_z_threshold_override() -> None:
    cfg = RegimeGateConfig(vol_z_max=5.0)
    bar = {"adx_14": 18.0, "vol_z": 3.0}  # under the new threshold
    allowed, _ = is_grid_safe(bar, config=cfg)
    assert allowed is True


def test_missing_vol_z_doesnt_block() -> None:
    """vol_z is optional; missing -> only ADX gate applies."""
    bar = {"adx_14": 18.0}
    allowed, _ = is_grid_safe(bar)
    assert allowed is True


# ---------------------------------------------------------------------------
# Combined
# ---------------------------------------------------------------------------


def test_both_signals_must_clear_for_allow() -> None:
    """Even with ranging ADX, high vol_z still blocks."""
    bar = {"adx_14": 15.0, "vol_z": 3.0}
    allowed, _ = is_grid_safe(bar)
    assert allowed is False

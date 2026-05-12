"""Tests for wave-8 sizing kaizen on cl_momentum + gc_momentum.

The wave-7 dual-basis watchdog diagnosed cl_momentum and gc_momentum
as USD-CRITICAL but R-HEALTHY — i.e., the strategies have edge in
R-multiples but the dollar magnitude per trade exceeded the operator's
USD retirement floor. Wave-8 cut their risk_per_trade_pct from 0.005
(0.5%) to 0.0025 (0.25%) so a single stopped-out trade no longer
breaches the floor.

These tests pin the wave-8 risk values so an accidental revert is
caught at CI rather than only being noticed when the watchdog starts
firing CRITICAL again.
"""
# ruff: noqa: PLR2004
from __future__ import annotations

import pytest


def test_gc_momentum_preset_uses_wave8_risk() -> None:
    """gc_momentum_preset must keep risk_per_trade_pct halved to 0.0025.

    Pre-wave-8: 0.005 -> $147/R against -$200 floor -> 1.4 stopouts
                breach the threshold (sensitivity too high).
    Post-wave-8: 0.0025 -> ~$74/R against -$200 floor -> 2.7 stopouts
                 to breach (operator gets room to evaluate edge).
    """
    from eta_engine.strategies.commodity_momentum_strategy import (
        gc_momentum_preset,
    )

    cfg = gc_momentum_preset()
    assert cfg.risk_per_trade_pct == 0.0025, (
        f"gc_momentum risk_per_trade_pct={cfg.risk_per_trade_pct} "
        "diverged from wave-8 baseline 0.0025; if this is a deliberate "
        "tuning, update both this test AND the preset docstring."
    )


def test_cl_momentum_preset_uses_wave8_risk() -> None:
    """cl_momentum_preset must keep risk_per_trade_pct halved to 0.0025.

    CL is the highest-dollar contract in the diamond fleet ($1,000/point),
    so per-trade USD risk swings the most when risk_per_trade_pct is at
    the default 0.5%. Wave-8 halves it specifically to keep one stopout
    inside the -$1,500 USD retirement floor.
    """
    from eta_engine.strategies.commodity_momentum_strategy import (
        cl_momentum_preset,
    )

    cfg = cl_momentum_preset()
    assert cfg.risk_per_trade_pct == 0.0025, (
        f"cl_momentum risk_per_trade_pct={cfg.risk_per_trade_pct} "
        "diverged from wave-8 baseline 0.0025; if this is a deliberate "
        "tuning, update both this test AND the preset docstring."
    )


@pytest.mark.parametrize(
    "preset_factory_name",
    ["gc_momentum_preset", "cl_momentum_preset"],
)
def test_wave8_presets_strictly_halved_from_default(
    preset_factory_name: str,
) -> None:
    """Both wave-8 presets must be at most half of the MomentumConfig
    default risk_per_trade_pct. Catches accidental restoration of the
    default value via a copy-paste from another preset factory."""
    from eta_engine.strategies import commodity_momentum_strategy as mom

    default_cfg = mom.MomentumConfig()
    factory = getattr(mom, preset_factory_name)
    cfg = factory()
    assert cfg.risk_per_trade_pct <= default_cfg.risk_per_trade_pct / 2.0, (
        f"{preset_factory_name}.risk_per_trade_pct={cfg.risk_per_trade_pct} "
        f"is more than half of the MomentumConfig default "
        f"{default_cfg.risk_per_trade_pct} — wave-8 kaizen requires "
        "strict halving to bring USD-per-R inside the watchdog floor."
    )


def test_other_momentum_presets_unchanged_by_wave8() -> None:
    """Wave-8 only touches cl_momentum + gc_momentum. Other commodity
    momentum presets (ng_momentum_preset, etc.) must keep the default
    risk_per_trade_pct so this kaizen wave doesn't accidentally affect
    bots that didn't have the USD-CRITICAL diagnosis."""
    from eta_engine.strategies.commodity_momentum_strategy import (
        ng_momentum_preset,
    )

    cfg = ng_momentum_preset()
    # ng_momentum was NOT in wave-8 scope; should still be at 0.005 default
    assert cfg.risk_per_trade_pct == 0.005, (
        f"ng_momentum risk_per_trade_pct={cfg.risk_per_trade_pct} "
        "changed; wave-8 was scoped to cl + gc only — verify intent."
    )

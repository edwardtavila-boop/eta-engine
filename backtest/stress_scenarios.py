"""2008/2020/2022 stress-scenario synthetic return generator — P3_PROOF stress.

Why
---
Live trading must survive regimes that don't exist in the Databento sample.
Rather than re-import tick data for every crisis, we emit **synthetic return
paths** that match the signature of known crises:

* **2008 — Slow-grind drawdown** — multi-week equity erosion, moderate vol,
  no flash crash. Tests tail-drawdown limits.
* **2020 — Flash crash + V-recovery** — single day -10% gap, then rapid
  recovery over 2-3 weeks. Tests stop-hunt + whipsaw exit logic.
* **2022 — Regime-change grind** — sustained downtrend with rising realized
  vol. Tests correlation brake + momentum-to-mean-reversion flip handling.

Each scenario returns a deterministic ``np.ndarray`` of per-bar returns,
parameterised by seed so walk-forward tests are reproducible. Output plugs
into :class:`eta_engine.core.portfolio_risk.PortfolioRisk` or the
:mod:`eta_engine.backtest.engine` replay harness.
"""

from __future__ import annotations

import logging
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ScenarioKind = Literal["2008_slow_grind", "2020_flash_crash", "2022_regime_change"]


class ScenarioSpec(BaseModel):
    """Stored metadata for a generated stress path."""

    kind: ScenarioKind
    n_bars: int
    seed: int
    realized_vol: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    notes: list[str] = Field(default_factory=list)


def _drawdown(returns: np.ndarray) -> float:
    equity = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / peak
    return float(np.max(dd))


# ---------------------------------------------------------------------------
# Scenario generators
# ---------------------------------------------------------------------------


def scenario_2008_slow_grind(
    n_bars: int = 250,
    *,
    seed: int = 2008,
    mean_drift: float = -0.0015,  # ~-0.15% per bar mean
    base_vol: float = 0.018,
) -> tuple[np.ndarray, ScenarioSpec]:
    """Slow-grind bear market with periodic relief rallies.

    Signature: persistent negative drift, modestly elevated vol, no single-
    bar catastrophic shock. Tests DD limits + re-risking discipline.
    """
    rng = np.random.default_rng(seed)
    base = rng.normal(mean_drift, base_vol, size=n_bars)
    # Add slow relief rallies every ~30 bars: 5-bar +0.02 drift burst.
    for start in range(30, n_bars, 40):
        base[start : start + 5] += rng.normal(0.02, 0.005, size=min(5, n_bars - start))
    spec = ScenarioSpec(
        kind="2008_slow_grind",
        n_bars=n_bars,
        seed=seed,
        realized_vol=float(np.std(base)),
        total_return=float(np.prod(1.0 + base) - 1.0),
        max_drawdown=_drawdown(base),
    )
    return base, spec


def scenario_2020_flash_crash(
    n_bars: int = 120,
    *,
    seed: int = 2020,
    crash_magnitude: float = -0.10,
    recovery_bars: int = 30,
) -> tuple[np.ndarray, ScenarioSpec]:
    """Single-bar -10% gap followed by rapid V-recovery.

    Signature: one catastrophic bar, then 30 bars of positive drift restoring
    ~90% of the loss. Tests stop-hunt + gap-fill logic.
    """
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0005, 0.01, size=n_bars)
    crash_idx = n_bars // 4
    returns[crash_idx] = crash_magnitude
    # Positive recovery drift for recovery_bars after crash
    rec_end = min(crash_idx + 1 + recovery_bars, n_bars)
    recovery_drift = (-crash_magnitude * 0.9) / recovery_bars  # reclaim 90% of drop
    returns[crash_idx + 1 : rec_end] += recovery_drift
    spec = ScenarioSpec(
        kind="2020_flash_crash",
        n_bars=n_bars,
        seed=seed,
        realized_vol=float(np.std(returns)),
        total_return=float(np.prod(1.0 + returns) - 1.0),
        max_drawdown=_drawdown(returns),
        notes=[f"crash_bar={crash_idx}", f"crash_magnitude={crash_magnitude}"],
    )
    return returns, spec


def scenario_2022_regime_change(
    n_bars: int = 300,
    *,
    seed: int = 2022,
    mean_drift: float = -0.0008,
    vol_trend_per_bar: float = 6e-5,
) -> tuple[np.ndarray, ScenarioSpec]:
    """Persistent downtrend with gradually rising realized vol.

    Signature: sustained negative drift + vol drift (each bar's sigma grows).
    Tests correlation brake engagement + momentum→mean-reversion switch.
    """
    rng = np.random.default_rng(seed)
    base_vol = 0.010
    vols = base_vol + vol_trend_per_bar * np.arange(n_bars)
    returns = rng.normal(0.0, 1.0, size=n_bars) * vols + mean_drift
    spec = ScenarioSpec(
        kind="2022_regime_change",
        n_bars=n_bars,
        seed=seed,
        realized_vol=float(np.std(returns)),
        total_return=float(np.prod(1.0 + returns) - 1.0),
        max_drawdown=_drawdown(returns),
        notes=[f"vol_end={vols[-1]:.4f}", f"vol_start={vols[0]:.4f}"],
    )
    return returns, spec


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_DISPATCH = {
    "2008_slow_grind": scenario_2008_slow_grind,
    "2020_flash_crash": scenario_2020_flash_crash,
    "2022_regime_change": scenario_2022_regime_change,
}


def generate(kind: ScenarioKind, **kwargs: float | int) -> tuple[np.ndarray, ScenarioSpec]:
    """Dispatch to the right scenario generator by name.

    Example
    -------
    >>> returns, spec = generate("2020_flash_crash", n_bars=200)
    >>> spec.max_drawdown  # doctest: +SKIP
    0.10...
    """
    if kind not in _DISPATCH:
        raise ValueError(f"unknown scenario {kind!r}; valid: {list(_DISPATCH)}")
    return _DISPATCH[kind](**kwargs)  # type: ignore[arg-type]


def generate_all(
    n_bars_each: int = 250,
    *,
    seed_base: int = 0,
) -> dict[str, tuple[np.ndarray, ScenarioSpec]]:
    """Emit all three scenarios with deterministic seeds offset by ``seed_base``."""
    return {
        kind: _DISPATCH[kind](n_bars=n_bars_each, seed=seed_base + i)  # type: ignore[arg-type]
        for i, kind in enumerate(_DISPATCH)
    }

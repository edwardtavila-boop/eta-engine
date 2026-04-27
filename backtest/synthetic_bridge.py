"""EVOLUTIONARY TRADING ALGO  //  backtest.synthetic_bridge.

Bridge between :mod:`brain.synthetic` regime generator and
:mod:`backtest.stress_scenarios` historical-crisis replays.

Why this module exists
----------------------
* ``brain.synthetic`` emits regime-conditioned OHLCV bars (GBM + vol
  clustering + heavy tails) -- useful to *expand* backtests with more
  bars of a specific regime.
* ``backtest.stress_scenarios`` emits canned crisis return paths
  (2008 slow grind, 2020 flash crash, 2022 regime change) -- useful to
  *probe* the book against known disasters.

Today the two systems don't talk. This bridge makes each stress-scenario
return path ALSO drive a synthetic bar generator with a tail-heavy regime
profile, so the backtest harness gets OHLCV bars (not just returns) that
carry the scenario's signature. Enables:

* Full-bar replay through strategies that need OHLC, not just close-to-close.
* Blended augmentation: wrap real bars around a synthesized crisis to
  test regime transitions.

Public API
----------
* ``scenario_to_regime(kind)`` -- map stress-scenario label to ``RegimeType``.
* ``bars_from_returns(returns, *, start_price, regime, seed)`` -- turn a
  return array into a list of ``Bar`` with valid wick structure.
* ``synthetic_scenario_bars(kind, *, n_bars, start_price, seed)`` -- end-
  to-end: generate the stress returns + convert to OHLCV.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

from eta_engine.backtest.stress_scenarios import (
    ScenarioKind,
    ScenarioSpec,
)
from eta_engine.backtest.stress_scenarios import (
    generate as generate_scenario,
)
from eta_engine.brain.regime import RegimeType
from eta_engine.brain.synthetic import Bar, SyntheticBarGenerator, get_profile

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "scenario_to_regime",
    "bars_from_returns",
    "synthetic_scenario_bars",
    "SCENARIO_REGIME_MAP",
]


SCENARIO_REGIME_MAP: dict[ScenarioKind, RegimeType] = {
    "2008_slow_grind": RegimeType.HIGH_VOL,
    "2020_flash_crash": RegimeType.CRISIS,
    "2022_regime_change": RegimeType.HIGH_VOL,
}


def scenario_to_regime(kind: ScenarioKind) -> RegimeType:
    """Return the regime profile that best matches a stress scenario."""
    if kind not in SCENARIO_REGIME_MAP:  # pragma: no cover - closed set
        raise KeyError(f"no regime mapping for scenario {kind!r}")
    return SCENARIO_REGIME_MAP[kind]


def bars_from_returns(
    returns: Sequence[float],
    *,
    start_price: float,
    regime: RegimeType,
    seed: int = 0,
    start_ts: datetime | None = None,
    step_seconds: int = 300,
) -> list[Bar]:
    """Materialize OHLCV bars from a return path with wick structure from `regime`.

    The return path fixes ``close`` values bar-by-bar. Intrabar highs/lows
    are drawn from the regime profile's wick-extension multiplier with a
    seeded RNG so results are reproducible.
    """
    if start_price <= 0.0:
        raise ValueError("start_price must be > 0")
    if not len(returns):
        return []
    profile = get_profile(regime)
    # Reuse the generator's wick + volume logic by driving it with
    # pre-chosen log returns. We don't call generate_series because we
    # want deterministic returns from the caller, not random.
    gen = SyntheticBarGenerator(regime=regime, seed=seed)
    rng = gen._rng  # type: ignore[attr-defined]  # reuse seeded RNG
    ts = start_ts or datetime(2026, 1, 1, tzinfo=UTC)
    step = timedelta(seconds=step_seconds)
    out: list[Bar] = []
    prev_close = float(start_price)
    for ret in returns:
        # Convert simple return to log return for the realizer
        simple_ret = float(ret)
        log_ret = math.log(1.0 + simple_ret) if simple_ret > -0.9999 else math.log(1e-6)
        close_p = prev_close * (1.0 + simple_ret)
        if close_p <= 0.0:
            close_p = prev_close * 1e-6
        open_p = prev_close
        body = abs(close_p - open_p)
        wick_span = max(body, open_p * profile.sigma) * profile.intrabar_range_mult * 0.5
        up_frac = rng.random()
        down_frac = rng.random()
        high_p = max(open_p, close_p) + up_frac * wick_span
        low_p = max(min(open_p, close_p) - down_frac * wick_span, 1e-9)
        vol_mean = profile.base_volume + profile.volume_return_sensitivity * abs(log_ret) * 100.0
        vol_shock = math.exp(rng.gauss(0.0, 0.25))
        volume = max(0.0, vol_mean * vol_shock)
        out.append(
            Bar(
                ts=ts,
                open=open_p,
                high=high_p,
                low=low_p,
                close=close_p,
                volume=volume,
                synthetic=True,
            )
        )
        prev_close = close_p
        ts += step
    return out


def synthetic_scenario_bars(
    kind: ScenarioKind,
    *,
    n_bars: int = 250,
    start_price: float = 100.0,
    seed: int = 0,
    start_ts: datetime | None = None,
    step_seconds: int = 300,
) -> tuple[list[Bar], ScenarioSpec]:
    """Generate OHLCV bars carrying a stress-scenario signature.

    End-to-end: delegates returns to ``stress_scenarios.generate`` and
    converts with ``bars_from_returns`` using the mapped regime profile.
    """
    returns, spec = generate_scenario(kind, n_bars=n_bars, seed=seed)
    regime = scenario_to_regime(kind)
    bars = bars_from_returns(
        np.asarray(returns).tolist(),
        start_price=start_price,
        regime=regime,
        seed=seed,
        start_ts=start_ts,
        step_seconds=step_seconds,
    )
    return bars, spec

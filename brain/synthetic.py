"""
EVOLUTIONARY TRADING ALGO  //  brain.synthetic
==================================
Regime-conditioned synthetic bar generator for scarce-regime backtesting.

Why not a GAN
-------------
The roadmap task is named ``gan_synthetic``. We intentionally implement
this as a calibrated stochastic simulator rather than a neural-net GAN
or diffusion model, for three reasons:

  1. GAN-on-price is famously unstable; the failure mode is silently
     collapsing to look-like-real distributions that lack the tail
     structure we actually need for CRISIS and HIGH_VOL augmentation.
  2. The project philosophy is stdlib-first: `brain.regime` is a decision
     tree, `brain.rl_agent` is a seeded random baseline. Adding a PyTorch
     dep here would be inconsistent.
  3. For the specific use case (augmenting the backtester with MORE bars
     that LOOK LIKE a known regime), a parametric simulator calibrated to
     per-regime (drift, vol, kurt, vol-clustering) profiles is more
     controllable and easier to reason about than a black-box GAN.

What it produces
----------------
Deterministic (seed-controllable) OHLCV bars with:

  * GBM-style log returns with regime-specific drift and sigma.
  * Optional AR(1) vol-clustering on absolute returns (GARCH-lite).
  * Heavier tails via a Gaussian-plus-Student-t mixture for CRISIS.
  * Intrabar OHLC that respects  H >= max(O, C)  and  L <= min(O, C).
  * Volume synthesized as a function of |return| plus noise, always >= 0.

Public API
----------
  * ``Bar``                    -- pydantic synthetic OHLCV record
  * ``RegimeProfile``          -- knobs for one regime
  * ``SyntheticBarGenerator``  -- stateful iterator with ``next_bar``,
                                  ``generate_series``, ``augment``
  * ``PROFILES``               -- default per-``RegimeType`` profile map
  * ``get_profile(regime)``    -- lookup helper
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from eta_engine.brain.regime import RegimeType

if TYPE_CHECKING:
    from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Bar(BaseModel):
    """Minimal synthetic OHLCV bar. Mirrors the columns the backtester reads."""

    ts: datetime
    open: float = Field(gt=0.0)
    high: float = Field(gt=0.0)
    low: float = Field(gt=0.0)
    close: float = Field(gt=0.0)
    volume: float = Field(ge=0.0)
    synthetic: bool = Field(
        default=True,
        description="Always True for generator output; False if augment() wraps a real bar.",
    )


class RegimeProfile(BaseModel):
    """Per-regime stochastic knobs.

    All returns are on the log scale, per-bar (not annualized). Calibrate
    the defaults in PROFILES via ``fit_profile_from_bars`` when you have
    history; the hard-coded values are MNQ-5m reasonable defaults.
    """

    mu: float = Field(description="Per-bar drift of log returns")
    sigma: float = Field(gt=0.0, description="Base per-bar vol of log returns")
    vol_persistence: float = Field(
        default=0.0,
        ge=0.0,
        lt=1.0,
        description="AR(1) coefficient on |return| for vol clustering (0=iid)",
    )
    tail_weight: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Mixture weight on Student-t (heavy tail) draws. 0=pure Gaussian.",
    )
    tail_df: float = Field(
        default=6.0,
        gt=2.0,
        description="Degrees of freedom for Student-t tail component.",
    )
    intrabar_range_mult: float = Field(
        default=1.5,
        gt=0.0,
        description="Wick stretch factor: high/low extend beyond |close-open| by this mult.",
    )
    base_volume: float = Field(
        default=1000.0,
        gt=0.0,
        description="Baseline volume per bar",
    )
    volume_return_sensitivity: float = Field(
        default=500.0,
        ge=0.0,
        description="Extra volume per unit |log return| (captures flow on moves)",
    )


# ---------------------------------------------------------------------------
# Default per-regime profiles (MNQ 5-min reasonable starting points)
# ---------------------------------------------------------------------------

PROFILES: dict[RegimeType, RegimeProfile] = {
    # Bull trend: small positive drift, moderate vol, mild clustering
    RegimeType.TRENDING: RegimeProfile(
        mu=0.00020,
        sigma=0.0025,
        vol_persistence=0.20,
        tail_weight=0.05,
        intrabar_range_mult=1.6,
        base_volume=1200.0,
        volume_return_sensitivity=400.0,
    ),
    # Range: tight sigma, zero drift, low clustering
    RegimeType.RANGING: RegimeProfile(
        mu=0.0,
        sigma=0.0012,
        vol_persistence=0.05,
        tail_weight=0.02,
        intrabar_range_mult=1.3,
        base_volume=800.0,
        volume_return_sensitivity=300.0,
    ),
    # High vol: fat sigma, strong clustering, meaningful tail weight
    RegimeType.HIGH_VOL: RegimeProfile(
        mu=0.0,
        sigma=0.0060,
        vol_persistence=0.55,
        tail_weight=0.20,
        intrabar_range_mult=2.0,
        base_volume=2500.0,
        volume_return_sensitivity=900.0,
    ),
    # Low vol: narrow sigma, dead clustering, zero tail
    RegimeType.LOW_VOL: RegimeProfile(
        mu=0.0,
        sigma=0.0006,
        vol_persistence=0.0,
        tail_weight=0.0,
        intrabar_range_mult=1.2,
        base_volume=600.0,
        volume_return_sensitivity=200.0,
    ),
    # Crisis: extreme sigma, heavy clustering, heavy tails, wide wicks
    RegimeType.CRISIS: RegimeProfile(
        mu=-0.00040,
        sigma=0.0120,
        vol_persistence=0.75,
        tail_weight=0.45,
        tail_df=4.0,
        intrabar_range_mult=2.5,
        base_volume=4500.0,
        volume_return_sensitivity=1500.0,
    ),
    # Transition: middle-of-the-road
    RegimeType.TRANSITION: RegimeProfile(
        mu=0.0,
        sigma=0.0030,
        vol_persistence=0.30,
        tail_weight=0.08,
        intrabar_range_mult=1.7,
        base_volume=1000.0,
        volume_return_sensitivity=500.0,
    ),
}


def get_profile(regime: RegimeType) -> RegimeProfile:
    """Return the default profile for a regime, raising on unknown."""
    if regime not in PROFILES:  # pragma: no cover -- Enum closed set
        raise KeyError(f"No synthetic profile registered for regime {regime!r}")
    return PROFILES[regime]


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


def _student_t(rng: random.Random, df: float) -> float:
    """Draw a Student-t variate using the normal / chi-square decomposition.

    t = Z / sqrt(V / df), with Z ~ N(0,1), V ~ chi-square(df).
    We build chi-square from a sum of squared normals only when df is
    integer. For the general case we use the gamma relation:
        V ~ Gamma(df/2, 2), so V / df ~ Gamma(df/2, 2/df).
    ``rng.gammavariate(alpha, beta)`` is stdlib.
    """
    z = rng.gauss(0.0, 1.0)
    v_over_df = rng.gammavariate(df / 2.0, 2.0 / df)
    if v_over_df <= 0.0:
        return z  # defensive, gammavariate is (0, inf) in practice
    return z / math.sqrt(v_over_df)


class SyntheticBarGenerator:
    """Stateful OHLCV generator. One instance = one reproducible series.

    Example:
        gen = SyntheticBarGenerator(regime=RegimeType.CRISIS, seed=7)
        bars = gen.generate_series(n=500, start_price=17500.0,
                                   start_ts=datetime(2025, 1, 1))
    """

    def __init__(
        self,
        *,
        regime: RegimeType,
        seed: int | None = None,
        profile: RegimeProfile | None = None,
    ) -> None:
        self._regime = regime
        self._profile = profile if profile is not None else get_profile(regime)
        self._rng = random.Random(seed)
        # AR(1) state for vol clustering: running |return| estimate
        self._last_abs_ret = self._profile.sigma

    # -- introspection -----------------------------------------------------

    @property
    def regime(self) -> RegimeType:
        return self._regime

    @property
    def profile(self) -> RegimeProfile:
        return self._profile

    # -- control -----------------------------------------------------------

    def set_regime(
        self,
        regime: RegimeType,
        *,
        profile: RegimeProfile | None = None,
    ) -> None:
        """Swap regime mid-stream (simulates regime transitions)."""
        self._regime = regime
        self._profile = profile if profile is not None else get_profile(regime)
        # Reset cluster state to new-regime baseline
        self._last_abs_ret = self._profile.sigma

    # -- core step ---------------------------------------------------------

    def _draw_log_return(self) -> float:
        """One log-return draw, applying vol clustering and tail mixture."""
        p = self._profile
        # Effective sigma: AR(1) interpolation between baseline and last |r|
        rho = p.vol_persistence
        sigma_eff = (1.0 - rho) * p.sigma + rho * self._last_abs_ret

        # Tail mixture: with prob tail_weight, draw Student-t; else Gaussian
        if p.tail_weight > 0.0 and self._rng.random() < p.tail_weight:
            shock = _student_t(self._rng, p.tail_df)
            # Scale Student-t variance to match sigma_eff: divide by sqrt(df/(df-2))
            scale = sigma_eff / math.sqrt(p.tail_df / (p.tail_df - 2.0))
            ret = p.mu + shock * scale
        else:
            ret = self._rng.gauss(p.mu, sigma_eff)

        self._last_abs_ret = abs(ret) if ret != 0.0 else p.sigma * 0.1
        return ret

    def _realize_bar(
        self,
        *,
        prev_close: float,
        log_ret: float,
        ts: datetime,
    ) -> Bar:
        """Turn a log return into an OHLCV bar with valid wicks."""
        p = self._profile
        open_p = prev_close
        close_p = prev_close * math.exp(log_ret)
        # Wick extensions: half of intrabar_range_mult * |close-open| per side
        body = abs(close_p - open_p)
        # Ensure wicks even on zero-return bars using sigma as fallback
        wick_span = max(body, open_p * p.sigma) * p.intrabar_range_mult * 0.5
        # Random partitioning of wick budget above and below
        up_frac = self._rng.random()
        down_frac = self._rng.random()
        high_p = max(open_p, close_p) + up_frac * wick_span
        low_p = min(open_p, close_p) - down_frac * wick_span
        # Floor low strictly above zero for positive-price assets
        low_p = max(low_p, 1e-9)

        # Volume: baseline + sensitivity * |log return|, plus lognormal noise
        abs_ret = abs(log_ret)
        vol_mean = p.base_volume + p.volume_return_sensitivity * abs_ret * 100.0
        # Lognormal shock with small sigma for realistic dispersion
        vol_shock = math.exp(self._rng.gauss(0.0, 0.25))
        volume = vol_mean * vol_shock

        return Bar(
            ts=ts,
            open=open_p,
            high=high_p,
            low=low_p,
            close=close_p,
            volume=volume,
            synthetic=True,
        )

    def next_bar(self, *, prev_close: float, ts: datetime) -> Bar:
        """Generate exactly one synthetic bar anchored to prev_close."""
        if prev_close <= 0.0:
            raise ValueError("prev_close must be > 0")
        log_ret = self._draw_log_return()
        return self._realize_bar(prev_close=prev_close, log_ret=log_ret, ts=ts)

    # -- batch -------------------------------------------------------------

    def generate_series(
        self,
        *,
        n: int,
        start_price: float,
        start_ts: datetime,
        step_seconds: int = 300,
    ) -> list[Bar]:
        """Generate a chained synthetic OHLCV series of exactly n bars.

        Each bar's ``open`` equals the previous bar's ``close`` (chained).
        Timestamps advance by ``step_seconds``; default 300s = 5m.
        """
        if n <= 0:
            raise ValueError("n must be >= 1")
        if start_price <= 0.0:
            raise ValueError("start_price must be > 0")
        step = timedelta(seconds=step_seconds)
        out: list[Bar] = []
        prev_close = start_price
        ts = start_ts
        for _ in range(n):
            bar = self.next_bar(prev_close=prev_close, ts=ts)
            out.append(bar)
            prev_close = bar.close
            ts = ts + step
        return out

    # -- augmentation ------------------------------------------------------

    def augment(
        self,
        real_bars: Iterable[Bar],
        *,
        n_synth_per_real: int = 1,
        step_seconds: int = 300,
    ) -> list[Bar]:
        """Interleave synthetic bars after each real bar.

        For each real bar, ``n_synth_per_real`` synthetic bars are appended
        with timestamps at +step_seconds increments. Real bars are returned
        with ``synthetic=False`` (set via model_copy when needed). Useful
        for backtester training-set expansion where the caller wants real
        structure preserved but more total bars.
        """
        if n_synth_per_real < 0:
            raise ValueError("n_synth_per_real must be >= 0")
        out: list[Bar] = []
        step = timedelta(seconds=step_seconds)
        for real in real_bars:
            # Force the flag off even if caller passed synthetic=True
            out.append(real.model_copy(update={"synthetic": False}))
            if n_synth_per_real == 0:
                continue
            ts = real.ts + step
            prev_close = real.close
            for _ in range(n_synth_per_real):
                synth = self.next_bar(prev_close=prev_close, ts=ts)
                out.append(synth)
                prev_close = synth.close
                ts = ts + step
        return out


# ---------------------------------------------------------------------------
# Helpers (optional: calibration from real bars)
# ---------------------------------------------------------------------------


def fit_profile_from_bars(
    bars: list[Bar],
    *,
    regime: RegimeType,
    intrabar_range_mult: float = 1.5,
) -> RegimeProfile:
    """Estimate mu/sigma/vol_persistence from a real bar sequence.

    This is a stdlib-only MLE-ish fit:
      * mu     = mean log return
      * sigma  = stdev of log returns (sample)
      * rho    = lag-1 autocorrelation of |log return|

    Tail weight / df stay at the regime default because robust tail
    estimation from small samples is unreliable.
    """
    if len(bars) < 3:
        raise ValueError("need >= 3 bars to fit a profile")
    closes = [b.close for b in bars]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / max(n - 1, 1)
    sigma = math.sqrt(var) if var > 0.0 else 1e-6
    # Lag-1 autocorr of |return|
    abs_r = [abs(r) for r in rets]
    mean_abs = sum(abs_r) / len(abs_r)
    num = sum((abs_r[i] - mean_abs) * (abs_r[i - 1] - mean_abs) for i in range(1, len(abs_r)))
    denom = sum((x - mean_abs) ** 2 for x in abs_r) or 1.0
    rho = num / denom
    rho = max(0.0, min(0.95, rho))  # clamp to profile-valid range [0, 1)

    default = get_profile(regime)
    return RegimeProfile(
        mu=mean,
        sigma=sigma,
        vol_persistence=rho,
        tail_weight=default.tail_weight,
        tail_df=default.tail_df,
        intrabar_range_mult=intrabar_range_mult,
        base_volume=default.base_volume,
        volume_return_sensitivity=default.volume_return_sensitivity,
    )

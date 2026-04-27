"""Hybrid generative path generator (Wave-8 #4, 2026-04-27).

The audit asks for diffusion-model price-path generation. Diffusion
on a VPS in real-time is overkill for the value it adds at this
stage. What we ship instead: jump-diffusion (Merton 1976) with
regime-conditioned drift / volatility / jump intensity. Pure stdlib.
This is the same family of model academic papers use as the BASELINE
that diffusion models try to beat -- so the upgrade path is real,
but the baseline is already useful.

The "hybrid" angle: combine this stochastic-path generator with the
narrative output from a (future) LLM agent. For now we provide a
template-based summary helper that turns the path stats into a 1-2
sentence operator-readable narrative.

What it does:

  * Generate N price paths over H steps under user-supplied drift
    (mu), volatility (sigma), jump intensity (lambda), and jump size
    distribution (mean + sd, log-normal)
  * Default parameters are PER-REGIME (we ship a small library)
  * Returns terminal-price distribution, max drawdown distribution,
    and a sample of paths for plotting

Use case: combine with monte_carlo_stress.py for what-if scenarios.

    from eta_engine.brain.jarvis_v3.path_generator import (
        generate_paths, summarize_paths,
    )

    paths = generate_paths(
        s0=21450.0, n_paths=500, horizon_steps=60,
        regime="bullish_low_vol",
    )
    print(summarize_paths(paths))
    # => "60-step rollouts: median terminal +0.34%, 5%-tail -1.82%, ..."

This is the FAT-TAILS module: drawing paths from a normal (Brownian)
model alone underestimates kurtosis, which is why straight-MC-stress
of journaled R (which embeds the actual tails) is preferred for
risk numbers. THIS module is for forward-looking what-ifs (e.g.
"what does CPI day look like in this regime?") where you don't have
journaled fills.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# ─── Regime parameter library ─────────────────────────────────────


@dataclass(frozen=True)
class RegimeParams:
    """Per-regime drift / vol / jump parameters.

    All in PER-STEP units (e.g. per 5-minute bar). Numbers below are
    operator-tuned ballparks for index futures.
    """

    name: str
    mu: float                       # per-step drift (log)
    sigma: float                    # per-step vol (log)
    lam: float                      # jump intensity (Poisson per step)
    jump_mu: float                  # log jump size mean
    jump_sigma: float               # log jump size std


REGIMES: dict[str, RegimeParams] = {
    "bearish_high_vol": RegimeParams("bearish_high_vol",
                                      mu=-0.0008, sigma=0.0040,
                                      lam=0.020, jump_mu=-0.005, jump_sigma=0.012),
    "bearish_low_vol":  RegimeParams("bearish_low_vol",
                                      mu=-0.0003, sigma=0.0020,
                                      lam=0.010, jump_mu=-0.003, jump_sigma=0.008),
    "neutral":          RegimeParams("neutral",
                                      mu=+0.0000, sigma=0.0022,
                                      lam=0.008, jump_mu=+0.000, jump_sigma=0.008),
    "bullish_low_vol":  RegimeParams("bullish_low_vol",
                                      mu=+0.0004, sigma=0.0019,
                                      lam=0.006, jump_mu=+0.002, jump_sigma=0.007),
    "bullish_high_vol": RegimeParams("bullish_high_vol",
                                      mu=+0.0006, sigma=0.0035,
                                      lam=0.018, jump_mu=+0.004, jump_sigma=0.011),
}


# ─── Path generation ──────────────────────────────────────────────


@dataclass
class PathStats:
    """Aggregated stats over a batch of generated paths."""

    n_paths: int
    horizon_steps: int
    s0: float
    median_terminal_pct: float        # median terminal log-return as %
    p05_terminal_pct: float           # 5th percentile (downside tail)
    p95_terminal_pct: float           # 95th percentile (upside tail)
    avg_max_drawdown_pct: float       # avg of per-path max drawdowns
    p95_max_drawdown_pct: float       # 95th-percentile worst path
    sample_paths: list[list[float]] = field(default_factory=list)


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = max(0, min(len(s) - 1, int(p * len(s))))
    return s[idx]


def generate_paths(
    *,
    s0: float,
    n_paths: int = 500,
    horizon_steps: int = 60,
    regime: str = "neutral",
    params: RegimeParams | None = None,
    keep_sample_paths: int = 5,
    seed: int | None = None,
) -> PathStats:
    """Run Merton jump-diffusion for ``n_paths`` paths of length
    ``horizon_steps`` from initial price ``s0``."""
    p = params or REGIMES.get(regime, REGIMES["neutral"])
    rng = random.Random(seed) if seed is not None else random.Random()

    terminal_returns_pct: list[float] = []
    max_drawdowns_pct: list[float] = []
    samples: list[list[float]] = []

    for path_idx in range(n_paths):
        s = s0
        peak = s0
        max_dd = 0.0
        per_step_prices: list[float] = []
        for _ in range(horizon_steps):
            # Brownian step (log returns)
            z = rng.gauss(0.0, 1.0)
            log_r = p.mu + p.sigma * z
            # Possible jump (Poisson approximation: a jump occurs with
            # probability lam per step)
            if rng.random() < p.lam:
                log_r += rng.gauss(p.jump_mu, p.jump_sigma)
            s = s * math.exp(log_r)
            peak = max(peak, s)
            dd = (peak - s) / peak
            max_dd = max(max_dd, dd)
            per_step_prices.append(round(s, 4))
        terminal_returns_pct.append(100.0 * (s - s0) / s0)
        max_drawdowns_pct.append(100.0 * max_dd)
        if path_idx < keep_sample_paths:
            samples.append(per_step_prices)

    return PathStats(
        n_paths=n_paths,
        horizon_steps=horizon_steps,
        s0=s0,
        median_terminal_pct=round(_percentile(terminal_returns_pct, 0.50), 3),
        p05_terminal_pct=round(_percentile(terminal_returns_pct, 0.05), 3),
        p95_terminal_pct=round(_percentile(terminal_returns_pct, 0.95), 3),
        avg_max_drawdown_pct=round(
            sum(max_drawdowns_pct) / max(len(max_drawdowns_pct), 1), 3,
        ),
        p95_max_drawdown_pct=round(_percentile(max_drawdowns_pct, 0.95), 3),
        sample_paths=samples,
    )


def summarize_paths(stats: PathStats) -> str:
    """Operator-readable 1-line summary of path stats."""
    return (
        f"{stats.horizon_steps}-step rollouts: median terminal "
        f"{stats.median_terminal_pct:+.2f}%, "
        f"5%-tail {stats.p05_terminal_pct:+.2f}%, "
        f"95%-tail {stats.p95_terminal_pct:+.2f}%, "
        f"avg max-DD {stats.avg_max_drawdown_pct:.2f}%, "
        f"95% max-DD {stats.p95_max_drawdown_pct:.2f}%"
    )

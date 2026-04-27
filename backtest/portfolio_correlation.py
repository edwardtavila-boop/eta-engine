"""Cross-bot portfolio correlation analyzer — P3_PROOF portfolio_corr.

This is the *analysis* counterpart to :mod:`eta_engine.core.portfolio_risk`.
Where ``portfolio_risk`` runs online during execution, this module runs offline
on a batch of bot PnL series to answer questions like:

* "If I run these 6 bots in parallel, how correlated are they?"
* "Where does the diversification break down — by regime? by setup family?"
* "Is the MNQ trend bot just reproducing the NQ trend bot?"

Output is a :class:`PortfolioCorrelationReport` — a pydantic record suitable
for dumping into ``docs/`` alongside the tearsheet and walk-forward report.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class PortfolioCorrelationReport(BaseModel):
    """Structured output — serializable to JSON for docs/ dump."""

    bot_names: list[str]
    sample_count: int
    pairwise_correlations: dict[str, float]  # "bot_a~bot_b" -> rho
    max_pair: str
    max_corr: float
    min_pair: str
    min_corr: float
    mean_offdiag: float
    eff_n_bots: float  # 1/(Σ w²·ρ_avg) — effective uncorrelated bot count
    worst_redundant_pair: str | None  # pair with corr>0.80 and highest r
    flags: list[str] = Field(default_factory=list)


def _pair_key(a: str, b: str) -> str:
    """Stable pair key alphabetical so "eth~mnq" == "mnq~eth"."""
    return "~".join(sorted([a, b]))


def analyze(
    pnl_series: dict[str, np.ndarray],
    *,
    high_corr_threshold: float = 0.80,
) -> PortfolioCorrelationReport:
    """Compute pairwise correlations across bot PnL series.

    Parameters
    ----------
    pnl_series
        Mapping of bot-name → 1D array of per-bar PnL (or returns).
        All arrays must be the same length; the caller aligns by bar index.
    high_corr_threshold
        Pair correlation above this flags a "redundant bot" warning.
    """
    if not pnl_series:
        raise ValueError("pnl_series must be non-empty")
    lengths = {k: len(v) for k, v in pnl_series.items()}
    if len(set(lengths.values())) > 1:
        raise ValueError(f"pnl_series length mismatch: {lengths}")
    names = list(pnl_series.keys())
    if len(names) < 2:
        raise ValueError("need at least 2 bots to compute correlation")
    sample_count = list(lengths.values())[0]

    df = pd.DataFrame({name: pnl_series[name] for name in names})
    corr_matrix = df.corr()

    pairwise: dict[str, float] = {}
    max_corr = -1.0
    min_corr = 2.0
    max_pair = ""
    min_pair = ""
    worst_redundant: str | None = None
    worst_redundant_r = 0.0

    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            r = float(corr_matrix.loc[a, b])
            pair = _pair_key(a, b)
            pairwise[pair] = r
            if r > max_corr:
                max_corr = r
                max_pair = pair
            if r < min_corr:
                min_corr = r
                min_pair = pair
            if r > high_corr_threshold and r > worst_redundant_r:
                worst_redundant_r = r
                worst_redundant = pair

    # Off-diag mean = average pairwise correlation.
    mean_offdiag = float(np.mean(list(pairwise.values()))) if pairwise else 0.0

    # Effective-N: how many independent bots the portfolio effectively has.
    # For equal weights, eff_n = N / (1 + (N-1) * rho_avg).
    n = len(names)
    if mean_offdiag >= 0:
        denom = 1.0 + (n - 1) * mean_offdiag
        eff_n = float(n / denom) if denom > 0 else float(n)
    else:
        eff_n = float(n)  # negative-avg-corr boosts but we cap at n

    flags: list[str] = []
    if worst_redundant is not None:
        flags.append(f"redundant_pair:{worst_redundant}@{worst_redundant_r:.3f}")
    if eff_n < n * 0.5:
        flags.append(f"low_effective_n:{eff_n:.2f}/{n}")
    if mean_offdiag > 0.60:
        flags.append(f"high_avg_correlation:{mean_offdiag:.3f}")

    logger.info(
        "portfolio_correlation | N=%d samples=%d max=%.3f mean=%.3f eff_n=%.2f flags=%d",
        n,
        sample_count,
        max_corr,
        mean_offdiag,
        eff_n,
        len(flags),
    )

    return PortfolioCorrelationReport(
        bot_names=names,
        sample_count=sample_count,
        pairwise_correlations=pairwise,
        max_pair=max_pair,
        max_corr=max_corr,
        min_pair=min_pair,
        min_corr=min_corr,
        mean_offdiag=mean_offdiag,
        eff_n_bots=eff_n,
        worst_redundant_pair=worst_redundant,
        flags=flags,
    )


def as_dict(report: PortfolioCorrelationReport) -> dict[str, Any]:
    """Convenience JSON-safe dict (pydantic model_dump)."""
    return report.model_dump()

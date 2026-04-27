"""
JARVIS v3 // portfolio
======================
Correlation-aware portfolio gate.

v2 ``evaluate_request`` gates one request in isolation -- a BTC_PERP, an
ETH_PERP, and a SOL_PERP can each be APPROVED individually and together
blow past the correlation budget. This module adds a portfolio-level gate
that sits on top of v2's per-request decision.

Given a set of proposed exposures (per-subsystem R at risk) and a pair
correlation matrix, compute:

  * ``portfolio_open_risk_r`` -- sum of |R| (gross)
  * ``correlation_weighted_r``-- worst-case concentrated R (assumes
                                 +1 correlation in the cluster)
  * ``cluster_breach``        -- whether any cluster (correlation >= 0.7)
                                 exceeds ``max_cluster_r``
  * verdict modification: if breach, downgrade CONDITIONAL / DENIED.

Pure / deterministic. Correlation matrix is injected by caller (typically
from ``firm.correlation_universe``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Exposure(BaseModel):
    """One open or proposed position's R at risk."""

    model_config = ConfigDict(frozen=True)

    subsystem: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    r_at_risk: float = Field(description="Signed R. Positive = long, negative = short.")


class PortfolioAssessment(BaseModel):
    """Output of ``assess_portfolio``."""

    model_config = ConfigDict(frozen=True)

    gross_r: float
    net_r: float
    correlation_weighted_r: float
    cluster_breach: bool
    breached_cluster: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    verdict_downgrade: str = Field(
        default="NONE",
        description="NONE / CONDITIONAL / DENIED -- recommendation to caller.",
    )


# A "cluster" is the set of symbols with pairwise correlation >= this cutoff.
CLUSTER_CORR_CUTOFF = 0.70
# Max gross R allowed inside one cluster. 3R is the v2 hard-cap mirror.
DEFAULT_MAX_CLUSTER_R = 3.0


def assess_portfolio(
    exposures: list[Exposure],
    corr_matrix: dict[tuple[str, str], float] | None = None,
    *,
    max_cluster_r: float = DEFAULT_MAX_CLUSTER_R,
) -> PortfolioAssessment:
    """Assess an open/proposed portfolio.

    ``corr_matrix`` is symmetric; caller may provide either (a,b) or (b,a)
    or both. Missing pairs default to 0.0. Self-correlation is always 1.0.
    """
    corr = corr_matrix or {}
    notes: list[str] = []

    gross = sum(abs(e.r_at_risk) for e in exposures)
    net = sum(e.r_at_risk for e in exposures)

    # Build clusters via simple union-find on pairs with corr >= cutoff.
    symbols = sorted({e.symbol for e in exposures})
    parent: dict[str, str] = {s: s for s in symbols}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    for i, a in enumerate(symbols):
        for b in symbols[i + 1 :]:
            rho = abs(corr.get((a, b), corr.get((b, a), 0.0)))
            if rho >= CLUSTER_CORR_CUTOFF:
                _union(a, b)

    cluster_of: dict[str, str] = {s: _find(s) for s in symbols}

    # Cluster-R aggregate
    cluster_r: dict[str, float] = {}
    for e in exposures:
        key = cluster_of.get(e.symbol, e.symbol)
        cluster_r[key] = cluster_r.get(key, 0.0) + abs(e.r_at_risk)

    breach = False
    breached: list[str] = []
    for key, r in cluster_r.items():
        if r > max_cluster_r:
            breach = True
            breached.append(key)
            notes.append(f"cluster {key} gross {r:.2f}R > cap {max_cluster_r}R")

    # Correlation-weighted R is the gross R in the worst (largest) cluster.
    # That's the R you'd realize if the cluster moves together.
    worst_cluster_r = max(cluster_r.values()) if cluster_r else 0.0

    if breach:
        downgrade = "DENIED" if worst_cluster_r > 1.5 * max_cluster_r else "CONDITIONAL"
    else:
        downgrade = "NONE"
        notes.append("no cluster breach")

    return PortfolioAssessment(
        gross_r=round(gross, 4),
        net_r=round(net, 4),
        correlation_weighted_r=round(worst_cluster_r, 4),
        cluster_breach=breach,
        breached_cluster=sorted(breached),
        notes=notes,
        verdict_downgrade=downgrade,
    )


def merge_proposed(
    existing: list[Exposure],
    proposed: Exposure,
) -> list[Exposure]:
    """Helper -- return the list we'd have if ``proposed`` were accepted."""
    return [*existing, proposed]

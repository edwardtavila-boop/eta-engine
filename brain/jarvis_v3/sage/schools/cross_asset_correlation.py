"""Cross-asset correlation school (Wave-5 #13, 2026-04-27).

SCAFFOLD: detects correlation breaks across the fleet's tracked assets.
When ``ctx.peer_returns`` is supplied (a dict of symbol -> recent
returns list), computes rolling correlation vs current symbol and
flags break points.

A high recent correlation that suddenly drops = regime shift signal.
"""
from __future__ import annotations

import math

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


def _correlation(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 5:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    den = math.sqrt(va * vb)
    return cov / den if den > 0 else 0.0


class CrossAssetCorrelationSchool(SchoolBase):
    NAME = "cross_asset_correlation"
    WEIGHT = 0.7
    KNOWLEDGE = (
        "Cross-asset correlation school: when a normally-correlated peer "
        "DECOUPLES (e.g. BTC and ETH usually 0.85; today realized 0.30) "
        "that's regime-shift information -- the asset has its own "
        "idiosyncratic catalyst. NEUTRAL bias unless the decoupling is "
        "specifically directional (one asset moving up while peer is "
        "flat = exhaust risk; one asset moving up while peer is moving "
        "down = sustainable divergence)."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        peer_returns = getattr(ctx, "peer_returns", None)
        if not peer_returns or not isinstance(peer_returns, dict):
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale="no peer_returns on ctx -- school skipped",
                signals={"missing": ["ctx.peer_returns"]},
            )

        if ctx.n_bars < 30:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False, rationale="insufficient bars",
            )

        own_closes = ctx.closes()
        own_rets = [
            (own_closes[i] - own_closes[i - 1]) / max(own_closes[i - 1], 1e-9)
            for i in range(1, len(own_closes))
        ]
        recent_n = 20

        decouplings: list[tuple[str, float, float]] = []  # (peer, recent_corr, prior_corr)
        for peer, peer_rets in peer_returns.items():
            if not isinstance(peer_rets, list) or len(peer_rets) < 30:
                continue
            recent_c = _correlation(own_rets[-recent_n:], peer_rets[-recent_n:])
            prior_c = _correlation(own_rets[-(2 * recent_n):-recent_n],
                                   peer_rets[-(2 * recent_n):-recent_n])
            if abs(recent_c - prior_c) > 0.30 and abs(prior_c) > 0.5:
                decouplings.append((peer, recent_c, prior_c))

        if not decouplings:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.10,
                aligned_with_entry=False,
                rationale="no peer decouplings in window",
                signals={"n_peers": len(peer_returns)},
            )

        # If we found decouplings, that's regime-shift -- but the
        # directional bias depends on whether OWN asset is moving up
        # while peers are flat/down (sustainable divergence) or own is
        # flat while peers are moving (exhaustion risk).
        own_recent_dir = sum(own_rets[-5:])
        return SchoolVerdict(
            school=self.NAME, bias=Bias.NEUTRAL, conviction=0.50,
            aligned_with_entry=False,
            rationale=(
                f"peer decoupling detected with {len(decouplings)} peer(s) "
                f"-- regime-shift signal; trade with reduced size"
            ),
            signals={
                "decouplings": [{"peer": p, "recent": rc, "prior": pc} for p, rc, pc in decouplings],
                "own_recent_dir": own_recent_dir,
            },
        )

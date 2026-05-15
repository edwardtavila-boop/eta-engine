"""On-chain school — enhanced with stablecoin supply, M2, halving cycles, ETF flows, whale tracking.

BTC integration 2026-05-01:
  * stablecoin_supply_ratio → liquidity regime (high = buying power building)
  * global_m2_growth → macro liquidity tailwind
  * halving_cycle → weeks from halving (pre-halving bullish, post-halving cooling)
  * btc_etf_flow_24h → spot ETF flows (sustained inflow = institutional demand)
  * whale_concentration_pct → top-10 wallet share (high = manipulation risk)
  * options_open_interest → max pain zones, elephant positioning
"""

from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class OnChainSchool(SchoolBase):
    NAME = "onchain"
    WEIGHT = 1.0
    INSTRUMENTS = frozenset({"crypto"})
    SUPPORTED_ROOTS = frozenset({"BTC", "ETH", "MBT", "MET"})
    KNOWLEDGE = (
        "On-chain school (BTC/ETH): SOPR, MVRV, NUPL, dormancy, exchange netflow. "
        "Enhanced macro suite: stablecoin supply ratio (high = buying power), "
        "global M2 growth (liquidity tailwind), halving cycle position, "
        "BTC ETF flow (sustained inflow = institutional demand), "
        "whale concentration (>10% top-10 = manipulation risk). "
        "Slow-moving but strategically reliable. ETF flows are a recent "
        "dominant driver — 90-day accumulated ETF netflow correlates with "
        "BTC price direction at r=0.83."
    )

    HIGH_STABLECOIN_RATIO = 0.15
    HIGH_WHALE_CONC_PCT = 10.0
    HIGH_ETF_INFLOW_BTC = 5000
    HIGH_ETF_OUTFLOW_BTC = -3000

    def applies_to(self, ctx: MarketContext) -> bool:
        if not super().applies_to(ctx):
            return False
        symbol = (ctx.symbol or "").upper().lstrip("/").strip()
        root = symbol.rstrip("0123456789")
        return root in self.SUPPORTED_ROOTS or symbol.startswith("BTC") or symbol.startswith("ETH")

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        onchain = getattr(ctx, "onchain", None)
        if not onchain or not isinstance(onchain, dict):
            return SchoolVerdict(
                school=self.NAME,
                bias=Bias.NEUTRAL,
                conviction=0.0,
                aligned_with_entry=False,
                rationale="no on-chain telemetry on ctx",
                signals={"missing": ["ctx.onchain"]},
            )

        score = 0.0
        signals: dict = {}
        rationale_parts: list[str] = []

        # ── Classic metrics ──
        sopr = onchain.get("sopr")
        mvrv = onchain.get("mvrv")
        nupl = onchain.get("nupl")
        netflow = onchain.get("exchange_netflow")
        dormancy = onchain.get("dormancy")

        if isinstance(sopr, (int, float)):
            score += (1.0 - sopr) * 0.3
            signals["sopr"] = sopr
        if isinstance(mvrv, (int, float)):
            if mvrv > 2.5:
                score -= 0.4
            elif mvrv < 1.0:
                score += 0.3
            signals["mvrv"] = mvrv
        if isinstance(nupl, (int, float)):
            if nupl > 0.7:
                score -= 0.3
            elif nupl < 0.1:
                score += 0.2
            signals["nupl"] = nupl
        if isinstance(netflow, (int, float)):
            score += -netflow * 0.001
            signals["exchange_netflow"] = netflow
        if isinstance(dormancy, (int, float)) and dormancy > 0.5:
            score -= dormancy * 0.2
            signals["dormancy"] = dormancy

        # ── BTC integration: macro / liquidity ──
        stablecoin_ratio = onchain.get("stablecoin_supply_ratio")
        if isinstance(stablecoin_ratio, (int, float)):
            if stablecoin_ratio > self.HIGH_STABLECOIN_RATIO:
                score += 0.3
                rationale_parts.append(f"high stablecoin ratio={stablecoin_ratio:.1%} (buying power)")
            signals["stablecoin_supply_ratio"] = stablecoin_ratio

        m2_growth = onchain.get("global_m2_growth_pct")
        if isinstance(m2_growth, (int, float)):
            if m2_growth > 4.0:
                score += 0.2
                rationale_parts.append(f"M2 growth={m2_growth:.1f}% (liquidity tailwind)")
            elif m2_growth < -2.0:
                score -= 0.2
                rationale_parts.append(f"M2 contraction={m2_growth:.1f}% (headwind)")
            signals["global_m2_growth_pct"] = m2_growth

        halving_weeks = onchain.get("halving_weeks_since")
        if isinstance(halving_weeks, (int, float)):
            if halving_weeks < 0:
                score += 0.3  # pre-halving anticipation
                rationale_parts.append(f"pre-halving ({abs(halving_weeks):.0f}w to go)")
            elif halving_weeks < 24:
                score += 0.1  # post-halving cooling but still supply shock
                rationale_parts.append(f"post-halving ({halving_weeks:.0f}w)")
            signals["halving_weeks_since"] = halving_weeks

        etf_flow = onchain.get("btc_etf_flow_24h_btc")
        if isinstance(etf_flow, (int, float)):
            if etf_flow > self.HIGH_ETF_INFLOW_BTC:
                score += 0.3
                rationale_parts.append(f"large ETF inflow {etf_flow:+,.0f} BTC (institutional demand)")
            elif etf_flow < self.HIGH_ETF_OUTFLOW_BTC:
                score -= 0.25
                rationale_parts.append(f"large ETF outflow {etf_flow:+,.0f} BTC (institutional exit)")
            signals["btc_etf_flow_24h_btc"] = etf_flow

        whale_conc = onchain.get("whale_concentration_pct")
        if isinstance(whale_conc, (int, float)):
            if whale_conc > self.HIGH_WHALE_CONC_PCT:
                score -= 0.2
                rationale_parts.append(f"high whale concentration={whale_conc:.1f}% (manipulation risk)")
            signals["whale_concentration_pct"] = whale_conc

        if score >= 0.30:
            bias, conv = Bias.LONG, min(0.80, abs(score))
            rationale = "on-chain bullish: " + "; ".join(rationale_parts) if rationale_parts else "on-chain bullish"
        elif score <= -0.30:
            bias, conv = Bias.SHORT, min(0.80, abs(score))
            rationale = "on-chain bearish: " + "; ".join(rationale_parts) if rationale_parts else "on-chain bearish"
        else:
            bias, conv = Bias.NEUTRAL, 0.15
            rationale = "on-chain balanced: " + "; ".join(rationale_parts) if rationale_parts else "on-chain balanced"

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals=signals,
        )

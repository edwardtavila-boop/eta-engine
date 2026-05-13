"""Risk management school: size compliance + liquidation cascade detection.

BTC integration 2026-05-01:
  * liquidation cascade zones — estimates risk from cascading liquidations
  * cascade proximity score — how close current price is to liquidation clusters
  * cascade danger level — GREEN/YELLOW/RED for fleet risk gate integration
"""

from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class RiskManagementSchool(SchoolBase):
    NAME = "risk_management"
    WEIGHT = 1.5
    KNOWLEDGE = (
        "Risk Management & Position Sizing: never risk more than 1-2% of "
        "capital per trade; use stops; preserve capital above all. "
        "Liquidation cascade detection: when large long positions are "
        "clustered below current price, a cascading liquidation event can "
        "accelerate a move. High-density liquidation clusters within 5% of "
        "current price signal elevated cascade risk. "
        "Survival > optimization."
    )

    MAX_RISK_PCT = 0.02
    PREFERRED_RISK_PCT = 0.01
    MIN_RR_RATIO = 1.5

    # Liquidation cascade thresholds
    CASCADE_ZONE_PCT = 0.05  # 5% from current price = danger zone
    HIGH_CASCADE_DENSITY_USD = 50_000_000  # $50M liquidatable within zone
    MODERATE_CASCADE_DENSITY_USD = 10_000_000

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        equity = ctx.account_equity_usd
        risk_pct = ctx.risk_per_trade_pct
        stop_dist = ctx.stop_distance_pct
        signals: dict = {}

        # ── Liquidation cascade check ──
        liq_telemetry = getattr(ctx, "liquidation", None)
        if isinstance(liq_telemetry, dict):
            current_price = getattr(ctx, "price", None)
            levels = liq_telemetry.get("levels", [])
            cascade_risk = self._assess_cascade_risk(current_price, levels)
            signals["liq_cascade_risk_level"] = cascade_risk["level"]
            signals["liq_cascade_density_usd"] = cascade_risk["density_usd"]
            signals["liq_cascade_n_levels"] = cascade_risk["n_levels"]
            signals["liq_cascade_nearest_pct"] = cascade_risk["nearest_pct"]
        else:
            cascade_risk = {"level": "UNKNOWN", "density_usd": 0.0, "block": False}

        # ── Size compliance check ──
        if equity is None or risk_pct is None:
            rationale = "risk parameters not provided — assuming compliant"
            if cascade_risk.get("block"):
                rationale += f" | CASCADE RISK: {cascade_risk['level']}"
                return SchoolVerdict(
                    school=self.NAME,
                    bias=Bias.NEUTRAL,
                    conviction=0.0,
                    aligned_with_entry=False,
                    rationale=rationale,
                    signals=signals,
                )
            return SchoolVerdict(
                school=self.NAME,
                bias=Bias.NEUTRAL,
                conviction=0.5,
                aligned_with_entry=True,
                rationale=rationale,
                signals=signals,
            )

        if risk_pct > self.MAX_RISK_PCT:
            return SchoolVerdict(
                school=self.NAME,
                bias=Bias.NEUTRAL,
                conviction=0.0,
                aligned_with_entry=False,
                rationale=f"risk={risk_pct * 100:.2f}% exceeds {self.MAX_RISK_PCT * 100:.0f}% cap",
                signals={**signals, "risk_pct": risk_pct, "violation": True},
            )

        if cascade_risk.get("block"):
            signals["cascade_block"] = True
            return SchoolVerdict(
                school=self.NAME,
                bias=Bias.NEUTRAL,
                conviction=0.1,
                aligned_with_entry=False,
                rationale=f"LIQUIDATION CASCADE RISK [{cascade_risk['level']}]: "
                f"${cascade_risk['density_usd']:,.0f} within "
                f"{cascade_risk['nearest_pct'] * 100:.1f}% of price — DO NOT TRADE",
                signals=signals,
            )

        if stop_dist is None or stop_dist <= 0:
            return SchoolVerdict(
                school=self.NAME,
                bias=Bias.NEUTRAL,
                conviction=0.4,
                aligned_with_entry=True,
                rationale=f"risk={risk_pct * 100:.2f}% within cap but no stop",
                signals=signals,
            )

        if risk_pct <= self.PREFERRED_RISK_PCT:
            conv = 0.95
            r = f"risk={risk_pct * 100:.2f}% compliant"
        else:
            slack = (self.MAX_RISK_PCT - risk_pct) / max(self.MAX_RISK_PCT - self.PREFERRED_RISK_PCT, 1e-9)
            conv = 0.5 + 0.4 * max(0.0, min(1.0, slack))
            r = f"risk={risk_pct * 100:.2f}% partial compliance"

        if cascade_risk["level"] == "YELLOW":
            conv *= 0.7
            r += f" | cascade YELLOW (${cascade_risk['density_usd']:,.0f} within zone)"

        return SchoolVerdict(
            school=self.NAME,
            bias=Bias.NEUTRAL,
            conviction=conv,
            aligned_with_entry=True,
            rationale=r,
            signals={**signals, "risk_pct": risk_pct, "stop_distance_pct": stop_dist},
        )

    def _assess_cascade_risk(self, current_price: float | None, levels: list) -> dict:
        """Assess liquidation cascade risk from heatmap level data."""
        if not current_price or not levels:
            return {"level": "UNKNOWN", "density_usd": 0.0, "n_levels": 0, "nearest_pct": 0.0, "block": False}

        zone_total = 0.0
        nearest_pct = 1.0
        for level in levels:
            price = level.get("price", 0)
            dist_pct = abs(price - current_price) / max(current_price, 1)
            if dist_pct <= self.CASCADE_ZONE_PCT:
                zone_total += level.get("total_size_usd", 0)
                nearest_pct = min(nearest_pct, dist_pct)

        n_levels = len(
            [
                level
                for level in levels
                if abs(level.get("price", 0) - current_price) / max(current_price, 1) <= self.CASCADE_ZONE_PCT
            ]
        )

        if zone_total > self.HIGH_CASCADE_DENSITY_USD:
            return {
                "level": "RED",
                "density_usd": zone_total,
                "n_levels": n_levels,
                "nearest_pct": nearest_pct,
                "block": True,
            }
        if zone_total > self.MODERATE_CASCADE_DENSITY_USD:
            return {
                "level": "YELLOW",
                "density_usd": zone_total,
                "n_levels": n_levels,
                "nearest_pct": nearest_pct,
                "block": False,
            }

        return {
            "level": "GREEN",
            "density_usd": zone_total,
            "n_levels": n_levels,
            "nearest_pct": nearest_pct,
            "block": False,
        }

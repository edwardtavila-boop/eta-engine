"""Adversarial Red-Team school — enhanced with crypto attack surfaces.

BTC integration 2026-05-01:
  * Whale manipulation risk: weekend thin liquidity, exchange concentration
  * Liquidation cascade vulnerability: high-lev positions near price
  * Weekend/session liquidity: crypto 24/7 ≠ uniform liquidity
  * Stablecoin/flow manipulation risk
"""

from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)
from eta_engine.brain.jarvis_v3.sage.feature_cache import get_or_compute


def _ema(values: list[float], period: int) -> list[float]:
    if not values or period < 1:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


class RedTeamSchool(SchoolBase):
    NAME = "red_team"
    WEIGHT = 1.3
    KNOWLEDGE = (
        "Adversarial Red Team: deliberately argues opposite of proposed entry. "
        "Standard technical counters: over-extension from MA, volume divergence, "
        "failed S/R breakouts, crowded positioning. Crypto-specific counters: "
        "whale manipulation risk (thin weekend liquidity, large wallets near "
        "key levels), liquidation cascade vulnerability (high-lev positions "
        "clustered within 3-5% of price), session-risk (crypto 24/7 ≠ uniform "
        "liquidity — weekends drop 40-60%, overnight spread widens), "
        "stablecoin flow regime changes (sudden USDT minting = buy pressure)."
    )

    STRETCH_EMA20 = 0.02
    WEEKEND_VOL_DROP_THRESHOLD = 0.4

    def _bars(self, ctx: MarketContext) -> list[dict]:
        return getattr(ctx, "bars", []) or []

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        entry_side = ctx.side.lower()
        bars = self._bars(ctx)
        closes = [b.get("close", 0) for b in bars if isinstance(b, dict)]
        volumes = [b.get("volume", 0) for b in bars if isinstance(b, dict)]

        stretch = 0.0
        overstretched_up = overstretched_dn = False
        bearish_div = bullish_div = False

        if len(closes) >= 20:
            ema20 = get_or_compute(ctx, "ema_20", lambda: _ema(closes, 20))
            if ema20 and ema20[-1] > 0 and closes[-1] > 0:
                stretch = (closes[-1] - ema20[-1]) / ema20[-1]
                overstretched_up = stretch > self.STRETCH_EMA20
                overstretched_dn = stretch < -self.STRETCH_EMA20

        if len(closes) >= 5 and len(volumes) >= 5:
            price_up = closes[-1] > closes[-5]
            vol_down = volumes[-1] < sum(volumes[-5:]) / max(len(volumes[-5:]), 1) * 0.8
            bearish_div = price_up and vol_down
            bullish_div = not price_up and vol_down

        crypto_attack = self._check_crypto_attacks(ctx)
        signals: dict = {}

        if entry_side == "long":
            counter_bias = Bias.SHORT
            counter_reasons: list[str] = []

            if overstretched_up and bearish_div:
                conviction = 0.75
                counter_reasons.append(f"overstretched +{stretch * 100:.1f}% + bearish vol divergence")
            elif overstretched_up:
                conviction = 0.45
                counter_reasons.append(f"overstretched +{stretch * 100:.1f}% from EMA20")
            elif bearish_div:
                conviction = 0.40
                counter_reasons.append("bearish volume divergence")
            else:
                conviction = 0.10

            if crypto_attack:
                for attack in crypto_attack:
                    counter_reasons.append(attack)
                    conviction = max(conviction, 0.55)

            if conviction >= 0.40:
                return SchoolVerdict(
                    school=self.NAME,
                    bias=counter_bias,
                    conviction=conviction,
                    aligned_with_entry=False,
                    rationale="COUNTER to long: " + "; ".join(counter_reasons),
                    signals={**signals, "stretch": stretch, "crypto_attacks": len(crypto_attack)},
                )
            return SchoolVerdict(
                school=self.NAME,
                bias=Bias.NEUTRAL,
                conviction=0.10,
                aligned_with_entry=True,
                rationale="no credible counter to long entry",
                signals={"stretch": stretch},
            )

        else:
            counter_bias = Bias.LONG
            counter_reasons: list[str] = []

            if overstretched_dn and bullish_div:
                conviction = 0.75
                counter_reasons.append(f"overstretched {stretch * 100:.1f}% + bullish vol divergence")
            elif overstretched_dn:
                conviction = 0.45
                counter_reasons.append(f"overstretched {stretch * 100:.1f}% below EMA20")
            elif bullish_div:
                conviction = 0.40
                counter_reasons.append("bullish volume divergence")
            else:
                conviction = 0.10

            if crypto_attack:
                for attack in crypto_attack:
                    counter_reasons.append(attack)
                    conviction = max(conviction, 0.55)

            if conviction >= 0.40:
                return SchoolVerdict(
                    school=self.NAME,
                    bias=counter_bias,
                    conviction=conviction,
                    aligned_with_entry=False,
                    rationale="COUNTER to short: " + "; ".join(counter_reasons),
                    signals={**signals, "stretch": stretch, "crypto_attacks": len(crypto_attack)},
                )
            return SchoolVerdict(
                school=self.NAME,
                bias=Bias.NEUTRAL,
                conviction=0.10,
                aligned_with_entry=True,
                rationale="no credible counter to short entry",
                signals={"stretch": stretch},
            )

    def _check_crypto_attacks(self, ctx: MarketContext) -> list[str]:
        """Identify crypto-specific attack vectors. Returns list of attack strings."""
        attacks: list[str] = []
        telemetry = getattr(ctx, "onchain", None) or {}

        # 1. Whale proximity — large wallets near current price
        whale_levels = telemetry.get("whale_concentration_pct")
        if isinstance(whale_levels, (int, float)) and whale_levels > 10.0:
            price = getattr(ctx, "price", 0)
            if price > 0:
                attacks.append("whale concentration >10% — potential manipulation near key levels")

        # 2. Weekend/session risk
        from datetime import datetime

        now = datetime.now()
        if now.weekday() >= 5:
            vol_factor = telemetry.get("weekend_vol_factor", 1.0)
            if vol_factor < self.WEEKEND_VOL_DROP_THRESHOLD:
                attacks.append(
                    f"weekend liquidity {vol_factor * 100:.0f}% of weekday — "
                    "slippage risk 2-5x, whale moves close markets"
                )

        # 3. Stablecoin flow manipulation
        stablecoin_ratio = telemetry.get("stablecoin_supply_ratio")
        if isinstance(stablecoin_ratio, (int, float)) and stablecoin_ratio < 0.05:
            attacks.append("stablecoin supply depleted (<5%) — limited buying power, any sell order can cascade")

        # 4. Exchange concentration risk
        exchange_netflow = telemetry.get("exchange_netflow")
        if isinstance(exchange_netflow, (int, float)) and exchange_netflow > 5000:
            attacks.append(
                f"large exchange inflow {exchange_netflow:+,.0f} — potential sell-side pressure from exchange deposits"
            )

        return attacks

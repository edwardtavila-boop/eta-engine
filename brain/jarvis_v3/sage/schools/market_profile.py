"""Auction Market Theory / Market Profile school.

Heuristic: build a quick volume profile from the last 50 bars; identify
Point of Control (POC, highest-volume price), Value Area (~70% of total
volume around POC). Bias by where current price sits vs VAH (value area
high) / VAL (value area low) / POC.
"""
from __future__ import annotations

from collections import defaultdict

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class MarketProfileSchool(SchoolBase):
    NAME = "market_profile"
    WEIGHT = 1.1
    KNOWLEDGE = (
        "Market Profile / Auction Market Theory (J. Peter Steidlmayer, 1980s): "
        "markets are continuous auctions seeking value. Value Area = price "
        "range containing ~70% of trading volume. POC = Point of Control "
        "(highest-volume price node). Prices rotate inside VA until imbalance "
        "drives a trend. HVN = magnet, LVN = transit. Outside VA = trend or "
        "rejection signal."
    )

    PRICE_BUCKET_PCT = 0.0005  # 5 bps buckets

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        n = ctx.n_bars
        if n < 30:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({n} < 30)",
            )
        bars = ctx.bars[-50:]
        last_close = float(bars[-1]["close"])
        if last_close <= 0:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False, rationale="zero last close",
            )
        bucket = max(1.0, last_close * self.PRICE_BUCKET_PCT)

        vol_at: dict[float, float] = defaultdict(float)
        for b in bars:
            o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
            v = float(b.get("volume", 0))
            mid = (o + h + l + c) / 4
            key = round(mid / bucket) * bucket
            vol_at[key] += v

        if not vol_at:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False, rationale="no volume in profile",
            )

        # POC = max-volume bucket
        poc_price = max(vol_at, key=vol_at.get)
        total = sum(vol_at.values())
        target = 0.70 * total

        # Value area: expand symmetrically around POC until 70% captured
        sorted_levels = sorted(vol_at.keys())
        poc_idx = sorted_levels.index(poc_price)
        cumulative = vol_at[poc_price]
        lo_idx = hi_idx = poc_idx
        while cumulative < target and (lo_idx > 0 or hi_idx < len(sorted_levels) - 1):
            up_vol = vol_at[sorted_levels[hi_idx + 1]] if hi_idx + 1 < len(sorted_levels) else 0
            dn_vol = vol_at[sorted_levels[lo_idx - 1]] if lo_idx - 1 >= 0 else 0
            if up_vol >= dn_vol and hi_idx + 1 < len(sorted_levels):
                hi_idx += 1
                cumulative += up_vol
            elif lo_idx - 1 >= 0:
                lo_idx -= 1
                cumulative += dn_vol
            else:
                break
        val = sorted_levels[lo_idx]
        vah = sorted_levels[hi_idx]

        # Classify position
        if last_close > vah:
            bias = Bias.LONG
            rationale = f"price above VAH ({vah:.2f}) -- value migrating up"
            conv = 0.60
        elif last_close < val:
            bias = Bias.SHORT
            rationale = f"price below VAL ({val:.2f}) -- value migrating down"
            conv = 0.60
        elif abs(last_close - poc_price) / max(poc_price, 1e-9) < 0.001:
            bias = Bias.NEUTRAL
            rationale = f"price at POC ({poc_price:.2f}) -- magnet level"
            conv = 0.30
        else:
            bias = Bias.NEUTRAL
            rationale = f"price inside value area (VAL={val:.2f}, VAH={vah:.2f}) -- rotational"
            conv = 0.20

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "poc": poc_price,
                "vah": vah,
                "val": val,
                "value_area_pct": cumulative / max(total, 1e-9),
                "n_buckets": len(vol_at),
            },
        )

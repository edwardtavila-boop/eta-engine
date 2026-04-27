"""Smart Money Concepts (SMC) / ICT school.

Heuristic: detect Break of Structure (BOS) and Change of Character
(ChoCH) from recent pivot sequence; flag Fair Value Gaps (FVGs) where
candle bodies skip a price range; identify last clear order block.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)
from eta_engine.brain.jarvis_v3.sage.schools.support_resistance import _find_pivots


class SmcIctSchool(SchoolBase):
    NAME = "smc_ict"
    WEIGHT = 1.0
    KNOWLEDGE = (
        "Smart Money Concepts (SMC) / ICT (Inner Circle Trader, Michael "
        "Huddleston, 2000s-2010s): institutions hunt retail liquidity at "
        "stop-pools then reverse. Key tools: order blocks (institutional "
        "supply/demand zones), fair value gaps (price imbalances), liquidity "
        "sweeps (wicks past obvious highs/lows that reverse), break of "
        "structure (BOS = continuation), change of character (ChoCH = "
        "potential reversal). Heavily overlaps Wyckoff with modern terminology."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        n = ctx.n_bars
        if n < 30:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({n} < 30)",
            )

        highs = ctx.highs()
        lows = ctx.lows()
        last_close = float(ctx.bars[-1]["close"])

        # Pivot structure: find swing highs + lows
        pivot_highs = _find_pivots(highs, kind="high")
        pivot_lows = _find_pivots(lows, kind="low")
        if len(pivot_highs) < 2 or len(pivot_lows) < 2:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.10,
                aligned_with_entry=False,
                rationale="insufficient pivot structure for BOS/ChoCH",
            )

        # Latest 2 swing highs + lows
        sh1, sh2 = pivot_highs[-2:]
        sl1, sl2 = pivot_lows[-2:]
        bullish_structure = sh2[1] > sh1[1] and sl2[1] > sl1[1]
        bearish_structure = sh2[1] < sh1[1] and sl2[1] < sl1[1]

        # BOS: last close > most recent swing high (bullish BOS) or < most recent swing low (bearish)
        most_recent_high = sh2[1]
        most_recent_low = sl2[1]
        bullish_bos = last_close > most_recent_high
        bearish_bos = last_close < most_recent_low

        # ChoCH: structure WAS bullish but now broke a swing low (or vice versa)
        bullish_choch = bearish_structure and bullish_bos
        bearish_choch = bullish_structure and bearish_bos

        # FVG (Fair Value Gap) on last 3 bars: prev bar's high < next bar's low (bullish gap)
        fvg_up = fvg_down = False
        if n >= 3:
            b1, b2, b3 = ctx.bars[-3:]
            if float(b1["high"]) < float(b3["low"]):
                fvg_up = True
            elif float(b1["low"]) > float(b3["high"]):
                fvg_down = True

        # Decide
        if bullish_choch:
            bias, rationale, conv = Bias.LONG, "ChoCH up: bearish structure broken by close above swing high", 0.80
        elif bearish_choch:
            bias, rationale, conv = Bias.SHORT, "ChoCH down: bullish structure broken by close below swing low", 0.80
        elif bullish_bos:
            bias, rationale, conv = Bias.LONG, "Bullish BOS: close above prior swing high", 0.65
        elif bearish_bos:
            bias, rationale, conv = Bias.SHORT, "Bearish BOS: close below prior swing low", 0.65
        elif bullish_structure and fvg_up:
            bias, rationale, conv = Bias.LONG, "Bullish structure + recent FVG up", 0.55
        elif bearish_structure and fvg_down:
            bias, rationale, conv = Bias.SHORT, "Bearish structure + recent FVG down", 0.55
        elif bullish_structure:
            bias, rationale, conv = Bias.LONG, "Bullish structure intact (HH+HL)", 0.35
        elif bearish_structure:
            bias, rationale, conv = Bias.SHORT, "Bearish structure intact (LH+LL)", 0.35
        else:
            bias, rationale, conv = Bias.NEUTRAL, "Mixed structure -- no clear SMC setup", 0.15

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "bullish_bos": bullish_bos,
                "bearish_bos": bearish_bos,
                "bullish_choch": bullish_choch,
                "bearish_choch": bearish_choch,
                "fvg_up": fvg_up,
                "fvg_down": fvg_down,
                "bullish_structure": bullish_structure,
                "bearish_structure": bearish_structure,
                "swing_high": most_recent_high,
                "swing_low": most_recent_low,
            },
        )

"""Narrative / sentiment pressure school for outside-of-price context."""

from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)
from eta_engine.brain.jarvis_v3.sentiment_pressure import (
    primary_asset_for_symbol,
    summarize_pressure,
)


def _float_value(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class SentimentPressureSchool(SchoolBase):
    NAME = "sentiment_pressure"
    WEIGHT = 0.8
    INSTRUMENTS = frozenset({"crypto", "futures", "equity", "fx"})
    KNOWLEDGE = (
        "Narrative / sentiment pressure school: uses warmed non-price telemetry "
        "from public headline flow, fear/greed, and topic flags to judge whether "
        "outside-of-price pressure is risk-on, risk-off, mixed, or neutral. "
        "Useful for catching crowd positioning and macro headline drag that pure "
        "price schools may not see yet."
    )

    def applies_to(self, ctx: MarketContext) -> bool:
        if not super().applies_to(ctx):
            return False
        return isinstance(ctx.sentiment, dict) and bool(ctx.sentiment)

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        sentiment = ctx.sentiment if isinstance(ctx.sentiment, dict) else {}
        asset_summaries = sentiment.get("asset_summaries") if isinstance(sentiment.get("asset_summaries"), list) else []
        pressure = sentiment.get("pressure") if isinstance(sentiment.get("pressure"), dict) else {}
        if not asset_summaries and not pressure:
            return SchoolVerdict(
                school=self.NAME,
                bias=Bias.NEUTRAL,
                conviction=0.0,
                aligned_with_entry=False,
                rationale="no sentiment telemetry on ctx",
                signals={"missing": ["ctx.sentiment"]},
            )

        if not pressure:
            pressure = summarize_pressure(asset_summaries)

        status = str(pressure.get("status") or "unknown").strip().lower()
        score = _float_value(pressure.get("score"))
        if score is None:
            score = 0.0
        macro_topics = pressure.get("macro_topics") if isinstance(pressure.get("macro_topics"), list) else []
        lead_positive = str(pressure.get("lead_positive_asset") or "")
        lead_negative = str(pressure.get("lead_negative_asset") or "")
        selected_assets = [str(row.get("asset") or "") for row in asset_summaries if str(row.get("asset") or "")]
        primary_asset = primary_asset_for_symbol(ctx.symbol)

        if status == "mixed":
            bias = Bias.NEUTRAL
            conviction = max(0.2, min(0.4, abs(score) + 0.15))
            rationale = "narrative mixed: crypto and macro are pulling in different directions"
        elif status == "neutral" and abs(score) < 0.1:
            bias = Bias.NEUTRAL
            conviction = 0.15
            rationale = "narrative balanced: no strong outside-of-price pressure"
        elif status == "unknown" and abs(score) < 0.05:
            bias = Bias.NEUTRAL
            conviction = 0.0
            rationale = "sentiment pressure unavailable"
        else:
            bias = Bias.LONG if score >= 0.0 else Bias.SHORT
            conviction = min(0.8, max(0.2, abs(score) * 1.6))
            if status in {"risk_on", "risk_off"}:
                conviction = max(conviction, 0.45)
            primary_asset_led = (bias == Bias.LONG and lead_positive == primary_asset) or (
                bias == Bias.SHORT and lead_negative == primary_asset
            )
            if primary_asset and primary_asset_led:
                conviction = min(0.85, conviction + 0.1)
            if (
                ctx.instrument_class in {"futures", "equity", "fx"}
                and primary_asset == ""
                and selected_assets == ["macro"]
            ):
                conviction = min(conviction, 0.55)
            direction_label = "risk-on" if bias == Bias.LONG else "risk-off"
            if bias == Bias.LONG and lead_positive:
                detail = lead_positive
            elif bias == Bias.SHORT and lead_negative:
                detail = lead_negative
            else:
                detail = ""
            if detail:
                rationale = f"{direction_label} narrative led by {detail}"
            else:
                rationale = f"{direction_label} narrative pressure"
            if macro_topics:
                rationale += f" with macro focus on {macro_topics[0]}"

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=round(conviction, 4),
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "status": status,
                "score": round(score, 4),
                "lead_positive_asset": lead_positive,
                "lead_negative_asset": lead_negative,
                "macro_topics": macro_topics,
                "selected_assets": selected_assets,
            },
        )

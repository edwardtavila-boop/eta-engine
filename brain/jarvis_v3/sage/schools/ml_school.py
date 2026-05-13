"""ML school (Wave-5 #15, 2026-04-27).

Model-backed statistical school for the last-50-bar tape. When a
trained classifier exists it is loaded from disk. When it does not, the
school falls back to a deterministic bounded classifier using trend,
momentum, range expansion, and volume confirmation so Sage never loses
this school entirely because an optional model artifact is absent.

The model file is expected at ``state/sage/ml_model.pkl`` (joblib-pickled
sklearn pipeline). When present, the school loads it once + runs it on
the current bars to produce a probability + bias.

To train a model:
    python scripts/sage_train_ml_school.py --window-days 60 \\
        --out state/sage/ml_model.pkl
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)

logger = logging.getLogger(__name__)


class MLSchool(SchoolBase):
    NAME = "ml"
    WEIGHT = 1.0
    KNOWLEDGE = (
        "ML school: gradient-boosted classifier trained on (last 50 bars "
        "features) -> realized R 50 bars later. Features: returns, vol, "
        "vol-of-vol, EMA gaps, range, body-to-range ratio, volume rank. "
        "Output: probability of LONG winner, of SHORT winner; emits the "
        "argmax with conviction = max(P) - 0.5. Bandit decides whether "
        "ML earns weight vs human-encoded schools."
    )

    MODEL_PATH = Path(__file__).resolve().parents[4] / "state" / "sage" / "ml_model.pkl"

    _model = None  # cached after first load
    _load_attempted = False

    def _load_model(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True
        if not self.MODEL_PATH.exists():
            return
        try:
            import joblib

            self.__class__._model = joblib.load(self.MODEL_PATH)
            logger.info("ML school model loaded from %s", self.MODEL_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ML school model load failed: %s", exc)

    def _features(self, ctx: MarketContext) -> list[float] | None:
        if ctx.n_bars < 50:
            return None
        closes = ctx.closes()[-50:]
        highs = ctx.highs()[-50:]
        lows = ctx.lows()[-50:]
        volumes = ctx.volumes()[-50:]
        # 5 simple features; real model would use many more
        rets = [(closes[i] - closes[i - 1]) / max(closes[i - 1], 1e-9) for i in range(1, len(closes))]
        recent_vol = sum(abs(r) for r in rets[-10:]) / 10
        baseline_vol = sum(abs(r) for r in rets) / len(rets)
        last_range = highs[-1] - lows[-1]
        avg_range = sum(high - low for high, low in zip(highs, lows, strict=True)) / len(highs)
        mean_volume = sum(volumes) / len(volumes)
        volume_std = (sum((v - mean_volume) ** 2 for v in volumes) / len(volumes)) ** 0.5
        last_vol_z = (volumes[-1] - mean_volume) / max(volume_std, 1e-9)
        return [
            recent_vol / max(baseline_vol, 1e-9),
            last_range / max(avg_range, 1e-9),
            last_vol_z,
            sum(rets[-5:]) * 100,  # last 5-bar return %
            (closes[-1] - closes[0]) / max(closes[0], 1e-9) * 100,  # 50-bar return %
        ]

    def _fallback_probabilities(self, feats: list[float]) -> tuple[float, float, dict[str, float]]:
        """Deterministic no-artifact classifier.

        The fallback is intentionally conservative: it can vote with the
        tape, but conviction is capped below the model-backed path and
        range shocks dampen confidence.
        """
        vol_ratio, range_ratio, volume_z, ret5_pct, ret50_pct = feats
        trend_score = math.tanh(ret50_pct / 4.0)
        momentum_score = math.tanh(ret5_pct / 1.5)
        volume_confirmation = math.tanh(volume_z / 2.0)
        shock_penalty = min(0.35, max(0.0, range_ratio - 1.6) * 0.18)
        volatility_penalty = min(0.25, max(0.0, vol_ratio - 1.8) * 0.12)
        directional_edge = (
            0.55 * trend_score
            + 0.30 * momentum_score
            + 0.15 * volume_confirmation * (1.0 if trend_score >= 0 else -1.0)
        )
        confidence_scale = max(0.35, 1.0 - shock_penalty - volatility_penalty)
        p_long = 0.5 + (0.38 * directional_edge * confidence_scale)
        p_long = max(0.02, min(0.98, p_long))
        p_short = 1.0 - p_long
        diagnostics = {
            "trend_score": round(trend_score, 4),
            "momentum_score": round(momentum_score, 4),
            "volume_confirmation": round(volume_confirmation, 4),
            "shock_penalty": round(shock_penalty, 4),
            "volatility_penalty": round(volatility_penalty, 4),
            "directional_edge": round(directional_edge, 4),
        }
        return p_long, p_short, diagnostics

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        self._load_model()
        feats = self._features(ctx)
        if feats is None:
            return SchoolVerdict(
                school=self.NAME,
                bias=Bias.NEUTRAL,
                conviction=0.0,
                aligned_with_entry=False,
                rationale="insufficient bars for feature window",
            )
        diagnostics: dict[str, float] = {}
        try:
            if self._model is None:
                p_long, p_short, diagnostics = self._fallback_probabilities(feats)
                source = "deterministic_fallback"
            else:
                proba = self._model.predict_proba([feats])[0]
                p_long, p_short = float(proba[0]), float(proba[1])
                source = "trained_model"
        except Exception as exc:  # noqa: BLE001
            return SchoolVerdict(
                school=self.NAME,
                bias=Bias.NEUTRAL,
                conviction=0.0,
                aligned_with_entry=False,
                rationale=f"model inference failed: {exc}",
            )

        if p_long > p_short:
            bias, conv = Bias.LONG, max(0.0, p_long - 0.5) * 2
            rationale = f"ML {source} predicts P(long winner)={p_long:.2f}"
        else:
            bias, conv = Bias.SHORT, max(0.0, p_short - 0.5) * 2
            rationale = f"ML {source} predicts P(short winner)={p_short:.2f}"

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=min(0.85 if self._model else 0.65, conv),
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "p_long": round(p_long, 4),
                "p_short": round(p_short, 4),
                "features": feats,
                "source": source,
                "model_path": str(self.MODEL_PATH),
                **diagnostics,
            },
        )

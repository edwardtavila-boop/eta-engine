"""ML school (Wave-5 #15, 2026-04-27).

SCAFFOLD: a placeholder for a gradient-boosted classifier trained on
(last 50 bars features) -> realized R 50 bars later. Until a model is
trained + persisted, this school returns NEUTRAL with conviction=0.

The model file is expected at ``state/sage/ml_model.pkl`` (joblib-pickled
sklearn pipeline). When present, the school loads it once + runs it on
the current bars to produce a probability + bias.

To train a model:
    python scripts/sage_train_ml_school.py --window-days 60 \\
        --out state/sage/ml_model.pkl
(script is a future deliverable; the inference path is shipped now.)
"""
from __future__ import annotations

import logging
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

    MODEL_PATH = (
        Path(__file__).resolve().parents[4]
        / "state" / "sage" / "ml_model.pkl"
    )

    _model = None     # cached after first load
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
        rets = [(closes[i] - closes[i - 1]) / max(closes[i - 1], 1e-9)
                for i in range(1, len(closes))]
        recent_vol = sum(abs(r) for r in rets[-10:]) / 10
        baseline_vol = sum(abs(r) for r in rets) / len(rets)
        last_range = highs[-1] - lows[-1]
        avg_range = sum(h - l for h, l in zip(highs, lows)) / len(highs)
        last_vol_z = (volumes[-1] - sum(volumes) / len(volumes)) / max(
            (sum((v - sum(volumes) / len(volumes)) ** 2 for v in volumes) / len(volumes)) ** 0.5, 1e-9
        )
        return [
            recent_vol / max(baseline_vol, 1e-9),
            last_range / max(avg_range, 1e-9),
            last_vol_z,
            sum(rets[-5:]) * 100,   # last 5-bar return %
            (closes[-1] - closes[0]) / max(closes[0], 1e-9) * 100,  # 50-bar return %
        ]

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        self._load_model()
        feats = self._features(ctx)
        if feats is None:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale="insufficient bars for feature window",
            )
        if self._model is None:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"no trained model at {self.MODEL_PATH} -- school skipped",
                signals={"features": feats, "model_path": str(self.MODEL_PATH)},
            )

        try:
            proba = self._model.predict_proba([feats])[0]
            p_long, p_short = float(proba[0]), float(proba[1])
        except Exception as exc:  # noqa: BLE001
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"model inference failed: {exc}",
            )

        if p_long > p_short:
            bias, conv = Bias.LONG, max(0.0, p_long - 0.5) * 2
            rationale = f"ML predicts P(long winner)={p_long:.2f}"
        else:
            bias, conv = Bias.SHORT, max(0.0, p_short - 0.5) * 2
            rationale = f"ML predicts P(short winner)={p_short:.2f}"

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME, bias=bias, conviction=min(0.85, conv),
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={"p_long": p_long, "p_short": p_short, "features": feats},
        )

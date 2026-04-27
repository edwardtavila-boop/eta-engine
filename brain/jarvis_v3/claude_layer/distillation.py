"""
JARVIS v3 // claude_layer.distillation
======================================
Layer 4 -- self-play distillation.

Offline, we run tens of thousands of synthetic debates via
``next_level.self_play`` and collect triples:

    (deterministic_verdict, claude_verdict, features)

We then fit a small pure-Python classifier that predicts the
probability Claude would agree with the JARVIS deterministic verdict.
At production time, if the classifier says ``p_agree >= 0.92``, we
SKIP the Claude call -- JARVIS's verdict wins by default.

This distills Claude's marginal value down into a fast local model,
so only genuinely ambiguous decisions burn API tokens.

Classifier: logistic regression with hand-picked features. No numpy;
uses the same stdlib-only GD from ``calibration.py``. Persistent JSON.

Pure / deterministic / no external deps.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# Feature schema (must match what the escalation path computes per request)
FEATURE_KEYS: tuple[str, ...] = (
    "stress_composite",
    "sizing_mult",
    "regime_crisis",  # 0/1
    "event_within_1h",  # 0/1
    "portfolio_breach",  # 0/1
    "doctrine_net_bias",
    "r_at_risk",
    "operator_overrides_24h",
    "precedent_n_log",  # log1p(precedent_n)
    "anomaly_count",
)


class DistillSample(BaseModel):
    """One triple used for training."""

    model_config = ConfigDict(frozen=True)

    features: dict[str, float]
    deterministic_verdict: str
    claude_verdict: str

    @property
    def agreement_label(self) -> int:
        """1 if Claude agreed with JARVIS, else 0."""
        return 1 if self.deterministic_verdict.upper() == self.claude_verdict.upper() else 0


class DistillerModel(BaseModel):
    """Logistic regression weights + bias."""

    model_config = ConfigDict(frozen=False)

    weights: dict[str, float] = Field(default_factory=dict)
    bias: float = 0.0
    train_n: int = Field(ge=0, default=0)
    accuracy: float = Field(ge=0.0, le=1.0, default=0.0)
    version: int = Field(ge=0, default=0)

    def predict_agreement(self, features: dict[str, float]) -> float:
        z = self.bias
        for k in FEATURE_KEYS:
            if k in features and k in self.weights:
                z += self.weights[k] * features[k]
        # stable logistic
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        ez = math.exp(z)
        return ez / (1.0 + ez)


class SkipDecision(BaseModel):
    """Output of ``should_skip_claude``."""

    model_config = ConfigDict(frozen=True)

    skip_claude: bool
    p_agree: float = Field(ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    model_version: int = Field(ge=0)
    reason: str


class Distiller:
    """Trains + persists the classifier. Stateless prediction via ``predict``."""

    def __init__(self, model: DistillerModel | None = None) -> None:
        self.model = model or DistillerModel()

    # ------------------------------------------------------------------
    # Training -- pure Python GD
    # ------------------------------------------------------------------
    def fit(
        self,
        samples: list[DistillSample],
        *,
        lr: float = 0.05,
        iters: int = 500,
        l2: float = 0.01,
    ) -> DistillerModel:
        if not samples:
            return self.model
        # Extract feature matrix + labels
        xs = [_extract(s.features) for s in samples]
        ys = [s.agreement_label for s in samples]
        n = len(samples)
        w = {k: 0.0 for k in FEATURE_KEYS}
        b = 0.0
        for _ in range(iters):
            grads = {k: 0.0 for k in FEATURE_KEYS}
            grad_b = 0.0
            for x, y in zip(xs, ys, strict=True):
                z = b + sum(w[k] * x.get(k, 0.0) for k in FEATURE_KEYS)
                p = 1.0 / (1.0 + math.exp(-z)) if z >= 0 else (math.exp(z) / (1.0 + math.exp(z)))
                err = p - y
                for k in FEATURE_KEYS:
                    grads[k] += err * x.get(k, 0.0)
                grad_b += err
            for k in FEATURE_KEYS:
                w[k] -= lr * (grads[k] / n + l2 * w[k])
            b -= lr * grad_b / n
        # Compute train accuracy for reporting
        correct = 0
        for x, y in zip(xs, ys, strict=True):
            z = b + sum(w[k] * x.get(k, 0.0) for k in FEATURE_KEYS)
            pred = 1 if z >= 0 else 0
            if pred == y:
                correct += 1
        self.model = DistillerModel(
            weights=w,
            bias=b,
            train_n=n,
            accuracy=round(correct / n, 4),
            version=self.model.version + 1,
        )
        return self.model

    # ------------------------------------------------------------------
    # Prediction + skip logic
    # ------------------------------------------------------------------
    def should_skip(
        self,
        features: dict[str, float],
        *,
        skip_threshold: float = 0.92,
    ) -> SkipDecision:
        feats = _extract(features)
        p = self.model.predict_agreement(feats)
        skip = p >= skip_threshold
        reason = (
            f"classifier p_agree={p:.3f} >= {skip_threshold} -- skip Claude"
            if skip
            else f"classifier p_agree={p:.3f} < {skip_threshold} -- invoke Claude"
        )
        return SkipDecision(
            skip_claude=skip,
            p_agree=round(p, 4),
            threshold=skip_threshold,
            model_version=self.model.version,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save(self, path: Path | str) -> None:
        Path(path).write_text(
            json.dumps(self.model.model_dump(), indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path | str) -> Distiller:
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(model=DistillerModel.model_validate(data))


def _extract(raw: dict[str, float]) -> dict[str, float]:
    """Normalize raw features -> model features (binning, log-transform)."""
    out: dict[str, float] = {k: 0.0 for k in FEATURE_KEYS}
    out["stress_composite"] = float(raw.get("stress_composite", 0.0))
    out["sizing_mult"] = float(raw.get("sizing_mult", 1.0))
    out["regime_crisis"] = float(1.0 if str(raw.get("regime", "")).upper() == "CRISIS" else 0.0)
    hev = raw.get("hours_until_event")
    out["event_within_1h"] = float(1.0 if hev is not None and 0 <= hev <= 1.0 else 0.0)
    out["portfolio_breach"] = float(1.0 if raw.get("portfolio_breach") else 0.0)
    out["doctrine_net_bias"] = float(raw.get("doctrine_net_bias", 0.0))
    out["r_at_risk"] = float(raw.get("r_at_risk", 0.0))
    out["operator_overrides_24h"] = float(raw.get("operator_overrides_24h", 0))
    out["precedent_n_log"] = math.log1p(max(0, int(raw.get("precedent_n", 0))))
    out["anomaly_count"] = float(raw.get("anomaly_count", 0))
    return out

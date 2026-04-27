"""
JARVIS v3 // regime_stress
==========================
Regime-aware stress weighting.

v2 uses a single fixed ``STRESS_WEIGHTS`` dict. In CRISIS the VIX + macro_bias
factors should dominate; in RISK_ON the override_rate + autopilot factors
should dominate (those are the signals that matter when everything looks fine
on the surface). This module exposes per-regime weight profiles and a
blender so v2 stress can be re-weighted without touching ``jarvis_context``.

Pure / deterministic / no I/O.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RegimeLabel = Literal["RISK_ON", "RISK_OFF", "NEUTRAL", "CRISIS", "UNKNOWN"]


class RegimeWeightProfile(BaseModel):
    """A weight set for a specific regime. Must sum to 1.0 within 1e-6."""

    model_config = ConfigDict(frozen=True)

    regime: RegimeLabel
    weights: dict[str, float] = Field(min_length=1)

    def check_sum(self) -> float:
        return sum(self.weights.values())


# Canonical profiles. Keys mirror v2 STRESS_WEIGHTS.
# Hand-tuned based on the operator's doctrine:
#   * CRISIS   -- macro_bias + macro_event + regime_risk dominate
#   * RISK_OFF -- equity_dd + open_risk dominate (defend capital)
#   * RISK_ON  -- override_rate + autopilot dominate (catch complacency drift)
#   * NEUTRAL  -- same shape as v2 baseline
_PROFILES: dict[RegimeLabel, dict[str, float]] = {
    "CRISIS": {
        "macro_event": 0.30,
        "equity_dd": 0.15,
        "open_risk": 0.10,
        "regime_risk": 0.20,
        "override_rate": 0.05,
        "autopilot": 0.05,
        "correlations": 0.05,
        "macro_bias": 0.10,
    },
    "RISK_OFF": {
        "macro_event": 0.20,
        "equity_dd": 0.30,
        "open_risk": 0.20,
        "regime_risk": 0.10,
        "override_rate": 0.07,
        "autopilot": 0.05,
        "correlations": 0.05,
        "macro_bias": 0.03,
    },
    "RISK_ON": {
        "macro_event": 0.15,
        "equity_dd": 0.15,
        "open_risk": 0.10,
        "regime_risk": 0.05,
        "override_rate": 0.20,
        "autopilot": 0.15,
        "correlations": 0.15,
        "macro_bias": 0.05,
    },
    "NEUTRAL": {
        "macro_event": 0.25,
        "equity_dd": 0.25,
        "open_risk": 0.15,
        "regime_risk": 0.10,
        "override_rate": 0.10,
        "autopilot": 0.07,
        "correlations": 0.05,
        "macro_bias": 0.03,
    },
    "UNKNOWN": {
        "macro_event": 0.25,
        "equity_dd": 0.25,
        "open_risk": 0.15,
        "regime_risk": 0.10,
        "override_rate": 0.10,
        "autopilot": 0.07,
        "correlations": 0.05,
        "macro_bias": 0.03,
    },
}


def weights_for_regime(regime: str) -> dict[str, float]:
    """Return the weight dict for a regime, defaulting to NEUTRAL profile."""
    key = _normalize(regime)
    return dict(_PROFILES.get(key, _PROFILES["NEUTRAL"]))


def profile_for_regime(regime: str) -> RegimeWeightProfile:
    """Return a pydantic-wrapped profile (safer for logging / audit)."""
    key = _normalize(regime)
    return RegimeWeightProfile(regime=key, weights=weights_for_regime(key))


def reweight(
    components_raw: dict[str, float],
    regime: str,
) -> tuple[float, dict[str, float], str]:
    """Apply regime weighting to an already-computed set of raw 0..1 values.

    Returns (composite, contributions, binding_constraint).
    ``contributions`` is ``{component_name: weight * raw}`` so callers can
    show the per-factor breakdown (explainable alerts, #11).
    """
    w = weights_for_regime(regime)
    contributions = {name: float(raw) * w.get(name, 0.0) for name, raw in components_raw.items()}
    composite = max(0.0, min(1.0, sum(contributions.values())))
    binding = max(contributions.items(), key=lambda kv: kv[1])[0] if contributions else "none"
    return composite, contributions, binding


def _normalize(regime: str) -> RegimeLabel:
    r = (regime or "").upper().replace("-", "_").strip()
    if r in {"RISK_ON", "RISK_OFF", "NEUTRAL", "CRISIS"}:
        return r  # type: ignore[return-value]
    # Some V3 regimes come as "RISK-ON" already; handled above. Map stragglers.
    if r in {"TREND_UP", "BULL"}:
        return "RISK_ON"
    if r in {"TREND_DOWN", "BEAR"}:
        return "RISK_OFF"
    if r in {"CHOP", "RANGE"}:
        return "NEUTRAL"
    return "UNKNOWN"

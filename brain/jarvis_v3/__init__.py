"""
EVOLUTIONARY TRADING ALGO  //  brain.jarvis_v3
==================================
JARVIS v3 -- the Evolutionary Trading Algo Core.

Builds on v2 (brain.jarvis_context + brain.jarvis_admin + brain.model_policy)
with fourteen concrete upgrades + a supercharge layer that turns JARVIS from
an observer/gate into the full fleet admin:

Cognition
  * regime_stress         -- regime-aware stress weighting
  * horizons              -- multi-horizon context (now/15m/1h/overnight)
  * predictive            -- forward-looking stress projection (EWMA)
  * calibration           -- Platt-calibrated verdict confidence
  * portfolio             -- correlation-aware portfolio gate

Learning
  * bandit                -- contextual LLM-routing bandit
  * preferences           -- operator-preference learner
  * critique              -- self-critique / drift detector
  * precedent             -- knowledge graph of (regime,event) -> outcome

Observability
  * nl_query              -- natural-language audit query
  * alerts_explain        -- why-now traces for alerts
  * dashboard_payload     -- JSON payload for the React JARVIS tile

Cost / Infra
  * budget                -- Max-plan quota tracker + downshift logic
  * anomaly               -- distribution-drift detection on inputs

Supercharge / Evolutionary Trading Algo Core
  * vps                   -- JARVIS as VPS admin (systemctl / processes / ssh)
  * skills_registry       -- every local skill callable through JARVIS
  * mcp_registry          -- every MCP routed through JARVIS
  * kaizen                -- continuous-improvement loop
  * philosophy            -- encoded Evolutionary Trading Algo doctrine
  * unleashed             -- meta-controller that orchestrates everything

Design principles carry from v2: pure / deterministic / pydantic-typed /
opt-in (nothing breaks if a caller doesn't know about v3).
"""

from __future__ import annotations

__all__ = [
    "regime_stress",
    "horizons",
    "predictive",
    "calibration",
    "portfolio",
    "bandit",
    "preferences",
    "critique",
    "precedent",
    "nl_query",
    "alerts_explain",
    "dashboard_payload",
    "budget",
    "anomaly",
    "vps",
    "skills_registry",
    "mcp_registry",
    "kaizen",
    "philosophy",
    "unleashed",
]

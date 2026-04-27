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

Consolidation (Wave-12 / 2026-04-27): JARVIS as source of truth
  * intelligence          -- JarvisIntelligence wraps JarvisAdmin with
                              memory_rag + causal + world_model +
                              firm_board_debate; one consult() call
  * feedback_loop         -- close_trade() propagates realized R to
                              memory + bandits + calibrator + journal
  * health_check          -- jarvis_health() one-call self-diagnostic
  * admin_query           -- read-only operator queries against the
                              verdict + trade-close logs

Self-awareness (Wave-13 / 2026-04-27): JARVIS knows himself
  * replay_engine         -- counterfactual replay of new policy over
                              past consultations -> quantified lift
  * premortem             -- enumerate failure modes BEFORE approval
                              with kill-prob from world model + RAG
  * thesis_tracker        -- written thesis + invalidation rules,
                              runtime monitor for early-exit signals
  * ood_detector          -- Mahalanobis-style novelty score so JARVIS
                              shrinks confidence in unprecedented states
  * self_drift_monitor    -- JARVIS watches his own verdict
                              distribution; flags drift > 2-sigma
  * postmortem            -- auto-postmortem generator for losing
                              trades with per-layer attribution

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
    # Wave-12 consolidation (JARVIS as source of truth)
    "intelligence",
    "feedback_loop",
    "health_check",
    "admin_query",
    # Wave-13 self-awareness
    "replay_engine",
    "premortem",
    "thesis_tracker",
    "ood_detector",
    "self_drift_monitor",
    "postmortem",
    # Wave-14 explainability + operator-facing layer
    "narrative_generator",
    "operator_coach",
    "skill_health_registry",
    "daily_brief",
    # Wave-15 fleet coordination
    "fleet_allocator",
    "risk_budget_allocator",
    "divergence_detector",
    "pair_arbitrage_scanner",
    # Wave-16 live-readiness validation
    "walk_forward_harness",
    "pre_live_gate",
    "ab_framework",
    "regression_test_set",
    # Wave-16 final integration: one entry point that wires every wave
    "jarvis_full",
]

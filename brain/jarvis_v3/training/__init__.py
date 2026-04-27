"""
EVOLUTIONARY TRADING ALGO  //  brain.jarvis_v3.training
============================================
Persona training, capability awareness, collaboration patterns.

Operator directive (2026-04-24): "train jarvis along with robin and batman
to be the best ai possible. optimize his skills and mcp usage and install
in him knowledge of what he can do at its peak."

Why this module exists
----------------------
JARVIS / BATMAN / ALFRED / ROBIN each have locked model tiers and lanes,
but until now they had no CANONICAL self-knowledge: no catalog of what
they can do, which MCPs they're cleared to use, how to collaborate with
the others. This module installs that self-knowledge as a set of
pydantic-typed, testable, dashboard-exposed contracts.

Components
----------
  * peak_manuals    -- "Who am I at my best?" for each persona
  * skills_catalog  -- Every task category a persona can handle
  * mcp_awareness   -- Which MCP tools + usage patterns per persona
  * collaboration   -- Inter-persona protocols (when to defer, escalate, veto)
  * eval_harness    -- Measure persona response quality on synthetic prompts
  * curriculum      -- Ordered training exercises for calibration

Design principles (same as rest of jarvis_v3)
---------------------------------------------
1. Pure stdlib + pydantic. Training data is data, not LLM calls.
2. Content is FROZEN. Operator edits the manuals; code just reads them.
3. Every piece exposes itself via /api/personas on the dashboard.
4. Tests assert no persona lacks identity/catalog/awareness/protocol entries.
"""

from __future__ import annotations

__all__ = [
    "peak_manuals",
    "skills_catalog",
    "mcp_awareness",
    "collaboration",
    "eval_harness",
    "curriculum",
]

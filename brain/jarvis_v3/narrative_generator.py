"""Narrative generator (Wave-14, 2026-04-27).

Translates JARVIS's structured outputs (ConsolidatedVerdict + the
wave-13 self-awareness layers) into operator-readable prose.

Three verbosity levels:
  * "terse"     -- 1 sentence
  * "standard"  -- 1 paragraph, 4-6 sentences
  * "verbose"   -- multi-paragraph, all layer outputs

Pure stdlib + string templates. No LLM. When LLM access is wired
in production, the same call signature lets you swap in a richer
generator without changing call sites.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.intelligence import ConsolidatedVerdict

logger = logging.getLogger(__name__)

Verbosity = Literal["terse", "standard", "verbose"]


def verdict_to_narrative(
    verdict: ConsolidatedVerdict,
    *,
    verbosity: Verbosity = "standard",
) -> str:
    """Return a prose description of ``verdict``.

    Used by:
      - operator dashboard (verbose)
      - per-trade journal entry (standard)
      - alert payload (terse)
    """
    if verbosity == "terse":
        return _terse(verdict)
    if verbosity == "verbose":
        return _verbose(verdict)
    return _standard(verdict)


def _terse(v: ConsolidatedVerdict) -> str:
    if v.operator_override_level in {"HARD_PAUSE", "KILL"}:
        return f"BLOCKED ({v.operator_override_level}): operator override active."
    base_to_phrase = {
        "APPROVED": "approved",
        "CONDITIONAL": "approved at half size",
        "DEFERRED": "deferred",
        "DENIED": "denied",
    }
    phrase = base_to_phrase.get(v.final_verdict, v.final_verdict.lower())
    size_pct = int(round(v.final_size_multiplier * 100))
    return f"{phrase.capitalize()} at {size_pct}% size, {int(v.confidence * 100)}% confidence."


def _standard(v: ConsolidatedVerdict) -> str:
    parts: list[str] = []

    # Headline
    parts.append(_terse(v))

    # Why this verdict
    if v.intelligence_enabled:
        if v.firm_board_consensus >= 0.6:
            parts.append(
                f"Firm board reached strong consensus ({v.firm_board_consensus:.2f}).",
            )
        elif v.firm_board_consensus < 0.4:
            parts.append(
                f"Firm board split ({v.firm_board_consensus:.2f}) -- low conviction.",
            )

        if v.causal_score > 0.3:
            parts.append(
                f"Causal evidence supports the trade (score {v.causal_score:+.2f}).",
            )
        elif v.causal_score < -0.2:
            parts.append(
                f"Causal evidence is weak (score {v.causal_score:+.2f}); signal may be correlation-only.",
            )

        if v.world_model_expected_r > 0.5:
            parts.append(
                f"World model expects +{v.world_model_expected_r:.2f}R on this state.",
            )
        elif v.world_model_expected_r < -0.3:
            parts.append(
                f"World model expects {v.world_model_expected_r:+.2f}R -- unfavorable.",
            )

        if v.rag_cautions:
            parts.append(
                f"RAG flagged {len(v.rag_cautions)} caution(s) from analog episodes.",
            )
        if v.rag_boosts:
            parts.append(
                f"RAG boost: {len(v.rag_boosts)} winning analog(s).",
            )

        if v.firm_board_devils_advocate:
            parts.append(
                f"Devil's advocate fired ({v.firm_board_devils_advocate}).",
            )

    # Override status
    if v.operator_override_level == "SOFT_PAUSE":
        parts.append("Operator soft-pause is active; new entries blocked.")

    return " ".join(parts)


def _verbose(v: ConsolidatedVerdict) -> str:
    blocks: list[str] = []

    # Header
    size_pct = int(round(v.final_size_multiplier * 100))
    blocks.append(
        f"DECISION: {v.final_verdict} at {size_pct}% size "
        f"(confidence {v.confidence:.2f}). "
        f"Subsystem: {v.subsystem}. Action: {v.action}.",
    )

    # Operator override
    blocks.append(
        f"Operator override: {v.operator_override_level}.",
    )

    # JarvisAdmin chain-of-command
    blocks.append(
        f"JarvisAdmin verdict: {v.base_verdict} (reason: {v.base_reason}).",
    )

    # Intelligence layer
    if v.intelligence_enabled:
        intel: list[str] = []
        intel.append(
            f"Firm-board consensus: {v.firm_board_consensus:.2f}; "
            f"devils advocate: {v.firm_board_devils_advocate or 'none'}.",
        )
        intel.append(
            f"Causal evidence: score {v.causal_score:+.3f}. {v.causal_reason}",
        )
        intel.append(
            f"World model: best action '{v.world_model_best_action}' with expected R {v.world_model_expected_r:+.3f}.",
        )
        if v.rag_summary:
            intel.append(f"RAG: {v.rag_summary}")
        if v.rag_cautions:
            intel.append("Cautions: " + "; ".join(v.rag_cautions))
        if v.rag_boosts:
            intel.append("Boosts: " + "; ".join(v.rag_boosts))
        if v.layer_errors:
            intel.append("Layer errors: " + "; ".join(v.layer_errors))
        blocks.append("Intelligence layer:\n  " + "\n  ".join(intel))
    else:
        blocks.append("Intelligence layer disabled (passthrough mode).")

    return "\n\n".join(blocks)


def health_to_narrative(report: object) -> str:
    """Pretty-print a HealthReport as 4-6 sentences."""
    summary = getattr(report, "summary", "")
    components = getattr(report, "components", [])
    issues = getattr(report, "issues", [])
    out = [summary]
    if issues:
        out.append("Issues:")
        for issue in issues[:5]:
            out.append(f"  - {issue}")
    healthy = [c for c in components if getattr(c, "status", "") == "OK"]
    if healthy:
        out.append(f"{len(healthy)} components healthy.")
    return "\n".join(out)


def drift_to_narrative(report: object) -> str:
    """Pretty-print a SelfDriftReport."""
    summary = getattr(report, "summary", "")
    signals = getattr(report, "signals", [])
    out = [summary]
    for s in signals[:5]:
        sev = getattr(s, "severity", "info")
        metric = getattr(s, "metric", "")
        note = getattr(s, "note", "")
        out.append(f"  [{sev}] {metric}: {note}")
    return "\n".join(out)

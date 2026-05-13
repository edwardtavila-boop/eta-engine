from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from eta_engine.brain.jarvis_v3.narrative_generator import verdict_to_narrative

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.intelligence import ConsolidatedVerdict

logger = logging.getLogger(__name__)

LLM_NARRATIVE_SYSTEM_PROMPT = """You are JARVIS, an elite trading AI for the Evolutionary Trading Algo fleet.

You translate structured trading verdicts into concise, insightful prose for the operator (Edward).

Rules:
1. Never use markdown formatting in the narrative.
2. Keep it 3-6 sentences for standard, 1 sentence for terse.
3. Mention the key binding constraint driving the decision.
4. If the firm board was split, note the disagreement.
5. If the world model sees opportunity or risk, mention it.
6. Never pretend to have information not in the verdict.
7. Be direct and precise — Edward reads fast."""


def llm_narrative(
    verdict: ConsolidatedVerdict,
    *,
    verbosity: Literal["terse", "standard", "verbose"] = "standard",
    force_template: bool = False,
) -> str:
    if force_template:
        return verdict_to_narrative(verdict, verbosity=verbosity)

    # Routed via the Force-Multiplier orchestrator: DOC_WRITING category
    # maps to DEEPSEEK (Worker Bee) — narrative prose is exactly the
    # high-volume cheap-token work DeepSeek V4 Flash is designed for.
    # Using route_and_execute (not direct chat_completion) gives this
    # call site automatic telemetry + cost ceiling + consistent fallback.
    try:
        from eta_engine.brain.model_policy import TaskCategory
        from eta_engine.brain.multi_model import route_and_execute

        context = _build_llm_context(verdict)

        if verbosity == "terse":
            prompt = f"Generate ONE sentence summarizing this trading decision:\n\n{context}"
            max_tok = 80
        elif verbosity == "verbose":
            prompt = (
                f"Generate a detailed 2-3 paragraph briefing for the operator from "
                f"this structured verdict:\n\n{context}"
            )
            max_tok = 600
        else:
            prompt = f"Generate a 3-6 sentence narrative for the operator from this structured verdict:\n\n{context}"
            max_tok = 300

        resp = route_and_execute(
            category=TaskCategory.DOC_WRITING,
            system_prompt=LLM_NARRATIVE_SYSTEM_PROMPT,
            user_message=prompt,
            max_tokens=max_tok,
            temperature=0.5,
            # Per-call ceiling: caps a runaway verbose narrative at $0.005
            # (worst case 600 tok × $0.28/1M out). Ample for normal use.
            max_cost_usd=0.005,
        )

        if resp.text:
            return resp.text.strip()

        logger.info("llm_narrative: empty response, falling back to template")
        return verdict_to_narrative(verdict, verbosity=verbosity)

    except Exception as exc:
        logger.warning("llm_narrative failed (%s), falling back to template", exc)
        return verdict_to_narrative(verdict, verbosity=verbosity)


def _build_llm_context(verdict: ConsolidatedVerdict) -> str:
    lines = [f"Decision: {verdict.final_verdict}"]
    lines.append(f"Confidence: {verdict.confidence:.2f}")
    lines.append(f"Subsystem: {verdict.subsystem}")
    lines.append(f"Action: {verdict.action}")

    if verdict.operator_override_level:
        lines.append(f"Operator override: {verdict.operator_override_level}")

    lines.append(f"Base verdict: {verdict.base_verdict}")
    lines.append(f"Base reason: {verdict.base_reason}")

    if verdict.final_size_multiplier is not None:
        lines.append(f"Size multiplier: {verdict.final_size_multiplier:.2f}")

    if verdict.intelligence_enabled:
        lines.append(f"Firm board consensus: {verdict.firm_board_consensus:.2f}")
        lines.append(f"Causal score: {verdict.causal_score:+.3f}")
        lines.append(f"Causal reason: {verdict.causal_reason}")

        if verdict.world_model_expected_r is not None:
            lines.append(f"World model expected R: {verdict.world_model_expected_r:+.3f}")
        if verdict.world_model_best_action:
            lines.append(f"World model best action: {verdict.world_model_best_action}")

        if verdict.rag_cautions:
            lines.append(f"RAG cautions ({len(verdict.rag_cautions)}): " + "; ".join(verdict.rag_cautions[:3]))
        if verdict.rag_boosts:
            lines.append(f"RAG boosts ({len(verdict.rag_boosts)}): " + "; ".join(verdict.rag_boosts[:3]))
        if verdict.firm_board_devils_advocate:
            lines.append(f"Devil's advocate: {verdict.firm_board_devils_advocate}")

        if verdict.layer_errors:
            lines.append(f"Layer errors: {'; '.join(verdict.layer_errors[:3])}")

    return "\n".join(lines)

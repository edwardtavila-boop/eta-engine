"""LLM narrative layer (Wave-5 #18, 2026-04-27).

Synthesizes a 1-paragraph human-readable narrative of a SageReport.
When ANTHROPIC_API_KEY is present + the eta_engine LLM client is
configured, uses Claude Haiku for the synthesis. Otherwise falls back
to a deterministic template-based narrative.

Cached on (symbol, last bar ts) so the same report only generates one
LLM call.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.sage.base import SageReport

logger = logging.getLogger(__name__)

_NARRATIVE_CACHE: dict[tuple[str, str], str] = {}
_CACHE_LOCK = threading.Lock()


def _any_llm_key() -> bool:
    try:
        from eta_engine.brain.llm_provider import Provider, _get_api_key

        return bool(_get_api_key(Provider.DEEPSEEK) or _get_api_key(Provider.ANTHROPIC))
    except Exception:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))


def _template_narrative(report: SageReport, *, symbol: str = "") -> str:
    """Fallback deterministic narrative (no LLM call)."""
    aligned = report.schools_aligned_with_entry
    n = report.schools_consulted

    # Top 3 aligned + top disagreeing for color
    sorted_schools = sorted(
        report.per_school.items(),
        key=lambda kv: kv[1].conviction,
        reverse=True,
    )
    top_align = [
        f"{name} ({v.conviction:.2f})" for name, v in sorted_schools if v.aligned_with_entry and v.conviction > 0.3
    ][:3]
    top_disagree = [
        f"{name} ({v.conviction:.2f})"
        for name, v in sorted_schools
        if not v.aligned_with_entry and v.bias.value != "neutral" and v.conviction > 0.3
    ][:2]

    parts = [
        f"{symbol or 'asset'}: sage reads {report.composite_bias.value} "
        f"with {report.conviction * 100:.0f}% conviction "
        f"({aligned}/{n} schools aligned)."
    ]
    if top_align:
        parts.append("Strongest aligned: " + ", ".join(top_align) + ".")
    if top_disagree:
        parts.append("Concerns: " + ", ".join(top_disagree) + ".")
    return " ".join(parts)


def explain_sage(
    report: SageReport,
    *,
    symbol: str = "",
    use_llm: bool = True,
    bar_ts_key: str = "",
) -> str:
    """Return a human-readable 1-paragraph explanation of the sage report.

    Falls back to deterministic template if LLM unavailable.
    """
    cache_key = (symbol, bar_ts_key)
    with _CACHE_LOCK:
        if cache_key in _NARRATIVE_CACHE:
            return _NARRATIVE_CACHE[cache_key]

    text: str
    if use_llm and _any_llm_key():
        try:
            text = _llm_narrative(report, symbol=symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM narrative failed (falling back to template): %s", exc)
            text = _template_narrative(report, symbol=symbol)
    else:
        text = _template_narrative(report, symbol=symbol)

    with _CACHE_LOCK:
        if len(_NARRATIVE_CACHE) > 256:
            _NARRATIVE_CACHE.clear()
        _NARRATIVE_CACHE[cache_key] = text
    return text


def _llm_narrative(report: SageReport, *, symbol: str = "") -> str:
    """Synthesize a sage-report narrative via the Force-Multiplier orchestrator.

    Routed through DOC_WRITING -> DEEPSEEK (Worker Bee). Picks up automatic
    telemetry, per-call budget enforcement, and graceful fallback for free.
    Migrated from direct chat_completion() (Wave-19, 2026-05-04).
    """
    from eta_engine.brain.model_policy import TaskCategory
    from eta_engine.brain.multi_model import route_and_execute

    school_lines = "\n".join(
        f"  - {name}: bias={v.bias.value}, conviction={v.conviction:.2f}, rationale={v.rationale}"
        for name, v in report.per_school.items()
    )
    prompt = (
        f"You are JARVIS, a multi-school market-theory consultant. Synthesize "
        f"the following sage report into ONE paragraph (3-4 sentences) that a "
        f"trader can read in 5 seconds. Be specific, no hedging, no marketing "
        f"language.\n\n"
        f"Symbol: {symbol or 'asset'}\n"
        f"Composite bias: {report.composite_bias.value}\n"
        f"Conviction: {report.conviction:.2f}\n"
        f"Schools aligned with entry: {report.schools_aligned_with_entry}/"
        f"{report.schools_consulted}\n"
        f"Per-school verdicts:\n{school_lines}\n\n"
        f"Output ONLY the paragraph, no preamble."
    )
    resp = route_and_execute(
        category=TaskCategory.DOC_WRITING,
        user_message=prompt,
        max_tokens=350,
        # 350 tokens × $0.28/1M out = $0.000098 worst case; $0.005 cap is ample.
        max_cost_usd=0.005,
    )
    return resp.text or _template_narrative(report, symbol=symbol)

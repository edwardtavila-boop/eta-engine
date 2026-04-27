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
from typing import Any

from eta_engine.brain.jarvis_v3.sage.base import SageReport

logger = logging.getLogger(__name__)

_NARRATIVE_CACHE: dict[tuple[str, str], str] = {}
_CACHE_LOCK = threading.Lock()


def _template_narrative(report: SageReport, *, symbol: str = "") -> str:
    """Fallback deterministic narrative (no LLM call)."""
    aligned = report.schools_aligned_with_entry
    disagree = report.schools_disagreeing_with_entry
    n = report.schools_consulted

    # Top 3 aligned + top disagreeing for color
    sorted_schools = sorted(
        report.per_school.items(),
        key=lambda kv: kv[1].conviction,
        reverse=True,
    )
    top_align = [
        f"{name} ({v.conviction:.2f})"
        for name, v in sorted_schools
        if v.aligned_with_entry and v.conviction > 0.3
    ][:3]
    top_disagree = [
        f"{name} ({v.conviction:.2f})"
        for name, v in sorted_schools
        if not v.aligned_with_entry and v.bias.value != "neutral" and v.conviction > 0.3
    ][:2]

    parts = [
        f"{symbol or 'asset'}: sage reads {report.composite_bias.value} "
        f"with {report.conviction*100:.0f}% conviction "
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
    if use_llm and os.environ.get("ANTHROPIC_API_KEY"):
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
    """Call Claude Haiku for a synthesized narrative.

    Lazily imports anthropic so we don't require it as a hard dependency.
    """
    from anthropic import Anthropic
    client = Anthropic()
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
    msg = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=350,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()

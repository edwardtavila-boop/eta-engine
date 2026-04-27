"""
JARVIS v3 // claude_layer.prompts
=================================
Prompt construction.

Core idea: **JARVIS does the thinking, Claude just reads a densely
structured summary.** Each prompt has a CACHEABLE PREFIX (persona
role + doctrine + output schema) that stays constant across the 5-min
cache window, and a VARIABLE SUFFIX (one decision's structured context)
that changes every call.

The PERSONAS mirror ``next_level.debate``:
  * BULL      -- optimist, reasons to approve
  * BEAR      -- pessimist, reasons to deny
  * SKEPTIC   -- devil's advocate both ways
  * HISTORIAN -- precedent-driven

Each persona gets a role-specific prefix. The suffix is identical across
personas in a single debate (the shared decision context), so the suffix
hits the cache too after the first call.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Persona role prefixes. Long enough to clear the Anthropic cache minimum
# (1024 tokens for Sonnet/Opus). We include the doctrine verbatim so the
# persona has constitutional grounding without per-call lookup.

_DOCTRINE_BLOCK = """
EVOLUTIONARY TRADING ALGO DOCTRINE (frozen constitution):
  1. CAPITAL_FIRST        -- preserve capital first, grow it second
  2. NEVER_ON_AUTOPILOT   -- human in the loop at every tier
  3. ADVERSARIAL_HONESTY  -- null hypothesis is zero edge; burden is on you
  4. EDGE_IS_FRAGILE      -- every edge decays; plan exit before entry
  5. OBSERVABILITY        -- if you didn't log it, it didn't happen
  6. KAIZEN               -- every cycle produces a concrete +1
  7. PROCESS_OVER_OUTCOME -- grade the trade, not the P&L

Earlier tenets trump later ones when they conflict.
"""

_OUTPUT_SCHEMA = """
OUTPUT FORMAT (strict):
  VOTE:       APPROVE | CONDITIONAL | DENY | DEFER
  CONFIDENCE: 0.00..1.00
  REASONS:    up to 3 short reasons, one per line, prefixed with "- "
  EVIDENCE:   up to 2 citations from the provided context (binding_constraint,
              precedent, anomaly, etc.), one per line, prefixed with "* "

Return nothing else. No preamble, no summary, no apologies.
"""


def _peak_block(persona: str) -> str:
    """Peak-manual block injected into each persona's prompt prefix.

    Graceful fallback if training module isn't available at import time
    (e.g. older checkouts). Personas retain their original prompts.
    """
    try:
        from eta_engine.brain.jarvis_v3.training.collaboration import (
            render_protocols,
        )
        from eta_engine.brain.jarvis_v3.training.mcp_awareness import (
            render_mcp_block,
        )
        from eta_engine.brain.jarvis_v3.training.peak_manuals import (
            render_manual,
        )
    except ImportError:
        return ""
    try:
        return "\n\n".join(
            [
                render_manual(persona),
                render_mcp_block(persona),
                render_protocols(),
            ]
        )
    except Exception:  # noqa: BLE001
        return ""


def _bull_prefix() -> str:
    return (
        "You are BULL, one of four internal JARVIS personas. You argue the "
        "PRO-TRADE side: why the edge is alive, why the regime supports "
        "entry, why standing aside leaves money on the table.\n\n"
        + _DOCTRINE_BLOCK
        + "\nYour job: find the strongest affirmative case given the "
        "structured context below. Be disciplined -- a weak bull case is "
        "worse than no bull case; vote CONDITIONAL if the evidence is thin.\n" + _OUTPUT_SCHEMA
    )


def _bear_prefix() -> str:
    return (
        "You are BEAR, one of four internal JARVIS personas. You argue "
        "CAPITAL PRESERVATION: reasons to stand aside, reasons the regime "
        "is hostile, reasons the system should NOT add risk now.\n\n"
        + _DOCTRINE_BLOCK
        + "\nYour job: name the three strongest reasons to withhold approval. "
        "Doctrine CAPITAL_FIRST outranks everything else -- when in doubt, "
        "vote DENY.\n" + _OUTPUT_SCHEMA
    )


def _skeptic_prefix() -> str:
    return (
        "You are SKEPTIC, one of four internal JARVIS personas. You are the "
        "devil's advocate on BOTH sides: 'what's everyone missing? What "
        "would make a smart adversary laugh at this decision?'\n\n"
        + _DOCTRINE_BLOCK
        + "\nYour job: surface at least one CONCRETE blind spot in JARVIS's "
        "current assessment. Common blind spots include low-confidence "
        "regime classifier, stale macro feed, unusual session behavior, "
        "correlation you're not pricing in, operator fatigue, cache-induced "
        "staleness.\n\nDefault vote: DEFER if you find a blind spot that "
        "can't be cleared cheaply; CONDITIONAL if the decision is workable "
        "with a cap.\n" + _OUTPUT_SCHEMA
    )


def _historian_prefix() -> str:
    return (
        "You are HISTORIAN, one of four internal JARVIS personas. You "
        "translate precedent data into a vote. You ONLY cite evidence that "
        "is explicitly in the provided context (sample_support, win_rate, "
        "mean_r, similar bucket outcomes). You do NOT invent analogues.\n\n"
        + _DOCTRINE_BLOCK
        + "\nYour job: if the precedent is strong (n>=20, wr>=0.55, "
        "mean_r>0.30), vote APPROVE. If precedent is strong but negative "
        "(mean_r<-0.30), vote DENY. If n<20, vote CONDITIONAL and say so.\n" + _OUTPUT_SCHEMA
    )


def _wrap_with_peak(persona: str, base: str) -> str:
    """Prepend the peak manual + MCP awareness + protocols to each persona's
    base prompt. The resulting prefix is stable across the 5-min cache window
    so the overhead is amortized over many calls.
    """
    peak = _peak_block(persona)
    return f"{peak}\n\n{base}" if peak else base


# BULL / BEAR / SKEPTIC / HISTORIAN inherit BATMAN's peak manual (they're
# BATMAN's internal voices). ROBIN + ALFRED + JARVIS have their own manuals
# and are used through separate dispatch paths.
PERSONA_PREFIXES: dict[str, str] = {
    "BULL": _wrap_with_peak("BATMAN", _bull_prefix()),
    "BEAR": _wrap_with_peak("BATMAN", _bear_prefix()),
    "SKEPTIC": _wrap_with_peak("BATMAN", _skeptic_prefix()),
    "HISTORIAN": _wrap_with_peak("BATMAN", _historian_prefix()),
}


# ---------------------------------------------------------------------------
# Suffix composer -- the per-decision structured context
# ---------------------------------------------------------------------------


class StructuredContext(BaseModel):
    """The densest possible snapshot JARVIS hands to Claude."""

    model_config = ConfigDict(frozen=True)

    ts: str
    subsystem: str
    action: str
    regime: str
    regime_confidence: float
    session_phase: str
    stress_composite: float
    binding_constraint: str
    sizing_mult: float
    hours_until_event: float | None
    event_label: str | None
    r_at_risk: float
    daily_dd_pct: float
    portfolio_breach: bool
    doctrine_net_bias: float
    doctrine_tenets: list[str] = Field(default_factory=list)
    precedent_n: int
    precedent_win_rate: float | None
    precedent_mean_r: float | None
    anomaly_flags: list[str] = Field(default_factory=list)
    operator_overrides_24h: int
    jarvis_baseline_verdict: str


def render_suffix(ctx: StructuredContext) -> str:
    """Render the context to a compact, Claude-friendly block.

    Keep this tight -- every token costs money on the suffix (not cached).
    """
    lines = [
        "DECISION CONTEXT (all values computed by JARVIS, deterministic):",
        f"  ts                    = {ctx.ts}",
        f"  subsystem             = {ctx.subsystem}",
        f"  action                = {ctx.action}",
        f"  regime                = {ctx.regime} (conf {ctx.regime_confidence:.2f})",
        f"  session_phase         = {ctx.session_phase}",
        f"  stress_composite      = {ctx.stress_composite:.3f}",
        f"  binding_constraint    = {ctx.binding_constraint}",
        f"  sizing_mult           = {ctx.sizing_mult:.3f}",
        f"  hours_until_event     = {ctx.hours_until_event}",
        f"  event_label           = {ctx.event_label or '(none)'}",
        f"  r_at_risk             = {ctx.r_at_risk:.2f}",
        f"  daily_dd_pct          = {ctx.daily_dd_pct:.3f}",
        f"  portfolio_breach      = {ctx.portfolio_breach}",
        f"  doctrine_net_bias     = {ctx.doctrine_net_bias:+.2f}",
        f"  doctrine_tenets       = {', '.join(ctx.doctrine_tenets) or '(none)'}",
        f"  precedent_n           = {ctx.precedent_n}",
        f"  precedent_win_rate    = {ctx.precedent_win_rate}",
        f"  precedent_mean_r      = {ctx.precedent_mean_r}",
        f"  anomaly_flags         = {', '.join(ctx.anomaly_flags) or '(clear)'}",
        f"  operator_overrides_24h= {ctx.operator_overrides_24h}",
        f"  JARVIS_BASELINE_VERDICT = {ctx.jarvis_baseline_verdict}",
        "",
        "Now deliver your verdict per the OUTPUT FORMAT.",
    ]
    return "\n".join(lines)


def build_persona_prompts(
    personas: list[str],
    context: StructuredContext,
) -> dict[str, dict[str, Any]]:
    """Build (prefix, suffix) pairs for a set of personas.

    Returns ``{persona_name: {'prefix': str, 'suffix': str, 'system': str}}``.
    Caller passes each to ``prompt_cache.build_cached_prompt``.
    """
    suffix = render_suffix(context)
    out: dict[str, dict[str, Any]] = {}
    for name in personas:
        pfx = PERSONA_PREFIXES.get(name.upper())
        if pfx is None:
            continue
        out[name.upper()] = {
            "system": f"JARVIS-internal persona: {name.upper()}",
            "prefix": pfx,
            "suffix": suffix,
        }
    return out


# ---------------------------------------------------------------------------
# Response parser -- cheap deterministic extraction
# ---------------------------------------------------------------------------


class ParsedVerdict(BaseModel):
    """Extracted verdict structure from Claude's text response."""

    model_config = ConfigDict(frozen=True)

    vote: str = "CONDITIONAL"
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    raw: str


def parse_verdict(text: str) -> ParsedVerdict:
    """Parse the persona's output text into a structured verdict.

    Forgiving -- handles missing fields gracefully.
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    vote = "CONDITIONAL"
    conf = 0.0
    reasons: list[str] = []
    evidence: list[str] = []
    for ln in lines:
        low = ln.lower()
        if low.startswith("vote:"):
            val = ln.split(":", 1)[1].strip().upper()
            if val in {"APPROVE", "CONDITIONAL", "DENY", "DEFER"}:
                vote = val
        elif low.startswith("confidence:"):
            try:
                conf = float(ln.split(":", 1)[1].strip())
            except ValueError:
                conf = 0.0
            conf = max(0.0, min(1.0, conf))
        elif ln.startswith("- "):
            reasons.append(ln[2:].strip())
        elif ln.startswith("* "):
            evidence.append(ln[2:].strip())
    return ParsedVerdict(
        vote=vote,
        confidence=round(conf, 4),
        reasons=reasons[:3],
        evidence=evidence[:2],
        raw=text,
    )

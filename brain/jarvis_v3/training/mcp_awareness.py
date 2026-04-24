"""
JARVIS v3 // training.mcp_awareness
===================================
Which MCP tools each persona is cleared to use, with usage patterns.

Wire: persona -> (mcp_server, tool_name) -> UsagePattern. The eval
harness injects this into prompts so personas actually know what MCPs
they have at hand + how to invoke them.

Not a security boundary (that's mcp_registry.py). This is the
CAPABILITY MAP -- "here's what you can reach for when you need X."
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class UsagePattern(BaseModel):
    """How a persona should invoke a specific MCP tool."""
    model_config = ConfigDict(frozen=True)

    server:   str = Field(min_length=1)
    tool:     str = Field(min_length=1)
    when:     str = Field(min_length=1,
                          description="Trigger phrase / task condition")
    how:      str = Field(min_length=1,
                          description="Invocation pattern / argument shape")
    example:  str = ""


# ---------------------------------------------------------------------------
# BATMAN -- adversarial / architectural analysis
# ---------------------------------------------------------------------------

BATMAN_MCPS: list[UsagePattern] = [
    UsagePattern(
        server="tradingview", tool="pine_analyze",
        when="reviewing a Pine Script strategy for hidden bugs",
        how="chart_get_state -> pine_get_source -> pine_analyze for lookahead, repaint, and session issues",
        example="pine_analyze(source=..., checks=['lookahead','repaint','bounds'])",
    ),
    UsagePattern(
        server="tradingview", tool="data_get_strategy_results",
        when="validating a backtest claim in a red-team pass",
        how="Pull the results, compare claimed Sharpe / profit factor / max DD",
        example="data_get_strategy_results(symbol='MNQ1!', timeframe='5')",
    ),
    UsagePattern(
        server="tradingview", tool="batch_run",
        when="stress-testing across symbols / timeframes",
        how="batch_run the same strategy across 5+ symbol+TF pairs to find regime fragility",
        example="batch_run(symbols=['MNQ1!','NQ1!','ES1!'], timeframes=['5','15'])",
    ),
    UsagePattern(
        server="Desktop_Commander", tool="read_file",
        when="auditing a proposed architectural change",
        how="Read the files listed in the diff, cross-check imports + docstrings",
    ),
]

# ---------------------------------------------------------------------------
# ALFRED -- steady steward work
# ---------------------------------------------------------------------------

ALFRED_MCPS: list[UsagePattern] = [
    UsagePattern(
        server="Desktop_Commander", tool="write_file",
        when="delivering a code or doc change",
        how="Write the entire final file content (not a diff); operator reviews + commits",
    ),
    UsagePattern(
        server="Desktop_Commander", tool="edit_block",
        when="small surgical edit to an existing file",
        how="Use edit_block for surgical changes; avoid rewriting whole files unnecessarily",
    ),
    UsagePattern(
        server="Desktop_Commander", tool="start_process",
        when="running pytest / ruff / mypy after a change",
        how="start_process('pytest tests/test_X.py -q --tb=short'); poll via read_process_output",
    ),
    UsagePattern(
        server="tradingview", tool="pine_save",
        when="updating an existing Pine script non-adversarially",
        how="pine_open -> modify -> pine_save with a clear comment",
    ),
    UsagePattern(
        server="tradingview", tool="data_get_ohlcv",
        when="data-pipeline work (Databento backfill verification)",
        how="Pull OHLCV, spot-check a few bars against parquet cache",
    ),
]

# ---------------------------------------------------------------------------
# ROBIN -- grunt work (terse, mechanical)
# ---------------------------------------------------------------------------

ROBIN_MCPS: list[UsagePattern] = [
    UsagePattern(
        server="Desktop_Commander", tool="read_file",
        when="trivial lookup or log tail",
        how="read_file(path, offset, length); return just the requested lines",
    ),
    UsagePattern(
        server="Desktop_Commander", tool="get_file_info",
        when="size / mtime / exists? queries",
        how="get_file_info and return the one field asked for",
    ),
    UsagePattern(
        server="tradingview", tool="quote_get",
        when="current price snapshot",
        how="quote_get(symbol); return just last/bid/ask",
    ),
]

# ---------------------------------------------------------------------------
# JARVIS -- NONE. JARVIS is the policy engine. No MCP access on hot path.
# ---------------------------------------------------------------------------

JARVIS_MCPS: list[UsagePattern] = []


PERSONA_MCPS: dict[str, list[UsagePattern]] = {
    "BATMAN": BATMAN_MCPS,
    "ALFRED": ALFRED_MCPS,
    "ROBIN":  ROBIN_MCPS,
    "JARVIS": JARVIS_MCPS,
}


def mcps_for(persona: str) -> list[UsagePattern]:
    """Return the MCP usage patterns for a persona (case-insensitive)."""
    return PERSONA_MCPS.get(persona.upper(), [])


def render_mcp_block(persona: str) -> str:
    """Render usage patterns for injection into the persona's prompt."""
    patterns = mcps_for(persona)
    if not patterns:
        return f"{persona.upper()}: no MCP access (deterministic role)."
    lines = [f"=== {persona.upper()} :: MCP CAPABILITIES ===", ""]
    for p in patterns:
        lines.append(f"  [{p.server}::{p.tool}]")
        lines.append(f"    when: {p.when}")
        lines.append(f"    how:  {p.how}")
        if p.example:
            lines.append(f"    ex:   {p.example}")
        lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)

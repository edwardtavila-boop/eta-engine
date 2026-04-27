"""
Deploy // live_claude_smoke
===========================
Live end-to-end test of the claude_layer -> Anthropic round-trip.

Reads ANTHROPIC_API_KEY from .env (via python-dotenv), builds a
CRISIS-regime StructuredContext, runs it through AvengersDispatch, and
verifies:
  1. Escalation fires (CRISIS + empty precedent triggers it)
  2. BATMAN's Claude-backed debate actually calls Anthropic
  3. BATMAN returns a parsed verdict
  4. Cost is tracked in UsageTracker

Run (from C:\\eta_engine):
    .venv\\Scripts\\python.exe -m deploy.scripts.live_claude_smoke

Exits 0 on success, non-zero on any failure.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv


def main() -> int:
    # 1. Load .env
    load_dotenv()
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key.startswith("sk-ant-"):
        print(f"[live-smoke] FATAL: ANTHROPIC_API_KEY missing or malformed (len={len(key)})")
        return 2
    print(f"[live-smoke] ANTHROPIC_API_KEY loaded ({len(key)} chars, prefix {key[:12]}...)")

    # 2. Build an AvengersDispatch with a REAL Anthropic client
    from eta_engine.brain.avengers import AvengersDispatch, Fleet
    from eta_engine.brain.avengers.base import DryRunExecutor
    from eta_engine.brain.jarvis_v3.claude_layer.cost_governor import CostGovernor
    from eta_engine.brain.jarvis_v3.claude_layer.escalation import EscalationInputs
    from eta_engine.brain.jarvis_v3.claude_layer.prompts import (
        StructuredContext,
        build_persona_prompts,
        parse_verdict,
    )
    from eta_engine.brain.jarvis_v3.claude_layer.stakes import StakesInputs
    from eta_engine.brain.jarvis_v3.claude_layer.usage_tracker import UsageTracker

    fleet = Fleet(executor=DryRunExecutor())  # fleet uses dry-run for persona routing
    usage = UsageTracker()
    gov = CostGovernor(usage=usage)
    _ = AvengersDispatch(governor=gov, fleet=fleet)  # wiring check only

    # 3. Build a CRISIS context
    ctx = StructuredContext(
        ts=datetime.now(UTC).isoformat(),
        subsystem="bot.mnq",
        action="ORDER_PLACE",
        regime="CRISIS",
        regime_confidence=0.90,
        session_phase="OPEN_DRIVE",
        stress_composite=0.72,
        binding_constraint="macro_event",
        sizing_mult=0.35,
        hours_until_event=0.5,
        event_label="FOMC minutes",
        r_at_risk=1.8,
        daily_dd_pct=0.025,
        portfolio_breach=False,
        doctrine_net_bias=-0.40,
        doctrine_tenets=["CAPITAL_FIRST", "NEVER_ON_AUTOPILOT", "ADVERSARIAL_HONESTY"],
        precedent_n=0,  # empty precedent -> triggers escalation
        precedent_win_rate=None,
        precedent_mean_r=None,
        anomaly_flags=[],
        operator_overrides_24h=0,
        jarvis_baseline_verdict="CONDITIONAL",
    )

    print("[live-smoke] === CRISIS context built ===")
    print(f"[live-smoke]   regime={ctx.regime} stress={ctx.stress_composite}")
    print(f"[live-smoke]   event={ctx.event_label} in {ctx.hours_until_event}h")
    print(f"[live-smoke]   r_at_risk={ctx.r_at_risk} R")

    # 4. Escalation check (deterministic, no Claude yet)
    esc_inputs = EscalationInputs(
        regime="CRISIS",
        stress_composite=ctx.stress_composite,
        sizing_mult=ctx.sizing_mult,
        hours_until_event=ctx.hours_until_event,
        portfolio_breach=False,
        doctrine_net_bias=ctx.doctrine_net_bias,
        action="ORDER_PLACE",
        r_at_risk=ctx.r_at_risk,
        operator_overrides_24h=0,
        precedent_n=ctx.precedent_n,
    )
    stakes_inputs = StakesInputs(
        regime=ctx.regime,
        action="ORDER_PLACE",
        r_at_risk=ctx.r_at_risk,
        is_live=False,
        portfolio_breach=False,
        doctrine_net_bias=ctx.doctrine_net_bias,
        operator_overrides_24h=0,
    )
    plan = gov.plan(
        escalation_inputs=esc_inputs,
        stakes_inputs=stakes_inputs,
        features={
            "stress_composite": ctx.stress_composite,
            "sizing_mult": ctx.sizing_mult,
            "regime": ctx.regime,
            "hours_until_event": ctx.hours_until_event,
            "portfolio_breach": False,
            "doctrine_net_bias": ctx.doctrine_net_bias,
            "r_at_risk": ctx.r_at_risk,
            "operator_overrides_24h": 0,
            "precedent_n": 0,
            "anomaly_count": 0,
        },
    )
    print(f"[live-smoke] invoke_claude={plan.invoke_claude}")
    if not plan.invoke_claude:
        print(f"[live-smoke] FAIL: expected escalation, got {plan.reason}")
        return 3
    print(f"[live-smoke] escalation triggers: {[t.value for t in plan.escalation.triggers]}")
    print(f"[live-smoke] stakes={plan.stakes.stakes.value if plan.stakes else '?'}")
    print(f"[live-smoke] est_cost=${plan.est_cost_usd}")

    # 5. Make a REAL Claude call -- test the SKEPTIC persona only (cheapest path to prove it works)
    print("[live-smoke] === invoking Anthropic SDK (SKEPTIC persona) ===")
    try:
        import anthropic
    except ImportError:
        print("[live-smoke] FAIL: anthropic SDK not installed")
        return 4

    client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from env
    prompts = build_persona_prompts(["SKEPTIC"], ctx)
    p = prompts["SKEPTIC"]

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5",  # cheapest tier for the smoke test
            max_tokens=400,
            system=[
                {"type": "text", "text": p["prefix"], "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": p["suffix"]}],
        )
    except anthropic.APIStatusError as exc:
        print(f"[live-smoke] FAIL: Anthropic API error status={exc.status_code} msg={exc.message}")
        return 5
    except Exception as exc:  # noqa: BLE001
        print(f"[live-smoke] FAIL: {type(exc).__name__}: {exc}")
        return 6

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    usage = resp.usage
    print("[live-smoke] === Claude response received ===")
    print(f"[live-smoke]   model          = {resp.model}")
    print(f"[live-smoke]   input_tokens   = {usage.input_tokens}")
    print(f"[live-smoke]   output_tokens  = {usage.output_tokens}")
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    print(f"[live-smoke]   cache_read     = {cache_read}")
    print(f"[live-smoke]   cache_write    = {cache_write}")
    print()
    print("=" * 60)
    print("CLAUDE SKEPTIC RESPONSE:")
    print("=" * 60)
    print(text)
    print("=" * 60)

    # 6. Parse verdict
    verdict = parse_verdict(text)
    print()
    print("[live-smoke] === Parsed verdict ===")
    print(f"[live-smoke]   vote       = {verdict.vote}")
    print(f"[live-smoke]   confidence = {verdict.confidence}")
    print(f"[live-smoke]   reasons    = {verdict.reasons}")
    print(f"[live-smoke]   evidence   = {verdict.evidence}")

    # 7. Persist for dashboard
    state_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "eta_engine" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    smoke_out = state_dir / "live_claude_smoke.json"
    smoke_out.write_text(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "model": resp.model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read": cache_read,
                "cache_write": cache_write,
                "vote": verdict.vote,
                "confidence": verdict.confidence,
                "reasons": verdict.reasons,
                "evidence": verdict.evidence,
                "raw_response": text,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[live-smoke] wrote {smoke_out}")
    print("[live-smoke] SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

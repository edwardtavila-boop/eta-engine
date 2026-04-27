"""
EVOLUTIONARY TRADING ALGO  //  scripts.daily_premarket
==========================================
07:00 ET pre-market briefing.

What it does
------------
Reads the latest snapshot inputs (macro / equity / regime / journal) from
``docs/premarket_inputs.json``, runs them through ``brain.jarvis_context``
to compute a JarvisContext, and writes three outputs under ``docs/``:

  * ``premarket_latest.json``   -- full JarvisContext (machine-readable)
  * ``premarket_latest.txt``    -- 80-col human summary
  * ``premarket_log.jsonl``     -- append-only history

Usage
-----
    python -m eta_engine.scripts.daily_premarket
    python -m eta_engine.scripts.daily_premarket \
        --inputs-path docs/premarket_inputs.json \
        --out-dir docs/

The inputs file is a JSON object with four keys:
``macro``, ``equity``, ``regime``, ``journal``, each containing the
fields expected by the corresponding pydantic model in
``brain.jarvis_context``.

If the inputs file is absent the script writes a stub with neutral defaults
and exits 0, so cron / scheduler won't noisily alert on a missing feed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.brain.jarvis_context import (  # noqa: E402
    EquitySnapshot,
    JarvisContext,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    build_snapshot,
)

DEFAULT_INPUTS = ROOT / "docs" / "premarket_inputs.json"
DEFAULT_OUT_DIR = ROOT / "docs"


def _stub_context() -> JarvisContext:
    """Neutral default when no inputs file exists."""
    return build_snapshot(
        macro=MacroSnapshot(vix_level=None, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=0.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="UNKNOWN", confidence=0.5),
        journal=JournalSnapshot(),
        notes=["no premarket_inputs.json found -- stub snapshot"],
    )


def _load_inputs(path: Path) -> JarvisContext:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return build_snapshot(
        macro=MacroSnapshot(**raw.get("macro", {})),
        equity=EquitySnapshot(**raw["equity"]),
        regime=RegimeSnapshot(**raw["regime"]),
        journal=JournalSnapshot(**raw.get("journal", {})),
        notes=raw.get("notes"),
    )


def _render_text(ctx: JarvisContext) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("EVOLUTIONARY TRADING ALGO  //  PRE-MARKET BRIEFING  (Jarvis v2)")
    lines.append(f"ts: {ctx.ts.isoformat()}")
    if ctx.session_phase is not None:
        lines.append(f"session: {ctx.session_phase.value}")
    lines.append("=" * 72)
    lines.append("")
    lines.append(f"ACTION:       {ctx.suggestion.action}")
    lines.append(f"REASON:       {ctx.suggestion.reason}")
    lines.append(f"CONFIDENCE:   {ctx.suggestion.confidence:.0%}")
    if ctx.suggestion.warnings:
        lines.append("WARNINGS:")
        for w in ctx.suggestion.warnings:
            lines.append(f"  - {w}")
    lines.append("")
    # -- v2 block -----------------------------------------------------------
    if ctx.stress_score is not None:
        lines.append("STRESS")
        lines.append(
            f"  composite:   {ctx.stress_score.composite:.0%}   binding: {ctx.stress_score.binding_constraint}",
        )
        for c in sorted(
            ctx.stress_score.components,
            key=lambda x: x.contribution,
            reverse=True,
        ):
            lines.append(
                f"    {c.name:<14}  raw={c.value:.2f}  w={c.weight:.2f}  ctr={c.contribution:.3f}  ({c.note})",
            )
        lines.append("")
    if ctx.sizing_hint is not None:
        lines.append("SIZING")
        lines.append(
            f"  size_mult:   {ctx.sizing_hint.size_mult:.0%}   ({ctx.sizing_hint.reason})",
        )
        lines.append("")
    if ctx.alerts:
        lines.append(f"ALERTS ({len(ctx.alerts)})")
        for a in ctx.alerts[:10]:  # top 10
            lines.append(
                f"  [{a.level.value:<8}] {a.code:<28} sev={a.severity:.2f}  {a.message}",
            )
        if len(ctx.alerts) > 10:
            lines.append(f"  ... +{len(ctx.alerts) - 10} more")
        lines.append("")
    if ctx.margins is not None:
        lines.append("MARGINS TO NEXT TIER")
        m = ctx.margins
        lines.append(
            f"  dd->REDUCE:      {m.dd_to_reduce * 100:+.2f}%"
            f"   dd->STAND_ASIDE: {m.dd_to_stand_aside * 100:+.2f}%"
            f"   dd->KILL: {m.dd_to_kill * 100:+.2f}%",
        )
        lines.append(
            f"  overrides->REVIEW: {m.overrides_to_review:+d}   open_risk->CAP:    {m.open_risk_to_cap_r:+.2f}R",
        )
        lines.append("")
    if ctx.trajectory is not None and ctx.trajectory.samples > 0:
        t = ctx.trajectory
        lines.append("TRAJECTORY")
        lines.append(
            f"  dd={t.dd.value}  stress={t.stress.value}  "
            f"overrides/24h={t.overrides_velocity_per_24h:.2f}  "
            f"samples={t.samples}  window={t.window_seconds / 60:.1f}min",
        )
        lines.append("")
    if ctx.explanation:
        lines.append("JARVIS SAYS")
        lines.append(f"  {ctx.explanation}")
        lines.append("")
    if ctx.playbook:
        lines.append("PLAYBOOK")
        for step in ctx.playbook:
            lines.append(f"  - {step}")
        lines.append("")
    # -- raw facts ----------------------------------------------------------
    lines.append("REGIME")
    lines.append(
        f"  current:     {ctx.regime.regime} (conf {ctx.regime.confidence:.0%})",
    )
    if ctx.regime.previous_regime:
        lines.append(f"  previous:    {ctx.regime.previous_regime}")
    lines.append(f"  flipped:     {ctx.regime.flipped_recently}")
    lines.append("")
    lines.append("MACRO")
    lines.append(f"  bias:        {ctx.macro.macro_bias}")
    if ctx.macro.vix_level is not None:
        lines.append(f"  VIX:         {ctx.macro.vix_level:.2f}")
    if ctx.macro.next_event_label:
        lines.append(f"  next event:  {ctx.macro.next_event_label}")
        if ctx.macro.hours_until_next_event is not None:
            lines.append(
                f"  hours:       {ctx.macro.hours_until_next_event:.1f}",
            )
    lines.append("")
    lines.append("EQUITY / RISK")
    lines.append(f"  equity:      ${ctx.equity.account_equity:,.2f}")
    lines.append(f"  pnl today:   ${ctx.equity.daily_pnl:+,.2f}")
    lines.append(f"  dd today:    {ctx.equity.daily_drawdown_pct:.2%}")
    lines.append(f"  positions:   {ctx.equity.open_positions}")
    lines.append(f"  open risk:   {ctx.equity.open_risk_r:.2f}R")
    lines.append("")
    lines.append("JOURNAL (24h)")
    lines.append(f"  kill:        {ctx.journal.kill_switch_active}")
    lines.append(f"  mode:        {ctx.journal.autopilot_mode}")
    lines.append(f"  executed:    {ctx.journal.executed_last_24h}")
    lines.append(f"  blocked:     {ctx.journal.blocked_last_24h}")
    lines.append(f"  overrides:   {ctx.journal.overrides_last_24h}")
    lines.append(f"  corr alert:  {ctx.journal.correlations_alert}")
    if ctx.notes:
        lines.append("")
        lines.append("NOTES")
        for n in ctx.notes:
            lines.append(f"  - {n}")
    lines.append("")
    return "\n".join(lines)


def _write_outputs(ctx: JarvisContext, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    latest_json = out_dir / "premarket_latest.json"
    latest_txt = out_dir / "premarket_latest.txt"
    log_jsonl = out_dir / "premarket_log.jsonl"

    payload = ctx.model_dump(mode="json")
    latest_json.write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    latest_txt.write_text(_render_text(ctx), encoding="utf-8")
    with log_jsonl.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str) + "\n")

    return {
        "latest_json": latest_json,
        "latest_txt": latest_txt,
        "log_jsonl": log_jsonl,
    }


def run(
    *,
    inputs_path: Path = DEFAULT_INPUTS,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> JarvisContext:
    ctx = _load_inputs(inputs_path) if inputs_path.exists() else _stub_context()
    _write_outputs(ctx, out_dir)
    return ctx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EVOLUTIONARY TRADING ALGO daily pre-market briefing",
    )
    parser.add_argument("--inputs-path", type=Path, default=DEFAULT_INPUTS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)

    ctx = run(inputs_path=args.inputs_path, out_dir=args.out_dir)
    sys.stdout.write(f"[premarket] {ctx.ts.isoformat()} -> {ctx.suggestion.action} ({ctx.suggestion.reason})\n")
    return 0


if __name__ == "__main__":
    # ISO-format UTC for log prefix
    sys.stdout.write(f"[{datetime.now(UTC).isoformat()}] daily_premarket\n")
    sys.exit(main())

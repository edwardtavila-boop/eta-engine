"""EVOLUTIONARY TRADING ALGO  //  scripts._chaos_drill_matrix.

Chaos-drill coverage matrix generator.

Why this module exists
----------------------
:mod:`scripts.chaos_drill` runs 4 drills: breaker, deadman, push, drift.
But the project has accreted ~14 safety-critical surfaces since those
drills were scoped: kill-switch, risk-engine, CFTC gate, two-factor,
smart-router, Firm gate, OOS qualifier, shadow tracker, live-shadow,
drift detector, runtime allowlist, dataset manifest, TCA refit,
sweep firm gate.

This module enumerates every safety surface, classifies whether a
chaos drill exists for it, and writes a coverage report. Feeds CI so a
new safety module cannot ship without its matching drill row.

CLI
---
::

    python -m eta_engine.scripts._chaos_drill_matrix
    # → writes reports/chaos_drill_matrix.md + returns exit code
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "SafetySurface",
    "SURFACES",
    "coverage_report",
    "render_markdown",
    "main",
]


@dataclass(frozen=True)
class SafetySurface:
    """One safety-critical surface that must have a chaos drill."""

    surface: str
    module_path: str
    drill_id: str | None  # None == no drill yet
    notes: str = ""

    @property
    def has_drill(self) -> bool:
        return self.drill_id is not None


SURFACES: tuple[SafetySurface, ...] = (
    SafetySurface(
        surface="circuit_breaker",
        module_path="eta_engine.brain.avengers.circuit_breaker",
        drill_id="breaker",
        notes="Existing breaker drill in chaos_drill.py",
    ),
    SafetySurface(
        surface="deadman_switch",
        module_path="eta_engine.brain.avengers.deadman",
        drill_id="deadman",
        notes="Existing deadman drill in chaos_drill.py",
    ),
    SafetySurface(
        surface="push_bus",
        module_path="eta_engine.brain.avengers.push",
        drill_id="push",
        notes="Existing push-bus drill in chaos_drill.py",
    ),
    SafetySurface(
        surface="drift_detector",
        module_path="eta_engine.brain.avengers.drift_detector",
        drill_id="drift",
        notes="Existing drift drill in chaos_drill.py",
    ),
    SafetySurface(
        surface="kill_switch_runtime",
        module_path="eta_engine.core.kill_switch_runtime",
        drill_id="kill_switch_runtime",
        notes="v0.1.56 CLOSURE: breaches 3% daily loss cap + verifies FLATTEN_ALL",
    ),
    SafetySurface(
        surface="risk_engine",
        module_path="eta_engine.core.risk_engine",
        drill_id="risk_engine",
        notes="v0.1.56 CLOSURE: trips risk-pct / leverage / daily-loss / DD guards",
    ),
    SafetySurface(
        surface="cftc_nfa_compliance",
        module_path="eta_engine.core.cftc_nfa_compliance",
        drill_id="cftc_nfa_compliance",
        notes="v0.1.56 CLOSURE: hits OWNS_ACCOUNT / external capital / pool / blackout",
    ),
    SafetySurface(
        surface="two_factor",
        module_path="eta_engine.core.two_factor",
        drill_id="two_factor",
        notes="v0.1.56 CLOSURE: missing claim + stale claim + fresh claim paths",
    ),
    SafetySurface(
        surface="smart_router",
        module_path="eta_engine.core.smart_router",
        drill_id="smart_router",
        notes="v0.1.56 CLOSURE: post-only reject / fallback / iceberg reveal",
    ),
    SafetySurface(
        surface="firm_gate",
        module_path="eta_engine.brain.sweep_firm_gate",
        drill_id="firm_gate",
        notes="v0.1.56 CLOSURE: GO / KILL / raising-runner / None-runner branches",
    ),
    SafetySurface(
        surface="oos_qualifier",
        module_path="eta_engine.strategies.oos_qualifier",
        drill_id="oos_qualifier",
        notes="v0.1.56 CLOSURE: failing qualification + empty-bars fallback",
    ),
    SafetySurface(
        surface="shadow_paper_tracker",
        module_path="eta_engine.strategies.shadow_paper_tracker",
        drill_id="shadow_paper_tracker",
        notes="v0.1.56 CLOSURE: 3-window streak rule + losing-window gate + reset",
    ),
    SafetySurface(
        surface="live_shadow_guard",
        module_path="eta_engine.core.live_shadow",
        drill_id="live_shadow_guard",
        notes="v0.1.56 CLOSURE: full-fill slippage + exhausted book + invalid order",
    ),
    SafetySurface(
        surface="runtime_allowlist",
        module_path="eta_engine.strategies.runtime_allowlist",
        drill_id="runtime_allowlist",
        notes="v0.1.56 CLOSURE: TTL freshness + invalidate + base-ordering guarantees",
    ),
    SafetySurface(
        surface="pnl_drift",
        module_path="eta_engine.brain.pnl_drift",
        drill_id="pnl_drift",
        notes="v0.1.56 CLOSURE: stationary phase silent + regime break down-alarm",
    ),
    SafetySurface(
        surface="order_state_reconcile",
        module_path="eta_engine.core.order_state_reconcile",
        drill_id="order_state_reconcile",
        notes="v0.1.56 CLOSURE: fill / cancel / ghost / orphan divergences + idempotency",
    ),
)


@dataclass(frozen=True)
class CoverageReport:
    total: int
    covered: int
    missing: tuple[str, ...]
    coverage_pct: float
    details: tuple[SafetySurface, ...] = field(default_factory=tuple)


def coverage_report(surfaces: tuple[SafetySurface, ...] = SURFACES) -> CoverageReport:
    total = len(surfaces)
    covered = sum(1 for s in surfaces if s.has_drill)
    missing = tuple(s.surface for s in surfaces if not s.has_drill)
    pct = (covered / total * 100.0) if total else 0.0
    return CoverageReport(
        total=total,
        covered=covered,
        missing=missing,
        coverage_pct=round(pct, 1),
        details=surfaces,
    )


def render_markdown(report: CoverageReport) -> str:
    """Render the coverage report as a GitHub-flavored Markdown table."""
    lines: list[str] = []
    lines.append("# EVOLUTIONARY TRADING ALGO // Chaos Drill Coverage Matrix")
    lines.append("")
    lines.append(f"**Coverage:** {report.covered} / {report.total}  ({report.coverage_pct:.1f}%)")
    lines.append("")
    lines.append("| Surface | Module | Drill | Status | Notes |")
    lines.append("|---|---|---|---|---|")
    for surface in report.details:
        status = "[PASS]" if surface.has_drill else "[GAP]"
        drill = surface.drill_id or "(missing)"
        lines.append(f"| {surface.surface} | `{surface.module_path}` | {drill} | {status} | {surface.notes} |")
    lines.append("")
    if report.missing:
        lines.append("## Missing drills (priority order)")
        lines.append("")
        for name in report.missing:
            lines.append(f"- `{name}`")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chaos-drill coverage matrix.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/chaos_drill_matrix.md"),
        help="Output path for the Markdown report.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also write a JSON sidecar alongside the Markdown report.",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=0.0,
        help="Exit non-zero when coverage drops below this percent.",
    )
    args = parser.parse_args(argv)

    report = coverage_report()
    md = render_markdown(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")
    if args.json:
        payload = {
            "total": report.total,
            "covered": report.covered,
            "missing": list(report.missing),
            "coverage_pct": report.coverage_pct,
            "details": [
                {
                    "surface": s.surface,
                    "module_path": s.module_path,
                    "drill_id": s.drill_id,
                    "has_drill": s.has_drill,
                    "notes": s.notes,
                }
                for s in report.details
            ],
        }
        args.output.with_suffix(".json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if args.fail_under and report.coverage_pct < args.fail_under:
        print(
            f"chaos drill coverage {report.coverage_pct:.1f}% below threshold {args.fail_under:.1f}%",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

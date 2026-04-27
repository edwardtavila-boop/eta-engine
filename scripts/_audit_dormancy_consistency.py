"""
Audit operator-facing docs + production code for stale Tradovate refs.

Why this exists
---------------
The operator mandate (2026-04-24, see ``memory/broker_dormancy_mandate.md``)
puts Tradovate in DORMANT_BROKERS until funding clears. Live futures route
through IBKR (primary) + Tastytrade (fallback). Several docs were written
when Tradovate was the assumed Tier-A venue and continued referencing it
in operationally-load-bearing places (T-minus checklists, Phase 2 smoke,
emergency-stop UI, kill-switch HTTP-5xx triggers). v0.1.71 (this branch)
rewrote ``docs/live_launch_runbook.md``, ``docs/mnq_live_operations_protocol.md``,
and ``docs/edge_rules.md`` -- but a future contributor adding a new doc or
amending an existing one could re-introduce the same drift.

This script is the regression CI gate. It walks every markdown file in
``docs/`` (and the operator-facing Python entrypoints in ``scripts/``)
looking for ``tradovate`` references. Each hit must satisfy at least one
of:

  1. The line itself or one of the ±5 surrounding lines mentions
     ``DORMANT`` / ``dormant`` / ``DORMANT_BROKERS`` / ``Appendix A`` /
     ``un-dormancy`` / ``dormancy_mandate``.
  2. The file is on the explicit allowlist (historical artefacts,
     architectural reference docs, the dormancy mandate itself).

Anything else is flagged as a stale reference; exit 1.

Usage
-----
    python -m eta_engine.scripts._audit_dormancy_consistency

    # CI mode: refuse to pass if any stale ref found.
    python -m eta_engine.scripts._audit_dormancy_consistency --strict

    # Just print findings (default; exit 0 even if findings present).
    python -m eta_engine.scripts._audit_dormancy_consistency --report
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Files exempt from the dormancy-consistency check. Each entry is a
#: relative path (POSIX-style) under ``eta_engine/``. The exemption
#: is per-file, not per-line; the auditor will not scan these files at
#: all.
ALLOWLIST: frozenset[str] = frozenset(
    {
        # The mandates themselves -- canonical references, mention Tradovate
        # freely in their own discussion of the dormancy.
        "memory/databento_mandate.md",
        "memory/broker_dormancy_mandate.md",
        # Historical sprint briefs / lineage docs -- frozen-in-time records
        # of decisions, not operator-facing instructions.
        "docs/sprint_closing_brief_20260424.md",
        "docs/canonical_v1_verdict_full.md",
        "docs/p9_real_data_verdict.md",
        # Cross-regime / optimization analysis writeups -- historical record
        # of cost assumptions used during a specific sweep, not live-routing
        # instructions.
        "docs/cross_regime",
        # Latency-stress + analysis writeups -- assume-the-venue context,
        # historical analysis. Not operationally load-bearing.
        "docs/mnq_latency_stress.md",
        "docs/latency_stress_mnq_only_v2_2_dow_thu.md",
        "docs/tradingview_data_substitution.md",
        # Architecture overview -- describes adapter slots, not active
        # routing. The Tradovate adapter still ships; it just isn't routed.
        "docs/ARCHITECTURE.md",
        "docs/MNQ_ENGINE_BRIDGE.md",
        # The live-launch runbook's Appendix A is the un-dormancy procedure
        # itself; allowlisting the file would also skip the body, so we
        # special-case it via the context-token "Appendix A" check (which
        # the appendix's own header line carries). The file therefore is
        # NOT in the allowlist; instead the Appendix A heading propagates
        # dormancy-context across the rest of the appendix.
        # Bump-script archive -- versioned narratives, frozen. Match both
        # the archive subdirectory and any top-level bump scripts that
        # haven't been moved yet (each is a one-shot from a v0.1.x
        # release that we don't rewrite).
        "scripts/_legacy_bumps",
        "scripts/_bump_roadmap_v*.py",
        # The auditor itself naturally mentions Tradovate.
        "scripts/_audit_dormancy_consistency.py",
        # Tradovate-specific tooling whose entire purpose is the dormancy
        # un-dormancy procedure. These scripts exist to set up / authorize
        # / monitor Tradovate; they are inherently Tradovate-keyed.
        "scripts/setup_tradovate_secrets.py",
        "scripts/authorize_tradovate.py",
        "scripts/_tradovate_session_drift.py",
        # connect_brokers exposes per-venue CLI flags (--tradovate-demo /
        # --tradovate-live) for completeness; using those flags while
        # dormant is the operator's call. Allowlist file-level since the
        # flag-name strings are the only matches.
        "scripts/connect_brokers.py",
        # Historical / analysis scripts that reference Tradovate in
        # context-only (cost assumptions, CSV format, comments) -- not
        # live-routing instructions.
        "scripts/optimize_confluence_params.py",
        "scripts/slippage_stress_mnq.py",
        "scripts/sweep_real_mnq.py",
        "scripts/latency_stress_mnq.py",
        "scripts/dual_data_collector.py",
        "scripts/fetch_tradingview_bars.py",
        "scripts/paper_run_harness.py",
        "scripts/_jarvis_final_revision.py",
        # live_vs_paper_drift parses a Tradovate fill CSV format; the
        # CLI flag and parser stay regardless of dormancy.
        "scripts/live_vs_paper_drift.py",
        # mnq_live_supervisor exposes --tradovate-symbol; CLI flag-name
        # stays through dormancy.
        "scripts/mnq_live_supervisor.py",
    }
)

#: Words / phrases that, if they appear within ±CONTEXT_LINES of a
#: Tradovate reference, mark the reference as dormancy-aware.
DORMANCY_CONTEXT_TOKENS: frozenset[str] = frozenset(
    {
        "DORMANT",
        "dormant",
        "DORMANT_BROKERS",
        "Appendix A",
        "appendix-a",
        "un-dormancy",
        "un-dormants",
        "un-dormanted",
        "dormancy_mandate",
        "dormancy mandate",
        "dormancy banner",
        "DORMANCY",
    }
)

#: How many lines above + below the matched line to scan for context.
#: 5 was too narrow for markdown files where a section header (e.g.
#: ``## Appendix A -- When Tradovate Un-Dormants``) propagates the
#: dormancy context across many lines. Markdown files use the wider
#: window; Python files use the narrower one (closer-knit code).
CONTEXT_LINES_MD: int = 80
CONTEXT_LINES_PY: int = 10

_TRADOVATE_RE = re.compile(r"\btradovate\b", re.IGNORECASE)


def _is_allowlisted(rel_path: str) -> bool:
    """Return True if any allowlist prefix or glob pattern matches."""
    import fnmatch as _fnmatch

    for entry in ALLOWLIST:
        if rel_path == entry or rel_path.startswith(entry + "/"):
            return True
        if "*" in entry and _fnmatch.fnmatch(rel_path, entry):
            return True
    return False


def _has_dormancy_context(lines: list[str], idx: int, *, is_markdown: bool) -> bool:
    """Check ±CONTEXT_LINES around ``idx`` for a dormancy token.

    Markdown files get a wider window so a section header like
    ``## Appendix A -- When Tradovate Un-Dormants`` propagates the
    dormancy context to every line in that section (operationally,
    once the operator is reading the appendix they know they are in
    the un-dormancy path; further occurrences of "Tradovate" inside
    that section are not stale -- they are the procedure itself).
    """
    window_size = CONTEXT_LINES_MD if is_markdown else CONTEXT_LINES_PY
    lo = max(0, idx - window_size)
    hi = min(len(lines), idx + window_size + 1)
    window = "\n".join(lines[lo:hi])
    return any(tok in window for tok in DORMANCY_CONTEXT_TOKENS)


def _files_to_scan(root: Path) -> list[Path]:
    """Yield every doc + production-script file we want to audit."""
    out: list[Path] = []
    for pattern in ("docs/**/*.md", "scripts/*.py"):
        out.extend(p for p in root.glob(pattern) if p.is_file())
    return sorted(out)


def audit(root: Path = ROOT) -> list[tuple[str, int, str]]:
    """Walk the configured tree; return a list of ``(rel_path, line_no, line_text)``
    triples for every Tradovate reference that lacks dormancy context.
    """
    findings: list[tuple[str, int, str]] = []
    for path in _files_to_scan(root):
        rel = path.relative_to(root).as_posix()
        if _is_allowlisted(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        is_markdown = path.suffix.lower() == ".md"
        for idx, line in enumerate(lines):
            if not _TRADOVATE_RE.search(line):
                continue
            if _has_dormancy_context(lines, idx, is_markdown=is_markdown):
                continue
            findings.append((rel, idx + 1, line.strip()))
    return findings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any stale reference is found",
    )
    p.add_argument(
        "--report",
        action="store_true",
        help="print findings; do not exit non-zero (default)",
    )
    args = p.parse_args(argv)

    findings = audit()

    if not findings:
        print("dormancy_audit: clean -- every Tradovate ref is dormancy-aware")
        return 0

    print(
        f"dormancy_audit: {len(findings)} stale Tradovate reference(s) in operator-facing docs / scripts:",
    )
    for rel, line_no, text in findings:
        print(f"  {rel}:{line_no}: {text}")
    print()
    print(
        "Each match must mention DORMANT / Appendix A / dormancy_mandate "
        f"within ±{CONTEXT_LINES_MD} (markdown) or ±{CONTEXT_LINES_PY} "
        "(python) lines, OR the file must be on the explicit allowlist "
        "in scripts/_audit_dormancy_consistency.py.",
    )

    if args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

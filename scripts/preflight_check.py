"""
Preflight check — one-shot terminal Go/No-Go before live capital cutover.

Operator runs this from any shell that has eta_engine on the path:

    python -m eta_engine.scripts.preflight_check
    python -m eta_engine.scripts.preflight_check --json     # machine-readable
    python -m eta_engine.scripts.preflight_check --silent   # exit code only

Exit codes:
    0 — READY (every check is PASS or WARN; zero FAIL)
    1 — NOT READY (at least one FAIL)
    2 — preflight itself crashed (should never happen — checks are guarded)

The check is READ-ONLY: it touches nothing trading-related, sends no
Telegram, makes no broker calls. It just answers "should I push the
button?" with a single line.

Designed for the "I'm about to flip live capital — sanity check
everything one more time" use case before 2026-05-15.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from eta_engine.brain.jarvis_v3 import preflight

_STATUS_TAG = {
    "PASS": "[ OK ]",
    "WARN": "[WARN]",
    "FAIL": "[FAIL]",
}


def _status_tag(status: str) -> str:
    return _STATUS_TAG.get(status, "[????]")


def render(report: preflight.PreflightReport) -> str:
    """Build the operator-friendly text report (ASCII-only for Windows cp1252)."""
    bar = "=" * 64
    lines = [
        "",
        bar,
        f"  PREFLIGHT  -  {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        bar,
        "",
        f"  VERDICT  :  {report.verdict}",
        f"  PASS     :  {report.n_pass:>3}",
        f"  WARN     :  {report.n_warn:>3}",
        f"  FAIL     :  {report.n_fail:>3}",
        "",
        "-" * 64,
        "",
    ]

    # Group by status: FAIL first, then WARN, then PASS
    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    sorted_checks = sorted(report.checks, key=lambda c: (order.get(c.status, 9), c.name))

    for c in sorted_checks:
        tag = _status_tag(c.status)
        lines.append(f"  {tag}  {c.name:<32}  {c.detail}")
    lines.append("")

    if report.n_fail > 0:
        lines.append("  ACTION REQUIRED: resolve every [FAIL] check before pushing capital.")
        lines.append("")
    elif report.n_warn > 0:
        lines.append("  OK to proceed, but review [WARN] items -- they could become FAILs.")
        lines.append("")
    else:
        lines.append("  All systems green. Push the button.")
        lines.append("")

    lines.append(bar)
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live-cutover preflight Go/No-Go reporter.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human text",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Suppress stdout; exit code communicates result",
    )
    args = parser.parse_args(argv)

    try:
        report = preflight.run_preflight()
    except Exception as exc:  # noqa: BLE001
        if not args.silent:
            print(f"preflight crashed: {exc}", file=sys.stderr)
        return 2

    if args.silent:
        pass
    elif args.json:
        print(json.dumps(report.to_dict(), default=str, indent=2))
    else:
        print(render(report))

    return 0 if report.verdict == "READY" else 1


if __name__ == "__main__":
    sys.exit(main())

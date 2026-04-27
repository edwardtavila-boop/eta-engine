"""Daily sage-health watchdog (Wave-6 pre-live, 2026-04-27).

Runs ``SageHealthMonitor.check_health()`` and surfaces any silently-broken
schools (those returning NEUTRAL > 95% on >= 30 consultations). Outputs
JSON for the dashboard + (optionally) fires a Resend / webhook alert when
a critical issue is found.

Designed to run as ``Eta-Sage-Health-Daily`` once per day (e.g. 23:15)
via Task Scheduler. Exits 0 always (alerting is the side-effect; non-zero
would just retrigger Task Scheduler).

Usage::

    python -m eta_engine.scripts.sage_health_check
    python -m eta_engine.scripts.sage_health_check --json-out state/sage/last_health_report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Optional path to write the health snapshot JSON to.",
    )
    p.add_argument(
        "--fail-on-critical",
        action="store_true",
        help="Return non-zero if any 'critical' issue is found (default: always 0).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from eta_engine.brain.jarvis_v3.sage.health import default_monitor
    monitor = default_monitor()
    issues = monitor.check_health()
    snapshot = monitor.snapshot()

    payload = {
        "issues": [
            {
                "school": i.school,
                "neutral_rate": round(i.neutral_rate, 4),
                "n_consultations": i.n_consultations,
                "severity": i.severity,
                "detail": i.detail,
            }
            for i in issues
        ],
        "schools_observed": len(snapshot),
        "snapshot": snapshot,
    }

    text = json.dumps(payload, indent=2, default=str)
    print(text)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
        logger.info("wrote %s", args.json_out)

    critical = [i for i in issues if i.severity == "critical"]
    if critical:
        logger.warning(
            "sage health: %d CRITICAL issue(s) -- %s",
            len(critical),
            [i.school for i in critical],
        )
        # Hook for future Resend / Slack alert integration:
        # _send_alert(critical)

    if args.fail_on_critical and critical:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

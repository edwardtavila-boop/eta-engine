"""Operator-focused VPS failover readiness summary.

This command is read-only: it runs the same local checks as
``vps_failover_drill`` and reports only current red/amber blockers plus the
next commands embedded in each blocker.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import vps_failover_drill  # noqa: E402

if TYPE_CHECKING:
    from eta_engine.scripts.vps_failover_drill import CheckResult

_SEVERITY_ORDER = {"red": 3, "amber": 2, "skip": 1, "green": 0}


def _overall_severity(checks: list[CheckResult]) -> str:
    """Return the strongest severity in a set of checks."""
    return max((c.severity for c in checks), key=lambda sev: _SEVERITY_ORDER.get(sev, -1), default="green")


def _commands_for_check(check: CheckResult) -> list[str]:
    """Extract concrete next commands from a check's structured details."""
    details = check.details or {}
    commands: list[str] = []
    copy_commands = details.get("copy_commands")
    if isinstance(copy_commands, list):
        commands.extend(str(cmd) for cmd in copy_commands)
    copy_command = details.get("copy_command")
    if isinstance(copy_command, str) and not commands:
        commands.append(copy_command)
        commands.append("$EDITOR .env")
    vps_commands = details.get("vps_commands")
    if isinstance(vps_commands, list):
        commands.extend(str(cmd) for cmd in vps_commands)

    missing = details.get("missing")
    if isinstance(missing, list):
        missing_text = " ".join(str(item) for item in missing)
        if "decision_journal.jsonl" in missing_text:
            commands.append("python -m eta_engine.scripts.decision_journal_smoke --json")
        if "runtime_log.jsonl" in missing_text:
            commands.append("python -m eta_engine.scripts.runtime_log_smoke --json")
        if "drift_watchdog.jsonl" in missing_text:
            commands.append("python -m eta_engine.scripts.drift_watchdog_smoke --json")

    return list(dict.fromkeys(commands))


def build_summary(*, skip_backup_test: bool = True) -> dict[str, Any]:
    """Run DR checks and return a compact blocker summary."""
    checks = vps_failover_drill.collect_checks(skip_backup_test=skip_backup_test)
    blockers = [check for check in checks if check.severity in {"red", "amber"}]
    counts = {
        severity: sum(1 for check in checks if check.severity == severity)
        for severity in ("red", "amber", "green", "skip")
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "overall_severity": _overall_severity(checks),
        "exit_code": vps_failover_drill.exit_code_for_checks(checks),
        "counts": counts,
        "blockers": [
            {
                **asdict(check),
                "next_commands": _commands_for_check(check),
            }
            for check in blockers
        ],
    }


def _print_human(summary: dict[str, Any]) -> None:
    """Print an operator-friendly blocker summary."""
    print(f"VPS failover readiness: {summary['overall_severity'].upper()}")
    print("Counts: " + ", ".join(f"{key}={value}" for key, value in summary["counts"].items()))
    blockers = summary["blockers"]
    if not blockers:
        print("No red/amber blockers.")
        return
    print()
    print("Current blockers:")
    for blocker in blockers:
        print(f"- [{blocker['severity'].upper()}] {blocker['name']}: {blocker['summary']}")
        for command in blocker.get("next_commands", []):
            print(f"  next: {command}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vps_failover_summary")
    parser.add_argument(
        "--backup-test",
        action="store_true",
        help="include the tar/untar backup round-trip; skipped by default for a fast read-only summary",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable blocker summary")
    args = parser.parse_args(argv)

    summary = build_summary(skip_backup_test=not args.backup_test)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        _print_human(summary)
    return int(summary["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())

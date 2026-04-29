"""Refresh launch-critical market data and republish readiness surfaces.

This is the safe operator entrypoint for the futures datasets that directly
gate paper-live launch freshness:

* MNQ1 5m via yfinance
* MNQ1 1h via yfinance
* NQ1 5m via yfinance
* NQ1 1h via yfinance
* NQ1 4h via yfinance 1h -> 4h resampling
* NQ1 daily via Yahoo Finance
* ES1 5m via yfinance

Databento remains dormant here. The command only calls existing canonical ETA
scripts and runs from ``C:\\EvolutionaryTradingAlgo`` so all writes stay under
the workspace root.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent


@dataclass(frozen=True)
class StepResult:
    name: str
    command: list[str]
    returncode: int
    stdout_tail: str
    stderr_tail: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def build_plan(*, skip_inventory: bool = False, skip_verify: bool = False) -> list[tuple[str, list[str]]]:
    """Return the ordered refresh plan as ``(name, command)`` tuples."""
    py = sys.executable
    plan: list[tuple[str, list[str]]] = [
        (
            "mnq_5m",
            [py, "-m", "eta_engine.scripts.fetch_index_futures_bars", "--symbol", "MNQ", "--timeframe", "5m"],
        ),
        (
            "mnq_1h",
            [
                py,
                "-m",
                "eta_engine.scripts.fetch_index_futures_bars",
                "--symbol",
                "MNQ",
                "--timeframe",
                "1h",
                "--period",
                "730d",
            ],
        ),
        (
            "nq_5m",
            [py, "-m", "eta_engine.scripts.fetch_index_futures_bars", "--symbol", "NQ", "--timeframe", "5m"],
        ),
        (
            "nq_1h",
            [
                py,
                "-m",
                "eta_engine.scripts.fetch_index_futures_bars",
                "--symbol",
                "NQ",
                "--timeframe",
                "1h",
                "--period",
                "730d",
            ],
        ),
        (
            "nq_4h",
            [
                py,
                "-m",
                "eta_engine.scripts.fetch_index_futures_bars",
                "--symbol",
                "NQ",
                "--timeframe",
                "4h",
                "--period",
                "730d",
            ],
        ),
        (
            "es_5m",
            [py, "-m", "eta_engine.scripts.fetch_index_futures_bars", "--symbol", "ES", "--timeframe", "5m"],
        ),
        (
            "nq_daily",
            [py, "-m", "eta_engine.scripts.extend_nq_daily_yahoo"],
        ),
    ]
    if not skip_inventory:
        plan.append(("announce_data_library", [py, "-m", "eta_engine.scripts.announce_data_library"]))
    if not skip_verify:
        plan.append(("paper_live_launch_check", [py, "-m", "eta_engine.scripts.paper_live_launch_check", "--json"]))
    return plan


def _tail(text: str, *, max_lines: int = 20) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def run_step(name: str, command: list[str]) -> StepResult:
    """Run one plan step from the canonical workspace root."""
    completed = subprocess.run(  # noqa: S603 - commands are fixed module invocations built above.
        command,
        cwd=WORKSPACE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return StepResult(
        name=name,
        command=command,
        returncode=completed.returncode,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
    )


def run_plan(*, skip_inventory: bool = False, skip_verify: bool = False) -> dict[str, object]:
    """Run the refresh plan and stop on the first failed step."""
    results: list[StepResult] = []
    for name, command in build_plan(skip_inventory=skip_inventory, skip_verify=skip_verify):
        result = run_step(name, command)
        results.append(result)
        if not result.ok:
            break
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace_root": str(WORKSPACE_ROOT),
        "ok": all(result.ok for result in results),
        "steps": [asdict(result) | {"ok": result.ok} for result in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refresh_launch_data")
    parser.add_argument("--skip-inventory", action="store_true", help="skip data inventory republish")
    parser.add_argument("--skip-verify", action="store_true", help="skip paper-live readiness verification")
    parser.add_argument("--json", action="store_true", help="emit machine-readable summary")
    args = parser.parse_args(argv)

    summary = run_plan(skip_inventory=args.skip_inventory, skip_verify=args.skip_verify)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"[refresh-launch-data] workspace={summary['workspace_root']}")
        for step in summary["steps"]:
            status = "OK" if step["ok"] else "FAIL"
            print(f"[{status}] {step['name']}: {' '.join(step['command'])}")
            if step["stdout_tail"]:
                print(step["stdout_tail"])
            if step["stderr_tail"]:
                print(step["stderr_tail"], file=sys.stderr)
        print(f"[refresh-launch-data] ok={summary['ok']}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

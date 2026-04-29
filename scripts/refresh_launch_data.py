"""Refresh launch-critical market data and republish readiness surfaces.

This is the safe operator entrypoint for the futures datasets that directly
gate paper-live launch freshness:

* MNQ1 5m via yfinance
* MNQ1 1h via yfinance
* MNQ1 4h via yfinance 1h -> 4h resampling
* NQ1 5m via yfinance
* NQ1 1h via yfinance
* NQ1 4h via yfinance 1h -> 4h resampling
* NQ1 daily via Yahoo Finance
* ES1 5m via yfinance
* DXY 5m/1h, VIX 5m, and VIX 1m via yfinance context indexes
* Optional: Fear & Greed macro sentiment and SOL daily on-chain history

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
class PlanStep:
    name: str
    command: list[str]
    required: bool = True


@dataclass(frozen=True)
class StepResult:
    name: str
    command: list[str]
    required: bool
    returncode: int
    stdout_tail: str
    stderr_tail: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def build_plan(
    *,
    skip_inventory: bool = False,
    skip_verify: bool = False,
    skip_optional: bool = False,
) -> list[PlanStep]:
    """Return the ordered refresh plan."""
    py = sys.executable
    plan: list[PlanStep] = [
        PlanStep(
            "mnq_5m",
            [py, "-m", "eta_engine.scripts.fetch_index_futures_bars", "--symbol", "MNQ", "--timeframe", "5m"],
        ),
        PlanStep(
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
        PlanStep(
            "mnq_4h",
            [
                py,
                "-m",
                "eta_engine.scripts.fetch_index_futures_bars",
                "--symbol",
                "MNQ",
                "--timeframe",
                "4h",
                "--period",
                "730d",
            ],
        ),
        PlanStep(
            "nq_5m",
            [py, "-m", "eta_engine.scripts.fetch_index_futures_bars", "--symbol", "NQ", "--timeframe", "5m"],
        ),
        PlanStep(
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
        PlanStep(
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
        PlanStep(
            "es_5m",
            [py, "-m", "eta_engine.scripts.fetch_index_futures_bars", "--symbol", "ES", "--timeframe", "5m"],
        ),
        PlanStep(
            "dxy_5m",
            [py, "-m", "eta_engine.scripts.fetch_market_context_bars", "--symbol", "DXY", "--timeframe", "5m"],
        ),
        PlanStep(
            "dxy_1h",
            [py, "-m", "eta_engine.scripts.fetch_market_context_bars", "--symbol", "DXY", "--timeframe", "1h"],
        ),
        PlanStep(
            "vix_5m",
            [py, "-m", "eta_engine.scripts.fetch_market_context_bars", "--symbol", "VIX", "--timeframe", "5m"],
        ),
        PlanStep(
            "vix_1m",
            [py, "-m", "eta_engine.scripts.fetch_market_context_bars", "--symbol", "VIX", "--timeframe", "1m"],
        ),
        PlanStep(
            "nq_daily",
            [py, "-m", "eta_engine.scripts.extend_nq_daily_yahoo"],
        ),
    ]
    if not skip_optional:
        plan.extend([
            PlanStep(
                "fear_greed_macro",
                [py, "-m", "eta_engine.scripts.fetch_fear_greed_alternative"],
                required=False,
            ),
            PlanStep(
                "sol_onchain",
                [py, "-m", "eta_engine.scripts.fetch_onchain_history", "--symbol", "SOL"],
                required=False,
            ),
        ])
    if not skip_inventory:
        plan.append(PlanStep("announce_data_library", [py, "-m", "eta_engine.scripts.announce_data_library"]))
    if not skip_verify:
        plan.append(
            PlanStep("paper_live_launch_check", [py, "-m", "eta_engine.scripts.paper_live_launch_check", "--json"])
        )
    return plan


def _tail(text: str, *, max_lines: int = 20) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def run_step(step: PlanStep) -> StepResult:
    """Run one plan step from the canonical workspace root."""
    completed = subprocess.run(  # noqa: S603 - commands are fixed module invocations built above.
        step.command,
        cwd=WORKSPACE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return StepResult(
        name=step.name,
        command=step.command,
        required=step.required,
        returncode=completed.returncode,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
    )


def run_plan(
    *,
    skip_inventory: bool = False,
    skip_verify: bool = False,
    skip_optional: bool = False,
) -> dict[str, object]:
    """Run the refresh plan and stop on the first failed step."""
    results: list[StepResult] = []
    for step in build_plan(
        skip_inventory=skip_inventory,
        skip_verify=skip_verify,
        skip_optional=skip_optional,
    ):
        result = run_step(step)
        results.append(result)
        if not result.ok and result.required:
            break
    failed_required = [result.name for result in results if not result.ok and result.required]
    failed_optional = [result.name for result in results if not result.ok and not result.required]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace_root": str(WORKSPACE_ROOT),
        "ok": not failed_required,
        "failed_required": failed_required,
        "failed_optional": failed_optional,
        "steps": [asdict(result) | {"ok": result.ok} for result in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refresh_launch_data")
    parser.add_argument("--skip-inventory", action="store_true", help="skip data inventory republish")
    parser.add_argument("--skip-verify", action="store_true", help="skip paper-live readiness verification")
    parser.add_argument("--skip-optional", action="store_true", help="skip advisory optional feed refreshes")
    parser.add_argument("--json", action="store_true", help="emit machine-readable summary")
    args = parser.parse_args(argv)

    summary = run_plan(
        skip_inventory=args.skip_inventory,
        skip_verify=args.skip_verify,
        skip_optional=args.skip_optional,
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"[refresh-launch-data] workspace={summary['workspace_root']}")
        for step in summary["steps"]:
            status = "OK" if step["ok"] else ("OPTIONAL FAIL" if not step["required"] else "FAIL")
            print(f"[{status}] {step['name']}: {' '.join(step['command'])}")
            if step["stdout_tail"]:
                print(step["stdout_tail"])
            if step["stderr_tail"]:
                print(step["stderr_tail"], file=sys.stderr)
        print(f"[refresh-launch-data] ok={summary['ok']}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

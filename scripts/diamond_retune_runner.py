"""Run one paper-only diamond retune mission and write a receipt.

This is intentionally narrow: it executes only the registry-backed
``run_research_grid`` command emitted by ``diamond_retune_campaign``. It
does not place orders, edit the registry, mutate live routing, or promote a
bot. A successful research run still records ``broker_proof_required``.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts import workspace_roots  # noqa: E402

PYTHON_EXE = sys.executable
DEFAULT_CAMPAIGN_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "diamond_retune_campaign_latest.json"
OUT_LATEST = workspace_roots.ETA_RUNTIME_STATE_DIR / "diamond_retune_runner_latest.json"
ALLOWED_MODULE = "eta_engine.scripts.run_research_grid"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Executor = Callable[[list[str]], CommandResult]


def _as_float(value: Any, default: float = 0.0) -> float:  # noqa: ANN401
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def select_target(
    campaign: dict[str, Any],
    *,
    bot_id: str | None = None,
    rank: int = 1,
) -> dict[str, Any]:
    targets_raw = campaign.get("targets")
    targets = [row for row in targets_raw if isinstance(row, dict)] if isinstance(targets_raw, list) else []
    if bot_id:
        for target in targets:
            if str(target.get("bot_id") or "") == bot_id:
                return target
        msg = f"bot_id not in retune campaign: {bot_id}"
        raise ValueError(msg)
    ordered = sorted(targets, key=lambda row: int(_as_float(row.get("rank"), 999999)))
    if not ordered:
        raise ValueError("retune campaign has no targets")
    if rank < 1 or rank > len(ordered):
        msg = f"rank out of range: {rank}"
        raise ValueError(msg)
    return ordered[rank - 1]


def command_args_for_target(target: dict[str, Any]) -> list[str]:
    if target.get("safe_to_mutate_live") is not False:
        raise ValueError("target must explicitly be safe_to_mutate_live=false")
    if str(target.get("live_mutation_policy") or "") != "paper_only_advisory":
        raise ValueError("target must be paper_only_advisory")
    command = str(target.get("next_command") or "")
    parts = shlex.split(command)
    if len(parts) < 7:
        raise ValueError("target command is not an allowed registry research command")
    if parts[0].lower() not in {"python", "python.exe"}:
        raise ValueError("target command is not an allowed registry research command")
    if parts[1:3] != ["-m", ALLOWED_MODULE]:
        raise ValueError("target command is not an allowed registry research command")
    required = {
        "--source": "registry",
        "--report-policy": "runtime",
    }
    for flag, expected in required.items():
        if flag not in parts:
            raise ValueError("target command is not an allowed registry research command")
        idx = parts.index(flag)
        if idx + 1 >= len(parts) or parts[idx + 1] != expected:
            raise ValueError("target command is not an allowed registry research command")
    if "--bots" not in parts:
        raise ValueError("target command is not an allowed registry research command")
    return [PYTHON_EXE, *parts[1:]]


def _subprocess_executor(args: list[str], *, timeout_seconds: int) -> CommandResult:
    proc = subprocess.run(
        args,
        cwd=WORKSPACE_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout_seconds,
    )
    return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _status_for_returncode(returncode: int) -> str:
    if returncode == 124:
        return "research_timeout_keep_retuning"
    if returncode == 0:
        return "research_passed_broker_proof_required"
    return "research_failed_keep_retuning"


def run_campaign_once(
    campaign: dict[str, Any],
    *,
    out_path: Path = OUT_LATEST,
    bot_id: str | None = None,
    rank: int = 1,
    timeout_seconds: int = 1800,
    executor: Callable[[list[str]], CommandResult] | None = None,
) -> dict[str, Any]:
    target = select_target(campaign, bot_id=bot_id, rank=rank)
    args = command_args_for_target(target)
    started = datetime.now(UTC)
    run = executor or _subprocess_executor
    try:
        result = run(args, timeout_seconds=timeout_seconds)
    except (subprocess.TimeoutExpired, TimeoutError) as exc:
        result = CommandResult(returncode=124, stdout="", stderr=f"{type(exc).__name__}: {exc}")
    finished = datetime.now(UTC)
    receipt = {
        "kind": "eta_diamond_retune_runner",
        "generated_at_utc": finished.isoformat(),
        "campaign_generated_at_utc": campaign.get("generated_at_utc"),
        "selected_target": {
            "rank": target.get("rank"),
            "bot_id": target.get("bot_id"),
            "symbol": target.get("symbol"),
            "asset_sleeve": target.get("asset_sleeve"),
            "priority_score": target.get("priority_score"),
            "next_command": target.get("next_command"),
        },
        "started_at_utc": started.isoformat(),
        "finished_at_utc": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "exit_code": result.returncode,
        "status": _status_for_returncode(result.returncode),
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
        "promotion_block": "broker_proof_required",
        "live_mutation_policy": "paper_only_advisory",
        "safe_to_mutate_live": False,
    }
    workspace_roots.ensure_parent(out_path)
    out_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-path", type=Path, default=DEFAULT_CAMPAIGN_PATH)
    parser.add_argument("--out-path", type=Path, default=OUT_LATEST)
    parser.add_argument("--bot", default=None)
    parser.add_argument("--rank", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    receipt = run_campaign_once(
        _load_json(args.campaign_path),
        out_path=args.out_path,
        bot_id=args.bot,
        rank=args.rank,
        timeout_seconds=args.timeout_seconds,
    )
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        target = receipt["selected_target"]
        print(
            f"diamond retune runner: {target['bot_id']} "
            f"status={receipt['status']} exit={receipt['exit_code']}",
        )
    # A no-PASS research run is a valid strategy outcome, not a scheduler
    # failure. Unexpected exceptions still raise and surface as task failures.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

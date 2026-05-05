"""Three-AI autonomous coordination daemon.

Runs Claude as architect, DeepSeek as implementer, and Codex as verifier in a
repeatable coordination cycle. Runtime state is written only under the
canonical workspace var/eta_engine/state tree.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[2]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from eta_engine.brain.model_policy import TaskCategory  # noqa: E402
from eta_engine.scripts import workspace_roots  # noqa: E402

DEFAULT_CYCLE_INTERVAL_SEC = 600
DEFAULT_MAX_TOKENS = 300


@dataclass(frozen=True)
class RoleTask:
    result_key: str
    category: TaskCategory
    ai: str
    system_prompt: str
    user_message: str


RouteFn = Callable[..., object]
SleepFn = Callable[[float], None]


def role_tasks(cycle_id: str) -> tuple[RoleTask, ...]:
    return (
        RoleTask(
            result_key="architecture",
            category=TaskCategory.ARCHITECTURE_DECISION,
            ai="Claude",
            system_prompt=(
                "You are Claude, Lead Architect. Review the current state and identify the single "
                "highest-leverage action."
            ),
            user_message=f"Current state: autonomous coordination cycle {cycle_id}. Execute architecture review.",
        ),
        RoleTask(
            result_key="implementation",
            category=TaskCategory.STRATEGY_EDIT,
            ai="DeepSeek",
            system_prompt="You are DeepSeek, Worker Bee. Generate concrete implementation guidance.",
            user_message=f"Current state: autonomous coordination cycle {cycle_id}. Execute implementation planning.",
        ),
        RoleTask(
            result_key="verification",
            category=TaskCategory.TEST_EXECUTION,
            ai="Codex",
            system_prompt="You are Codex, Systems Expert. Verify system integrity and report anomalies.",
            user_message=f"Current state: autonomous coordination cycle {cycle_id}. Execute verification review.",
        ),
    )


def _provider_value(provider: object) -> str:
    return str(getattr(provider, "value", provider))


def _route_default(**kwargs: object) -> object:
    from eta_engine.brain.multi_model import route_and_execute

    return route_and_execute(**kwargs)


def run_coordination_cycle(
    *,
    route: RouteFn | None = None,
    now: datetime | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Run one three-role coordination cycle.

    ``route`` is injectable so tests and dry-run harnesses never need live LLM
    credentials. Provider failures degrade the cycle rather than crashing the
    daemon; each role records its own error.
    """
    route = route or _route_default
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    cycle_id = f"CYC-{now.strftime('%Y%m%dT%H%M%S')}"
    report: dict[str, Any] = {
        "cycle_id": cycle_id,
        "ts": now.isoformat(),
        "status": "running",
        "results": {},
    }

    error_count = 0
    for task in role_tasks(cycle_id):
        started = time.perf_counter()
        try:
            response = route(
                category=task.category,
                system_prompt=task.system_prompt,
                user_message=task.user_message,
                max_tokens=max_tokens,
            )
            report["results"][task.result_key] = {
                "ai": task.ai,
                "provider": _provider_value(response.provider),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "fallback": bool(getattr(response, "fallback_used", False)),
                "text": str(getattr(response, "text", ""))[:500],
            }
        except Exception as exc:  # noqa: BLE001
            error_count += 1
            report["results"][task.result_key] = {
                "ai": task.ai,
                "error": str(exc),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
            }

    if error_count == 0:
        report["status"] = "complete"
    elif error_count == len(report["results"]):
        report["status"] = "failed"
    else:
        report["status"] = "degraded"
    return report


def write_report(
    report: dict[str, Any],
    *,
    state_root: Path | None = None,
) -> dict[str, Path]:
    state_root = state_root or workspace_roots.ETA_RUNTIME_STATE_DIR
    state_root.mkdir(parents=True, exist_ok=True)
    jsonl_path = state_root / "three_ai_autonomous.jsonl"
    latest_path = state_root / "three_ai_latest.json"
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, default=str, sort_keys=True) + "\n")
    latest_path.write_text(json.dumps(report, default=str, indent=2, sort_keys=True), encoding="utf-8")
    return {"jsonl": jsonl_path, "latest": latest_path}


def print_cycle_summary(report: dict[str, Any]) -> None:
    print(f"  Results: {report['status']} | {report['cycle_id']}")
    for result in report.get("results", {}).values():
        ai = result.get("ai", "unknown")
        if "error" in result:
            print(f"  [{ai}] ERROR: {str(result['error'])[:80]}")
        else:
            fallback = " FALLBACK" if result.get("fallback") else ""
            print(f"  [{ai}] {result.get('provider')} ({result.get('elapsed_ms')}ms){fallback}")


def run_loop(
    *,
    interval_sec: float = DEFAULT_CYCLE_INTERVAL_SEC,
    max_cycles: int | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    state_root: Path | None = None,
    route: RouteFn | None = None,
    sleep: SleepFn = time.sleep,
) -> int:
    print(f"Three-AI Autonomous Daemon starting (cycle={interval_sec:g}s)")
    print(f"Workspace: {WORKSPACE}")
    print(f"State: {state_root or workspace_roots.ETA_RUNTIME_STATE_DIR}")
    print()

    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        cycle += 1
        print(f"[Cycle {cycle}] {datetime.now(UTC).strftime('%H:%M:%S')} - dispatching...")
        report = run_coordination_cycle(route=route, max_tokens=max_tokens)
        write_report(report, state_root=state_root)
        print_cycle_summary(report)
        print()
        if max_cycles is None or cycle < max_cycles:
            sleep(interval_sec)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument("--max-cycles", type=int, default=None, help="Run N cycles and exit.")
    parser.add_argument("--interval-sec", type=float, default=DEFAULT_CYCLE_INTERVAL_SEC)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--state-root", type=Path, default=workspace_roots.ETA_RUNTIME_STATE_DIR)
    args = parser.parse_args(argv)

    max_cycles = 1 if args.once else args.max_cycles
    return run_loop(
        interval_sec=args.interval_sec,
        max_cycles=max_cycles,
        max_tokens=args.max_tokens,
        state_root=args.state_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())

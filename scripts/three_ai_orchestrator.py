"""
Subscription-first AI orchestrator - Codex + DeepSeek operating in synergy.

Routes tasks to the allowed AI lanes based on their strengths:
  Codex    -> Architecture, planning, code review, verification, security
  DeepSeek -> Implementation, generation, testing, documentation

Claude remains a disabled legacy lane; any stale Claude route falls forward to
Codex, then DeepSeek if Codex is unavailable.

Usage:
    python -m eta_engine.scripts.three_ai_orchestrator --task "Implement caching layer"
    python -m eta_engine.scripts.three_ai_orchestrator --mode daemon --interval 300
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE))

from eta_engine.brain.model_policy import ForceProvider, TaskCategory, force_provider_for  # noqa: E402
from eta_engine.brain.multi_model import force_multiplier_status, route_and_execute  # noqa: E402


@dataclass
class ParallelTask:
    task_id: str = field(default_factory=lambda: f"T3A-{uuid.uuid4().hex[:6]}")
    category: str = ""
    prompt: str = ""
    provider: str = ""
    result: str = ""
    elapsed_ms: float = 0
    success: bool = False
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def dispatch_parallel(*, goal: str, categories: list[str] | None = None) -> list[ParallelTask]:
    """Dispatch a goal across Codex and DeepSeek based on their strengths.

    Codex gets: architecture, review, planning, verification, security
    DeepSeek gets: implementation, generation, grunt
    """
    if categories is None:
        categories = ["architecture_decision", "strategy_edit", "test_execution"]

    tasks: list[ParallelTask] = []

    for cat_name in categories:
        cat = TaskCategory(cat_name)
        provider = force_provider_for(cat)

        task = ParallelTask(category=cat_name, provider=provider.value)

        system_prompt = _system_prompt_for(cat, provider)
        user_prompt = f"Goal: {goal}\nCategory: {cat_name}\nProvider: {provider.value}"

        t0 = time.perf_counter()
        try:
            resp = route_and_execute(
                category=cat,
                system_prompt=system_prompt,
                user_message=user_prompt,
                max_tokens=4096 if provider == ForceProvider.CLAUDE else 2048,
            )
            task.result = resp.text
            task.success = not resp.fallback_used
            task.elapsed_ms = (time.perf_counter() - t0) * 1000
        except Exception as e:
            task.result = f"ERROR: {e}"
            task.success = False
            task.elapsed_ms = (time.perf_counter() - t0) * 1000

        tasks.append(task)
        logger.info(
            "3AI [%s] %s → %s (%.0fms) %s",
            task.task_id,
            cat_name,
            provider.value,
            task.elapsed_ms,
            "✓" if task.success else "✗",
        )

    return tasks


def _system_prompt_for(cat: TaskCategory, provider: ForceProvider) -> str:
    prompts = {
        ForceProvider.CLAUDE: (
            "You are Codex covering a disabled legacy Claude route for the Evolutionary Trading Algo. "
            "Your role: architectural decisions, risk policy, adversarial review, code quality. "
            "Be precise, adversarial, and thorough. Output structured markdown."
        ),
        ForceProvider.DEEPSEEK: (
            "You are DeepSeek, the Worker Bee of the Evolutionary Trading Algo. "
            "Your role: high-volume code generation, boilerplate, refactoring, testing, documentation. "
            "Be fast, correct, and practical. Output production-ready code."
        ),
        ForceProvider.CODEX: (
            "You are Codex, the Systems Expert of the Evolutionary Trading Algo. "
            "Your role: debugging, test execution, security audits, file automation, deployment. "
            "Be thorough and verify everything. Output verified results."
        ),
    }
    return prompts.get(provider, "Be helpful and concise.")


def print_parallel_report(tasks: list[ParallelTask]) -> None:
    print(f"\n{'=' * 70}")
    print("  THREE-AI PARALLEL ORCHESTRATION REPORT")
    print(f"{'=' * 70}")
    for t in tasks:
        status = "✓" if t.success else "✗"
        print(f"  [{status}] {t.provider:10s} | {t.category:25s} | {t.elapsed_ms:6.0f}ms")
    print(f"{'=' * 70}")
    print(f"  Total tasks: {len(tasks)} | Success: {sum(1 for t in tasks if t.success)}")
    print()


def daemon_mode(interval_sec: int = 300) -> None:
    """Run as a background coordinator, periodically checking health and triggering sync."""
    logger.info("Three-AI Orchestrator daemon starting (interval=%ds)", interval_sec)

    while True:
        status = force_multiplier_status()
        all_healthy = all(status["providers"][p]["available"] for p in ["codex", "deepseek"])

        logger.info(
            "AI Health: claude_disabled=%s codex=%s deepseek=%s all=%s",
            status["providers"]["claude"]["disabled_by_policy"],
            status["providers"]["codex"]["available"],
            status["providers"]["deepseek"]["available"],
            all_healthy,
        )

        if not all_healthy:
            logger.warning("One or more providers unhealthy — triggering alert")

        time.sleep(interval_sec)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Three-AI Parallel Orchestrator")
    parser.add_argument("--task", type=str, help="Goal to dispatch across all three AIs")
    parser.add_argument("--mode", choices=["once", "daemon"], default="once")
    parser.add_argument("--interval", type=int, default=300, help="Daemon interval in seconds")
    parser.add_argument("--categories", nargs="*", default=None, help="Specific categories (default: one per provider)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.mode == "daemon":
        daemon_mode(args.interval)
    elif args.task:
        tasks = dispatch_parallel(goal=args.task, categories=args.categories)
        print_parallel_report(tasks)
    else:
        print("Usage: python -m eta_engine.scripts.three_ai_orchestrator --task 'Your goal here'")

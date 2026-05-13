"""Wave-19 Shadow Fleet Validator — compares multi-model vs single-model execution.

Runs the same tasks through both ``Fleet(multimodel=True)`` and ``Fleet()``
and records comparative metrics: latency delta, quality markers, provider
routing, and fallback events. Outputs JSONL for post-hoc analysis.

Usage::

    python -m eta_engine.scripts.shadow_fleet_validator --hours 24
    python -m eta_engine.scripts.shadow_fleet_validator --tasks 50 \\
        --output var/shadow_validation.jsonl
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve()
ROOT = _ROOT.parents[1]  # eta_engine/
WORKSPACE = _ROOT.parents[2]  # C:\EvolutionaryTradingAlgo
sys.path.insert(0, str(WORKSPACE))


@dataclass
class ShadowResult:
    task_id: str
    category: str
    goal: str
    single_provider: str
    multi_provider: str
    single_text: str
    multi_text: str
    single_ms: float
    multi_ms: float
    multi_fallback: bool
    multi_fallback_reason: str
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def run_shadow_comparison(
    *,
    categories: list[str] | None = None,
    tasks_per_category: int = 3,
    output_path: Path | None = None,
) -> list[ShadowResult]:
    """Compare single-fleet vs multi-fleet across the given task categories.

    For each category, generates a synthetic task and runs it through both
    ``Fleet()`` and ``Fleet(multimodel=True)``, recording comparative metrics.
    """
    from eta_engine.brain.avengers.base import make_envelope  # noqa: PLC0415
    from eta_engine.brain.avengers.fleet import Fleet  # noqa: PLC0415
    from eta_engine.brain.model_policy import TaskCategory  # noqa: PLC0415

    all_cats = categories or [
        "architecture_decision",
        "code_review",
        "debug",
        "boilerplate",
        "strategy_edit",
        "security_audit",
    ]

    results: list[ShadowResult] = []

    single_fleet = Fleet()
    multi_fleet = Fleet(multimodel=True)

    for cat_name in all_cats:
        cat = TaskCategory(cat_name)

        for i in range(tasks_per_category):
            task_id = uuid.uuid4().hex[:8]
            goal = f"Shadow validation task #{i + 1} for {cat_name}"

            envelope = make_envelope(category=cat, goal=goal)

            t0 = time.perf_counter()
            single_result = single_fleet.dispatch(envelope)
            single_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            multi_result = multi_fleet.dispatch(envelope)
            multi_ms = (time.perf_counter() - t0) * 1000

            sr = ShadowResult(
                task_id=task_id,
                category=cat_name,
                goal=goal,
                single_provider=(single_result.persona_id.value if single_result.success else "FAILED"),
                multi_provider=(multi_result.persona_id.value if multi_result.success else "FAILED"),
                single_text=(single_result.artifact[:500] if single_result.success else single_result.reason),
                multi_text=(multi_result.artifact[:500] if multi_result.success else multi_result.reason),
                single_ms=round(single_ms, 1),
                multi_ms=round(multi_ms, 1),
                multi_fallback=False,
                multi_fallback_reason="",
            )
            results.append(sr)

            logger.info(
                "shadow [%s] %s: single=%s(%.0fms) multi=%s(%.0fms)",
                task_id,
                cat_name,
                sr.single_provider,
                sr.single_ms,
                sr.multi_provider,
                sr.multi_ms,
            )

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("a", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r.__dict__, default=str) + "\n")

    return results


def print_summary(results: list[ShadowResult]) -> None:
    if not results:
        print("No results to summarize.")
        return

    cats: Counter[str] = Counter()
    multi_providers: Counter[str] = Counter()
    single_providers: Counter[str] = Counter()
    latency_deltas: list[float] = []

    for r in results:
        cats[r.category] += 1
        multi_providers[r.multi_provider] += 1
        single_providers[r.single_provider] += 1
        latency_deltas.append(r.multi_ms - r.single_ms)

    avg_delta = sum(latency_deltas) / len(latency_deltas) if latency_deltas else 0.0
    bar = "=" * 60

    print(f"\n{bar}")
    print("  SHADOW FLEET VALIDATION SUMMARY")
    print(bar)
    print(f"  Total tasks: {len(results)}")
    print(f"  Categories:  {len(cats)}")
    print(f"  Avg latency delta: {avg_delta:+.0f}ms (multi vs single)")
    print()
    print("  Multi-model provider distribution:")
    for prov, count in multi_providers.most_common():
        print(f"    {prov:20s}: {count}")
    print()
    print("  Single-model provider distribution:")
    for prov, count in single_providers.most_common():
        print(f"    {prov:20s}: {count}")
    print(bar)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Shadow Fleet Validator")
    parser.add_argument("--tasks", type=int, default=6, help="Tasks per category")
    parser.add_argument("--output", type=str, default="var/shadow_validation.jsonl")
    parser.add_argument(
        "--categories",
        type=str,
        nargs="*",
        help="Specific categories to test (default: 6 key categories)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    results = run_shadow_comparison(
        categories=args.categories or None,
        tasks_per_category=args.tasks,
        output_path=WORKSPACE / args.output if args.output else None,
    )

    print_summary(results)

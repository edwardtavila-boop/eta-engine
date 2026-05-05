"""
Three-AI Coordination Sync — automated trigger for parallel AI cooperation.

Runs Claude (architect), DeepSeek (worker), and Codex (verifier) in sequence
on the current roadmap state. Each AI processes its lane and hands off to the next.

Intended to run as a scheduled task every 4 hours.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE))


def run_coordination_cycle() -> dict[str, object]:
    """One full coordination cycle across all three AIs."""
    from eta_engine.brain.model_policy import TaskCategory
    from eta_engine.brain.multi_model import force_multiplier_status, route_and_execute

    report = {
        "cycle_id": f"CYC-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}",
        "ts": datetime.now(UTC).isoformat(),
        "phases": {},
    }

    # Phase 1: Claude reviews current state
    t0 = time.perf_counter()
    status = force_multiplier_status()
    try:
        resp = route_and_execute(
            category=TaskCategory.ARCHITECTURE_DECISION,
            system_prompt=(
                "You are Claude, Lead Architect. Review the current system "
                "state and identify the highest-leverage next action."
            ),
            user_message=(
                "System status: "
                f"{json.dumps({k: v.get('available') for k, v in status['providers'].items()})}. "
                f"Health: {status['mode']}. Review and give one tactical recommendation."
            ),
            max_tokens=500,
        )
        report["phases"]["claude_review"] = {
            "provider": resp.provider.value,
            "text": resp.text[:500],
            "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "fallback": resp.fallback_used,
        }
    except Exception as e:
        report["phases"]["claude_review"] = {"error": str(e)}

    # Phase 2: DeepSeek generates implementation
    t0 = time.perf_counter()
    try:
        resp = route_and_execute(
            category=TaskCategory.STRATEGY_EDIT,
            system_prompt="You are DeepSeek, Worker Bee. Generate a concise implementation plan.",
            user_message="Based on system health check, what code changes are needed? Be specific and concise.",
            max_tokens=300,
        )
        report["phases"]["deepseek_plan"] = {
            "provider": resp.provider.value,
            "text": resp.text[:500],
            "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "fallback": resp.fallback_used,
        }
    except Exception as e:
        report["phases"]["deepseek_plan"] = {"error": str(e)}

    # Phase 3: Codex verifies
    t0 = time.perf_counter()
    try:
        resp = route_and_execute(
            category=TaskCategory.TEST_EXECUTION,
            system_prompt="You are Codex, Systems Expert. Verify system integrity.",
            user_message="Run verification: are all services up? Any anomalies? Report status.",
            max_tokens=300,
        )
        report["phases"]["codex_verify"] = {
            "provider": resp.provider.value,
            "text": resp.text[:500],
            "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "fallback": resp.fallback_used,
        }
    except Exception as e:
        report["phases"]["codex_verify"] = {"error": str(e)}

    # Write report
    report_path = WORKSPACE / "var" / "eta_engine" / "state" / "three_ai_coordination.jsonl"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "a") as f:
        f.write(json.dumps(report, default=str) + "\n")

    return report


if __name__ == "__main__":
    print("Three-AI Coordination Sync")
    print("=" * 50)
    report = run_coordination_cycle()
    for phase, data in report["phases"].items():
        if "error" in data:
            print(f"  {phase}: ERROR — {data['error'][:80]}")
        else:
            print(f"  {phase}: {data['provider']} ({data['elapsed_ms']:.0f}ms) — {data['text'][:80]}...")
    print(f"  Report: {report['cycle_id']}")
    print("=" * 50)

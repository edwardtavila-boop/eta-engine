"""Layer 11: Automated walk-forward retune scheduler. Runs
fleet_strategy_optimizer on a schedule, diffs results against
baselines, and raises drift alerts when strategies degrade.

Intended for scheduled task or daemon invocation. Does NOT mutate
the registry or baselines — writes advisory retune reports only.

Usage
-----
    python -m eta_engine.scripts.auto_retune_scheduler --bot btc_sage_daily_etf
    python -m eta_engine.scripts.auto_retune_scheduler --all-production
    python -m eta_engine.scripts.auto_retune_scheduler --json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from eta_engine.scripts import workspace_roots  # noqa: E402

SCHEDULED_DIR = workspace_roots.ETA_RUNTIME_STATE_DIR / "scheduled_retunes"
RETUNE_INTERVAL_DAYS = 7


@dataclass
class RetunePlan:
    bot_id: str
    strategy_id: str
    last_retune: str | None
    due: bool
    command: str
    note: str


def _last_retune(bot_id: str) -> str | None:
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return None
    tune = a.extras.get("research_tune")
    if isinstance(tune, dict):
        return tune.get("retuned_on") or tune.get("refreshed_on")
    return None


def _days_since(date_str: str | None) -> int | None:
    if date_str is None:
        return None
    try:
        dt = datetime.fromisoformat(date_str)
        return int((datetime.now(tz=UTC) - dt).total_seconds() / 86400)
    except (ValueError, TypeError):
        return None


def build_retune_queue(all_production: bool = False, bot_filter: str | None = None) -> list[RetunePlan]:
    from eta_engine.strategies.per_bot_registry import all_assignments, is_active

    plans: list[RetunePlan] = []
    for a in all_assignments():
        if not is_active(a):
            continue
        if bot_filter and a.bot_id != bot_filter:
            continue
        status = a.extras.get("promotion_status", "")
        if all_production and status not in {"production_candidate", "production"}:
            continue
        if status in {"shadow_benchmark", "deactivated", "deprecated"}:
            continue

        last = _last_retune(a.bot_id)
        days = _days_since(last)
        due = days is None or days >= RETUNE_INTERVAL_DAYS
        cmd = f"python -m eta_engine.scripts.fleet_strategy_optimizer --only-bot {a.bot_id}"
        wf_ov = a.extras.get("walk_forward_overrides", {})
        if isinstance(wf_ov, dict) and wf_ov.get("agg_degradation_mode"):
            cmd += " --agg-degradation"
        note = (
            f"Last retune: {days}d ago (due)"
            if due
            else f"Last retune: {days}d ago (next in {RETUNE_INTERVAL_DAYS - (days or 0)}d)"
        )
        plans.append(RetunePlan(a.bot_id, a.strategy_id, last, due, cmd, note))

    return sorted(plans, key=lambda p: (not p.due, _days_since(p.last_retune) or 99999))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="auto_retune_scheduler")
    p.add_argument("--bot", type=str, default=None)
    p.add_argument("--all-production", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    plans = build_retune_queue(all_production=args.all_production, bot_filter=args.bot)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "bot_id": p.bot_id,
                        "strategy_id": p.strategy_id,
                        "last_retune": p.last_retune,
                        "due": p.due,
                        "command": p.command,
                        "note": p.note,
                    }
                    for p in plans
                ],
                indent=2,
            )
        )
    else:
        print(f"{'Bot':<24} {'Last retune':<14} {'Due':<6} {'Command'}")
        print("-" * 120)
        for p in plans:
            due_str = "YES" if p.due else "no"
            print(f"{p.bot_id:<24} {p.last_retune or 'never':<14} {due_str:<6} {p.command}")
        due_count = sum(1 for p in plans if p.due)
        print(f"\n{due_count}/{len(plans)} bots due for retune")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Summarize diamond retune campaign progress from the runner history.

The campaign says what should be tried. The runner history says what the
VPS actually tried. This script joins both into a compact operator surface
without granting promotion or live-routing authority.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts import workspace_roots  # noqa: E402

DEFAULT_CAMPAIGN_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "diamond_retune_campaign_latest.json"
DEFAULT_HISTORY_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "diamond_retune_runner_history.jsonl"
OUT_LATEST = workspace_roots.ETA_RUNTIME_STATE_DIR / "diamond_retune_status_latest.json"

STUCK_ATTEMPT_FLOOR = 3


def _as_float(value: Any, default: float = 0.0) -> float:  # noqa: ANN401
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_history(path: Path = DEFAULT_HISTORY_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _targets(campaign: dict[str, Any]) -> list[dict[str, Any]]:
    raw = campaign.get("targets")
    rows = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    return sorted(rows, key=lambda row: int(_as_float(row.get("rank"), 999999)))


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return sorted(rows, key=lambda row: str(row.get("generated_at_utc") or ""))[-1]


def _retune_state(*, attempts: int, latest_status: str) -> str:
    if attempts <= 0:
        return "NOT_ATTEMPTED"
    if latest_status == "research_passed_broker_proof_required":
        return "PASS_AWAITING_BROKER_PROOF"
    if latest_status == "research_low_sample_keep_collecting":
        return "COLLECT_MORE_SAMPLE"
    if latest_status == "research_near_miss_keep_tuning":
        return "NEAR_MISS_RETUNE"
    if latest_status == "research_unstable_positive_keep_tuning":
        return "UNSTABLE_POSITIVE_RETUNE"
    if latest_status == "research_timeout_keep_retuning":
        return "TIMEOUT_RETRY"
    if attempts >= STUCK_ATTEMPT_FLOOR:
        return "STUCK_RESEARCH_FAILING"
    return "KEEP_RETUNING"


def _next_action(state: str, bot_id: str) -> str:
    if state == "NOT_ATTEMPTED":
        return "run the next scheduled paper-research attempt; no live changes"
    if state == "PASS_AWAITING_BROKER_PROOF":
        return "review research artifact, then require fresh broker closes before any promotion"
    if state == "COLLECT_MORE_SAMPLE":
        return "collect more paper closes before promotion; no live changes"
    if state == "NEAR_MISS_RETUNE":
        return "apply focused tuning to the highest-impact filters, rerun paper research, no live changes"
    if state == "UNSTABLE_POSITIVE_RETUNE":
        return "improve window consistency with tighter regime/session filters, rerun paper research, no live changes"
    if state == "TIMEOUT_RETRY":
        return "retry with normal timeout or smaller max-bars smoke; no live changes"
    if state == "STUCK_RESEARCH_FAILING":
        return f"pause repeated {bot_id} attempts until a new hypothesis or parameter family is added"
    return "keep rotating through paper research; no live changes"


def build_status(*, campaign: dict[str, Any], history_rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in history_rows:
        bot_id = str(row.get("bot_id") or "")
        if bot_id:
            grouped[bot_id].append(row)

    bot_rows: list[dict[str, Any]] = []
    for target in _targets(campaign):
        bot_id = str(target.get("bot_id") or "")
        rows = grouped.get(bot_id, [])
        latest = _latest(rows) or {}
        latest_status = str(latest.get("status") or "")
        research_signal = latest.get("research_signal") if isinstance(latest.get("research_signal"), dict) else {}
        attempts = len(rows)
        state = _retune_state(attempts=attempts, latest_status=latest_status)
        bot_rows.append(
            {
                "bot_id": bot_id,
                "rank": target.get("rank"),
                "symbol": target.get("symbol"),
                "asset_sleeve": target.get("asset_sleeve"),
                "priority_score": target.get("priority_score"),
                "attempts": attempts,
                "last_run_id": latest.get("run_id"),
                "last_status": latest_status or None,
                "last_exit_code": latest.get("exit_code"),
                "last_attempt_at_utc": latest.get("generated_at_utc"),
                "research_signal": research_signal,
                "retune_state": state,
                "next_action": _next_action(state, bot_id),
                "promotion_block": "broker_proof_required",
                "live_mutation_policy": "paper_only_advisory",
                "safe_to_mutate_live": False,
            }
        )

    attempted = {row["bot_id"] for row in bot_rows if int(row["attempts"]) > 0}
    return {
        "kind": "eta_diamond_retune_status",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "campaign_generated_at_utc": campaign.get("generated_at_utc"),
        "summary": {
            "n_targets": len(bot_rows),
            "n_attempted_bots": len(attempted),
            "n_unattempted_targets": sum(1 for row in bot_rows if int(row["attempts"]) == 0),
            "n_research_passed_broker_proof_required": sum(
                1 for row in bot_rows if row["retune_state"] == "PASS_AWAITING_BROKER_PROOF"
            ),
            "n_low_sample_keep_collecting": sum(1 for row in bot_rows if row["retune_state"] == "COLLECT_MORE_SAMPLE"),
            "n_near_miss_keep_tuning": sum(1 for row in bot_rows if row["retune_state"] == "NEAR_MISS_RETUNE"),
            "n_unstable_positive_keep_tuning": sum(
                1 for row in bot_rows if row["retune_state"] == "UNSTABLE_POSITIVE_RETUNE"
            ),
            "n_stuck_research_failing": sum(1 for row in bot_rows if row["retune_state"] == "STUCK_RESEARCH_FAILING"),
            "n_timeout_retry": sum(1 for row in bot_rows if row["retune_state"] == "TIMEOUT_RETRY"),
            "safe_to_mutate_live": False,
        },
        "bots": bot_rows,
    }


def run(
    *,
    campaign_path: Path = DEFAULT_CAMPAIGN_PATH,
    history_path: Path = DEFAULT_HISTORY_PATH,
    out_path: Path = OUT_LATEST,
) -> dict[str, Any]:
    report = build_status(campaign=_load_json(campaign_path), history_rows=load_history(history_path))
    workspace_roots.ensure_parent(out_path)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _print(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print("=" * 112)
    print(
        " DIAMOND RETUNE STATUS  "
        f"attempted={summary['n_attempted_bots']}/{summary['n_targets']} "
        f"stuck={summary['n_stuck_research_failing']} "
        f"passes_need_broker={summary['n_research_passed_broker_proof_required']}",
    )
    print("=" * 112)
    for row in report["bots"]:
        print(
            f"#{row['rank']} {row['bot_id']:<24} {row['retune_state']:<28} "
            f"attempts={row['attempts']:<3} action={row['next_action']}",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-path", type=Path, default=DEFAULT_CAMPAIGN_PATH)
    parser.add_argument("--history-path", type=Path, default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--out-path", type=Path, default=OUT_LATEST)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run(campaign_path=args.campaign_path, history_path=args.history_path, out_path=args.out_path)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

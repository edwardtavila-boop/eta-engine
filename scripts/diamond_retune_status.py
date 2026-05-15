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
DEFAULT_LEDGER_PATH = workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH
OUT_LATEST = workspace_roots.ETA_RUNTIME_STATE_DIR / "diamond_retune_status_latest.json"

STUCK_ATTEMPT_FLOOR = 3
BROKER_PROOF_CLOSE_TARGET = 100


def _as_float(value: Any, default: float = 0.0) -> float:  # noqa: ANN401
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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


def _research_backlog(campaign: dict[str, Any]) -> list[dict[str, Any]]:
    raw = campaign.get("research_backlog")
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


def _broker_close_evidence(
    ledger: dict[str, Any] | None,
    bot_id: str,
    *,
    required_closes: int = BROKER_PROOF_CLOSE_TARGET,
) -> dict[str, Any]:
    payload = ledger if isinstance(ledger, dict) else {}
    per_bot = payload.get("per_bot") if isinstance(payload.get("per_bot"), dict) else {}
    stats = per_bot.get(bot_id) if isinstance(per_bot.get(bot_id), dict) else {}
    close_count = int(_as_float(stats.get("closed_trade_count"), 0.0))
    remaining = max(0, required_closes - close_count)
    progress_pct = round(min(100.0, (close_count / required_closes) * 100.0), 2) if required_closes > 0 else 100.0
    return {
        "source": "closed_trade_ledger_latest" if payload else "missing_closed_trade_ledger",
        "source_generated_at_utc": payload.get("generated_at_utc"),
        "data_sources_filter": (
            payload.get("data_sources_filter") if isinstance(payload.get("data_sources_filter"), list) else []
        ),
        "closed_trade_count": close_count,
        "required_closed_trade_count": required_closes,
        "remaining_closed_trade_count": remaining,
        "sample_progress_pct": progress_pct,
        "has_required_sample": remaining <= 0,
        "total_realized_pnl": round(_as_float(stats.get("total_realized_pnl")), 2),
        "cumulative_r": round(_as_float(stats.get("cumulative_r")), 4),
        "profit_factor": stats.get("profit_factor"),
        "win_rate_pct": stats.get("win_rate_pct"),
    }


def _next_action(state: str, bot_id: str, *, broker_evidence: dict[str, Any] | None = None) -> str:
    broker_evidence = broker_evidence if isinstance(broker_evidence, dict) else {}
    closed_trade_count = int(_as_float(broker_evidence.get("closed_trade_count"), 0.0))
    required_closes = int(_as_float(broker_evidence.get("required_closed_trade_count"), BROKER_PROOF_CLOSE_TARGET))
    remaining = int(_as_float(broker_evidence.get("remaining_closed_trade_count"), 0.0))
    if state == "NOT_ATTEMPTED":
        return "run the next scheduled paper-research attempt; no live changes"
    if state == "PASS_AWAITING_BROKER_PROOF":
        if remaining > 0:
            return (
                f"review research artifact, then collect {remaining} more paper/broker closes "
                f"({closed_trade_count}/{required_closes}) before any promotion"
            )
        return (
            f"broker close sample met ({closed_trade_count}/{required_closes}); "
            "review research artifact and broker proof metrics before any promotion"
        )
    if state == "COLLECT_MORE_SAMPLE":
        if remaining > 0:
            return (
                f"collect {remaining} more paper/broker closes "
                f"({closed_trade_count}/{required_closes}) before promotion; no live changes"
            )
        return (
            f"broker close sample met ({closed_trade_count}/{required_closes}); "
            "collect more independent research windows before promotion; no live changes"
        )
    if state == "NEAR_MISS_RETUNE":
        return "apply focused tuning to the highest-impact filters, rerun paper research, no live changes"
    if state == "UNSTABLE_POSITIVE_RETUNE":
        return "improve window consistency with tighter regime/session filters, rerun paper research, no live changes"
    if state == "TIMEOUT_RETRY":
        return "retry with normal timeout or smaller max-bars smoke; no live changes"
    if state == "STUCK_RESEARCH_FAILING":
        return f"pause repeated {bot_id} attempts until a new hypothesis or parameter family is added"
    return "keep rotating through paper research; no live changes"


def _research_backlog_row(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": target.get("rank"),
        "bot_id": str(target.get("bot_id") or ""),
        "strategy_id": str(target.get("strategy_id") or target.get("bot_id") or ""),
        "issue_code": str(target.get("issue_code") or "research_gate_failed"),
        "summary": str(target.get("summary") or "research candidate gate not fully passed"),
        "research_signal": target.get("research_signal") if isinstance(target.get("research_signal"), dict) else {},
        "next_command": str(target.get("next_command") or ""),
        "verification_command": str(target.get("verification_command") or ""),
        "retune_state": "RESEARCH_GATE_FAILED",
        "next_action": "rerun runtime-only research grid, then launch-check; no live changes",
        "promotion_block": "research_gate_required",
        "live_mutation_policy": "paper_only_advisory",
        "safe_to_mutate_live": False,
    }


def build_status(
    *,
    campaign: dict[str, Any],
    history_rows: list[dict[str, Any]],
    closed_trade_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        broker_evidence = _broker_close_evidence(closed_trade_ledger, bot_id)
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
                "broker_close_evidence": broker_evidence,
                "retune_state": state,
                "next_action": _next_action(state, bot_id, broker_evidence=broker_evidence),
                "promotion_block": "broker_proof_required",
                "live_mutation_policy": "paper_only_advisory",
                "safe_to_mutate_live": False,
            }
        )

    research_backlog = [_research_backlog_row(target) for target in _research_backlog(campaign)]
    attempted = {row["bot_id"] for row in bot_rows if int(row["attempts"]) > 0}
    broker_proof_rows = [
        row.get("broker_close_evidence") for row in bot_rows if isinstance(row.get("broker_close_evidence"), dict)
    ]
    proof_gaps = [int(_as_float(row.get("remaining_closed_trade_count"), 0.0)) for row in broker_proof_rows]
    return {
        "kind": "eta_diamond_retune_status",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "campaign_generated_at_utc": campaign.get("generated_at_utc"),
        "summary": {
            "n_targets": len(bot_rows),
            "n_attempted_bots": len(attempted),
            "n_unattempted_targets": sum(1 for row in bot_rows if int(row["attempts"]) == 0),
            "n_research_backlog_targets": len(research_backlog),
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
            "broker_proof_required_closes": BROKER_PROOF_CLOSE_TARGET,
            "n_broker_proof_ready": sum(1 for row in broker_proof_rows if bool(row.get("has_required_sample"))),
            "n_broker_proof_shortfall": sum(1 for gap in proof_gaps if gap > 0),
            "largest_broker_proof_gap": max(proof_gaps, default=0),
            "total_broker_proof_gap": sum(proof_gaps),
            "safe_to_mutate_live": False,
        },
        "bots": bot_rows,
        "research_backlog": research_backlog,
    }


def run(
    *,
    campaign_path: Path = DEFAULT_CAMPAIGN_PATH,
    history_path: Path = DEFAULT_HISTORY_PATH,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
    out_path: Path = OUT_LATEST,
) -> dict[str, Any]:
    report = build_status(
        campaign=_load_json(campaign_path),
        history_rows=load_history(history_path),
        closed_trade_ledger=_load_optional_json(ledger_path),
    )
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
    backlog = report.get("research_backlog") if isinstance(report.get("research_backlog"), list) else []
    if backlog:
        print("-" * 112)
        print(f" RESEARCH BACKLOG  targets={len(backlog)}")
        for row in backlog:
            print(
                f"#{row['rank']} {row['bot_id']:<24} {row['retune_state']:<24} "
                f"action={row['next_action']}",
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-path", type=Path, default=DEFAULT_CAMPAIGN_PATH)
    parser.add_argument("--history-path", type=Path, default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--ledger-path", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--out-path", type=Path, default=OUT_LATEST)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run(
        campaign_path=args.campaign_path,
        history_path=args.history_path,
        ledger_path=args.ledger_path,
        out_path=args.out_path,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

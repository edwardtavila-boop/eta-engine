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
from eta_engine.scripts.retune_advisory_cache import build_retune_advisory, summarize_active_experiment  # noqa: E402

DEFAULT_CAMPAIGN_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "diamond_retune_campaign_latest.json"
DEFAULT_HISTORY_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "diamond_retune_runner_history.jsonl"
DEFAULT_LEDGER_PATH = workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH
DEFAULT_RETUNE_TRUTH_CHECK_PATH = (
    workspace_roots.ETA_RUNTIME_STATE_DIR / "health" / "diamond_retune_truth_check_latest.json"
)
DEFAULT_PUBLIC_RETUNE_TRUTH_PATH = (
    workspace_roots.ETA_RUNTIME_STATE_DIR / "health" / "public_diamond_retune_truth_latest.json"
)
DEFAULT_PUBLIC_BROKER_CLOSE_CACHE_PATH = (
    workspace_roots.ETA_RUNTIME_STATE_DIR / "health" / "public_broker_close_truth_latest.json"
)
DEFAULT_RETUNE_ADVISORY_HEALTH_DIR = workspace_roots.ETA_RUNTIME_HEALTH_DIR
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


def _public_retune_truth_override(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    surface = payload.get("surface") if isinstance(payload.get("surface"), dict) else {}
    source = "public_diamond_retune_truth_cache"
    if not surface and isinstance(payload.get("public_surface"), dict):
        surface = payload.get("public_surface")
        source = "diamond_retune_truth_check_public_surface"
    normalized = surface.get("normalized") if isinstance(surface.get("normalized"), dict) else {}
    summary = surface.get("summary") if isinstance(surface.get("summary"), dict) else {}
    focus_bot = str(normalized.get("focus_bot") or payload.get("focus_bot") or "").strip()
    if not focus_bot:
        return {}
    override = {
        "broker_truth_focus_bot_id": focus_bot,
        "broker_truth_focus_issue_code": normalized.get("focus_issue"),
        "broker_truth_focus_state": normalized.get("focus_state"),
        "broker_truth_focus_strategy_kind": normalized.get("focus_strategy_kind"),
        "broker_truth_focus_best_session": normalized.get("focus_best_session"),
        "broker_truth_focus_worst_session": normalized.get("focus_worst_session"),
        "broker_truth_focus_next_command": normalized.get("focus_command"),
        "broker_truth_focus_closed_trade_count": normalized.get("focus_closed_trade_count"),
        "broker_truth_focus_total_realized_pnl": normalized.get("focus_total_realized_pnl"),
        "broker_truth_focus_profit_factor": normalized.get("focus_profit_factor"),
        "broker_truth_summary_line": summary.get("broker_truth_summary_line"),
        "safe_to_mutate_live": normalized.get("safe_to_mutate_live"),
        "broker_truth_focus_source": source,
        "broker_truth_focus_source_generated_at_utc": (
            payload.get("generated_at_utc")
            or surface.get("observed_ts")
            or summary.get("generated_at_utc")
        ),
    }
    return {
        key: value
        for key, value in override.items()
        if value is not None and (not isinstance(value, str) or value.strip())
    }


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
    total_realized_pnl = round(_as_float(stats.get("total_realized_pnl")), 2)
    profit_factor = _as_float(stats.get("profit_factor"), 0.0)
    has_required_sample = remaining <= 0
    has_positive_edge = has_required_sample and total_realized_pnl > 0.0 and profit_factor > 1.0
    if has_positive_edge:
        edge_status = "broker_edge_ready"
    elif has_required_sample:
        edge_status = "sample_met_negative_edge"
    elif payload:
        edge_status = "needs_more_broker_closes"
    else:
        edge_status = "missing_closed_trade_ledger"
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
        "has_required_sample": has_required_sample,
        "has_positive_edge": has_positive_edge,
        "edge_status": edge_status,
        "total_realized_pnl": total_realized_pnl,
        "cumulative_r": round(_as_float(stats.get("cumulative_r")), 4),
        "profit_factor": stats.get("profit_factor"),
        "win_rate_pct": stats.get("win_rate_pct"),
    }


def _public_broker_close_evidence(
    cache: dict[str, Any] | None,
    bot_id: str,
    *,
    required_closes: int = BROKER_PROOF_CLOSE_TARGET,
) -> dict[str, Any]:
    payload = cache if isinstance(cache, dict) else {}
    surface = payload.get("surface") if isinstance(payload.get("surface"), dict) else {}
    normalized = surface.get("normalized") if isinstance(surface.get("normalized"), dict) else {}
    focus_bot = str(normalized.get("focus_bot") or payload.get("focus_bot") or "").strip()
    if not focus_bot or focus_bot != bot_id:
        return {}

    close_count = int(_as_float(normalized.get("focus_closed_trade_count"), 0.0))
    remaining = max(0, required_closes - close_count)
    progress_pct = round(min(100.0, (close_count / required_closes) * 100.0), 2) if required_closes > 0 else 100.0
    total_realized_pnl = round(_as_float(normalized.get("focus_total_realized_pnl")), 2)
    profit_factor = _as_float(normalized.get("focus_profit_factor"), 0.0)
    has_required_sample = remaining <= 0
    has_positive_edge = has_required_sample and total_realized_pnl > 0.0 and profit_factor > 1.0
    if has_positive_edge:
        edge_status = "broker_edge_ready"
    elif has_required_sample:
        edge_status = "sample_met_negative_edge"
    else:
        edge_status = "needs_more_broker_closes"
    return {
        "source": "public_broker_close_truth_cache",
        "source_generated_at_utc": payload.get("generated_at_utc"),
        "data_sources_filter": ["public_broker_close_truth_cache"],
        "closed_trade_count": close_count,
        "required_closed_trade_count": required_closes,
        "remaining_closed_trade_count": remaining,
        "sample_progress_pct": progress_pct,
        "has_required_sample": has_required_sample,
        "has_positive_edge": has_positive_edge,
        "edge_status": edge_status,
        "total_realized_pnl": total_realized_pnl,
        "cumulative_r": None,
        "profit_factor": round(profit_factor, 4),
        "win_rate_pct": None,
        "broker_snapshot_source": str(normalized.get("broker_snapshot_source") or ""),
        "reporting_timezone": str(normalized.get("reporting_timezone") or ""),
    }


def _should_prefer_public_broker_close_evidence(
    local_evidence: dict[str, Any],
    public_evidence: dict[str, Any],
) -> bool:
    public_count = int(_as_float(public_evidence.get("closed_trade_count"), 0.0))
    local_count = int(_as_float(local_evidence.get("closed_trade_count"), 0.0))
    if public_count <= local_count:
        return False
    if local_count <= 0:
        return True
    return local_count <= max(5, public_count // 4)


def _select_broker_close_evidence(
    ledger: dict[str, Any] | None,
    bot_id: str,
    *,
    public_broker_close_cache: dict[str, Any] | None = None,
    required_closes: int = BROKER_PROOF_CLOSE_TARGET,
) -> dict[str, Any]:
    local_evidence = _broker_close_evidence(ledger, bot_id, required_closes=required_closes)
    public_evidence = _public_broker_close_evidence(
        public_broker_close_cache,
        bot_id,
        required_closes=required_closes,
    )
    if public_evidence and _should_prefer_public_broker_close_evidence(local_evidence, public_evidence):
        public_evidence["advisory_override_applied"] = True
        public_evidence["advisory_override_reason"] = "public_sample_stronger_than_local"
        public_evidence["local_source"] = str(local_evidence.get("source") or "")
        public_evidence["local_closed_trade_count"] = int(_as_float(local_evidence.get("closed_trade_count"), 0.0))
        public_evidence["local_total_realized_pnl"] = round(_as_float(local_evidence.get("total_realized_pnl")), 2)
        public_evidence["local_profit_factor"] = local_evidence.get("profit_factor")
        return public_evidence

    local_evidence["advisory_override_applied"] = False
    local_evidence["advisory_override_reason"] = ""
    return local_evidence


def _next_action(state: str, bot_id: str, *, broker_evidence: dict[str, Any] | None = None) -> str:
    broker_evidence = broker_evidence if isinstance(broker_evidence, dict) else {}
    closed_trade_count = int(_as_float(broker_evidence.get("closed_trade_count"), 0.0))
    required_closes = int(_as_float(broker_evidence.get("required_closed_trade_count"), BROKER_PROOF_CLOSE_TARGET))
    remaining = int(_as_float(broker_evidence.get("remaining_closed_trade_count"), 0.0))
    has_required_sample = bool(broker_evidence.get("has_required_sample"))
    has_positive_edge = bool(broker_evidence.get("has_positive_edge"))
    broker_pnl = _as_float(broker_evidence.get("total_realized_pnl"), 0.0)
    broker_pf = _as_float(broker_evidence.get("profit_factor"), 0.0)
    if has_required_sample and not has_positive_edge:
        edge_note = f"sample met ({closed_trade_count}/{required_closes}) but broker edge is negative"
        if broker_pnl or broker_pf:
            edge_note += f" (PnL ${broker_pnl:,.2f}, PF {broker_pf:.2f})"
        return f"{edge_note}; retune or demote before any promotion; no live changes"
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


def _broker_truth_focus(bot_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the single operator-focus line for broker-proven retune truth."""

    def _evidence(row: dict[str, Any]) -> dict[str, Any]:
        evidence = row.get("broker_close_evidence")
        return evidence if isinstance(evidence, dict) else {}

    def _impact(row: dict[str, Any]) -> float:
        evidence = _evidence(row)
        return abs(_as_float(evidence.get("total_realized_pnl"), 0.0))

    negative_edge_rows = [
        row for row in bot_rows if str(_evidence(row).get("edge_status") or "") == "sample_met_negative_edge"
    ]
    proof_ready_rows = [
        row
        for row in bot_rows
        if bool(_evidence(row).get("has_required_sample")) and bool(_evidence(row).get("has_positive_edge"))
    ]
    focus = (
        sorted(negative_edge_rows, key=_impact, reverse=True)[0]
        if negative_edge_rows
        else sorted(proof_ready_rows, key=_impact, reverse=True)[0]
        if proof_ready_rows
        else bot_rows[0]
        if bot_rows
        else {}
    )
    evidence = _evidence(focus)
    bot_id = str(focus.get("bot_id") or "")
    edge_status = str(evidence.get("edge_status") or "")
    closed = int(_as_float(evidence.get("closed_trade_count"), 0.0))
    required = int(_as_float(evidence.get("required_closed_trade_count"), BROKER_PROOF_CLOSE_TARGET))
    remaining = int(_as_float(evidence.get("remaining_closed_trade_count"), max(0, required - closed)))
    pnl = _as_float(evidence.get("total_realized_pnl"), 0.0)
    profit_factor = _as_float(evidence.get("profit_factor"), 0.0)

    if not bot_id:
        line = "No broker retune target is available; keep live mutation disabled."
    elif edge_status == "sample_met_negative_edge":
        line = (
            f"{bot_id}: sample met ({closed}/{required}) but broker edge is negative "
            f"(PnL ${pnl:,.2f}, PF {profit_factor:.2f}); retune or demote before promotion."
        )
    elif edge_status == "broker_edge_ready":
        line = (
            f"{bot_id}: broker sample is positive ({closed}/{required}, "
            f"PnL ${pnl:,.2f}, PF {profit_factor:.2f}); human review still required before promotion."
        )
    else:
        line = (
            f"{bot_id}: needs {remaining} more broker closes ({closed}/{required}) before promotion proof; "
            "no live changes."
        )

    return {
        "broker_truth_focus_bot_id": bot_id,
        "broker_truth_focus_state": str(focus.get("retune_state") or ""),
        "broker_truth_focus_edge_status": edge_status,
        "broker_truth_focus_source": str(evidence.get("source") or ""),
        "broker_truth_focus_advisory_override_applied": bool(evidence.get("advisory_override_applied")),
        "broker_truth_focus_closed_trade_count": closed,
        "broker_truth_focus_required_closed_trade_count": required,
        "broker_truth_focus_remaining_closed_trade_count": remaining,
        "broker_truth_focus_total_realized_pnl": round(pnl, 2),
        "broker_truth_focus_profit_factor": round(profit_factor, 4),
        "issue_code": str(focus.get("issue_code") or ""),
        "priority_score": focus.get("priority_score"),
        "strategy_kind": str(focus.get("strategy_kind") or ""),
        "best_session": str(focus.get("best_session") or ""),
        "worst_session": str(focus.get("worst_session") or ""),
        "parameter_focus": focus.get("parameter_focus") if isinstance(focus.get("parameter_focus"), list) else [],
        "primary_experiment": str(focus.get("primary_experiment") or ""),
        "next_command": str(focus.get("next_command") or ""),
        "broker_truth_focus_next_action": str(focus.get("next_action") or ""),
        "broker_truth_summary_line": line,
    }


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


def _parameter_focus(target: dict[str, Any]) -> list[str]:
    raw = target.get("parameter_focus")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item or "").strip()]


def build_status(
    *,
    campaign: dict[str, Any],
    history_rows: list[dict[str, Any]],
    closed_trade_ledger: dict[str, Any] | None = None,
    public_retune_truth: dict[str, Any] | None = None,
    public_retune_truth_check: dict[str, Any] | None = None,
    public_broker_close_truth_cache: dict[str, Any] | None = None,
    retune_advisory: dict[str, Any] | None = None,
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
        broker_evidence = _select_broker_close_evidence(
            closed_trade_ledger,
            bot_id,
            public_broker_close_cache=public_broker_close_truth_cache,
        )
        bot_rows.append(
            {
                "bot_id": bot_id,
                "rank": target.get("rank"),
                "symbol": target.get("symbol"),
                "asset_sleeve": target.get("asset_sleeve"),
                "strategy_kind": str(target.get("strategy_kind") or ""),
                "issue_code": str(target.get("issue_code") or ""),
                "priority_score": target.get("priority_score"),
                "best_session": str(target.get("best_session") or ""),
                "worst_session": str(target.get("worst_session") or ""),
                "parameter_focus": _parameter_focus(target),
                "primary_experiment": str(target.get("primary_experiment") or ""),
                "next_command": str(target.get("next_command") or ""),
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
    broker_sample_ready = [row for row in broker_proof_rows if bool(row.get("has_required_sample"))]
    broker_edge_ready = [row for row in broker_proof_rows if bool(row.get("has_positive_edge"))]
    broker_truth_focus = _broker_truth_focus(bot_rows)
    summary = {
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
        "n_broker_sample_ready": len(broker_sample_ready),
        "n_broker_edge_ready": len(broker_edge_ready),
        "n_broker_proof_ready": len(broker_edge_ready),
        "n_broker_sample_ready_negative_edge": len(broker_sample_ready) - len(broker_edge_ready),
        "n_broker_proof_shortfall": sum(1 for gap in proof_gaps if gap > 0),
        "largest_broker_proof_gap": max(proof_gaps, default=0),
        "total_broker_proof_gap": sum(proof_gaps),
        "safe_to_mutate_live": False,
        "broker_truth_focus_issue_code": str(broker_truth_focus.get("issue_code") or ""),
        "broker_truth_focus_priority_score": broker_truth_focus.get("priority_score"),
        "broker_truth_focus_strategy_kind": str(broker_truth_focus.get("strategy_kind") or ""),
        "broker_truth_focus_best_session": str(broker_truth_focus.get("best_session") or ""),
        "broker_truth_focus_worst_session": str(broker_truth_focus.get("worst_session") or ""),
        "broker_truth_focus_parameter_focus": broker_truth_focus.get("parameter_focus")
        if isinstance(broker_truth_focus.get("parameter_focus"), list)
        else [],
        "broker_truth_focus_primary_experiment": str(
            broker_truth_focus.get("primary_experiment") or "",
        ),
        "broker_truth_focus_next_command": str(broker_truth_focus.get("next_command") or ""),
        **broker_truth_focus,
    }
    public_focus_override = _public_retune_truth_override(public_retune_truth)
    if not public_focus_override:
        public_focus_override = _public_retune_truth_override(public_retune_truth_check)
    if public_focus_override:
        summary["public_truth_override_applied"] = True
        if summary.get("broker_truth_focus_bot_id") != public_focus_override.get("broker_truth_focus_bot_id"):
            summary["local_broker_truth_focus_bot_id"] = summary.get("broker_truth_focus_bot_id")
        summary.update(public_focus_override)
        focus_bot = str(summary.get("broker_truth_focus_bot_id") or "")
        focus_state = str(summary.get("broker_truth_focus_state") or "")
        closed_trade_count = int(_as_float(summary.get("broker_truth_focus_closed_trade_count"), 0.0))
        required_closed_trade_count = int(
            _as_float(summary.get("broker_proof_required_closes"), BROKER_PROOF_CLOSE_TARGET),
        )
        remaining_closed_trade_count = max(0, required_closed_trade_count - closed_trade_count)
        total_realized_pnl = _as_float(summary.get("broker_truth_focus_total_realized_pnl"), 0.0)
        profit_factor = _as_float(summary.get("broker_truth_focus_profit_factor"), 0.0)
        has_required_sample = remaining_closed_trade_count <= 0
        has_positive_edge = has_required_sample and total_realized_pnl > 0.0 and profit_factor > 1.0
        if has_positive_edge:
            edge_status = "broker_edge_ready"
        elif has_required_sample:
            edge_status = "sample_met_negative_edge"
        elif closed_trade_count > 0:
            edge_status = "needs_more_broker_closes"
        else:
            edge_status = "missing_closed_trade_ledger"
        focus_broker_evidence = {
            "closed_trade_count": closed_trade_count,
            "required_closed_trade_count": required_closed_trade_count,
            "remaining_closed_trade_count": remaining_closed_trade_count,
            "has_required_sample": has_required_sample,
            "has_positive_edge": has_positive_edge,
            "total_realized_pnl": round(total_realized_pnl, 2),
            "profit_factor": summary.get("broker_truth_focus_profit_factor"),
            "edge_status": edge_status,
        }
        summary["broker_truth_focus_required_closed_trade_count"] = required_closed_trade_count
        summary["broker_truth_focus_remaining_closed_trade_count"] = remaining_closed_trade_count
        summary["broker_truth_focus_edge_status"] = edge_status
        summary["broker_truth_focus_next_action"] = _next_action(
            focus_state,
            focus_bot,
            broker_evidence=focus_broker_evidence,
        )
    else:
        summary["public_truth_override_applied"] = False

    focus_bot = str(summary.get("broker_truth_focus_bot_id") or "")
    advisory_focus_bot = str((retune_advisory or {}).get("focus_bot") or "")
    active_experiment = (
        (retune_advisory or {}).get("active_experiment")
        if focus_bot and advisory_focus_bot == focus_bot and isinstance((retune_advisory or {}).get("active_experiment"), dict)
        else None
    )
    advisory_preferred_action = str((retune_advisory or {}).get("preferred_action") or "")
    if advisory_preferred_action:
        summary["broker_truth_focus_next_action"] = advisory_preferred_action
    if isinstance(active_experiment, dict) and active_experiment:
        summary["broker_truth_focus_active_experiment"] = active_experiment
        experiment_summary = summarize_active_experiment(active_experiment)
        if experiment_summary:
            summary["broker_truth_focus_active_experiment_summary_line"] = experiment_summary["headline"]

    return {
        "kind": "eta_diamond_retune_status",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "campaign_generated_at_utc": campaign.get("generated_at_utc"),
        "focus_bot": summary.get("broker_truth_focus_bot_id"),
        "focus_issue": summary.get("broker_truth_focus_issue_code"),
        "focus_state": summary.get("broker_truth_focus_state"),
        "focus_strategy_kind": summary.get("broker_truth_focus_strategy_kind"),
        "focus_best_session": summary.get("broker_truth_focus_best_session"),
        "focus_worst_session": summary.get("broker_truth_focus_worst_session"),
        "focus_command": summary.get("broker_truth_focus_next_command"),
        "focus_next_action": summary.get("broker_truth_focus_next_action"),
        "focus_closed_trade_count": summary.get("broker_truth_focus_closed_trade_count"),
        "focus_total_realized_pnl": summary.get("broker_truth_focus_total_realized_pnl"),
        "focus_profit_factor": summary.get("broker_truth_focus_profit_factor"),
        "focus_active_experiment": summary.get("broker_truth_focus_active_experiment"),
        "focus_active_experiment_summary_line": summary.get("broker_truth_focus_active_experiment_summary_line"),
        "safe_to_mutate_live": summary.get("safe_to_mutate_live"),
        "summary": summary,
        "bots": bot_rows,
        "research_backlog": research_backlog,
    }


def run(
    *,
    campaign_path: Path = DEFAULT_CAMPAIGN_PATH,
    history_path: Path = DEFAULT_HISTORY_PATH,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
    public_retune_truth_check_path: Path = DEFAULT_RETUNE_TRUTH_CHECK_PATH,
    public_retune_truth_path: Path = DEFAULT_PUBLIC_RETUNE_TRUTH_PATH,
    public_broker_close_cache_path: Path = DEFAULT_PUBLIC_BROKER_CLOSE_CACHE_PATH,
    retune_advisory_health_dir: Path = DEFAULT_RETUNE_ADVISORY_HEALTH_DIR,
    out_path: Path = OUT_LATEST,
) -> dict[str, Any]:
    report = build_status(
        campaign=_load_json(campaign_path),
        history_rows=load_history(history_path),
        closed_trade_ledger=_load_optional_json(ledger_path),
        public_retune_truth_check=_load_optional_json(public_retune_truth_check_path),
        public_retune_truth=_load_optional_json(public_retune_truth_path),
        public_broker_close_truth_cache=_load_optional_json(public_broker_close_cache_path),
        retune_advisory=build_retune_advisory(retune_advisory_health_dir),
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
    parser.add_argument("--public-retune-truth-check-path", type=Path, default=DEFAULT_RETUNE_TRUTH_CHECK_PATH)
    parser.add_argument("--public-retune-truth-path", type=Path, default=DEFAULT_PUBLIC_RETUNE_TRUTH_PATH)
    parser.add_argument("--public-broker-close-cache-path", type=Path, default=DEFAULT_PUBLIC_BROKER_CLOSE_CACHE_PATH)
    parser.add_argument("--out-path", type=Path, default=OUT_LATEST)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run(
        campaign_path=args.campaign_path,
        history_path=args.history_path,
        ledger_path=args.ledger_path,
        public_retune_truth_check_path=args.public_retune_truth_check_path,
        public_retune_truth_path=args.public_retune_truth_path,
        public_broker_close_cache_path=args.public_broker_close_cache_path,
        out_path=args.out_path,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

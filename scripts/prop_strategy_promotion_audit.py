"""Read-only promotion audit for the primary futures prop strategy.

This artifact turns the broader prop readiness gate into a plain operator
answer: whether `volume_profile_mnq` can move from paper soak toward a
controlled prop dry-run review, and exactly what evidence is still missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from eta_engine.scripts import workspace_roots  # noqa: E402
from eta_engine.scripts.futures_prop_ladder import PRIMARY_BOT  # noqa: E402

DEFAULT_OUT = workspace_roots.ETA_PROP_STRATEGY_PROMOTION_AUDIT_PATH


def _as_dict(value: Any) -> dict[str, Any]:  # noqa: ANN401
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:  # noqa: ANN401
    return value if isinstance(value, list) else []


def _checks_by_name(gate_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(check.get("name")): check
        for raw_check in _as_list(gate_report.get("checks"))
        if (check := _as_dict(raw_check)).get("name")
    }


def _primary_candidate(
    *,
    gate_report: dict[str, Any],
    ladder_report: dict[str, Any],
) -> dict[str, Any]:
    for raw_candidate in _as_list(ladder_report.get("candidates")):
        candidate = _as_dict(raw_candidate)
        if candidate.get("bot_id") == PRIMARY_BOT:
            return candidate
    primary_ladder = _as_dict(_checks_by_name(gate_report).get("primary_ladder"))
    return _as_dict(_as_dict(primary_ladder.get("evidence")).get("primary_candidate"))


def _with_live_deactivation(candidate: dict[str, Any], gate_report: dict[str, Any]) -> dict[str, Any]:
    live_check = _as_dict(_checks_by_name(gate_report).get("live_bot_gate"))
    evidence = _as_dict(live_check.get("evidence"))
    live_found = bool(evidence.get("live_readiness_found"))
    live_active = evidence.get("live_readiness_active")
    live_lane = str(evidence.get("live_readiness_launch_lane") or "")
    live_status = str(evidence.get("live_readiness_data_status") or "")
    live_promotion = str(evidence.get("live_readiness_promotion_status") or "")
    if not live_found:
        return candidate
    if not (
        live_active is False
        or live_lane.lower() == "deactivated"
        or live_status.lower() == "deactivated"
        or live_promotion.lower() == "deactivated"
    ):
        return candidate

    merged = dict(candidate)
    merged.update(
        {
            "active": live_active is not False,
            "launch_lane": live_lane or merged.get("launch_lane") or "",
            "data_status": live_status or merged.get("data_status") or "",
            "promotion_status": live_promotion or merged.get("promotion_status") or "",
            "deactivation_source": (
                evidence.get("live_readiness_deactivation_source") or merged.get("deactivation_source") or ""
            ),
            "deactivation_reason": (
                evidence.get("live_readiness_deactivation_reason") or merged.get("deactivation_reason") or ""
            ),
            "next_action": evidence.get("live_readiness_next_action") or merged.get("next_action") or "",
        },
    )
    blockers = [str(item) for item in _as_list(merged.get("blockers"))]
    source = str(merged.get("deactivation_source") or "").strip()
    blocker = f"live readiness is deactivated via {source}" if source else "live readiness is deactivated"
    if blocker not in blockers:
        blockers.insert(0, blocker)
    merged["blockers"] = blockers
    return merged


def _runner_candidates(ladder_report: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for raw_candidate in _as_list(ladder_report.get("candidates")):
        candidate = _as_dict(raw_candidate)
        bot_id = str(candidate.get("bot_id") or "").strip()
        if not bot_id or bot_id == PRIMARY_BOT:
            continue
        candidates.append(candidate)
    return candidates


def _status_by_check(gate_report: dict[str, Any]) -> dict[str, str]:
    checks = _checks_by_name(gate_report)
    names = (
        "primary_ladder",
        "prop_readiness",
        "broker_surfaces",
        "router_cleanliness",
        "broker_native_brackets",
        "closed_trade_ledger",
        "live_bot_gate",
    )
    return {name: str(_as_dict(checks.get(name)).get("status") or "UNKNOWN") for name in names}


def _strict_gate_status(candidate: dict[str, Any]) -> str:
    grade = str(candidate.get("evidence_grade") or "")
    if grade == "strict_pass":
        return "PASS"
    if grade in {"near_strict", "small_sample_watch", "watch_only"}:
        return "WATCH"
    return "BLOCKED"


def _is_deactivated(candidate: dict[str, Any]) -> bool:
    return (
        candidate.get("active") is False
        or str(candidate.get("launch_lane") or "").lower() == "deactivated"
        or str(candidate.get("data_status") or "").lower() == "deactivated"
        or str(candidate.get("promotion_status") or "").lower() == "deactivated"
    )


def _required_evidence(
    *,
    candidate: dict[str, Any],
    statuses: dict[str, str],
    gate_summary: str,
) -> list[str]:
    required: list[str] = []
    if _is_deactivated(candidate):
        source = str(candidate.get("deactivation_source") or "").strip()
        reason = str(candidate.get("deactivation_reason") or "").strip()
        if source == "kaizen_sidecar":
            required.append(
                f"review Kaizen retirement evidence for {PRIMARY_BOT}; do not reactivate for prop dry-run "
                "unless the operator explicitly overrides and paper-soak evidence recovers",
            )
        else:
            required.append(f"resolve {PRIMARY_BOT} deactivation before promotion review")
        if reason:
            required.append(f"document deactivation reason: {reason}")
        return required
    if not bool(candidate.get("can_live_trade")):
        required.append(
            f"set {PRIMARY_BOT} can_live_trade=true only after paper-soak promotion approval",
        )
    if not bool(candidate.get("live_routing_allowed")) and statuses.get("primary_ladder") != "PASS":
        required.append("clear primary_ladder to PASS in futures_prop_ladder")
    if statuses.get("prop_readiness") != "PASS":
        required.append("clear prop_readiness to PASS / READY_FOR_DRY_RUN")
    if statuses.get("broker_native_brackets") != "PASS":
        required.append("clear broker_native_brackets to PASS")
    if statuses.get("closed_trade_ledger") != "PASS":
        required.append("clear closed_trade_ledger to PASS with schema-backed outcomes")
    if statuses.get("live_bot_gate") != "PASS" and not any("can_live_trade=true" in item for item in required):
        required.append(f"publish {PRIMARY_BOT} as can_live_trade=true on the live fleet surface")
    if gate_summary != "READY_FOR_CONTROLLED_PROP_DRY_RUN" and not required:
        required.append("clear prop_live_readiness_gate to READY_FOR_CONTROLLED_PROP_DRY_RUN")
    return required


def _summary(
    *,
    candidate: dict[str, Any],
    gate_summary: str,
    required: list[str],
) -> str:
    if gate_summary == "READY_FOR_CONTROLLED_PROP_DRY_RUN" and not required:
        return "READY_FOR_PROP_DRY_RUN_REVIEW"
    if _is_deactivated(candidate):
        if candidate.get("deactivation_source") == "kaizen_sidecar":
            return "BLOCKED_KAIZEN_RETIRED"
        return "BLOCKED_DEACTIVATED"
    if str(candidate.get("launch_lane") or "") == "paper_soak" or not bool(candidate.get("can_live_trade")):
        return "BLOCKED_PAPER_SOAK"
    return "BLOCKED_READINESS"


def _broker_close_evidence(closed_trade_ledger: dict[str, Any], bot_id: str) -> dict[str, Any]:
    stats = _as_dict(_as_dict(closed_trade_ledger.get("per_bot")).get(bot_id))
    count = int(stats.get("closed_trade_count") or 0)
    total_pnl = float(stats.get("total_realized_pnl") or 0.0)
    profit_factor = stats.get("profit_factor")
    cumulative_r = float(stats.get("cumulative_r") or 0.0)
    if count <= 0:
        verdict = "MISSING_BROKER_CLOSES"
    elif count < 30:
        verdict = "SMALL_SAMPLE"
    elif total_pnl <= 0 or (profit_factor is not None and float(profit_factor) < 1.0):
        verdict = "NEGATIVE_OR_WEAK_BROKER_EDGE"
    else:
        verdict = "POSITIVE_BROKER_CLOSE_EVIDENCE"
    return {
        "source": "closed_trade_ledger_latest",
        "has_broker_closes": count > 0,
        "closed_trade_count": count,
        "total_realized_pnl": round(total_pnl, 2),
        "cumulative_r": round(cumulative_r, 4),
        "profit_factor": profit_factor,
        "win_rate_pct": stats.get("win_rate_pct"),
        "verdict": verdict,
    }


def _heartbeat_bot(supervisor_heartbeat: dict[str, Any], bot_id: str) -> dict[str, Any]:
    for raw_bot in _as_list(supervisor_heartbeat.get("bots")):
        bot = _as_dict(raw_bot)
        if str(bot.get("bot_id") or "").strip() == bot_id:
            return bot
    return {}


def _supervisor_watch_evidence(supervisor_heartbeat: dict[str, Any], bot_id: str) -> dict[str, Any]:
    bot = _heartbeat_bot(supervisor_heartbeat, bot_id)
    if not bot:
        return {
            "source": "jarvis_strategy_supervisor_heartbeat",
            "watched": False,
            "verdict": "NOT_WATCHED_BY_SUPERVISOR",
        }
    last_bar_ts = str(bot.get("last_bar_ts") or "").strip()
    last_signal_at = str(bot.get("last_signal_at") or "").strip()
    n_entries = int(bot.get("n_entries") or 0)
    n_exits = int(bot.get("n_exits") or 0)
    entry_enabled = bool(bot.get("entry_enabled", True))
    broker_rejects = int(bot.get("consecutive_broker_rejects") or 0)
    if not entry_enabled:
        verdict = "ENTRY_DISABLED"
    elif broker_rejects > 0:
        verdict = "BROKER_REJECTS"
    elif _as_dict(bot.get("open_position")):
        verdict = "OPEN_POSITION"
    elif n_entries or n_exits or last_signal_at:
        verdict = "SIGNALS_OR_ENTRIES_SEEN"
    elif last_bar_ts:
        verdict = "WATCHING_NO_SIGNAL_YET"
    else:
        verdict = "WATCHED_NO_MARKET_BARS"
    return {
        "source": "jarvis_strategy_supervisor_heartbeat",
        "watched": True,
        "verdict": verdict,
        "mode": bot.get("mode") or "",
        "entry_enabled": entry_enabled,
        "entry_disabled_reason": bot.get("entry_disabled_reason") or "",
        "last_bar_ts": last_bar_ts,
        "last_bar_close": bot.get("last_bar_close"),
        "last_signal_at": last_signal_at,
        "n_entries": n_entries,
        "n_exits": n_exits,
        "open_position": _as_dict(bot.get("open_position")),
        "last_jarvis_verdict": bot.get("last_jarvis_verdict") or "",
        "last_jarvis_verdict_reason": bot.get("last_jarvis_verdict_reason") or "",
        "last_aggregation_reject_reason": bot.get("last_aggregation_reject_reason") or "",
        "last_aggregation_reject_at": bot.get("last_aggregation_reject_at") or "",
        "consecutive_broker_rejects": broker_rejects,
    }


def _shadow_signal_evidence(shadow_signals: list[Any], bot_id: str) -> dict[str, Any]:
    rows = [
        _as_dict(raw)
        for raw in shadow_signals
        if str(_as_dict(raw).get("bot_id") or "").strip() == bot_id
    ]
    if not rows:
        return {
            "source": "shadow_signals",
            "has_shadow_signals": False,
            "signal_count": 0,
            "verdict": "NO_SHADOW_SIGNALS",
        }
    route_targets: dict[str, int] = {}
    route_reasons: dict[str, int] = {}
    lifecycles: dict[str, int] = {}
    for row in rows:
        target = str(row.get("route_target") or "unknown")
        reason = str(row.get("route_reason") or "unknown")
        lifecycle = str(row.get("lifecycle") or "unknown")
        route_targets[target] = route_targets.get(target, 0) + 1
        route_reasons[reason] = route_reasons.get(reason, 0) + 1
        lifecycles[lifecycle] = lifecycles.get(lifecycle, 0) + 1
    latest = rows[-1]
    paper_count = int(route_targets.get("paper") or 0)
    verdict = "SHADOW_PAPER_SIGNALS_SEEN" if paper_count else "SHADOW_SIGNALS_SEEN"
    return {
        "source": "shadow_signals",
        "has_shadow_signals": True,
        "signal_count": len(rows),
        "latest_ts": latest.get("ts") or "",
        "latest_signal_id": latest.get("signal_id") or "",
        "latest_side": latest.get("side") or "",
        "latest_route_target": latest.get("route_target") or "",
        "latest_route_reason": latest.get("route_reason") or "",
        "latest_lifecycle": latest.get("lifecycle") or "",
        "route_targets": dict(sorted(route_targets.items())),
        "route_reasons": dict(sorted(route_reasons.items())),
        "lifecycles": dict(sorted(lifecycles.items())),
        "verdict": verdict,
    }


def _runner_next_action(
    candidate: dict[str, Any],
    broker_evidence: dict[str, Any],
    watch_evidence: dict[str, Any],
    signal_evidence: dict[str, Any],
) -> str:
    bot_id = str(candidate.get("bot_id") or "runner").strip()
    close_count = int(broker_evidence.get("closed_trade_count") or 0)
    if _is_deactivated(candidate):
        return f"Keep {bot_id} deactivated until fresh paper-soak evidence repairs the retirement case"
    if close_count <= 0:
        if int(signal_evidence.get("signal_count") or 0) > 0:
            return (
                f"Convert {bot_id} shadow signals into paper-close outcomes; signals fire, "
                "but broker-backed closes are still missing"
            )
        watch_verdict = str(watch_evidence.get("verdict") or "")
        if watch_verdict == "NOT_WATCHED_BY_SUPERVISOR":
            return f"Wire {bot_id} into the paper-live supervisor before judging broker-close evidence"
        if watch_verdict == "WATCHING_NO_SIGNAL_YET":
            return f"Keep {bot_id} in paper watch; it is receiving bars but has not fired a signal yet"
        return (
            f"Collect broker-backed closes for {bot_id}; strict-gate/lab evidence is not promotion proof"
        )
    if close_count < 30:
        return f"Keep {bot_id} paper-only; broker-backed sample is still too small for promotion review"
    if broker_evidence.get("verdict") == "NEGATIVE_OR_WEAK_BROKER_EDGE":
        return f"Retune {bot_id} before promotion; broker-backed closes do not show a positive edge yet"
    if bool(candidate.get("live_routing_allowed")) and bool(candidate.get("can_live_trade")):
        return f"Review {bot_id} for controlled promotion only after every broker/order gate is still PASS"
    return (
        f"Keep {bot_id} paper-only; judge it on broker-backed closes, profit factor, drawdown, "
        "and native bracket coverage before any promotion"
    )


def _runner_operator_note(
    candidate: dict[str, Any],
    broker_evidence: dict[str, Any],
    watch_evidence: dict[str, Any],
    signal_evidence: dict[str, Any],
) -> str:
    if int(broker_evidence.get("closed_trade_count") or 0) <= 0:
        if int(signal_evidence.get("signal_count") or 0) > 0:
            return "Signals are firing in paper/shadow mode; next proof gap is closed outcomes, not signal wiring."
        if watch_evidence.get("verdict") == "WATCHING_NO_SIGNAL_YET":
            return "Good lab/ladder candidate; supervisor is watching it live, but no signal has fired yet."
        return "Good lab/ladder candidate, but not broker-proven yet."
    status = _strict_gate_status(candidate)
    if status == "PASS":
        return "Strongest current runner by ladder order; still requires full broker/order gate confirmation."
    if status == "WATCH":
        return "Promising runner; keep collecting broker-backed paper closes before promotion review."
    return "Runner remains blocked; useful for research, not promotion."


def _runner_summary(
    candidate: dict[str, Any],
    closed_trade_ledger: dict[str, Any],
    supervisor_heartbeat: dict[str, Any],
    shadow_signals: list[Any],
) -> dict[str, Any]:
    bot_id = str(candidate.get("bot_id") or "").strip()
    broker_evidence = _broker_close_evidence(
        closed_trade_ledger,
        bot_id,
    )
    watch_evidence = _supervisor_watch_evidence(supervisor_heartbeat, bot_id)
    signal_evidence = _shadow_signal_evidence(shadow_signals, bot_id)
    return {
        "bot_id": candidate.get("bot_id") or "",
        "role": candidate.get("role") or "runner",
        "symbol": candidate.get("symbol") or "",
        "launch_lane": candidate.get("launch_lane") or "",
        "active": candidate.get("active", True) is not False,
        "data_status": candidate.get("data_status") or "",
        "promotion_status": candidate.get("promotion_status") or "",
        "can_paper_trade": bool(candidate.get("can_paper_trade")),
        "can_live_trade": bool(candidate.get("can_live_trade")),
        "live_routing_allowed": bool(candidate.get("live_routing_allowed")),
        "evidence_grade": candidate.get("evidence_grade") or "missing_strict_gate",
        "strict_gate_status": _strict_gate_status(candidate),
        "strict_gate": _as_dict(candidate.get("strict_gate")),
        "broker_close_evidence": broker_evidence,
        "supervisor_watch_evidence": watch_evidence,
        "shadow_signal_evidence": signal_evidence,
        "ladder_blockers": [str(item) for item in _as_list(candidate.get("blockers"))],
        "next_action": _runner_next_action(candidate, broker_evidence, watch_evidence, signal_evidence),
        "operator_note": _runner_operator_note(candidate, broker_evidence, watch_evidence, signal_evidence),
    }


def _next_runner_candidate(runners: list[dict[str, Any]]) -> dict[str, Any]:
    for runner in runners:
        if runner.get("active", True) is not False and not _is_deactivated(runner):
            return runner
    return runners[0] if runners else {}


def _runner_required_evidence(next_runner: dict[str, Any], closed_trade_ledger: dict[str, Any]) -> list[str]:
    if not next_runner:
        return []
    bot_id = str(next_runner.get("bot_id") or "").strip()
    if not bot_id:
        return []
    broker_evidence = _broker_close_evidence(closed_trade_ledger, bot_id)
    if int(broker_evidence.get("closed_trade_count") or 0) <= 0:
        return [
            f"collect broker-backed closes for runner-up candidate {bot_id}; strict-gate/lab evidence is not "
            "promotion proof",
        ]
    return [
        f"evaluate runner-up candidate {bot_id} in paper soak; keep can_live_trade=false until "
        "broker-backed closes, prop readiness, and native brackets pass",
    ]


def build_promotion_audit_report(
    *,
    gate_report: dict[str, Any],
    ladder_report: dict[str, Any],
    closed_trade_ledger: dict[str, Any] | None = None,
    supervisor_heartbeat: dict[str, Any] | None = None,
    shadow_signals: list[Any] | None = None,
) -> dict[str, Any]:
    closed_trade_ledger = _as_dict(closed_trade_ledger)
    supervisor_heartbeat = _as_dict(supervisor_heartbeat)
    shadow_signals = _as_list(shadow_signals)
    candidate = _with_live_deactivation(
        _primary_candidate(gate_report=gate_report, ladder_report=ladder_report),
        gate_report,
    )
    runners = _runner_candidates(ladder_report)
    next_runner = _next_runner_candidate(runners)
    runner_summaries = [
        _runner_summary(runner, closed_trade_ledger, supervisor_heartbeat, shadow_signals)
        for runner in runners
    ]
    next_runner_summary = (
        _runner_summary(next_runner, closed_trade_ledger, supervisor_heartbeat, shadow_signals)
        if next_runner
        else {}
    )
    statuses = _status_by_check(gate_report)
    gate_summary = str(gate_report.get("summary") or "UNKNOWN")
    required = _required_evidence(candidate=candidate, statuses=statuses, gate_summary=gate_summary)
    summary = _summary(candidate=candidate, gate_summary=gate_summary, required=required)
    if summary == "BLOCKED_KAIZEN_RETIRED":
        required = list(dict.fromkeys(required + _runner_required_evidence(next_runner, closed_trade_ledger)))
    return {
        "kind": "eta_prop_strategy_promotion_audit",
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "summary": summary,
        "primary_bot": PRIMARY_BOT,
        "ready_for_prop_dry_run_review": summary == "READY_FOR_PROP_DRY_RUN_REVIEW",
        "primary": {
            "bot_id": candidate.get("bot_id") or PRIMARY_BOT,
            "symbol": candidate.get("symbol") or "",
            "launch_lane": candidate.get("launch_lane") or "",
            "active": candidate.get("active", True) is not False,
            "data_status": candidate.get("data_status") or "",
            "promotion_status": candidate.get("promotion_status") or "",
            "deactivation_source": candidate.get("deactivation_source") or "",
            "deactivation_reason": candidate.get("deactivation_reason") or "",
            "can_live_trade": bool(candidate.get("can_live_trade")),
            "live_routing_allowed": bool(candidate.get("live_routing_allowed")),
            "evidence_grade": candidate.get("evidence_grade") or "missing_strict_gate",
            "strict_gate_status": _strict_gate_status(candidate),
            "strict_gate": _as_dict(candidate.get("strict_gate")),
            "broker_close_evidence": _broker_close_evidence(
                closed_trade_ledger,
                str(candidate.get("bot_id") or PRIMARY_BOT),
            ),
            "ladder_blockers": [str(item) for item in _as_list(candidate.get("blockers"))],
        },
        "runner_up_count": len(runner_summaries),
        "next_runner_candidate": next_runner_summary,
        "runner_up_candidates": runner_summaries,
        "readiness": statuses,
        "required_evidence": required,
        "operator_note": _operator_note(summary, next_runner_candidate=next_runner_summary),
    }


def _operator_note(summary: str, *, next_runner_candidate: dict[str, Any] | None = None) -> str:
    if summary == "READY_FOR_PROP_DRY_RUN_REVIEW":
        return "Primary strategy is ready for operator review of a controlled DORMANT-lane prop dry run."
    if summary == "BLOCKED_KAIZEN_RETIRED":
        runner = _as_dict(next_runner_candidate)
        runner_id = str(runner.get("bot_id") or "").strip()
        if runner_id:
            return (
                "Primary strategy was retired by live Kaizen evidence; keep it out of prop routing and "
                f"focus runner-up review on {runner_id}."
            )
        return (
            "Primary strategy was retired by live Kaizen evidence; keep it out of prop routing and "
            "evaluate runner-up candidates."
        )
    if summary == "BLOCKED_DEACTIVATED":
        return "Primary strategy is deactivated; resolve the deactivation before any promotion review."
    if summary == "BLOCKED_PAPER_SOAK":
        return "Primary strategy remains in paper soak; do not promote until the listed evidence is cleared."
    return "Primary strategy still has readiness blockers; keep live routing disabled."


def write_report(report: dict[str, Any], path: Path = DEFAULT_OUT) -> Path:
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _current_reports(prop_account: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from eta_engine.scripts import prop_live_readiness_gate  # noqa: PLC0415

    inputs = prop_live_readiness_gate.load_gate_inputs(prop_account=prop_account)
    gate_report = prop_live_readiness_gate.build_gate_report(**inputs)
    return gate_report, _as_dict(inputs.get("ladder"))


def _current_closed_trade_ledger() -> dict[str, Any]:
    path = workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH
    if not path.exists():
        return {}
    try:
        return _as_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def _current_supervisor_heartbeat() -> dict[str, Any]:
    path = workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH
    if not path.exists():
        return {}
    try:
        return _as_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def _current_shadow_signals() -> list[Any]:
    try:
        from eta_engine.scripts.shadow_signal_logger import read_shadow_signals  # noqa: PLC0415

        return read_shadow_signals(path=workspace_roots.ETA_JARVIS_SHADOW_SIGNALS_PATH)
    except Exception:  # noqa: BLE001
        return []


def _print_human(report: dict[str, Any], out_path: Path | None = None) -> None:
    print()
    print("EVOLUTIONARY TRADING ALGO -- Prop Strategy Promotion Audit")
    print("=" * 72)
    print(f"summary    : {report['summary']}")
    print(f"primary bot: {report['primary_bot']}")
    print(f"ready      : {report['ready_for_prop_dry_run_review']}")
    if out_path is not None:
        print(f"artifact   : {out_path}")
    print("-" * 72)
    print(report["operator_note"])
    for item in report["required_evidence"]:
        print(f"  - {item}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only primary prop strategy promotion audit")
    parser.add_argument("--prop-account", default="blusky_50k", help="Configured prop account alias")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    gate_report, ladder_report = _current_reports(args.prop_account)
    report = build_promotion_audit_report(
        gate_report=gate_report,
        ladder_report=ladder_report,
        closed_trade_ledger=_current_closed_trade_ledger(),
        supervisor_heartbeat=_current_supervisor_heartbeat(),
        shadow_signals=_current_shadow_signals(),
    )
    out_path = None if args.no_write else write_report(report, args.out)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        _print_human(report, out_path)
    return 0 if report["ready_for_prop_dry_run_review"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

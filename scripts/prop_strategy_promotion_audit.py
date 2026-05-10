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
    if grade == "near_strict":
        return "WATCH"
    return "BLOCKED"


def _required_evidence(
    *,
    candidate: dict[str, Any],
    statuses: dict[str, str],
    gate_summary: str,
) -> list[str]:
    required: list[str] = []
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
    if str(candidate.get("launch_lane") or "") == "paper_soak" or not bool(candidate.get("can_live_trade")):
        return "BLOCKED_PAPER_SOAK"
    return "BLOCKED_READINESS"


def build_promotion_audit_report(
    *,
    gate_report: dict[str, Any],
    ladder_report: dict[str, Any],
) -> dict[str, Any]:
    candidate = _primary_candidate(gate_report=gate_report, ladder_report=ladder_report)
    statuses = _status_by_check(gate_report)
    gate_summary = str(gate_report.get("summary") or "UNKNOWN")
    required = _required_evidence(candidate=candidate, statuses=statuses, gate_summary=gate_summary)
    summary = _summary(candidate=candidate, gate_summary=gate_summary, required=required)
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
            "can_live_trade": bool(candidate.get("can_live_trade")),
            "live_routing_allowed": bool(candidate.get("live_routing_allowed")),
            "evidence_grade": candidate.get("evidence_grade") or "missing_strict_gate",
            "strict_gate_status": _strict_gate_status(candidate),
            "strict_gate": _as_dict(candidate.get("strict_gate")),
            "ladder_blockers": [str(item) for item in _as_list(candidate.get("blockers"))],
        },
        "readiness": statuses,
        "required_evidence": required,
        "operator_note": _operator_note(summary),
    }


def _operator_note(summary: str) -> str:
    if summary == "READY_FOR_PROP_DRY_RUN_REVIEW":
        return "Primary strategy is ready for operator review of a controlled DORMANT-lane prop dry run."
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
    report = build_promotion_audit_report(gate_report=gate_report, ladder_report=ladder_report)
    out_path = None if args.no_write else write_report(report, args.out)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        _print_human(report, out_path)
    return 0 if report["ready_for_prop_dry_run_review"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only operator checklist for prop dry-run preparation."""

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

DEFAULT_OUT = workspace_roots.ETA_PROP_OPERATOR_CHECKLIST_PATH
PARALLEL_LAUNCH_COMMAND = "python -m eta_engine.scripts.prop_launch_check --json"


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


def _blocked(check: dict[str, Any]) -> bool:
    return str(check.get("status") or "").upper() == "BLOCKED"


def _prop_readiness_step(check: dict[str, Any]) -> dict[str, Any]:
    evidence = _as_dict(check.get("evidence"))
    missing = [str(item) for item in _as_list(evidence.get("missing_secrets")) if item]
    if evidence.get("venue_policy") == "tradovate_dormant":
        return {
            "id": "hold_tradovate_dormant",
            "status": "blocked",
            "title": "Keep Tradovate DORMANT until explicit reactivation",
            "manual": False,
            "order_action": False,
            "command": "no-op: Tradovate stays dormant until explicit reactivation",
            "verification_command": "python -m eta_engine.scripts.prop_live_readiness_gate --json",
            "missing_secrets": missing,
            "detail": check.get("detail"),
        }
    return {
        "id": "seed_active_prop_api_secrets",
        "status": "blocked",
        "title": "Seed active broker/prop API credentials after funding/API unlock",
        "manual": True,
        "order_action": False,
        "command": "follow the active broker-specific secret setup runbook",
        "verification_command": "python -m eta_engine.scripts.prop_live_readiness_gate --json",
        "missing_secrets": missing,
        "detail": check.get("detail"),
    }


def _manual_oco_step(check: dict[str, Any]) -> dict[str, Any]:
    evidence = _as_dict(check.get("evidence"))
    position = _as_dict(evidence.get("primary_unprotected_position"))
    symbol = str(position.get("symbol") or "MNQM6").strip().upper()
    venue = str(position.get("venue") or "ibkr").strip().lower()
    position_summary = _as_dict(evidence.get("position_summary"))
    unprotected_symbols = [
        str(item).strip().upper() for item in _as_list(position_summary.get("unprotected_symbols")) if str(item).strip()
    ]
    if symbol and symbol not in unprotected_symbols:
        unprotected_symbols.insert(0, symbol)
    ack_commands = [
        (
            "python -m eta_engine.scripts.broker_bracket_audit "
            f"--ack-manual-oco --symbol {item} --venue {venue} --operator edward "
            "--expires-hours 24 --confirm"
        )
        for item in unprotected_symbols
    ]
    if not ack_commands:
        ack_commands = [
            (
                "python -m eta_engine.scripts.broker_bracket_audit "
                f"--ack-manual-oco --symbol {symbol} --venue {venue} --operator edward "
                "--expires-hours 24 --confirm"
            ),
        ]
    return {
        "id": "verify_manual_oco_or_flatten",
        "status": "blocked",
        "title": "Verify broker-native OCO or flatten before prop dry-run",
        "manual": True,
        "order_action": False,
        "alternative_order_action": True,
        "symbol": symbol,
        "venue": venue,
        "unprotected_symbols": unprotected_symbols,
        "ack_manual_oco_commands": ack_commands,
        "command": ack_commands[0],
        "verification_command": "python -m eta_engine.scripts.broker_bracket_audit --json",
        "detail": check.get("detail"),
    }


def _paper_soak_step(gate_report: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    evidence = _as_dict(check.get("evidence"))
    primary_bot = str(gate_report.get("primary_bot") or "volume_profile_mnq")
    primary_candidate = _as_dict(
        _as_dict(_checks_by_name(gate_report).get("primary_ladder", {})).get("evidence"),
    ).get("primary_candidate")
    candidate = _as_dict(primary_candidate)
    launch_lane = str(evidence.get("launch_lane") or candidate.get("launch_lane") or "paper_soak")
    return {
        "id": "hold_primary_paper_soak",
        "status": "blocked",
        "title": "Keep primary strategy in paper soak until live eligibility clears",
        "manual": False,
        "order_action": False,
        "bot_id": primary_bot,
        "launch_lane": launch_lane,
        "scope_family": str(gate_report.get("scope_family") or ""),
        "scope_mode": str(gate_report.get("scope_mode") or ""),
        "scope_note": str(gate_report.get("scope_note") or ""),
        "parallel_launch_surface": str(gate_report.get("parallel_launch_surface") or ""),
        "parallel_launch_scope": str(gate_report.get("parallel_launch_scope") or ""),
        "parallel_launch_note": str(gate_report.get("parallel_launch_note") or ""),
        "parallel_launch_command": PARALLEL_LAUNCH_COMMAND,
        "command": "python -m eta_engine.scripts.prop_strategy_promotion_audit --json",
        "promotion_audit_command": "python -m eta_engine.scripts.prop_strategy_promotion_audit --json",
        "ladder_command": "python -m eta_engine.scripts.futures_prop_ladder --json",
        "verification_command": "python -m eta_engine.scripts.prop_live_readiness_gate --json",
        "detail": check.get("detail"),
    }


def build_checklist_report(*, gate_report: dict[str, Any]) -> dict[str, Any]:
    checks = _checks_by_name(gate_report)
    checklist: list[dict[str, Any]] = []
    prop_check = checks.get("prop_readiness", {})
    bracket_check = checks.get("broker_native_brackets", {})
    live_bot_check = checks.get("live_bot_gate", {})

    if _blocked(bracket_check):
        checklist.append(_manual_oco_step(bracket_check))
    if _blocked(live_bot_check) or _blocked(checks.get("primary_ladder", {})):
        checklist.append(_paper_soak_step(gate_report, live_bot_check or checks.get("primary_ladder", {})))
    if _blocked(prop_check):
        checklist.append(_prop_readiness_step(prop_check))

    summary = str(gate_report.get("summary") or "UNKNOWN")
    can_start = summary == "READY_FOR_CONTROLLED_PROP_DRY_RUN" and not checklist
    scope_family = str(gate_report.get("scope_family") or "")
    scope_mode = str(gate_report.get("scope_mode") or "")
    scope_primary_bot = str(gate_report.get("primary_bot") or "")
    parallel_launch_surface = str(gate_report.get("parallel_launch_surface") or "")
    parallel_launch_scope = str(gate_report.get("parallel_launch_scope") or "")
    parallel_launch_note = str(gate_report.get("parallel_launch_note") or "")
    scope_summary = (
        f"{scope_family}/{scope_mode} for {scope_primary_bot}"
        if scope_family or scope_mode or scope_primary_bot
        else ""
    )
    parallel_lane_hint = (
        f"Separate lane: {parallel_launch_scope} via {parallel_launch_surface}"
        if parallel_launch_surface or parallel_launch_scope
        else ""
    )
    return {
        "kind": "eta_prop_operator_checklist",
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "summary": summary,
        "primary_bot": gate_report.get("primary_bot"),
        "scope_family": scope_family,
        "scope_mode": scope_mode,
        "scope_note": str(gate_report.get("scope_note") or ""),
        "scope_summary": scope_summary,
        "parallel_launch_surface": parallel_launch_surface,
        "parallel_launch_scope": parallel_launch_scope,
        "parallel_launch_note": parallel_launch_note,
        "parallel_launch_command": PARALLEL_LAUNCH_COMMAND,
        "parallel_lane_hint": parallel_lane_hint,
        "can_start_prop_dry_run": can_start,
        "blocking_step_count": len(checklist),
        "checklist": checklist,
        "gate_next_actions": _as_list(gate_report.get("next_actions")),
    }


def write_report(report: dict[str, Any], path: Path = DEFAULT_OUT) -> Path:
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _current_gate_report() -> dict[str, Any]:
    from eta_engine.scripts import prop_live_readiness_gate  # noqa: PLC0415

    inputs = prop_live_readiness_gate.load_gate_inputs()
    return prop_live_readiness_gate.build_gate_report(**inputs)


def _print_human(report: dict[str, Any], out_path: Path | None = None) -> None:
    print()
    print("EVOLUTIONARY TRADING ALGO -- Prop Operator Checklist")
    print("=" * 72)
    print(f"summary   : {report['summary']}")
    print(f"can start : {report['can_start_prop_dry_run']}")
    scope_summary = str(report.get("scope_summary") or "").strip()
    if scope_summary:
        print(f"lane      : {scope_summary}")
    scope_family = str(report.get("scope_family") or "").strip()
    scope_mode = str(report.get("scope_mode") or "").strip()
    if scope_family or scope_mode:
        print(f"scope     : {scope_family}/{scope_mode}")
    parallel_lane_hint = str(report.get("parallel_lane_hint") or "").strip()
    parallel_launch_command = str(report.get("parallel_launch_command") or "").strip()
    if parallel_lane_hint:
        print(f"parallel  : {parallel_lane_hint}")
    if parallel_launch_command:
        print(f"parallel cmd: {parallel_launch_command}")
    if out_path is not None:
        print(f"artifact  : {out_path}")
    print("-" * 72)
    if not report["checklist"]:
        print("No blocking operator checklist items.")
    for step in report["checklist"]:
        print(f"[{step['status']}] {step['id']}: {step['title']}")
        print(f"  command: {step['command']}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only prop dry-run operator checklist")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    report = build_checklist_report(gate_report=_current_gate_report())
    out_path = None if args.no_write else write_report(report, args.out)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        _print_human(report, out_path)
    return 0 if report["can_start_prop_dry_run"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Consolidated prop-live go/no-go gate.

This script is deliberately read-only. It combines the futures strategy
ladder, prop-account readiness, broker health, router cleanliness, bracket
coverage, and closed-trade ledger evidence into one hard dry-run gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from eta_engine.scripts import workspace_roots  # noqa: E402
from eta_engine.scripts.futures_prop_ladder import PRIMARY_BOT  # noqa: E402

DEFAULT_OUT = workspace_roots.ETA_RUNTIME_STATE_DIR / "prop_live_readiness_latest.json"
DEFAULT_LADDER_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "futures_prop_ladder_latest.json"
DEFAULT_PROP_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "tradovate_prop_readiness.json"
DEFAULT_LEDGER_PATH = workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH
DEFAULT_BRACKET_AUDIT_PATH = workspace_roots.ETA_BROKER_BRACKET_AUDIT_PATH
DEFAULT_MASTER_URL = "https://ops.evolutionarytradingalgo.com/api/master/status"
DEFAULT_FLEET_URL = "https://ops.evolutionarytradingalgo.com/api/bot-fleet"


def _as_dict(value: Any) -> dict[str, Any]:  # noqa: ANN401
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:  # noqa: ANN401
    return value if isinstance(value, list) else []


def _as_int(value: Any, default: int = 0) -> int:  # noqa: ANN401
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_json(path: Path) -> Any:  # noqa: ANN401
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _fetch_json(url: str, timeout_s: float = 10.0) -> Any:  # noqa: ANN401
    if not url:
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "eta-prop-live-readiness-gate"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
    except (OSError, urllib.error.URLError, TimeoutError):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _check(name: str, status: str, detail: str, **evidence: Any) -> dict[str, Any]:  # noqa: ANN401
    payload: dict[str, Any] = {"name": name, "status": status, "detail": detail}
    if evidence:
        payload["evidence"] = evidence
    return payload


def _primary_candidate(ladder: dict[str, Any]) -> dict[str, Any]:
    for candidate in _as_list(ladder.get("candidates")):
        candidate_dict = _as_dict(candidate)
        if candidate_dict.get("bot_id") == PRIMARY_BOT:
            return candidate_dict
    return {}


def _primary_ladder_check(ladder: dict[str, Any]) -> dict[str, Any]:
    summary = _as_dict(ladder.get("summary"))
    primary = _primary_candidate(ladder)
    primary_bot = str(summary.get("primary_bot") or primary.get("bot_id") or "")
    live_count = _as_int(summary.get("live_routing_allowed_count"))
    allowed = bool(primary.get("live_routing_allowed"))
    if primary_bot == PRIMARY_BOT and live_count >= 1 and allowed:
        return _check(
            "primary_ladder",
            "PASS",
            f"{PRIMARY_BOT} is the only primary candidate allowed for controlled prop dry-run",
            primary_bot=primary_bot,
            live_routing_allowed_count=live_count,
        )
    blockers = _as_list(primary.get("blockers"))
    detail = f"{PRIMARY_BOT} is not cleared by the futures prop ladder"
    if blockers:
        detail = f"{detail}: {'; '.join(str(blocker) for blocker in blockers)}"
    return _check(
        "primary_ladder",
        "BLOCKED",
        detail,
        primary_bot=primary_bot,
        live_routing_allowed_count=live_count,
        primary_candidate=primary,
    )


def _prop_readiness_check(prop: dict[str, Any]) -> dict[str, Any]:
    summary = str(prop.get("summary") or "UNKNOWN")
    if summary == "READY_FOR_DRY_RUN":
        return _check("prop_readiness", "PASS", "prop account credentials/auth are ready for dry-run")
    secret_presence = _as_dict(prop.get("secret_presence"))
    return _check(
        "prop_readiness",
        "BLOCKED",
        f"prop readiness is {summary}, not READY_FOR_DRY_RUN",
        prop_account=prop.get("prop_account"),
        phase=prop.get("phase"),
        missing_secrets=_as_list(secret_presence.get("missing")),
    )


def _broker_surfaces_check(master: dict[str, Any]) -> dict[str, Any]:
    systems = _as_dict(master.get("systems"))
    required = ("ibkr", "broker", "paper_live")
    statuses = {name: str(_as_dict(systems.get(name)).get("status") or "UNKNOWN") for name in required}
    if all(status == "GREEN" for status in statuses.values()):
        return _check("broker_surfaces", "PASS", "IBKR, broker router, and paper-live surfaces are green", **statuses)
    broker = _as_dict(systems.get("broker"))
    paper_live = _as_dict(systems.get("paper_live"))
    bracket_hold = (
        statuses.get("ibkr") == "GREEN"
        and statuses.get("broker") == "YELLOW"
        and statuses.get("paper_live") == "YELLOW"
        and str(broker.get("raw_status") or "").lower() == "ok"
        and str(broker.get("target_exit_status") or "") == "missing_brackets"
        and _as_int(broker.get("active_blocker_count")) == 0
        and bool(paper_live.get("critical_ready"))
        and (
            bool(paper_live.get("held_by_bracket_audit"))
            or str(paper_live.get("effective_status") or "") == "held_by_bracket_audit"
        )
    )
    if bracket_hold:
        return _check(
            "broker_surfaces",
            "PASS",
            "IBKR, broker router, and paper-live are operational; paper-live is held by bracket audit",
            **statuses,
            broker_raw_status=broker.get("raw_status"),
            target_exit_status=broker.get("target_exit_status"),
            paper_live_effective_status=paper_live.get("effective_status"),
        )
    return _check(
        "broker_surfaces",
        "BLOCKED",
        "one or more broker/control-plane surfaces are not green",
        **statuses,
    )


def _router_cleanliness_check(fleet: dict[str, Any]) -> dict[str, Any]:
    router = _as_dict(fleet.get("broker_router"))
    result_counts = _as_dict(router.get("result_status_counts"))
    active_blockers = _as_int(router.get("active_blocker_count"))
    pending = _as_int(router.get("pending_count"))
    processing = _as_int(router.get("processing_count"))
    failed = _as_int(router.get("failed_count"))
    quarantine = _as_int(router.get("quarantine_count"))
    rejected = _as_int(result_counts.get("REJECTED"))
    if active_blockers == 0 and pending == 0 and processing == 0:
        return _check(
            "router_cleanliness",
            "PASS",
            "router has no active blockers, pending orders, or processing orders",
            active_blocker_count=active_blockers,
            pending_count=pending,
            processing_count=processing,
            historical_failed_count=failed,
            historical_quarantine_count=quarantine,
            historical_rejected_count=rejected,
        )
    return _check(
        "router_cleanliness",
        "BLOCKED",
        "router has active work or blockers and is not clean enough for prop dry-run",
        active_blocker_count=active_blockers,
        pending_count=pending,
        processing_count=processing,
        failed_count=failed,
        quarantine_count=quarantine,
        rejected_count=rejected,
    )


def _derived_position_summary(fleet: dict[str, Any]) -> dict[str, int]:
    bots = [_as_dict(bot) for bot in _as_list(fleet.get("bots"))]
    open_position_count = 0
    broker_bracket_count = 0
    supervisor_local_count = 0
    for bot in bots:
        open_positions = _as_int(bot.get("open_positions"))
        if open_positions <= 0:
            continue
        open_position_count += open_positions
        if bool(bot.get("broker_bracket")):
            broker_bracket_count += open_positions
        else:
            supervisor_local_count += open_positions
    return {
        "broker_open_position_count": open_position_count,
        "broker_bracket_count": broker_bracket_count,
        "supervisor_local_position_count": supervisor_local_count,
    }


def _broker_native_brackets_check(
    fleet: dict[str, Any],
    broker_bracket_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    broker_bracket_audit = _as_dict(broker_bracket_audit)
    audit_summary = str(broker_bracket_audit.get("summary") or "")
    if broker_bracket_audit:
        if bool(broker_bracket_audit.get("ready_for_prop_dry_run")):
            detail = "broker-native bracket/OCO audit is clear"
            if audit_summary == "READY_OPEN_EXPOSURE_MANUAL_OCO_VERIFIED":
                detail = "broker-native bracket/OCO audit is clear via manual OCO verification"
            return _check(
                "broker_native_brackets",
                "PASS",
                detail,
                audit_summary=audit_summary,
                position_summary=_as_dict(broker_bracket_audit.get("position_summary")),
            )
        if audit_summary:
            return _check(
                "broker_native_brackets",
                "BLOCKED",
                str(
                    broker_bracket_audit.get("next_action")
                    or broker_bracket_audit.get("operator_action")
                    or f"broker-native bracket/OCO audit is {audit_summary}",
                ),
                audit_summary=audit_summary,
                position_summary=_as_dict(broker_bracket_audit.get("position_summary")),
                primary_unprotected_position=_as_dict(
                    broker_bracket_audit.get("primary_unprotected_position"),
                ),
            )

    summary = _as_dict(fleet.get("summary"))
    if not summary:
        summary = _derived_position_summary(fleet)
    broker_open = _as_int(summary.get("broker_open_position_count"))
    bracketed = _as_int(summary.get("broker_bracket_count"))
    supervisor_local = _as_int(summary.get("supervisor_local_position_count"))
    if broker_open == 0 and supervisor_local == 0:
        return _check("broker_native_brackets", "PASS", "no open exposure requires broker-native bracket proof")
    if broker_open > 0 and bracketed >= broker_open and supervisor_local == 0:
        return _check("broker_native_brackets", "PASS", "all open exposure is covered by broker-native brackets")
    return _check(
        "broker_native_brackets",
        "BLOCKED",
        "open exposure is still relying on supervisor-local protection or missing broker-native bracket coverage",
        broker_open_position_count=broker_open,
        broker_bracket_count=bracketed,
        supervisor_local_position_count=supervisor_local,
    )


def _closed_trade_ledger_check(ledger: dict[str, Any]) -> dict[str, Any]:
    closed_count = _as_int(ledger.get("closed_trade_count"))
    schema_version = _as_int(ledger.get("schema_version"))
    if closed_count > 0 and schema_version >= 1:
        return _check(
            "closed_trade_ledger",
            "PASS",
            "closed-trade ledger has schema-backed closed outcomes",
            closed_trade_count=closed_count,
            schema_version=schema_version,
        )
    return _check(
        "closed_trade_ledger",
        "BLOCKED",
        "missing schema-backed closed-trade outcomes for win-rate/PnL validation",
        closed_trade_count=closed_count,
        schema_version=schema_version,
    )


def _live_bot_gate_check(fleet: dict[str, Any]) -> dict[str, Any]:
    bots = [_as_dict(bot) for bot in _as_list(fleet.get("bots"))]
    primary = next((bot for bot in bots if bot.get("id") == PRIMARY_BOT), {})
    if primary and bool(primary.get("can_live_trade")):
        return _check("live_bot_gate", "PASS", f"{PRIMARY_BOT} is marked can_live_trade on the live fleet surface")
    if primary:
        return _check(
            "live_bot_gate",
            "BLOCKED",
            f"{PRIMARY_BOT} is visible but still not marked can_live_trade",
            launch_lane=primary.get("launch_lane"),
            bot_status=primary.get("status"),
        )
    return _check("live_bot_gate", "BLOCKED", f"{PRIMARY_BOT} is missing from the live fleet surface")


def _next_actions(checks: list[dict[str, Any]]) -> list[str]:
    blocked_checks = {
        str(check["name"]): check
        for check in checks
        if check.get("status") == "BLOCKED"
    }
    blocked = set(blocked_checks)
    actions: list[str] = []
    if "prop_readiness" in blocked:
        evidence = _as_dict(blocked_checks["prop_readiness"].get("evidence"))
        prop_account = str(evidence.get("prop_account") or "blusky_50k")
        missing = [str(item) for item in _as_list(evidence.get("missing_secrets")) if item]
        if missing:
            actions.append(
                "Seed Tradovate API secrets after funding/API unlock: "
                f"python -m eta_engine.scripts.setup_tradovate_secrets --prop-account {prop_account}; "
                f"missing: {', '.join(missing)}.",
            )
        else:
            actions.append(
                "Keep Tradovate DORMANT until funding/API unlock and explicit code/docs reactivation.",
            )
    if "primary_ladder" in blocked or "live_bot_gate" in blocked:
        live_evidence = _as_dict(blocked_checks.get("live_bot_gate", {}).get("evidence"))
        primary_candidate = _as_dict(
            _as_dict(blocked_checks.get("primary_ladder", {}).get("evidence")).get("primary_candidate"),
        )
        launch_lane = str(live_evidence.get("launch_lane") or primary_candidate.get("launch_lane") or "paper")
        blockers = [str(item) for item in _as_list(primary_candidate.get("blockers")) if item]
        detail = f"Keep {PRIMARY_BOT} in {launch_lane} until can_live_trade=true and the futures prop ladder clears"
        if blockers:
            detail = f"{detail}; current blocker(s): {'; '.join(blockers)}"
        actions.append(f"{detail}.")
    if "router_cleanliness" in blocked:
        actions.append("Archive or resolve historical failed/quarantined/rejected router residue before prop dry-run.")
    if "broker_native_brackets" in blocked:
        evidence = _as_dict(blocked_checks["broker_native_brackets"].get("evidence"))
        position = _as_dict(evidence.get("primary_unprotected_position"))
        position_summary = _as_dict(evidence.get("position_summary"))
        symbol = str(position.get("symbol") or "").strip().upper()
        venue = str(position.get("venue") or "ibkr").strip().lower()
        unprotected_symbols = [
            str(item).strip().upper()
            for item in _as_list(position_summary.get("unprotected_symbols"))
            if str(item).strip()
        ]
        if symbol and symbol not in unprotected_symbols:
            unprotected_symbols.insert(0, symbol)
        if unprotected_symbols:
            ack_commands = [
                (
                    "python -m eta_engine.scripts.broker_bracket_audit "
                    f"--ack-manual-oco --symbol {item} --venue {venue} --operator edward "
                    "--expires-hours 24 --confirm"
                )
                for item in unprotected_symbols
            ]
            actions.append(
                "After visually confirming broker-native TP/SL OCO in TWS/IB Gateway, record proof: "
                f"{'; '.join(ack_commands)}; otherwise flatten manually before prop dry-run.",
            )
        else:
            actions.append("Prove broker-native bracket/OCO coverage before any funded or prop dry-run exposure.")
    if "closed_trade_ledger" in blocked:
        actions.append("Ship a schema-backed closed-trade ledger so Actual Trades, Win Rate, PnL, and R are not stale.")
    if not actions:
        actions.append(f"Run the controlled no-live-money DORMANT-lane prop dry run for {PRIMARY_BOT} only.")
    return actions


def build_gate_report(
    *,
    ladder: dict[str, Any] | None = None,
    prop: dict[str, Any] | None = None,
    master: dict[str, Any] | None = None,
    fleet: dict[str, Any] | None = None,
    ledger: dict[str, Any] | None = None,
    broker_bracket_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ladder = _as_dict(ladder)
    prop = _as_dict(prop)
    master = _as_dict(master)
    fleet = _as_dict(fleet)
    ledger = _as_dict(ledger)

    checks = [
        _primary_ladder_check(ladder),
        _prop_readiness_check(prop),
        _broker_surfaces_check(master),
        _router_cleanliness_check(fleet),
        _broker_native_brackets_check(fleet, broker_bracket_audit),
        _closed_trade_ledger_check(ledger),
        _live_bot_gate_check(fleet),
    ]
    summary = (
        "BLOCKED"
        if any(check["status"] == "BLOCKED" for check in checks)
        else "READY_FOR_CONTROLLED_PROP_DRY_RUN"
    )
    return {
        "kind": "eta_prop_live_readiness_gate",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "summary": summary,
        "primary_bot": PRIMARY_BOT,
        "checks": checks,
        "next_actions": _next_actions(checks),
    }


def exit_code(report: dict[str, Any]) -> int:
    return 1 if report.get("summary") == "BLOCKED" else 0


def write_report(report: dict[str, Any], path: Path = DEFAULT_OUT) -> Path:
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _build_current_prop(prop_account: str) -> dict[str, Any]:
    try:
        from eta_engine.scripts.tradovate_prop_readiness import build_report  # noqa: PLC0415

        return build_report(prop_account=prop_account, phase="cutover")
    except Exception:  # noqa: BLE001
        return {}


def _build_current_ladder(prop: dict[str, Any]) -> dict[str, Any]:
    try:
        from eta_engine.scripts import futures_prop_ladder  # noqa: PLC0415

        return futures_prop_ladder.build_ladder_report(
            readiness_rows=futures_prop_ladder._readiness_rows_from_snapshot(),  # noqa: SLF001
            strict_gate_metrics=futures_prop_ladder._latest_strict_gate_metrics(),  # noqa: SLF001
            prop_readiness=prop,
        )
    except Exception:  # noqa: BLE001
        return {}


def _build_current_ledger() -> dict[str, Any]:
    try:
        from eta_engine.scripts.closed_trade_ledger import build_ledger_report  # noqa: PLC0415

        return build_ledger_report()
    except Exception:  # noqa: BLE001
        return {}


def _build_current_broker_bracket_audit(fleet: dict[str, Any]) -> dict[str, Any]:
    try:
        from eta_engine.scripts import broker_bracket_audit  # noqa: PLC0415

        return broker_bracket_audit.build_bracket_audit(
            fleet=fleet,
            manual_ack=broker_bracket_audit.load_manual_oco_ack(),
        )
    except Exception:  # noqa: BLE001
        return {}


def load_gate_inputs(
    *,
    prop_account: str = "blusky_50k",
    ladder_path: Path = DEFAULT_LADDER_PATH,
    prop_path: Path = DEFAULT_PROP_PATH,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
    bracket_audit_path: Path = DEFAULT_BRACKET_AUDIT_PATH,
    master_url: str = DEFAULT_MASTER_URL,
    fleet_url: str = DEFAULT_FLEET_URL,
) -> dict[str, dict[str, Any]]:
    prop = _build_current_prop(prop_account) or _as_dict(_load_json(prop_path))
    ladder = _build_current_ladder(prop) or _as_dict(_load_json(ladder_path))
    ledger = _build_current_ledger() or _as_dict(_load_json(ledger_path))
    fleet = _as_dict(_fetch_json(fleet_url))
    broker_bracket_audit = _build_current_broker_bracket_audit(fleet) or _as_dict(
        _load_json(bracket_audit_path),
    )
    return {
        "ladder": ladder,
        "prop": prop,
        "master": _as_dict(_fetch_json(master_url)),
        "fleet": fleet,
        "ledger": ledger,
        "broker_bracket_audit": broker_bracket_audit,
    }


def _print_human(report: dict[str, Any], out_path: Path | None = None) -> None:
    print()
    print("EVOLUTIONARY TRADING ALGO -- Prop Live Readiness Gate")
    print("=" * 72)
    print(f"summary    : {report['summary']}")
    print(f"primary bot: {report['primary_bot']}")
    if out_path is not None:
        print(f"artifact   : {out_path}")
    print("-" * 72)
    for check in report["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    print("-" * 72)
    print("next actions:")
    for action in report["next_actions"]:
        print(f"  - {action}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only prop-live go/no-go gate")
    parser.add_argument("--prop-account", default="blusky_50k", help="Configured prop account alias")
    parser.add_argument("--ledger-path", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--bracket-audit-path", type=Path, default=DEFAULT_BRACKET_AUDIT_PATH)
    parser.add_argument("--master-url", default=DEFAULT_MASTER_URL)
    parser.add_argument("--fleet-url", default=DEFAULT_FLEET_URL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    inputs = load_gate_inputs(
        prop_account=args.prop_account,
        ledger_path=args.ledger_path,
        bracket_audit_path=args.bracket_audit_path,
        master_url=args.master_url,
        fleet_url=args.fleet_url,
    )
    report = build_gate_report(**inputs)
    out_path = None if args.no_write else write_report(report, args.out)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        _print_human(report, out_path)
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())

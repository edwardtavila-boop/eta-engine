"""No-order readiness checklist for DORMANT Tradovate prop-account cutover.

This script is intentionally read-only. It checks whether ETA is ready
for the day Tradovate API access is unlocked after funding/subscription,
without enabling a bot route and without submitting orders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from eta_engine.core.secrets import SECRETS  # noqa: E402
from eta_engine.scripts.setup_tradovate_secrets import fields_for_prop_account  # noqa: E402

Phase = Literal["predeposit", "cutover"]

ENGINE_ROOT = _ROOT
WORKSPACE_ROOT = ENGINE_ROOT.parent
DEFAULT_ROUTING_CONFIG = ENGINE_ROOT / "configs" / "bot_broker_routing.yaml"
DEFAULT_AUTH_STATUS = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "tradovate_auth_status.json"
WINNING_BOT = "volume_profile_mnq"

_LOGIN_SUFFIXES = ("TRADOVATE_USERNAME", "TRADOVATE_PASSWORD")


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _secret_present(key: str) -> bool:
    return bool(SECRETS.get(key, required=False))


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _load_auth_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"result": "INVALID_JSON"}
    return data if isinstance(data, dict) else {}


def _secret_presence(keys: list[str]) -> dict[str, list[str]]:
    present = [key for key in keys if _secret_present(key)]
    missing = [key for key in keys if key not in present]
    return {"present": present, "missing": missing}


def _phase_status_for_missing(phase: Phase, missing: list[str]) -> str:
    if not missing:
        return "PASS"
    return "WAIT" if phase == "predeposit" else "BLOCKED"


def _routing_checks(alias: str, routing_config: Path) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    cfg = _load_yaml(routing_config)
    if not cfg:
        return [_check("routing_config", "BLOCKED", f"missing or unreadable: {routing_config}")]

    prop_accounts = cfg.get("prop_accounts") or {}
    account = prop_accounts.get(alias) if isinstance(prop_accounts, dict) else None
    if not isinstance(account, dict):
        checks.append(_check("prop_account_alias", "BLOCKED", f"{alias} is not configured under prop_accounts"))
    else:
        venue = str(account.get("venue") or "").strip().lower()
        prefix = str(account.get("creds_env_prefix") or "").strip()
        account_id_env = str(account.get("account_id_env") or "").strip()
        if venue == "tradovate" and prefix and account_id_env:
            checks.append(_check("prop_account_alias", "PASS", f"{alias} routes to Tradovate with prefixed secrets"))
        else:
            checks.append(_check("prop_account_alias", "BLOCKED", f"{alias} is missing venue/prefix/account_id_env"))

    bots = cfg.get("bots") or {}
    bot_cfg = bots.get(WINNING_BOT) if isinstance(bots, dict) else None
    if not isinstance(bot_cfg, dict):
        checks.append(_check("winning_bot_route", "SAFE_HELD", f"{WINNING_BOT} is not routed to Tradovate yet"))
        return checks

    venue = str(bot_cfg.get("venue") or "").strip().lower()
    bot_alias = str(bot_cfg.get("account_alias") or "").strip().lower()
    if venue == "tradovate" and bot_alias == alias:
        checks.append(_check("winning_bot_route", "PASS", f"{WINNING_BOT} is explicitly routed to {alias}"))
    else:
        checks.append(_check("winning_bot_route", "WARN", f"{WINNING_BOT} has non-target route: {bot_cfg}"))
    return checks


def _auth_check(alias: str, phase: Phase, auth_status: Path) -> dict[str, str]:
    data = _load_auth_status(auth_status)
    if not data:
        status = "WAIT" if phase == "predeposit" else "BLOCKED"
        return _check("oauth_authorization", status, f"no auth status artifact at {auth_status}")
    if data.get("result") == "AUTHORIZED" and data.get("credential_scope") == alias:
        endpoint = data.get("endpoint") or "unknown endpoint"
        return _check("oauth_authorization", "PASS", f"last auth authorized for {alias} at {endpoint}")
    status = "WAIT" if phase == "predeposit" else "BLOCKED"
    result = data.get("result") or "UNKNOWN"
    scope = data.get("credential_scope") or "UNKNOWN"
    return _check(
        "oauth_authorization",
        status,
        f"last auth result={result} scope={scope}; expected AUTHORIZED/{alias}",
    )


def _activation_check(phase: Phase) -> dict[str, str]:
    enabled = _truthy(os.environ.get("ETA_TRADOVATE_ENABLED"))
    if enabled:
        return _check("tradovate_activation_flag", "PASS", "ETA_TRADOVATE_ENABLED is set")
    if phase == "predeposit":
        return _check("tradovate_activation_flag", "SAFE_HELD", "ETA_TRADOVATE_ENABLED is not set yet")
    return _check("tradovate_activation_flag", "BLOCKED", "set ETA_TRADOVATE_ENABLED=1 for cutover smoke")


def _summary(phase: Phase, checks: list[dict[str, str]]) -> str:
    statuses = {check["status"] for check in checks}
    if "BLOCKED" in statuses:
        return "BLOCKED"
    if phase == "cutover":
        return "READY_FOR_DRY_RUN" if "WAIT" not in statuses else "BLOCKED"
    return "READY_FOR_DEPOSIT"


def _next_actions(summary: str, secret_presence: dict[str, list[str]]) -> list[str]:
    missing = secret_presence["missing"]
    actions: list[str] = []
    if "BLUSKY_TRADOVATE_ACCOUNT_ID" in missing:
        actions.append("After funding/API activation, capture the numeric Tradovate account ID.")
    if any(key.endswith(("APP_ID", "APP_SECRET", "CID")) for key in missing):
        actions.append("After funding, purchase/enable API Access and generate the app ID, CID, and app secret.")
    if summary == "READY_FOR_DEPOSIT":
        actions.append("Deposit/fund the Tradovate account, complete CME agreement, and enable the API Access add-on.")
    if summary == "READY_FOR_DRY_RUN":
        actions.append("Run a no-live-money broker-router dry run before enabling volume_profile_mnq routing.")
    if summary == "BLOCKED":
        actions.append("Clear every BLOCKED check before running Tradovate cutover.")
    return actions


def build_report(
    *,
    prop_account: str = "blusky_50k",
    phase: Phase = "predeposit",
    routing_config: Path = DEFAULT_ROUTING_CONFIG,
    auth_status: Path = DEFAULT_AUTH_STATUS,
) -> dict[str, Any]:
    fields = fields_for_prop_account(prop_account)
    keys = [key for key, *_ in fields]
    login_keys = [key for key in keys if key.endswith(_LOGIN_SUFFIXES)]
    api_keys = [key for key in keys if key not in login_keys]
    presence = _secret_presence(keys)

    missing_login = [key for key in login_keys if key in presence["missing"]]
    missing_api = [key for key in api_keys if key in presence["missing"]]

    checks = [
        _check(
            "prop_login_credentials",
            "PASS" if not missing_login else "BLOCKED",
            "BluSky platform login is stored" if not missing_login else f"missing: {', '.join(missing_login)}",
        ),
        _check(
            "prop_api_credentials",
            _phase_status_for_missing(phase, missing_api),
            "all API/account fields are stored" if not missing_api else f"missing: {', '.join(missing_api)}",
        ),
        _activation_check(phase),
        *_routing_checks(prop_account, routing_config),
        _auth_check(prop_account, phase, auth_status),
    ]
    summary = _summary(phase, checks)

    return {
        "kind": "eta_tradovate_prop_readiness",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "prop_account": prop_account,
        "phase": phase,
        "summary": summary,
        "secret_presence": presence,
        "routing_config": str(routing_config),
        "auth_status": str(auth_status),
        "checks": checks,
        "next_actions": _next_actions(summary, presence),
    }


def exit_code(report: dict[str, Any]) -> int:
    return 1 if report.get("summary") == "BLOCKED" else 0


def _print_human(report: dict[str, Any]) -> None:
    print()
    print("EVOLUTIONARY TRADING ALGO -- Tradovate Prop Readiness")
    print("=" * 68)
    print(f"prop_account: {report['prop_account']}")
    print(f"phase       : {report['phase']}")
    print(f"summary     : {report['summary']}")
    print("-" * 68)
    for check in report["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    print("-" * 68)
    print("next actions:")
    for action in report["next_actions"]:
        print(f"  - {action}")
    print("=" * 68)


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only Tradovate prop readiness check")
    ap.add_argument("--prop-account", default="blusky_50k", help="Configured prop account alias")
    ap.add_argument("--phase", choices=["predeposit", "cutover"], default="predeposit")
    ap.add_argument("--routing-config", type=Path, default=DEFAULT_ROUTING_CONFIG)
    ap.add_argument("--auth-status", type=Path, default=DEFAULT_AUTH_STATUS)
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = ap.parse_args()

    report = build_report(
        prop_account=args.prop_account,
        phase=args.phase,
        routing_config=args.routing_config,
        auth_status=args.auth_status,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())

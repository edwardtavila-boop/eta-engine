"""Secrets validator - verify VPS is ready for autonomous operation.

Checks required secrets for both presence and basic semantic validity.
Run before VPS bootstrap or after credential rotation.

Usage:
    python scripts/secrets_validator.py
    python scripts/secrets_validator.py --json

Exit codes:
    0 = all required secrets are valid
    1 = only optional secrets still need attention
    2 = one or more required secrets are missing, invalid, or placeholders
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


class SecretStatus(StrEnum):
    PRESENT = "present"
    PLACEHOLDER = "placeholder"
    INVALID = "invalid"
    MISSING = "missing"
    OPTIONAL_MISSING = "optional_missing"


@dataclass
class SecretCheck:
    path: str
    status: SecretStatus
    description: str = ""
    required: bool = True
    detail: str = ""


SECRET_SPECS: list[tuple[str, str, bool]] = [
    ("secrets/telegram_bot_token.txt", "Telegram bot token from @BotFather", True),
    ("secrets/telegram_chat_id.txt", "Telegram chat ID for operator notifications", True),
    ("secrets/ibkr_account_id.txt", "IBKR account ID for 24/7 trading", True),
    ("secrets/ibkr_credentials.json", "IBKR credentials JSON with reconnect config", True),
    ("secrets/quantum_creds.json", "D-Wave/IBM Quantum API credentials", False),
    ("secrets/tastytrade_credentials.json", "Tastytrade credentials (secondary broker)", False),
]

_PLACEHOLDER_MARKERS = (
    "place your",
    "placeholder",
    "replace me",
    "change me",
    "changeme",
    "set me",
    "todo",
    "tbd",
    "your_token_here",
    "your secret here",
)
_TELEGRAM_BOT_TOKEN_PATTERN = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")
_TELEGRAM_CHAT_ID_PATTERN = re.compile(r"^-?\d{5,}$")
_IBKR_ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z]{2,6}[A-Za-z0-9]{3,}$")
_IBKR_LOGIN_KEYS = ("username", "user", "login", "ib_login_id", "user_id")
_TASTYTRADE_LOGIN_KEYS = ("username", "user", "login", "email")
_TASTYTRADE_SECRET_KEYS = ("password", "pass", "remember_token", "session_token", "api_token")


def _looks_like_placeholder(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True

    lowered = stripped.casefold()
    if lowered in {"none", "null"}:
        return True
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _validate_text_secret(
    path: Path,
    *,
    pattern: re.Pattern[str] | None = None,
    label: str,
) -> tuple[SecretStatus, str]:
    value = _load_text(path)
    if _looks_like_placeholder(value):
        return SecretStatus.PLACEHOLDER, "File contains placeholder/template text instead of a live secret."
    if pattern is not None and not pattern.fullmatch(value):
        return SecretStatus.INVALID, f"Expected a valid {label} value."
    return SecretStatus.PRESENT, ""


def _load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _resolve_string_value(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _validate_ibkr_credentials_json(path: Path) -> tuple[SecretStatus, str]:
    data = _load_json_object(path)
    if data is None:
        return SecretStatus.INVALID, "Expected a JSON object."

    login_id = _resolve_string_value(data, _IBKR_LOGIN_KEYS)
    if login_id is None:
        return SecretStatus.INVALID, "Missing username/login field required for unattended IBKR auth."
    if _looks_like_placeholder(login_id):
        return SecretStatus.PLACEHOLDER, "IBKR username/login is still placeholder text."
    return SecretStatus.PRESENT, ""


def _validate_quantum_credentials_json(path: Path) -> tuple[SecretStatus, str]:
    data = _load_json_object(path)
    if data is None:
        return SecretStatus.INVALID, "Expected a JSON object."

    dwave = data.get("dwave") if isinstance(data.get("dwave"), dict) else {}
    ibm = data.get("ibm") if isinstance(data.get("ibm"), dict) else {}
    budget = data.get("budget") if isinstance(data.get("budget"), dict) else {}
    dwave_token = str(dwave.get("token", "")).strip()
    ibm_token = str(ibm.get("token", "")).strip()
    enable_cloud = bool(budget.get("enable_cloud", False))

    if enable_cloud and _looks_like_placeholder(dwave_token) and _looks_like_placeholder(ibm_token):
        return SecretStatus.INVALID, "Cloud quantum is enabled but no provider token is populated."
    if _looks_like_placeholder(dwave_token) and _looks_like_placeholder(ibm_token):
        return SecretStatus.PLACEHOLDER, "Quantum credentials are still the bootstrap template."
    return SecretStatus.PRESENT, ""


def _validate_tastytrade_credentials_json(path: Path) -> tuple[SecretStatus, str]:
    data = _load_json_object(path)
    if data is None:
        return SecretStatus.INVALID, "Expected a JSON object."

    login_id = _resolve_string_value(data, _TASTYTRADE_LOGIN_KEYS)
    secret_value = _resolve_string_value(data, _TASTYTRADE_SECRET_KEYS)
    if login_id is None or secret_value is None:
        return SecretStatus.INVALID, "Missing login or credential field required for Tastytrade auth."
    if _looks_like_placeholder(login_id) or _looks_like_placeholder(secret_value):
        return SecretStatus.PLACEHOLDER, "Tastytrade credentials are still placeholder text."
    return SecretStatus.PRESENT, ""


def _validate_secret_path(rel_path: str, full_path: Path) -> tuple[SecretStatus, str]:
    if rel_path.endswith("telegram_bot_token.txt"):
        return _validate_text_secret(full_path, pattern=_TELEGRAM_BOT_TOKEN_PATTERN, label="Telegram bot token")
    if rel_path.endswith("telegram_chat_id.txt"):
        return _validate_text_secret(full_path, pattern=_TELEGRAM_CHAT_ID_PATTERN, label="Telegram chat ID")
    if rel_path.endswith("ibkr_account_id.txt"):
        return _validate_text_secret(full_path, pattern=_IBKR_ACCOUNT_ID_PATTERN, label="IBKR account ID")
    if rel_path.endswith("ibkr_credentials.json"):
        return _validate_ibkr_credentials_json(full_path)
    if rel_path.endswith("quantum_creds.json"):
        return _validate_quantum_credentials_json(full_path)
    if rel_path.endswith("tastytrade_credentials.json"):
        return _validate_tastytrade_credentials_json(full_path)
    return SecretStatus.PRESENT, ""


def check_secrets(*, root: Path | None = None) -> list[SecretCheck]:
    base_root = root or ROOT
    results: list[SecretCheck] = []
    for rel_path, desc, required in SECRET_SPECS:
        full_path = base_root / rel_path
        if not full_path.exists():
            results.append(
                SecretCheck(
                    path=rel_path,
                    status=SecretStatus.MISSING if required else SecretStatus.OPTIONAL_MISSING,
                    description=desc,
                    required=required,
                )
            )
            continue

        status, detail = _validate_secret_path(rel_path, full_path)
        results.append(
            SecretCheck(
                path=rel_path,
                status=status,
                description=desc,
                required=required,
                detail=detail,
            )
        )
    return results


def _build_report(results: list[SecretCheck]) -> dict[str, Any]:
    required_issues = [result for result in results if result.required and result.status != SecretStatus.PRESENT]
    optional_issues = [result for result in results if not result.required and result.status != SecretStatus.PRESENT]

    if required_issues:
        exit_code = 2
    elif optional_issues:
        exit_code = 1
    else:
        exit_code = 0

    return {
        "exit_code": exit_code,
        "summary": {
            "required_invalid_count": len(required_issues),
            "optional_issue_count": len(optional_issues),
            "present_count": sum(1 for result in results if result.status == SecretStatus.PRESENT),
            "total_count": len(results),
        },
        "results": [
            {
                "path": result.path,
                "status": result.status,
                "description": result.description,
                "required": result.required,
                "detail": result.detail,
            }
            for result in results
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate ETA secrets before bootstrap or live operations.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results = check_secrets()
    report = _build_report(results)

    if args.json:
        print(json.dumps(report, indent=2))
        return int(report["exit_code"])

    required_issues = [result for result in results if result.required and result.status != SecretStatus.PRESENT]
    optional_issues = [result for result in results if not result.required and result.status != SecretStatus.PRESENT]

    lines = []
    for result in results:
        label = {
            SecretStatus.PRESENT: "OK",
            SecretStatus.PLACEHOLDER: "PLACEHOLDER",
            SecretStatus.INVALID: "INVALID",
            SecretStatus.MISSING: "MISSING",
            SecretStatus.OPTIONAL_MISSING: "N/A",
        }[result.status]
        lines.append(f"  [{label}] {result.path} - {result.description}")
        if result.detail:
            lines.append(f"        {result.detail}")

    print("=== Secrets Validation ===")
    for line in lines:
        print(line)

    print()
    if required_issues:
        print(f"CRITICAL: {len(required_issues)} required secret(s) are not ready:")
        for result in required_issues:
            print(f"  - {result.path}")
        print(f"Place files in {ROOT / 'secrets'}/")
        return 2
    if optional_issues:
        print(f"INFO: {len(optional_issues)} optional secret(s) still need attention:")
        for result in optional_issues:
            print(f"  - {result.path}")
        return 1
    print("All required secrets present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

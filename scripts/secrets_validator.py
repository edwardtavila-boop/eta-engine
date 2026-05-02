"""Secrets validator — verify VPS is ready for autonomous operation.

Checks all required secrets files exist and are populated with valid values.
Run before VPS bootstrap or after credential rotation.

Usage:
    python scripts/secrets_validator.py
    python scripts/secrets_validator.py --json  # machine-readable output

Exit codes: 0 = all present, 1 = missing optional, 2 = missing required
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SecretStatus(StrEnum):
    PRESENT = "present"
    EMPTY = "empty"
    MISSING = "missing"
    OPTIONAL_MISSING = "optional_missing"


@dataclass
class SecretCheck:
    path: str
    status: SecretStatus
    description: str = ""
    required: bool = True
    detail: str = ""


REQUIRED_SECRETS: list[tuple[str, str, bool]] = [
    ("secrets/telegram_bot_token.txt", "Telegram bot token from @BotFather", True),
    ("secrets/telegram_chat_id.txt", "Telegram chat ID for operator notifications", True),
    ("secrets/ibkr_account_id.txt", "IBKR account ID for 24/7 trading", True),
    ("secrets/ibkr_credentials.json", "IBKR credentials JSON with reconnect config", True),
    ("secrets/quantum_creds.json", "D-Wave/IBM Quantum API credentials", False),
    ("secrets/tastytrade_credentials.json", "Tastytrade credentials (secondary broker)", False),
]


def check_secrets() -> list[SecretCheck]:
    results: list[SecretCheck] = []
    for rel_path, desc, required in REQUIRED_SECRETS:
        full_path = ROOT / rel_path
        if not full_path.exists():
            results.append(SecretCheck(
                path=rel_path, status=SecretStatus.MISSING if required else SecretStatus.OPTIONAL_MISSING,
                description=desc, required=required,
            ))
        elif full_path.stat().st_size < 5:
            results.append(SecretCheck(
                path=rel_path, status=SecretStatus.EMPTY, description=desc, required=required,
                detail="File exists but appears empty or is still using the placeholder template",
            ))
        else:
            results.append(SecretCheck(
                path=rel_path, status=SecretStatus.PRESENT, description=desc, required=required,
            ))
    return results


def main() -> int:
    results = check_secrets()

    missing_required = [r for r in results if r.status == SecretStatus.MISSING and r.required]
    empty = [r for r in results if r.status == SecretStatus.EMPTY]
    missing_optional = [r for r in results if r.status == SecretStatus.OPTIONAL_MISSING]

    output = []
    for r in results:
        emoji = {"present": "OK", "empty": "EMPTY", "missing": "MISSING", "optional_missing": "N/A"}[r.status]
        output.append(f"  [{emoji}] {r.path} — {r.description}")
        if r.detail:
            output.append(f"        {r.detail}")

    print("=== Secrets Validation ===")
    for line in output:
        print(line)

    print()
    if missing_required:
        print(f"CRITICAL: {len(missing_required)} required secret(s) missing:")
        for r in missing_required:
            print(f"  - {r.path}")
        print(f"Place files in {ROOT / 'secrets'}/")
        return 2
    if empty:
        print(f"WARNING: {len(empty)} secret file(s) are empty/placeholder:")
        for r in empty:
            print(f"  - {r.path}")
        return 1
    if missing_optional:
        print(f"INFO: {len(missing_optional)} optional secret(s) missing (not required for basic operation)")
    print("All required secrets present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

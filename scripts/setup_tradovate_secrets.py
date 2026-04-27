"""
EVOLUTIONARY TRADING ALGO  //  scripts.setup_tradovate_secrets
==================================================
Interactive helper to populate the 5 Tradovate credentials in the OS
keyring (Windows Credential Manager / macOS Keychain / Linux Secret
Service). The operator types each value; this script never sees plaintext
on argv, never echoes it to the console, and never writes it to a file.

Once populated, ``scripts/authorize_tradovate.py`` can pick them up via
SECRETS.get() and perform the real OAuth2 flow.

Usage
-----
    python -m eta_engine.scripts.setup_tradovate_secrets
    python -m eta_engine.scripts.setup_tradovate_secrets --check
    python -m eta_engine.scripts.setup_tradovate_secrets --reset

Where to find each value
------------------------
TRADOVATE_USERNAME    Your Tradovate login email.
TRADOVATE_PASSWORD    Your Tradovate account password (same as web login).
TRADOVATE_APP_ID      Free-form name you registered the API app under
                      (e.g. "ApexPredator"). Default is "ApexPredator".
TRADOVATE_APP_SECRET  Secret issued when your CID was registered. Found
                      in Tradovate > Trader > Apps > [your app] > Secret.
TRADOVATE_CID         Numeric Client ID for the registered app. Same
                      location as APP_SECRET.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from eta_engine.core.secrets import (  # noqa: E402
    SECRETS,
    TRADOVATE_APP_ID,
    TRADOVATE_APP_SECRET,
    TRADOVATE_CID,
    TRADOVATE_PASSWORD,
    TRADOVATE_USERNAME,
)

# (key, prompt, is_secret, default)
_FIELDS: list[tuple[str, str, bool, str]] = [
    (TRADOVATE_USERNAME, "Tradovate username (email)", False, ""),
    (TRADOVATE_PASSWORD, "Tradovate account password", True, ""),
    (TRADOVATE_APP_ID, "Tradovate app ID (free-form name)", False, "ApexPredator"),
    (TRADOVATE_APP_SECRET, "Tradovate APP SECRET (from Trader > Apps)", True, ""),
    (TRADOVATE_CID, "Tradovate CID (numeric Client ID)", False, ""),
]


def _present(key: str) -> bool:
    return bool(SECRETS.get(key, required=False))


def _prompt(key: str, label: str, is_secret: bool, default: str) -> str:
    tag = "*" if is_secret else " "
    suffix = f" [default: {default}]" if default and not is_secret else ""
    prompt = f"{tag} {key:22s}  {label}{suffix}: "
    val = getpass.getpass(prompt) if is_secret else input(prompt)
    val = val.strip()
    if not val and default:
        val = default
    return val


def cmd_check() -> int:
    print()
    print("EVOLUTIONARY TRADING ALGO -- Tradovate secret status")
    print("=" * 60)
    missing = 0
    for key, _, _, _ in _FIELDS:
        ok = _present(key)
        icon = "[OK]" if ok else "[--]"
        print(f"  {icon} {key}")
        if not ok:
            missing += 1
    print("-" * 60)
    if missing == 0:
        print("All 5 Tradovate secrets present. Run authorize_tradovate next.")
        return 0
    print(f"{missing}/5 missing. Run without --check to populate them.")
    return 1


def _store(key: str, value: str) -> None:
    SECRETS.set(key, value, scope="keyring")


def _delete(key: str) -> None:
    try:
        import keyring  # noqa: PLC0415

        keyring.delete_password("eta_engine", key)
    except Exception:  # noqa: BLE001
        pass  # Not present is fine.


def cmd_reset() -> int:
    print()
    print("Clearing Tradovate secrets from keyring...")
    for key, _, _, _ in _FIELDS:
        _delete(key)
        print(f"  [--] {key}")
    print("Done. Rerun without --reset to repopulate.")
    return 0


def cmd_interactive() -> int:
    print()
    print("EVOLUTIONARY TRADING ALGO -- Tradovate secret setup")
    print("=" * 60)
    print("Type each value; password-type fields are masked (getpass).")
    print("Leave a prompt blank to skip (keeps any existing value).")
    print("-" * 60)

    updated = 0
    skipped_present: list[str] = []
    for key, label, is_secret, default in _FIELDS:
        already = _present(key)
        if already:
            skipped_present.append(key)
            # Offer to overwrite.
            ans = input(f"  {key} is already stored. Overwrite? [y/N]: ").strip().lower()
            if ans not in {"y", "yes"}:
                continue
        val = _prompt(key, label, is_secret, default)
        if not val:
            print(f"  (skipped {key}; still missing)")
            continue
        try:
            _store(key, val)
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERROR] keyring set failed for {key}: {type(exc).__name__}")
            return 2
        updated += 1
        print(f"  [OK] stored {key}")

    print("-" * 60)
    present_now = sum(1 for k, _, _, _ in _FIELDS if _present(k))
    print(f"Stored this run: {updated}")
    print(f"Present overall: {present_now}/5")
    if present_now == 5:
        print("All 5 present. Next:")
        print("  python -m eta_engine.scripts.authorize_tradovate")
        return 0
    missing = [k for k, _, _, _ in _FIELDS if not _present(k)]
    print(f"Still missing: {missing}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Setup Tradovate OAuth2 secrets")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="Just report which secrets are present; no prompts.")
    group.add_argument("--reset", action="store_true", help="Delete existing Tradovate secrets from keyring.")
    args = ap.parse_args()

    if args.check:
        return cmd_check()
    if args.reset:
        return cmd_reset()
    return cmd_interactive()


if __name__ == "__main__":
    sys.exit(main())

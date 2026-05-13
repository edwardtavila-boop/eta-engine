"""Sync Windows user environment variables from canonical ``.env`` files.

Ensures Windows user env vars match what's in the canonical ``.env`` files,
preventing stale credentials from shadowing updated values.

Usage::

    python eta_engine/scripts/env_sync.py          # dry-run
    python eta_engine/scripts/env_sync.py --apply  # apply changes
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILES = [
    ROOT / ".env",
    ROOT / "eta_engine" / ".env",
]

# Keys that should be synced from .env to Windows user env
SYNC_KEYS = [
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
]


def load_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for env_file in ENV_FILES:
        if not env_file.is_file():
            continue
        with env_file.open(encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if key in SYNC_KEYS and val:
                    values[key] = val
    return values


def get_windows_env(key: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603,S607 — fixed argv, key from SYNC_KEYS
            [
                "powershell",
                "-Command",
                f"[Environment]::GetEnvironmentVariable('{key}', 'User')",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        val = result.stdout.strip()
        return val if val else None
    except (subprocess.SubprocessError, OSError):
        return None


def set_windows_env(key: str, value: str) -> None:
    # Escape single quotes for safe PowerShell string literal embedding.
    safe_value = value.replace("'", "''")
    subprocess.run(  # noqa: S603,S607 — fixed argv, key from SYNC_KEYS
        [
            "powershell",
            "-Command",
            f"[Environment]::SetEnvironmentVariable('{key}', '{safe_value}', 'User')",
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    os.environ[key] = value


def main() -> None:
    apply_changes = "--apply" in sys.argv
    env_values = load_env_values()

    print("=== ENV SYNC ===")
    print(f"Source files: {[str(f) for f in ENV_FILES if f.is_file()]}")
    print()

    changes = 0
    for key in SYNC_KEYS:
        env_val = env_values.get(key, "")
        win_val = get_windows_env(key)

        if not env_val and not win_val:
            print(f"  {key}: not set in .env or Windows — OK")
            continue

        if env_val and not win_val:
            print(f"  {key}: PRESENT in .env, MISSING in Windows env")
            if apply_changes:
                set_windows_env(key, env_val)
                print("    -> SYNCED")
            else:
                print("    (dry-run: use --apply to sync)")
            changes += 1
        elif not env_val and win_val:
            print(f"  {key}: MISSING in .env, PRESENT in Windows env (stale?)")
            if apply_changes:
                set_windows_env(key, "")
                print("    -> CLEARED")
            else:
                print("    (dry-run: use --apply to clear)")
            changes += 1
        elif env_val != win_val:
            print(f"  {key}: MISMATCH (.env != Windows)")
            if apply_changes:
                set_windows_env(key, env_val)
                print("    -> SYNCED to .env value")
            else:
                print("    (dry-run: use --apply to sync)")
            changes += 1
        else:
            print(f"  {key}: SYNCED — OK")

    print()
    if changes == 0:
        print("All environment variables are in sync.")
    elif apply_changes:
        print(f"Applied {changes} change(s). Restart your terminal for full effect.")
    else:
        print(f"{changes} mismatch(es) found. Run with --apply to fix.")


if __name__ == "__main__":
    main()

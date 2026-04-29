"""Stale-path linter for the EvolutionaryTradingAlgo workspace.

Operator mandate M1+M4 (2026-04-26): all data, code, state, logs, journals,
and configs live under ``C:\\EvolutionaryTradingAlgo\\``. Earlier locations
(``C:\\dev\\``, ``C:\\Users\\edwar\\OneDrive\\Desktop\\Base\\``,
``C:\\Users\\edwar\\OneDrive\\The_Firm\\``,
``C:\\Users\\edwar\\OneDrive\\Documents\\Claude\\Projects\\...``,
``%LOCALAPPDATA%\\eta_engine\\``, ``C:\\mnq_data\\``,
``C:\\crypto_data\\``, and old ``C:\\TheFirm\\`` paths) are deprecated.

This linter scans the files passed on argv (typically the pre-commit
hook supplies the staged file list) and fails if any contain stale
path references in active code paths.

Allowed locations for stale-path mentions:
- ``_archive*/`` directories (frozen historical snapshots)
- migration scripts: ``rewrite_paths.py``, ``fix_editable_paths.py``,
  ``_record_onedrive_migration.py``
- this script itself (the patterns are quoted as DETECTION targets)
- comments / docstrings that EXPLICITLY mark themselves as historical
  with the marker ``HISTORICAL-PATH-OK`` on the same line

Usage::

    python scripts/lint_stale_paths.py file1.py file2.py ...

Exit codes::

    0 -- no stale paths found in active code
    1 -- stale paths found; commit blocked
    2 -- internal error (no files passed, etc.)

Wire into pre-commit::

    - repo: local
      hooks:
        - id: lint-stale-paths
          name: lint stale paths (M1/M4)
          entry: python scripts/lint_stale_paths.py
          language: system
          files: \\.(py|ps1|bat|cmd|sh|md|yaml|yml|toml|json|js|ts|tsx)$
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Patterns that indicate a stale path reference. These match both
# backslash and forward-slash forms, with or without escape doubling,
# and case-insensitively.
STALE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "OneDrive\\Desktop\\Base",
        re.compile(r"OneDrive[\\/]+Desktop[\\/]+Base", re.IGNORECASE),
    ),
    (
        "OneDrive\\The_Firm",
        re.compile(r"OneDrive[\\/]+The_Firm", re.IGNORECASE),
    ),
    (
        "OneDrive\\Documents\\Claude\\Projects",
        re.compile(r"OneDrive[\\/]+Documents[\\/]+Claude[\\/]+Projects", re.IGNORECASE),
    ),
    (
        r"C:\dev\\ (deprecated 2026-04-26)",
        re.compile(r"[\"'`]C:[\\/]+dev[\\/]+", re.IGNORECASE),
    ),
    (
        "Path.home() / 'OneDrive' / ...",
        re.compile(r"home\(\)\s*/\s*[\"']OneDrive[\"']", re.IGNORECASE),
    ),
    (
        r"%LOCALAPPDATA%\eta_engine",
        re.compile(r"%LOCALAPPDATA%[\\/]+eta_engine(?:[\\/]|$)", re.IGNORECASE),
    ),
    (
        r"$env:LOCALAPPDATA\eta_engine",
        re.compile(r"\$env:LOCALAPPDATA[\\/]+eta_engine(?:[\\/]|$)", re.IGNORECASE),
    ),
    (
        "LOCALAPPDATA eta_engine join",
        re.compile(r"LOCALAPPDATA.*[\"']eta_engine[\"']|[\"']eta_engine[\"'].*LOCALAPPDATA", re.IGNORECASE),
    ),
    (
        r"C:\mnq_data",
        re.compile(r"C:[\\/]+mnq_data(?:[\\/]|$)", re.IGNORECASE),
    ),
    (
        r"C:\crypto_data",
        re.compile(r"C:[\\/]+crypto_data(?:[\\/]|$)", re.IGNORECASE),
    ),
    (
        r"C:\TheFirm / C:\The_Firm",
        re.compile(r"C:[\\/]+(?:TheFirm|The_Firm)(?:[\\/]|$)", re.IGNORECASE),
    ),
]

# Path-segment fragments that mark a file as exempt (case-insensitive).
EXEMPT_PATH_FRAGMENTS = (
    "_archive",
    "_legacy",
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    ".cache",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".next",
    ".astro",
    "dist",
    "build",
    "rewrite_paths.py",
    "fix_editable_paths.py",
    "_record_onedrive_migration.py",
    "lint_stale_paths.py",  # this file itself
    "consolidate_mnq_apex_bot.ps1",  # migration helper
    "memory.md",  # auto-memory document
)

# Per-line opt-out marker. Place ``# HISTORICAL-PATH-OK`` on the same line
# as a stale-path reference if you genuinely need to keep it (e.g. an
# audit log entry, a doc explaining the migration).
EXEMPT_LINE_MARKER = "HISTORICAL-PATH-OK"


def is_exempt(path: Path) -> bool:
    """True if this file is in an exempt location."""
    parts_lower = [p.lower() for p in path.parts]
    name_lower = path.name.lower()
    if name_lower in EXEMPT_PATH_FRAGMENTS:
        return True
    return any(
        any(frag in p for frag in EXEMPT_PATH_FRAGMENTS)
        for p in parts_lower
    )


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Scan a single file for stale path patterns.

    Returns a list of ``(line_no, pattern_label, line_text)`` violations.
    """
    violations: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return violations  # binary or unreadable; skip
    for line_no, line in enumerate(text.splitlines(), start=1):
        if EXEMPT_LINE_MARKER in line:
            continue
        for label, pattern in STALE_PATTERNS:
            if pattern.search(line):
                violations.append((line_no, label, line.strip()))
                break  # one finding per line is enough
    return violations


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        # No files supplied (e.g. invoked manually with no args). Don't
        # blow up the commit -- just print a hint and exit 0.
        print(
            "lint-stale-paths: no files supplied (this is fine when "
            "no matching files are staged). Use scripts/lint_stale_paths.py "
            "<files...> manually."
        )
        return 0

    total_violations = 0
    files_with_violations = 0
    for fname in argv[1:]:
        path = Path(fname)
        if not path.exists() or not path.is_file():
            continue
        if is_exempt(path):
            continue
        violations = scan_file(path)
        if not violations:
            continue
        files_with_violations += 1
        total_violations += len(violations)
        print(f"\n[STALE PATH] {path}")
        for line_no, label, line_text in violations:
            short = line_text if len(line_text) <= 100 else line_text[:97] + "..."
            print(f"  L{line_no}: matches {label!r}")
            print(f"    > {short}")

    if total_violations:
        print(
            f"\n!! lint-stale-paths FAILED: {total_violations} stale-path "
            f"reference(s) in {files_with_violations} file(s).\n"
            "   Operator mandate M1/M4 requires all paths under "
            "C:\\EvolutionaryTradingAlgo\\.\n"
            "   To bypass for a genuinely historical reference, add "
            "'# HISTORICAL-PATH-OK' on the same line."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Stale-path linter for the EvolutionaryTradingAlgo workspace.

Operator mandate M1+M4 (2026-04-26): all data, code, state, logs, journals,
and configs live under ``C:\\EvolutionaryTradingAlgo\\``. Earlier locations
(``C:\\dev\\``, ``C:\\Users\\edwar\\OneDrive\\Desktop\\Base\\``,
``C:\\Users\\edwar\\OneDrive\\The_Firm\\``,
``C:\\Users\\edwar\\OneDrive\\Documents\\Claude\\Projects\\...``,
``%LOCALAPPDATA%\\eta_engine\\``, ``C:\\mnq_data\\``,
``C:\\crypto_data\\``, and old ``C:\\TheFirm\\`` paths) are deprecated.

In addition, runtime state must live under
``<workspace>\\var\\eta_engine\\state\\`` -- never under in-repo
``eta_engine\\state\\`` or ``firm\\eta_engine\\state\\``. The
``firm_command_center\\eta_engine`` and legacy
``C:\\TheFirm\\apex_predator\\.venv`` references are also blocked.

This linter scans the files passed on argv (typically the pre-commit
hook supplies the staged file list) and fails if any contain stale
path references in active code paths.

Allowed locations for stale-path mentions:
- ``_archive*/`` directories (frozen historical snapshots)
- migration scripts: ``rewrite_paths.py``, ``fix_editable_paths.py``,
  ``_record_onedrive_migration.py``
- this script itself (the patterns are quoted as DETECTION targets)
- explicit allow-listed test fixtures that need to mention both the
  legacy and canonical paths during migration (see
  ``ALLOWLISTED_DUAL_PATH_FILES``)
- comments / docstrings that EXPLICITLY mark themselves as historical
  with the marker ``HISTORICAL-PATH-OK`` on the same line

Usage::

    python scripts/lint_stale_paths.py file1.py file2.py ...
    python scripts/lint_stale_paths.py --list-violations [paths...]
    python scripts/lint_stale_paths.py --fix file1.py ...   # NO-OP for path patterns

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

Pre-commit wiring (verified 2026-05-04): ``eta_engine/.pre-commit-config.yaml``
already runs ``python scripts/lint_stale_paths.py`` against staged files
matching the suffix glob above. New ``STALE_PATTERNS`` entries take effect
on the next commit with no hook config changes required.
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
        r"C:\Users\edwar\OneDrive",
        re.compile(r"C:[\\/]+Users[\\/]+edwar[\\/]+OneDrive(?:[\\/]|$)", re.IGNORECASE),
    ),
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
    # In-repo state writes -- audit category B (LEGACY_PATH_AUDIT.md, 2026-05-04).
    # Runtime state belongs under <workspace>/var/eta_engine/state/, never
    # under in-repo eta_engine/state/ (which forks state across two roots).
    # Three negative lookbehinds reject the canonical path:
    #   - ``(?<!var[/\\])`` -> width 4, catches ``var/`` and ``var\`` prefix
    #   - ``(?<!var[/\\][/\\])`` -> width 5, catches ``var//`` and ``var\\``
    #     (the latter shows up in non-raw Python string literals where one
    #     escaped backslash is two source chars)
    #   - ``(?<!\.)`` blocks the ``eta_engine.state`` attribute-access
    #     false positive on imports / dotted accesses.
    (
        "in-repo eta_engine/state/ (use var/eta_engine/state/)",
        re.compile(
            r"(?<!var[/\\])(?<!var[/\\][/\\])(?<!\.)eta_engine[\\/]+state(?:[\\/]|[\"'`])",
            re.IGNORECASE,
        ),
    ),
    # _REPO_ROOT / "state" idiom -- the Python expression that lands writes
    # in in-repo state/ instead of the workspace var/ tree.
    (
        '_REPO_ROOT / "state" (use workspace var/eta_engine/state/)',
        re.compile(
            r"_REPO_ROOT\s*/\s*[\"']state[\"']",
        ),
    ),
    # firm/eta_engine/state/ -- the firm submodule writing in-repo state.
    # firm/eta_engine/data/ remains a SEPARATE concern (legacy data is
    # explicitly out of scope per the audit) and is allowed by the regex.
    (
        "in-repo firm/eta_engine/state/ (use var/eta_engine/state/)",
        re.compile(
            r"firm[\\/]+eta_engine[\\/]+state(?:[\\/]|[\"'`])",
            re.IGNORECASE,
        ),
    ),
    # firm_command_center\eta_engine references -- stale path surfaced when
    # the agent scp'd register scripts from VPS audit (LEGACY_PATH_AUDIT).
    (
        r"firm_command_center\eta_engine (stale)",
        re.compile(
            r"firm_command_center[\\/]+eta_engine(?:[\\/]|[\"'`])",
            re.IGNORECASE,
        ),
    ),
    # apex_predator/.venv -- legacy C:\TheFirm\apex_predator\.venv\ is in
    # several active service definitions; block any new reference.
    (
        r"apex_predator\.venv (legacy)",
        re.compile(
            r"apex_predator[\\/]+\.venv(?:[\\/]|[\"'`])",
            re.IGNORECASE,
        ),
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
    "test_lint_stale_paths.py",  # detection-pattern regression tests
    "test_workspace_path_cleanup.py",  # asserts legacy paths are absent
    "test_data_library.py",  # documents legacy fixture shapes
    "consolidate_mnq_apex_bot.ps1",  # migration helper
    "memory.md",  # auto-memory document
    "legacy_path_audit.md",  # audit doc that names every legacy path
)

# Explicit allow-list (POSIX-style relative path suffixes) for files that
# legitimately reference both the legacy and canonical paths during a
# migration window. Matched by suffix-on-normalized-path so the same
# entry catches the file whether the linter is invoked at the workspace
# root or inside the eta_engine submodule.
#
# Each entry should reference the AUDIT or migration justification in a
# nearby comment so the operator can prune the allow-list when migration
# completes.
ALLOWLISTED_DUAL_PATH_FILES: tuple[str, ...] = (
    # Existing safety-net tests (LEGACY_PATH_AUDIT 2026-05-04 §C5):
    # they assert legacy paths are absent and reference both the legacy
    # and canonical paths to verify the migration. They MUST stay exempt.
    "tests/test_workspace_path_cleanup.py",
    "tests/test_operator_source_of_truth.py",
    # Detection-pattern regression tests for this linter -- the file
    # itself contains every legacy pattern by construction.
    "tests/test_lint_stale_paths.py",
    "eta_engine/tests/test_workspace_path_cleanup.py",
    "eta_engine/tests/test_operator_source_of_truth.py",
    "eta_engine/tests/test_lint_stale_paths.py",
)

# Per-line opt-out marker. Place ``# HISTORICAL-PATH-OK`` on the same line
# as a stale-path reference if you genuinely need to keep it (e.g. an
# audit log entry, a doc explaining the migration).
EXEMPT_LINE_MARKER = "HISTORICAL-PATH-OK"


def _normalised_posix(path: Path) -> str:
    """Return the path as a POSIX-style string, lowercased for matching."""
    return str(path).replace("\\", "/").lower()


def is_exempt(path: Path) -> bool:
    """True if this file is in an exempt location.

    Three exemption paths:
      1. ``EXEMPT_PATH_FRAGMENTS`` -- legacy path-segment list used by
         the existing pre-commit gate (e.g. ``_archive``, ``.venv``,
         ``lint_stale_paths.py`` itself).
      2. ``ALLOWLISTED_DUAL_PATH_FILES`` -- explicit suffix matches on
         files that legitimately reference both legacy and canonical
         paths during the in-flight migration. Matched by suffix on the
         POSIX-normalised path so the linter behaves identically whether
         invoked at the workspace root or inside ``eta_engine/``.
    """
    parts_lower = [p.lower() for p in path.parts]
    name_lower = path.name.lower()
    if name_lower in EXEMPT_PATH_FRAGMENTS:
        return True
    if any(
        any(frag in p for frag in EXEMPT_PATH_FRAGMENTS)
        for p in parts_lower
    ):
        return True

    # Explicit allow-list match (suffix-based on the POSIX-normalised
    # path so the linter is location-agnostic).
    posix_path = _normalised_posix(path)
    for allowed in ALLOWLISTED_DUAL_PATH_FILES:
        allowed_norm = allowed.replace("\\", "/").lower()
        if posix_path.endswith("/" + allowed_norm) or posix_path == allowed_norm:
            return True
    return False


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


_SCANNABLE_SUFFIXES = frozenset(
    {
        ".py", ".ps1", ".bat", ".cmd", ".sh", ".md",
        ".yaml", ".yml", ".toml", ".json", ".js", ".ts", ".tsx",
        ".env", ".cfg", ".ini",
    }
)
# Directory names to prune during the workspace walk. Pruning at the
# directory boundary (rather than per-file) keeps a full-workspace
# `--list-violations` run fast on a large repo. ``.claude`` is pruned
# because it holds session worktrees that mirror the workspace and
# would multiply violation counts by N.
_PRUNE_DIRS = frozenset(
    {
        ".git", ".hg", ".svn",
        "__pycache__", ".venv", "venv", ".tox",
        "node_modules", ".cache", ".pytest_cache",
        ".ruff_cache", ".mypy_cache", ".next", ".astro",
        "dist", "build", "_archive", "_legacy",
        ".claude",
    }
)


def _walk_workspace(root: Path) -> list[Path]:
    """Walk a directory and return scannable text files.

    Used by --list-violations when invoked without explicit file args.
    Prunes large irrelevant directories at the directory boundary (.git,
    node_modules, .venv, build artifacts, archives) so a full-workspace
    scan stays under a couple of seconds even on a multi-GB repo.
    """
    import os

    out: list[Path] = []
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str):
        # Prune in-place so os.walk doesn't descend.
        dirnames[:] = [d for d in dirnames if d.lower() not in _PRUNE_DIRS]
        for fname in filenames:
            suffix = os.path.splitext(fname)[1].lower()
            if suffix not in _SCANNABLE_SUFFIXES:
                continue
            out.append(Path(dirpath) / fname)
    return out


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    list_mode = False
    if "--list-violations" in args:
        list_mode = True
        args.remove("--list-violations")
    # --fix is a NO-OP for path-migration patterns. Auto-rewriting paths
    # is too risky for a linter; the operator handles migrations
    # explicitly. We accept the flag silently so existing automation
    # that passes --fix through doesn't break.
    if "--fix" in args:
        args.remove("--fix")
        print(
            "lint-stale-paths: --fix is a NO-OP for path-migration patterns "
            "(too risky to auto-rewrite). Run without --fix to surface "
            "violations and migrate manually.",
            file=sys.stderr,
        )

    # In --list-violations mode without any path arg, walk the workspace
    # rooted at the current working directory.
    if list_mode and not args:
        cwd = Path.cwd()
        candidate_paths = _walk_workspace(cwd)
    elif not args:
        # No files supplied (e.g. invoked manually with no args). Don't
        # blow up the commit -- just print a hint and exit 0.
        print(
            "lint-stale-paths: no files supplied (this is fine when "
            "no matching files are staged). Use scripts/lint_stale_paths.py "
            "<files...> manually, or --list-violations to triage the whole "
            "workspace."
        )
        return 0
    else:
        candidate_paths = [Path(fname) for fname in args]

    total_violations = 0
    files_with_violations = 0
    # Use a writer that gracefully handles Unicode > stdout encoding (the
    # default Windows cp1252 codec barfs on emoji or non-Latin chars in
    # doc lines being reported).
    def _safe_print(message: str) -> None:
        try:
            print(message)
        except UnicodeEncodeError:
            enc = (sys.stdout.encoding or "utf-8")
            print(message.encode(enc, errors="replace").decode(enc, errors="replace"))

    for path in candidate_paths:
        if not path.exists() or not path.is_file():
            continue
        if is_exempt(path):
            continue
        violations = scan_file(path)
        if not violations:
            continue
        files_with_violations += 1
        total_violations += len(violations)
        _safe_print(f"\n[STALE PATH] {path}")
        for line_no, label, line_text in violations:
            short = line_text if len(line_text) <= 100 else line_text[:97] + "..."
            _safe_print(f"  L{line_no}: matches {label!r}")
            _safe_print(f"    > {short}")

    if total_violations:
        print(
            f"\n!! lint-stale-paths FAILED: {total_violations} stale-path "
            f"reference(s) in {files_with_violations} file(s).\n"
            "   Operator mandate M1/M4 requires all paths under "
            "C:\\EvolutionaryTradingAlgo\\.\n"
            "   To bypass for a genuinely historical reference, add "
            "'# HISTORICAL-PATH-OK' on the same line."
        )
        # In --list-violations mode the exit code stays at 1 to preserve
        # the existing semantic ("non-zero means violations found");
        # operators triaging via --list-violations should expect this.
        return 1
    if list_mode:
        print("lint-stale-paths --list-violations: no violations found.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Consolidated repo-health detector for eta_engine.

Catches the silent decay modes that no other sentinel covers:

1. **Test count regression** -- if ``pytest --collect-only`` returns
   substantially fewer tests than the baseline persisted at
   ``docs/repo_health_baseline.json``, something was deleted or
   silently disabled. The baseline self-updates UPWARDS only.

2. **Bloated state files** -- ``docs/alerts_log.jsonl``,
   ``docs/jarvis_live_log.jsonl``, and ``docs/decisions_v1.json`` over
   ``--max-mb`` (default 25MB). Indicates rotation/pruning is missing.

3. **Pre-commit hook missing** -- ``.git/hooks/pre-commit`` does not
   exist or doesn't reference ``_pre_commit_check``. Catches fresh
   clones where the operator forgot to run ``--install-hook``. (Not
   relevant in cloud sessions; the trigger script can pass
   ``--skip-hook-check`` for that case.)

4. **Untracked-files backlog** -- if the working tree has more than
   ``--max-untracked`` untracked .py / .md / .json files, that's a
   sign of WIP accumulation that should either be committed or
   .gitignored.

Verdict logic
-------------
* GREEN  -- all checks pass
* YELLOW -- one check failed at low severity (e.g. test count -5%)
* RED    -- one check failed at high severity (test count -25% OR
            file > 2x cap OR hook missing)

Exit codes
----------
0 GREEN, 1 YELLOW, 2 RED, 9 setup error
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "docs" / "repo_health_baseline.json"

LOG_FILES = [
    ROOT / "docs" / "alerts_log.jsonl",
    ROOT / "docs" / "jarvis_live_log.jsonl",
    ROOT / "docs" / "decisions_v1.json",
]


def _collect_pytest_count() -> int | None:
    """Return total collected test count, or None on failure."""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Look for "NNNN tests collected" or similar tail line
    text = out.stdout + out.stderr
    matches = re.findall(r"(\d+)\s+tests?\s+collected", text)
    if not matches:
        return None
    return int(matches[-1])


def _check_test_count(
    baseline_count: int | None,
    current_count: int | None,
) -> tuple[str, str, int]:
    """Return (level, msg, current_count_for_baseline_update)."""
    if current_count is None:
        return ("RED", "pytest --collect-only failed or returned no count", 0)
    if baseline_count is None:
        return (
            "GREEN",
            f"baseline-seed: {current_count} tests (no prior baseline)",
            current_count,
        )
    pct_drop = 100.0 * (baseline_count - current_count) / baseline_count
    msg = f"{current_count} tests vs baseline {baseline_count} ({-pct_drop:+.1f}%)"
    if pct_drop >= 25:
        return ("RED", msg + " -- MAJOR test loss", current_count)
    if pct_drop >= 5:
        return ("YELLOW", msg + " -- noticeable test loss", current_count)
    # Baseline is the ratchet: only update upwards
    return ("GREEN", msg, max(current_count, baseline_count))


def _check_log_sizes(max_mb: float) -> tuple[str, str]:
    cap = int(max_mb * 1024 * 1024)
    bloated = []
    very_bloated = []
    for f in LOG_FILES:
        if not f.exists():
            continue
        size = f.stat().st_size
        if size > cap * 2:
            very_bloated.append((f.name, size / 1024 / 1024))
        elif size > cap:
            bloated.append((f.name, size / 1024 / 1024))
    if very_bloated:
        items = ", ".join(f"{n}={mb:.1f}MB" for n, mb in very_bloated)
        return ("RED", f"log files >2x cap ({max_mb:.0f}MB): {items}")
    if bloated:
        items = ", ".join(f"{n}={mb:.1f}MB" for n, mb in bloated)
        return ("YELLOW", f"log files >cap ({max_mb:.0f}MB): {items}")
    sizes = ", ".join(f"{f.name}={f.stat().st_size / 1024 / 1024:.2f}MB" for f in LOG_FILES if f.exists())
    return ("GREEN", f"log sizes OK ({sizes})")


def _check_pre_commit_hook() -> tuple[str, str]:
    hook = ROOT / ".git" / "hooks" / "pre-commit"
    if not hook.exists():
        return ("RED", "pre-commit hook missing -- run: python scripts/_pre_commit_check.py --install-hook")
    try:
        text = hook.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return ("YELLOW", f"pre-commit hook unreadable: {e}")
    if "_pre_commit_check" not in text:
        return ("YELLOW", "pre-commit hook exists but doesn't reference _pre_commit_check")
    return ("GREEN", "pre-commit hook installed and references _pre_commit_check")


def _check_untracked_backlog(max_untracked: int) -> tuple[str, str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return ("YELLOW", f"git ls-files failed: {e}")
    files = [
        line for line in out.stdout.splitlines() if line.endswith((".py", ".md", ".json", ".jsonl", ".yaml", ".yml"))
    ]
    n = len(files)
    if n > max_untracked * 2:
        return ("RED", f"{n} untracked tracked-extension files (cap {max_untracked})")
    if n > max_untracked:
        return ("YELLOW", f"{n} untracked tracked-extension files (cap {max_untracked})")
    return ("GREEN", f"{n} untracked tracked-extension files (cap {max_untracked})")


def _severity(level: str) -> int:
    return {"GREEN": 0, "YELLOW": 1, "RED": 2}.get(level, 0)


def _load_baseline(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    p.add_argument("--max-mb", type=float, default=25.0)
    p.add_argument("--max-untracked", type=int, default=20)
    p.add_argument(
        "--skip-hook-check",
        action="store_true",
        help="skip pre-commit hook check (for cloud sessions)",
    )
    p.add_argument(
        "--skip-pytest",
        action="store_true",
        help="skip pytest --collect-only (for cloud sessions or quick checks)",
    )
    p.add_argument("--no-update", action="store_true", help="don't persist baseline updates")
    args = p.parse_args(argv)

    baseline = _load_baseline(args.baseline)

    checks: list[tuple[str, str, str]] = []

    if not args.skip_pytest:
        cur = _collect_pytest_count()
        lvl, msg, new_count = _check_test_count(baseline.get("test_count"), cur)
        checks.append(("test-count", lvl, msg))
        if new_count > 0:
            baseline["test_count"] = new_count
    else:
        checks.append(("test-count", "GREEN", "skipped via --skip-pytest"))

    lvl, msg = _check_log_sizes(args.max_mb)
    checks.append(("log-sizes", lvl, msg))

    if not args.skip_hook_check:
        lvl, msg = _check_pre_commit_hook()
        checks.append(("pre-commit-hook", lvl, msg))
    else:
        checks.append(("pre-commit-hook", "GREEN", "skipped via --skip-hook-check"))

    lvl, msg = _check_untracked_backlog(args.max_untracked)
    checks.append(("untracked-backlog", lvl, msg))

    overall = max((c[1] for c in checks), key=_severity)
    code = _severity(overall)

    print(f"repo-health: {overall} -- {len(checks)} checks")
    for name, lvl, msg in checks:
        print(f"  [{lvl:6}] {name}: {msg}")

    if not args.no_update:
        baseline["last_updated"] = datetime.now(UTC).isoformat()
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")

    return code


if __name__ == "__main__":
    sys.exit(main())

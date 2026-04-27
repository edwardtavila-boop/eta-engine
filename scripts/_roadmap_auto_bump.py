"""Scaffold the next roadmap bump script with detected data pre-filled.

Mechanical work:
* find the next version (look at ``scripts/_bump_roadmap_v0_1_NN.py``,
  pick max + 1)
* count current tests via ``pytest --collect-only``
* read previous tests_passing from ``roadmap_state.json``
* gather git data since the last bump's commit:
   - touched files (split by directory: bots/, strategies/, tests/, ...)
   - commit subject lines
* render a new ``_bump_roadmap_v0_1_<NN>.py`` with the standard
  shape, narrative slots pre-marked ``TODO:``

Usage
-----
    python scripts/_roadmap_auto_bump.py            # dry-run, prints to stdout
    python scripts/_roadmap_auto_bump.py --write    # creates the file
    python scripts/_roadmap_auto_bump.py --version v0.1.99   # override

This DOES NOT auto-commit, auto-bump roadmap_state.json, or auto-run
the new script. The operator reviews the scaffold, fills in the
narrative ``TODO:`` slots, then runs the bump file by hand.

Why this exists
---------------
The mechanical fields (version, tests_passing_before, tests_passing_
after, files touched) are derivable; the operator's narrative
("THEME -- what shipped, why it matters") is not. This script does
the boring half so the operator can focus on the half that matters.
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
SCRIPTS_DIR = ROOT / "scripts"
STATE_PATH = ROOT / "roadmap_state.json"

BUMP_NAME_RE = re.compile(r"^_bump_roadmap_v(\d+)_(\d+)_(\d+)\.py$")


def _find_next_version() -> tuple[str, str, Path | None]:
    """Return (next_version, prev_version, prev_path)."""
    versions: list[tuple[tuple[int, int, int], Path]] = []
    for f in SCRIPTS_DIR.glob("_bump_roadmap_v*.py"):
        m = BUMP_NAME_RE.match(f.name)
        if not m:
            continue
        versions.append((tuple(int(x) for x in m.groups()), f))
    if not versions:
        return ("v0.1.0", "(none)", None)
    versions.sort()
    (a, b, c), prev_path = versions[-1]
    next_v = f"v{a}.{b}.{c + 1}"
    prev_v = f"v{a}.{b}.{c}"
    return (next_v, prev_v, prev_path)


def _run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        **kw,  # type: ignore[arg-type]
    )


def _collect_pytest_count() -> int | None:
    out = _run([sys.executable, "-m", "pytest", "--collect-only", "-q"], timeout=180)
    text = out.stdout + out.stderr
    matches = re.findall(r"(\d+)\s+tests?\s+collected", text)
    return int(matches[-1]) if matches else None


def _read_prev_tests() -> int:
    if not STATE_PATH.exists():
        return 0
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return 0
    sa = state.get("shared_artifacts", {})
    return int(sa.get("eta_engine_tests_passing", 0) or 0)


def _git_data_since(prev_path: Path | None) -> dict:
    """Find files & commits since the previous bump file was committed."""
    out = {
        "since_ref": None,
        "files": [],
        "subjects": [],
        "by_dir": {},
    }
    if prev_path is None:
        return out
    rel_prev = str(prev_path.relative_to(ROOT)).replace("\\", "/")
    log = _run(["git", "log", "-1", "--format=%H", "--", rel_prev])
    if log.returncode != 0 or not log.stdout.strip():
        return out
    sha = log.stdout.strip()
    out["since_ref"] = sha
    diff = _run(["git", "diff", "--name-only", f"{sha}..HEAD"])
    files = [f for f in diff.stdout.splitlines() if f.strip()]
    out["files"] = files
    subjects = _run(["git", "log", "--format=%s", f"{sha}..HEAD"])
    out["subjects"] = [s for s in subjects.stdout.splitlines() if s.strip()]

    by_dir: dict[str, list[str]] = {}
    for f in files:
        top = f.split("/", 1)[0] if "/" in f else "(root)"
        by_dir.setdefault(top, []).append(f)
    out["by_dir"] = by_dir
    return out


def _render_bump(version: str, prev_version: str, tests_now: int, tests_prev: int, git: dict) -> str:
    delta = tests_now - tests_prev
    underscore_v = version.replace(".", "_").lstrip("v")
    files_block_lines = []
    for dir_, fs in sorted(git["by_dir"].items()):
        files_block_lines.append(f"        {dir_!r}: [")
        for f in sorted(fs):
            files_block_lines.append(f"            {f!r},")
        files_block_lines.append("        ],")
    files_block = "\n".join(files_block_lines) if files_block_lines else "        # (no files detected)"

    subjects_block = "\n".join(f"#   * {s}" for s in git["subjects"]) or "#   * (no commits detected)"

    return f'''"""One-shot: bump roadmap_state.json to {version}.

TODO: 2-line theme name -- WHAT SHIPPED -- one-line summary

Context
-------
TODO: what was the previous bump ({prev_version}) state, what's
missing, why does this bump exist now?

What ships
----------
TODO: bullet list of files modified + what changed in each.
Auto-detected files since {prev_version}:
{subjects_block}

Delta
-----
  * tests_passing: {tests_prev} -> {tests_now} ({delta:+d})
  * Ruff-clean on every new + modified file
  * TODO: phase-level status delta if any

Why this matters
----------------
TODO: why does this matter for the founder directive / the live
trading loop / the next phase?
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "{version}"
NEW_TESTS_ABS = {tests_now}


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_{underscore_v}_TODO_short_slug"] = {{
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": "TODO: one-line description of the bundle",
        "theme": "TODO: 3-5 line theme statement",
        "operator_directive_quote": "TODO: relevant operator quote if any",
        "artifacts_added": {{
            # TODO: split detected files into added vs modified
        }},
        "artifacts_modified": {{
{files_block}
        }},
        "ruff_clean_on": [
            # TODO: list every file ruff was run against
        ],
        "phase_reconciliation": {{
            "overall_progress_pct": state.get("overall_progress_pct", 99),
            "status": "TODO: status note",
        }},
        "python_touched": True,
        "jsx_touched": False,
        "tests_passing_before": prev_tests,
        "tests_passing_after": NEW_TESTS_ABS,
        "tests_new": NEW_TESTS_ABS - prev_tests,
    }}

    milestones = state.setdefault("milestones", [])
    if isinstance(milestones, list):
        milestones.append(
            {{
                "version": VERSION,
                "timestamp_utc": now,
                "title": "TODO: 1-2 sentence milestone title",
                "tests_delta": NEW_TESTS_ABS - prev_tests,
                "tests_passing": NEW_TESTS_ABS,
            }},
        )

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to {{VERSION}} at {{now}}")
    print(
        f"  tests_passing: {{prev_tests}} -> {{NEW_TESTS_ABS}} "
        f"({{NEW_TESTS_ABS - prev_tests:+d}})",
    )


if __name__ == "__main__":
    main()
'''


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--version",
        help="override the auto-detected version (e.g. v0.1.99)",
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="actually create the file (default: print to stdout)",
    )
    p.add_argument(
        "--no-pytest",
        action="store_true",
        help="skip pytest --collect-only (use --tests-now to set explicitly)",
    )
    p.add_argument(
        "--tests-now",
        type=int,
        default=None,
        help="override the test count (default: auto via pytest --collect-only)",
    )
    args = p.parse_args(argv)

    next_v, prev_v, prev_path = _find_next_version()
    version = args.version or next_v
    print(f"[auto-bump] next version: {version} (previous: {prev_v})", file=sys.stderr)

    if args.tests_now is not None:
        tests_now = args.tests_now
    elif args.no_pytest:
        tests_now = _read_prev_tests()
        print(f"[auto-bump] --no-pytest set; reusing prev count {tests_now}", file=sys.stderr)
    else:
        print("[auto-bump] running pytest --collect-only ...", file=sys.stderr)
        cnt = _collect_pytest_count()
        if cnt is None:
            print("[auto-bump] pytest --collect-only failed; pass --tests-now N", file=sys.stderr)
            return 9
        tests_now = cnt

    tests_prev = _read_prev_tests()
    print(
        f"[auto-bump] tests: {tests_prev} -> {tests_now} ({tests_now - tests_prev:+d})",
        file=sys.stderr,
    )

    git = _git_data_since(prev_path)
    print(
        f"[auto-bump] {len(git['files'])} files touched, {len(git['subjects'])} commits since {prev_v}",
        file=sys.stderr,
    )

    body = _render_bump(version, prev_v, tests_now, tests_prev, git)

    underscore_v = version.replace(".", "_").lstrip("v")
    out_path = SCRIPTS_DIR / f"_bump_roadmap_v{underscore_v}.py"
    if args.write:
        if out_path.exists():
            print(f"[auto-bump] REFUSE: {out_path} exists -- pass a different --version", file=sys.stderr)
            return 1
        out_path.write_text(body, encoding="utf-8")
        print(f"[auto-bump] wrote {out_path}", file=sys.stderr)
        print(
            f"[auto-bump] generated at {datetime.now(UTC).isoformat()}",
            file=sys.stderr,
        )
        print(
            "[auto-bump] next: edit the TODO slots, run the file, commit",
            file=sys.stderr,
        )
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Generate a markdown change summary from git log.

Pairs with ``_roadmap_auto_bump.py``. The auto-bump scaffolds the
mechanical fields (version, file lists, test deltas) but leaves the
narrative slots blank with ``TODO:`` markers. This script fills in
the narrative half by mining the git log between two refs.

Usage
-----
    python scripts/_change_summary.py                       # since last bump
    python scripts/_change_summary.py --since HEAD~10       # since 10 commits ago
    python scripts/_change_summary.py --since v0.1.49 --to v0.1.50
    python scripts/_change_summary.py --markdown            # render full bump-style markdown

What it produces
----------------
* a candidate one-line bundle name (theme): inferred from commit
  subject prefixes (feat / fix / chore / docs / test / refactor)
  weighted by line-change volume
* a top-3 file-impact list (most-changed paths)
* a per-directory diff stat (lines added / removed by top dir)
* a candidate "What ships" bullet list (commit subjects, deduped)
* a "Why this matters" template (left for the operator)

Default --since target: git log of ``scripts/_bump_roadmap_v*.py``,
pick the most recent file's commit SHA.

Output is plain markdown to stdout. Pipe to clipboard or paste
straight into the auto-bump scaffold's TODO slots.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"

BUMP_NAME_RE = re.compile(r"^_bump_roadmap_v(\d+)_(\d+)_(\d+)\.py$")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True, check=False,
    )


def _last_bump_sha() -> str | None:
    """Return the commit SHA of the most recent _bump_roadmap_*.py file."""
    versions: list[tuple[tuple[int, int, int], Path]] = []
    for f in SCRIPTS_DIR.glob("_bump_roadmap_v*.py"):
        m = BUMP_NAME_RE.match(f.name)
        if not m:
            continue
        versions.append((tuple(int(x) for x in m.groups()), f))
    if not versions:
        return None
    versions.sort()
    rel = str(versions[-1][1].relative_to(ROOT)).replace("\\", "/")
    out = _run(["git", "log", "-1", "--format=%H", "--", rel])
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return out.stdout.strip()


def _commit_subjects(since: str, to: str) -> list[str]:
    out = _run(["git", "log", f"{since}..{to}", "--format=%s"])
    return [s.strip() for s in out.stdout.splitlines() if s.strip()]


def _commit_count(since: str, to: str) -> int:
    out = _run(["git", "log", f"{since}..{to}", "--format=oneline"])
    return len([line for line in out.stdout.splitlines() if line.strip()])


def _diff_numstat(since: str, to: str) -> list[tuple[int, int, str]]:
    """Return [(insertions, deletions, path), ...] from git diff --numstat."""
    out = _run(["git", "diff", "--numstat", f"{since}..{to}"])
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        ins_str, del_str, path = parts
        try:
            ins = int(ins_str) if ins_str != "-" else 0
            dels = int(del_str) if del_str != "-" else 0
        except ValueError:
            continue
        rows.append((ins, dels, path))
    return rows


def _classify_subject(s: str) -> str:
    m = re.match(r"^(feat|fix|chore|docs|test|refactor|perf|style|ci)", s, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return "other"


def _infer_theme(subjects: list[str], numstat: list[tuple[int, int, str]]) -> str:
    """Pick a theme based on subject prefixes weighted by line-change volume."""
    if not subjects:
        return "(no commits since last bump)"
    # Score each subject by total churn it represents
    type_counter: Counter[str] = Counter()
    for s in subjects:
        type_counter[_classify_subject(s)] += 1
    dominant = type_counter.most_common(1)[0][0]
    # Find the largest individual commit subject (proxy for biggest change)
    largest = subjects[0] if subjects else "(no subject)"
    return f"{dominant.upper()} -- {largest}"


def _by_dir(numstat: list[tuple[int, int, str]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for ins, dels, path in numstat:
        top = path.split("/", 1)[0] if "/" in path else "(root)"
        d = out.setdefault(top, {"ins": 0, "dels": 0, "files": 0})
        d["ins"] += ins
        d["dels"] += dels
        d["files"] += 1
    return out


def _render_markdown(
    since: str, to: str, subjects: list[str], numstat: list[tuple[int, int, str]],
) -> str:
    if not subjects:
        return f"# Change summary {since}..{to}\n\n(no commits in range)\n"
    theme = _infer_theme(subjects, numstat)
    by_dir = _by_dir(numstat)
    top_files = sorted(numstat, key=lambda x: -(x[0] + x[1]))[:5]

    lines = [
        f"# Change summary {since}..{to}",
        "",
        f"**Inferred theme:** {theme}",
        "",
        f"**Commits:** {len(subjects)}",
        f"**Files touched:** {len(numstat)}",
        f"**Total churn:** +{sum(i for i, *_ in numstat)} / -{sum(d for _, d, _ in numstat)}",
        "",
        "## By directory",
        "",
    ]
    for d in sorted(by_dir, key=lambda k: -by_dir[k]["ins"] - by_dir[k]["dels"]):
        st = by_dir[d]
        lines.append(
            f"- **{d}/** ({st['files']} files, +{st['ins']} / -{st['dels']})",
        )
    lines.extend(["", "## Top 5 most-changed files", ""])
    for ins, dels, path in top_files:
        lines.append(f"- `{path}` (+{ins} / -{dels})")
    lines.extend(["", "## Commit log", ""])
    for s in subjects:
        lines.append(f"- {s}")
    lines.extend([
        "",
        "## Suggested narrative slots",
        "",
        "**Bundle name (TODO):** `<short_slug>` -- one-line summary of what shipped",
        "",
        "**Theme (3-5 lines, TODO):** what was the prior state, what was the gap,",
        "what does this bundle add, why now?",
        "",
        "**Why it matters (TODO):** how does this advance the founder directive,",
        "the live trading loop, or the next phase?",
        "",
    ])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--since", default=None,
        help="git ref to compare from (default: SHA of last _bump_roadmap_*.py)",
    )
    p.add_argument("--to", default="HEAD", help="git ref to compare to (default HEAD)")
    p.add_argument(
        "--markdown", action="store_true",
        help="render markdown (default: plain text key=value)",
    )
    args = p.parse_args(argv)

    since = args.since or _last_bump_sha()
    if since is None:
        print("change-summary: cannot find last bump SHA -- pass --since explicitly", file=sys.stderr)
        return 9
    to = args.to

    subjects = _commit_subjects(since, to)
    numstat = _diff_numstat(since, to)

    if args.markdown:
        print(_render_markdown(since, to, subjects, numstat))
        return 0

    # Plain text mode
    print(f"change-summary: {since[:8]}..{to}")
    print(f"  commits: {len(subjects)}")
    print(f"  files:   {len(numstat)}")
    print(f"  churn:   +{sum(i for i, *_ in numstat)} / -{sum(d for _, d, _ in numstat)}")
    print(f"  theme:   {_infer_theme(subjects, numstat)}")
    print()
    print("  by directory:")
    by_dir = _by_dir(numstat)
    for d in sorted(by_dir, key=lambda k: -by_dir[k]["ins"] - by_dir[k]["dels"])[:10]:
        st = by_dir[d]
        print(f"    {d:>12}/  files={st['files']:>3}  +{st['ins']:>5} / -{st['dels']:>5}")
    print()
    print("  recent commits:")
    for s in subjects[:10]:
        print(f"    * {s}")
    if len(subjects) > 10:
        print(f"    ... and {len(subjects) - 10} more (--markdown for full list)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

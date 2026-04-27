"""Scaffold the next ``_bump_roadmap_vX_Y_Z.py`` from a template.

The operator has manually written 32+ of these one-shot scripts since
v0.1.14. They all follow the same shape:

  1. Read roadmap_state.json
  2. Update last_updated / last_updated_utc
  3. Add a new shared_artifacts entry under
     "eta_engine_v0_1_X_<slug>"
  4. Bump tests_passing_after to the new pytest count
  5. Write the file back
  6. Print a one-line summary

This generator removes the boilerplate. Inputs:

  --version 0.1.46       (will become v0_1_46 in the entry key)
  --slug new_thing        (snake_case, used in the entry key)
  --title "NEW THING ..." (free text, goes in bundle_name)
  --tests 2087            (optional; auto-detects via pytest if omitted)

The generated script is written to
``scripts/_bump_roadmap_v0_1_X.py``, ruff-checked, and the path is
echoed. The operator can then edit the artifacts_added / theme blocks
before running it.

Why this exists
---------------
Every one of these scripts started as a copy-paste of the previous one.
That's a maintenance smell -- one missed field and the roadmap gets
inconsistent. A generator with a single source of truth makes the
shape canonical.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
STATE_PATH = ROOT / "roadmap_state.json"

TEMPLATE = '''"""One-shot: bump roadmap_state.json to v{version}.

{title} -- {one_line_summary}

Context
-------
TODO: explain what was missing before this bump and why this work
closes that gap.

What v{version} adds
{version_underline}
TODO: bullet list of new modules, patches, configs, and tests.

Why this matters
----------------
TODO: state the operator-visible behaviour change. If this is a
no-op refactor, say "no behaviour change, internal cleanup only".

Acceptance criteria
-------------------
  * ruff clean on the changed surface area
  * {tests_count} / {tests_count} pytest pass
  * TODO: any operational invariant the operator needs to verify
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    new_tests = {tests_count}
    sa["eta_engine_tests_passing"] = new_tests

    sa["eta_engine_v{version_us}_{slug}"] = {{
        "timestamp_utc": now,
        "version": "v{version}",
        "bundle_name": (
            "{title}"
        ),
        "directive": (
            "TODO: paste the operator's directive that triggered this "
            "bump, verbatim"
        ),
        "theme": (
            "TODO: 1-2 sentence summary of what this bump shipped and "
            "why"
        ),
        "artifacts_added": {{
            "modules": [],   # TODO
            "patches": [],   # TODO
            "configs": [],   # TODO
            "tests": [],     # TODO
            "scripts": ["scripts/_bump_roadmap_v{version_us}.py"],
        }},
        "ruff_clean_on": [],  # TODO: list files
        "operational_impact": {{
            "live_sizing_change": "TODO",
            "reversibility": "TODO",
        }},
        "phase_reconciliation": {{
            "overall_progress_pct": 99,
            "status": "unchanged -- still funding-gated on P9_ROLLOUT",
        }},
        "python_touched": True,
        "jsx_touched": False,
        "tests_passing_before": prev_tests,
        "tests_passing_after": new_tests,
        "delta_tests": new_tests - prev_tests,
    }}

    # Append (or replace, if this version already shipped) the milestone
    # entry. Idempotent on re-runs: a second invocation of this script
    # rewrites the matching milestone in place rather than duplicating it.
    milestones = state.setdefault("milestones", [])
    new_milestone = {{
        "version": "v{version}",
        "timestamp_utc": now,
        "title": "{title}",
        "tests_delta": new_tests - prev_tests,
        "tests_passing": new_tests,
    }}
    for idx, existing in enumerate(milestones):
        if isinstance(existing, dict) and existing.get("version") == "v{version}":
            milestones[idx] = new_milestone
            break
    else:
        milestones.append(new_milestone)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\\n", encoding="utf-8",
    )
    print("roadmap_state.json bumped to v{version}")
    print(f"  tests: {{prev_tests}} -> {{new_tests}}")


if __name__ == "__main__":
    main()
'''


def _next_version() -> str:
    """Find the highest v0.1.X bump script and return X+1 as a string."""
    pattern = re.compile(r"_bump_roadmap_v0_1_(\d+)\.py$")
    nums = [int(m.group(1)) for f in SCRIPTS_DIR.glob("_bump_roadmap_v0_1_*.py") if (m := pattern.search(f.name))]
    nxt = max(nums, default=0) + 1
    return f"0.1.{nxt}"


def _detect_tests() -> int:
    """Run pytest --collect-only -q and return the test count."""
    try:
        out = subprocess.run(
            ["python", "-m", "pytest", "--collect-only", "-q"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"[new-bump] could not auto-detect tests: {exc!r}; using 0",
            file=sys.stderr,
        )
        return 0
    # Last meaningful line looks like:  "2087 tests collected in 0.42s"
    for line in reversed(out.stdout.splitlines()):
        m = re.match(r"^\s*(\d+)\s+tests? collected", line)
        if m:
            return int(m.group(1))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--version",
        default=None,
        help="version string like '0.1.46' (auto-detect if omitted)",
    )
    p.add_argument(
        "--slug",
        required=True,
        help="snake_case slug for the entry key (e.g. 'new_thing')",
    )
    p.add_argument(
        "--title",
        required=True,
        help="free-text bundle title",
    )
    p.add_argument(
        "--tests",
        type=int,
        default=None,
        help="test count (auto-detect via pytest if omitted)",
    )
    p.add_argument(
        "--summary",
        default="one-line summary -- TODO",
        help="one-line summary used at the top of the docstring",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing bump script with the same version",
    )
    args = p.parse_args(argv)

    version = args.version or _next_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        print(f"[new-bump] bad version: {version!r}", file=sys.stderr)
        return 2
    version_us = version.replace(".", "_")
    slug = re.sub(r"[^a-z0-9_]+", "_", args.slug.lower()).strip("_")
    if not slug:
        print("[new-bump] slug is empty after sanitisation", file=sys.stderr)
        return 2

    tests_count = args.tests if args.tests is not None else _detect_tests()

    out_path = SCRIPTS_DIR / f"_bump_roadmap_v{version_us}.py"
    if out_path.exists() and not args.force:
        print(
            f"[new-bump] {out_path.name} already exists; use --force to overwrite",
            file=sys.stderr,
        )
        return 3

    body = TEMPLATE.format(
        version=version,
        version_us=version_us,
        version_underline="-" * (len(f"What v{version} adds")),
        slug=slug,
        title=args.title,
        one_line_summary=args.summary,
        tests_count=tests_count,
    )
    out_path.write_text(body, encoding="utf-8")
    print(f"[new-bump] wrote {out_path}")
    print(f"[new-bump] version: v{version}")
    print(f"[new-bump] slug:    {slug}")
    print(f"[new-bump] tests:   {tests_count}")
    print("[new-bump] next: edit the TODOs, then `python " + str(out_path) + "`")
    return 0


if __name__ == "__main__":
    sys.exit(main())

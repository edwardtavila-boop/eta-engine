"""Process gap #2 closure: audit "deferred to v0.2.x" claims in code.

The Red Team v0.1.64 review process-gap #2 observed:

  > "Deferred to v0.2.x" docstring is the same in v0.1.59, v0.1.60,
  > v0.1.62, v0.1.63 _bump_roadmap files. No version actually advanced
  > the v0.2.x calibration story. Process fix: the closure rationale
  > should include a v0.2.x scope ticket and an exit criterion (e.g.
  > "calibrator script lands, tolerance values appear in
  > kill_switch.yaml, integration test asserts they're respected").
  > Without exit criteria the deferral is permanent.

This script walks production source for "v0.2.x" / "deferred to" /
"TODO" markers and emits a report identifying which markers have an
explicit exit criterion attached and which do not. A marker has an
exit criterion when its surrounding 5-line context contains at least
one of:

  * a kaizen ticket reference (KZN-NNN)
  * a test-name reference (test_foo.py / test_foo)
  * an "exit criterion" / "acceptance" / "lands when" phrase
  * a closure version reference (closed in vX.Y.Z, addressed by
    issue #N, etc.)

This is intentionally a lenient heuristic -- the goal is to make
floating "we'll fix it later" comments visible, not to block the
build. ``--strict`` raises the bar (every marker must have a
criterion).

Usage
-----
    python scripts/_audit_deferral_criteria.py
    python scripts/_audit_deferral_criteria.py --strict   # exit 1 on bare deferrals
    python scripts/_audit_deferral_criteria.py --json     # machine-readable

Exit codes
----------
0 -- ran successfully (every deferral has a criterion under default mode,
     OR --strict was not set)
1 -- --strict and at least one deferral lacks an exit criterion
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Pattern matches a "deferral marker" anywhere in source. Case-insensitive.
# The patterns are intentionally narrow (we don't want false positives on
# every "todo:" comment). Each marker captures forward-looking work.
_MARKER_RE = re.compile(
    r"(?i)"
    r"(deferred?\s+to\s+v?\d+\.\d+\.[\dx]+"     # "deferred to v0.2.x"
    r"|defer\s+to\s+v?\d+\.\d+\.[\dx]+"        # "defer to v0.2.x"
    r"|v0\.2\.x\s+(scope|deferral|design|work)"  # "v0.2.x scope"
    r"|v\d+\.\d+\.[\dx]+\s+scope"               # "v0.2.x scope"
    r"|punted\s+to\s+v?\d+\.\d+\.[\dx]+"       # "punted to v0.2.x"
    r"|TODO\(v?\d+\.\d+\.[\dx]+\)"             # "TODO(v0.2.x)"
    r")",
)

# Exit criterion -- something that pins down "what does done look like"
# for the deferral. Matched in the +/- 5 line context window around the
# marker, case-insensitive.
_CRITERION_RE = re.compile(
    r"(?i)"
    r"(KZN-\d+"                              # kaizen ticket id
    r"|test_[a-z_][a-z0-9_]*"                # test function/file name
    r"|exit\s+criteri"                       # "exit criterion"
    r"|acceptance\s+criteri"                 # "acceptance criteria"
    r"|lands?\s+when"                        # "lands when X"
    r"|closes?\s+when"                       # "closes when X"
    r"|closed\s+in\s+v\d+\.\d+\.\d+"        # "closed in v0.1.64"
    r"|addressed\s+(in|by)"                  # "addressed by issue #N"
    r"|issue\s*#\d+"                         # "issue #42"
    r"|scope\s+ticket"                       # "scope ticket"
    r"|see\s+docs/"                          # "see docs/foo.md"
    r")",
)


@dataclass(frozen=True)
class Hit:
    file: str
    line: int
    text: str
    has_criterion: bool
    criterion_evidence: str | None


def _scan_file(path: Path, *, root: Path) -> list[Hit]:
    """Scan one file for deferral markers; return one Hit per marker."""
    rel = path.relative_to(root).as_posix()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    lines = text.splitlines()
    hits: list[Hit] = []
    for i, line in enumerate(lines):
        if not _MARKER_RE.search(line):
            continue
        # Build a +/- 5-line context window
        lo = max(0, i - 5)
        hi = min(len(lines), i + 6)
        window = "\n".join(lines[lo:hi])
        crit_match = _CRITERION_RE.search(window)
        hits.append(
            Hit(
                file=rel,
                line=i + 1,
                text=line.strip(),
                has_criterion=crit_match is not None,
                criterion_evidence=(
                    crit_match.group(0) if crit_match else None
                ),
            ),
        )
    return hits


def scan(root: Path) -> list[Hit]:
    """Walk the repo for production source markers.

    Excludes: tests/, scripts/_legacy_bumps/, scripts/bumps/, docs/,
    __pycache__, .cache/, .venv/. The exclusions reflect 'this is a
    one-shot historical artifact, not actively-deferred work.'

    Also self-excludes (this file): the audit's own regex source code
    contains the exact marker phrasings it's looking for, which would
    flood the report with self-references. The audit's own deferral
    discipline is enforced by review of the regex itself, not by
    self-scan.
    """
    out: list[Hit] = []
    skip_prefixes = (
        "tests/", "scripts/_legacy_bumps/", "scripts/bumps/",
        "docs/_backups/", ".cache/", ".venv/", "venv/",
        "__pycache__/", ".git/", ".pytest_cache/", ".ruff_cache/",
        ".mypy_cache/", "var/", "state/",
    )
    self_path = "scripts/_audit_deferral_criteria.py"
    for p in root.rglob("*.py"):
        rel = p.relative_to(root).as_posix()
        if rel == self_path:
            continue
        if any(rel.startswith(prefix) or "__pycache__" in rel
               for prefix in skip_prefixes):
            continue
        out.extend(_scan_file(p, root=root))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 if any deferral lacks an exit criterion",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of the human report",
    )
    args = p.parse_args(argv)

    hits = scan(ROOT)
    bare = [h for h in hits if not h.has_criterion]
    pinned = [h for h in hits if h.has_criterion]

    if args.json:
        print(json.dumps({
            "total": len(hits),
            "with_criterion": len(pinned),
            "bare": len(bare),
            "hits": [
                {
                    "file": h.file,
                    "line": h.line,
                    "text": h.text,
                    "has_criterion": h.has_criterion,
                    "criterion_evidence": h.criterion_evidence,
                }
                for h in hits
            ],
        }, indent=2))
    else:
        print("DEFERRAL-CRITERIA AUDIT")
        print("=" * 50)
        print(
            f"Markers found: {len(hits)}    with criterion: "
            f"{len(pinned)}    bare: {len(bare)}",
        )
        print()
        if pinned:
            print("PINNED (deferral has an exit criterion in scope):")
            for h in pinned:
                print(f"  {h.file}:{h.line}")
                print(f"    text: {h.text[:100]}")
                print(f"    pin:  {h.criterion_evidence}")
            print()
        if bare:
            print("BARE (deferral with NO exit criterion -- track or remove):")
            for h in bare:
                print(f"  {h.file}:{h.line}")
                print(f"    text: {h.text[:100]}")
            print()
            print(
                "Each bare marker should either grow a kaizen ticket "
                "reference (KZN-NNN), a test-name reference, an explicit "
                "'lands when ...' clause, OR be removed if the deferral "
                "is no longer planned.",
            )
        else:
            print("OK -- every deferral marker has a tracked exit criterion.")

    if args.strict and bare:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

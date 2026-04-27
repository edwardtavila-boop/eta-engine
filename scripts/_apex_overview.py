"""One-screen operator dashboard: where is the firm right now?

Aggregates every signal worth knowing into one render:
  - git: branch, latest commit, ahead/behind, dirty file count
  - tests: total / passing / failing / skipped
  - bots: count + L0 import status
  - strategies: count + capability counts
  - sentinels: GREEN/YELLOW/RED tally
  - roadmap: phase, version, freshness

Output is markdown by default (paste into a session note or doc),
with an ANSI-color compact mode for terminal use.

Usage
-----
    python scripts/_eta_overview.py             # markdown
    python scripts/_eta_overview.py --compact   # one-screen terminal view
    python scripts/_eta_overview.py --json      # JSON for downstream tools

Why
---
Replaces the operator's "let me check git, then pytest, then sentinels,
then look at roadmap_state, then count strategies..." morning ritual.
One command, one screen, full context.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return (proc.returncode, proc.stdout + proc.stderr)
    except (subprocess.TimeoutExpired, OSError):
        return (124, "")


def _git_snapshot() -> dict:
    out: dict = {}
    out["branch"] = _run(["git", "branch", "--show-current"])[1].strip()
    rc, log = _run(["git", "log", "-1", "--format=%h %s (%cr)"])
    out["latest_commit"] = log.strip() if rc == 0 else "(none)"
    rc, count = _run(["git", "rev-list", "--count", "HEAD"])
    out["total_commits"] = count.strip() if rc == 0 else "?"
    rc, status = _run(["git", "status", "--porcelain"])
    if rc == 0:
        out["dirty_files"] = len([ln for ln in status.splitlines() if ln.strip()])
    else:
        out["dirty_files"] = -1
    rc, ahead = _run(["git", "rev-list", "--count", "@{u}..HEAD"])
    out["ahead"] = ahead.strip() if rc == 0 else "?"
    rc, behind = _run(["git", "rev-list", "--count", "HEAD..@{u}"])
    out["behind"] = behind.strip() if rc == 0 else "?"
    return out


def _pytest_snapshot() -> dict:
    rc, output = _run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        timeout=120,
    )
    out: dict = {"collected": None, "rc": rc}
    for line in output.splitlines():
        m = re.match(r"^\s*(\d+)\s+tests?\s+collected", line)
        if m:
            out["collected"] = int(m.group(1))
            break
    return out


def _bot_snapshot() -> dict:
    rc, output = _run(
        [sys.executable, "scripts/_bot_health_probe.py", "--level", "L0"],
        timeout=60,
    )
    summary = next(
        (ln for ln in reversed(output.splitlines()) if "bot-health-probe" in ln),
        "(no summary)",
    )
    return {"summary": summary.strip(), "rc": rc}


def _strategy_snapshot() -> dict:
    rc, output = _run([sys.executable, "scripts/_strategy_capability_matrix.py"])
    summary = next(
        (ln for ln in reversed(output.splitlines()) if ln.startswith("Total:")),
        "(no summary)",
    )
    return {"summary": summary.strip(), "rc": rc}


def _sentinel_snapshot() -> dict:
    rc, output = _run(
        [sys.executable, "scripts/_all_sentinels.py", "--fast"],
        timeout=120,
    )
    overall = next(
        (ln for ln in reversed(output.splitlines()) if "Overall:" in ln),
        "(no overall line)",
    )
    return {"summary": overall.strip(), "rc": rc}


def _roadmap_snapshot() -> dict:
    state_path = ROOT / "roadmap_state.json"
    if not state_path.exists():
        return {"error": "roadmap_state.json missing"}
    try:
        d = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"error": f"malformed JSON: {e}"}
    sa = d.get("shared_artifacts", {})
    return {
        "phase": d.get("current_phase", "?"),
        "progress_pct": d.get("overall_progress_pct", "?"),
        "tests_claimed": sa.get("eta_engine_tests_passing", "?"),
        "files_claimed": sa.get("eta_engine_python_files", "?"),
        "last_updated": d.get("last_updated", "?"),
    }


def _render_markdown(snap: dict) -> str:
    g, t, b, s, sen, r = (
        snap["git"],
        snap["pytest"],
        snap["bots"],
        snap["strategies"],
        snap["sentinels"],
        snap["roadmap"],
    )
    lines = ["# EVOLUTIONARY TRADING ALGO Overview\n"]

    lines.append("## Git\n")
    lines.append(f"- branch: `{g['branch']}` ({g['total_commits']} commits)")
    lines.append(f"- latest: {g['latest_commit']}")
    lines.append(f"- dirty files: {g['dirty_files']}")
    lines.append(f"- ahead/behind upstream: +{g['ahead']} / -{g['behind']}\n")

    lines.append("## Tests\n")
    lines.append(f"- collected: {t.get('collected', '?')}")
    lines.append(f"- collect rc: {t['rc']}\n")

    lines.append("## Fleet\n")
    lines.append(f"- bots: {b['summary']}")
    lines.append(f"- strategies: {s['summary']}\n")

    lines.append("## Sentinels\n")
    lines.append(f"- {sen['summary']}\n")

    lines.append("## Roadmap\n")
    lines.append(f"- phase: `{r.get('phase', '?')}`")
    lines.append(f"- progress: {r.get('progress_pct', '?')}%")
    lines.append(f"- claimed tests: {r.get('tests_claimed', '?')}")
    lines.append(f"- claimed py files: {r.get('files_claimed', '?')}")
    lines.append(f"- last updated: {r.get('last_updated', '?')}\n")

    return "\n".join(lines)


def _render_compact(snap: dict) -> str:
    g, t, b, s, sen, r = (
        snap["git"],
        snap["pytest"],
        snap["bots"],
        snap["strategies"],
        snap["sentinels"],
        snap["roadmap"],
    )
    lines = ["=== EVOLUTIONARY TRADING ALGO Overview ===", ""]
    lines.append(f"  git:        {g['branch']} @ {g['latest_commit'][:60]}")
    lines.append(f"              dirty={g['dirty_files']}  +{g['ahead']}/-{g['behind']}")
    lines.append(f"  tests:      {t.get('collected', '?')} collected")
    lines.append(f"  bots:       {b['summary']}")
    lines.append(f"  strategies: {s['summary']}")
    lines.append(f"  sentinels:  {sen['summary']}")
    lines.append(
        f"  roadmap:    phase={r.get('phase', '?')}  "
        f"pct={r.get('progress_pct', '?')}  "
        f"claimed_tests={r.get('tests_claimed', '?')}",
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--compact", action="store_true", help="terminal-friendly one-screen view")
    fmt.add_argument("--json", action="store_true", help="JSON output (machine-readable)")
    p.add_argument("--no-pytest", action="store_true", help="skip pytest collection (faster)")
    args = p.parse_args(argv)

    snap = {
        "git": _git_snapshot(),
        "pytest": {"collected": None, "rc": 0} if args.no_pytest else _pytest_snapshot(),
        "bots": _bot_snapshot(),
        "strategies": _strategy_snapshot(),
        "sentinels": _sentinel_snapshot(),
        "roadmap": _roadmap_snapshot(),
    }

    if args.json:
        print(json.dumps(snap, indent=2))
    elif args.compact:
        print(_render_compact(snap))
    else:
        print(_render_markdown(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main())

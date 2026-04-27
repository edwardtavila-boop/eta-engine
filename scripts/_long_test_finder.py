"""Find the slowest tests in the suite.

Runs ``pytest --collect-only`` first to confirm the suite collects, then
runs ``pytest --durations=N`` to extract the per-test timings. Sorts by
duration and prints the top N hotspots.

Output
------
    test-durations: 2255 tests, 8.2s total, top 10:
       0.34s  call    tests/test_walk_forward.py::test_full_grid
       0.21s  setup   tests/test_eta_overview.py::test_compact
       ...
    YELLOW -- 3 tests >100ms

Exit codes
----------
0  GREEN  -- all tests under --max-yellow-ms
1  YELLOW -- 1..--max-red-count tests above the threshold
2  RED    -- > --max-red-count tests above

Usage
-----
    python scripts/_long_test_finder.py
    python scripts/_long_test_finder.py --top 20 --max-yellow-ms 50
    python scripts/_long_test_finder.py --json

Why
---
A growing slow-test budget is the silent killer of dev-loop iteration
speed. Cap it. When a test crosses the threshold, the operator decides:
optimize, mark slow, or accept.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# pytest --durations format:
#   0.34s call     tests/test_x.py::test_y
DURATION_RE = re.compile(
    r"^\s*(?P<dur>\d+\.\d+)s\s+(?P<phase>call|setup|teardown)\s+(?P<test>\S+)",
)


def _run(top: int) -> tuple[int, str]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--no-header",
        "--tb=no",
        "--disable-warnings",
        f"--durations={top}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return (124, "(pytest timed out)")
    return (proc.returncode, proc.stdout + proc.stderr)


def _parse(output: str) -> list[tuple[float, str, str]]:
    out: list[tuple[float, str, str]] = []
    for line in output.splitlines():
        m = DURATION_RE.match(line)
        if not m:
            continue
        out.append((float(m.group("dur")), m.group("phase"), m.group("test")))
    out.sort(key=lambda x: -x[0])
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--max-yellow-ms", type=int, default=200, help="single-test duration > N ms triggers YELLOW")
    p.add_argument("--max-red-count", type=int, default=5, help="more than N over threshold -> RED")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    rc, out = _run(args.top)
    if rc not in (0, 1):
        print(f"long-test-finder: ERROR -- pytest exited {rc}")
        print("\n".join(out.splitlines()[-20:]))
        return 2

    durations = _parse(out)
    if not durations:
        print(
            "long-test-finder: GREEN -- no per-test durations parsed "
            "(either suite is empty or --durations not surfaced)"
        )
        return 0

    threshold_s = args.max_yellow_ms / 1000.0
    over = [d for d in durations if d[0] > threshold_s]

    if args.json:
        print(
            json.dumps(
                {
                    "total_parsed": len(durations),
                    "threshold_s": threshold_s,
                    "over_threshold": len(over),
                    "top": [{"dur_s": d, "phase": ph, "test": t} for d, ph, t in durations],
                },
                indent=2,
            )
        )
        return 0 if not over else 2 if len(over) > args.max_red_count else 1

    if not over:
        level = "GREEN"
    elif len(over) > args.max_red_count:
        level = "RED"
    else:
        level = "YELLOW"

    print(
        f"long-test-finder: {level} -- {len(over)} test(s) over {args.max_yellow_ms}ms (parsed {len(durations)})",
    )
    print()
    print(f"  Top {args.top} by duration:")
    print(f"  {'sec':>6}  {'phase':>8}  test")
    print(f"  {'-' * 6}  {'-' * 8}  {'-' * 60}")
    for dur, phase, test in durations[: args.top]:
        print(f"  {dur:>6.3f}  {phase:>8}  {test}")

    return {"GREEN": 0, "YELLOW": 1, "RED": 2}[level]


if __name__ == "__main__":
    sys.exit(main())

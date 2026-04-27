"""Per-module coverage ratchet -- mirror of _sharpe_drift.py for code coverage.

Runs ``coverage`` (or ``pytest --cov``) over the test suite, parses the
per-module result, and compares against a baseline persisted at
``docs/coverage_baseline.json``. The baseline ratchets UPWARDS only --
once a module hits a coverage high-water mark, the bar stays there.

Drop > ``--yellow-pct`` (default 5 percentage points) per module
triggers YELLOW. Drop > ``--red-pct`` (default 15 pp) triggers RED.
The aggregate verdict is the worst per-module level.

Why a ratchet, not absolute thresholds
--------------------------------------
Absolute coverage targets (e.g. "all modules > 90%") are
counter-productive: the operator has thoroughly tested modules at 95%
and integration-only modules at 40%. What MATTERS is regression --
removing tests or adding untested code paths to a previously
high-coverage module. The ratchet catches exactly that without
imposing a uniform target.

Inputs
------
* ``--report`` -- path to ``coverage.xml`` (cobertura format) or
  ``.coverage`` SQLite file. If neither exists, the script invokes
  ``python -m coverage run --source eta_engine -m pytest -x -q``
  and ``python -m coverage xml -o coverage.xml`` itself.
* ``--baseline`` -- the persisted ratchet (default
  ``docs/coverage_baseline.json``)
* ``--no-update`` -- compute deltas but don't update baseline
* ``--no-run`` -- assume report exists, don't run pytest

Exit codes
----------
0  GREEN
1  YELLOW (any module drops > yellow-pct)
2  RED (any module drops > red-pct)
9  setup error (no coverage tooling, no report, etc.)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "coverage.xml"
DEFAULT_BASELINE = ROOT / "docs" / "coverage_baseline.json"


def _have(cmd: str) -> bool:
    try:
        out = subprocess.run([sys.executable, "-m", cmd, "--version"], capture_output=True, check=False)
        return out.returncode == 0
    except OSError:
        return False


def _generate_report(report_path: Path) -> bool:
    """Run pytest with coverage and emit cobertura XML. Returns True on success."""
    if not _have("coverage"):
        return False
    cmds = [
        [sys.executable, "-m", "coverage", "run", "--source", "eta_engine", "-m", "pytest", "-x", "-q"],
        [sys.executable, "-m", "coverage", "xml", "-o", str(report_path)],
    ]
    for cmd in cmds:
        out = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)
        if out.returncode != 0 and "coverage xml" in " ".join(cmd):
            # XML emit may fail if pytest had errors but coverage still ran
            return report_path.exists()
        if out.returncode != 0 and "coverage run" in " ".join(cmd):
            # pytest may fail (broken test) but coverage still produced data
            continue
    return report_path.exists()


def _parse_cobertura(path: Path) -> dict[str, float]:
    """Return {module_relative_path: line_coverage_pct} from cobertura XML."""
    tree = ET.parse(path)
    root = tree.getroot()
    out: dict[str, float] = {}
    for cls in root.iter("class"):
        filename = cls.attrib.get("filename", "")
        line_rate = cls.attrib.get("line-rate", "0")
        try:
            pct = float(line_rate) * 100.0
        except ValueError:
            continue
        if filename:
            out[filename] = pct
    return out


def _classify(baseline_pct: float, current_pct: float, yellow: float, red: float) -> tuple[str, float]:
    drop = baseline_pct - current_pct
    if drop > red:
        return ("RED", drop)
    if drop > yellow:
        return ("YELLOW", drop)
    return ("GREEN", drop)


def _severity(level: str) -> int:
    return {"GREEN": 0, "YELLOW": 1, "RED": 2}.get(level, 0)


def _evaluate(
    current: dict[str, float],
    baseline: dict,
    yellow: float,
    red: float,
) -> tuple[list[dict], dict]:
    new_baseline = {
        "per_module": dict(baseline.get("per_module", {})),
        "samples": int(baseline.get("samples", 0)) + 1,
        "last_updated": datetime.now(UTC).isoformat(),
    }
    diagnostics = []
    for module, cur_pct in current.items():
        prev = baseline.get("per_module", {}).get(module)
        if prev is None:
            diagnostics.append(
                {
                    "module": module,
                    "level": "GREEN",
                    "drop": 0.0,
                    "current": cur_pct,
                    "baseline": None,
                    "seeded": True,
                }
            )
            new_baseline["per_module"][module] = cur_pct
            continue
        level, drop = _classify(float(prev), cur_pct, yellow, red)
        diagnostics.append(
            {
                "module": module,
                "level": level,
                "drop": drop,
                "current": cur_pct,
                "baseline": float(prev),
                "seeded": False,
            }
        )
        # Ratchet upwards only
        new_baseline["per_module"][module] = max(cur_pct, float(prev))
    return diagnostics, new_baseline


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    p.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    p.add_argument("--yellow-pct", type=float, default=5.0)
    p.add_argument("--red-pct", type=float, default=15.0)
    p.add_argument("--no-update", action="store_true")
    p.add_argument("--no-run", action="store_true", help="don't try to generate the report")
    args = p.parse_args(argv)

    if not args.report.exists():
        if args.no_run:
            print(f"coverage-drift: data-missing -- {args.report} not found and --no-run set")
            return 9
        if not _generate_report(args.report):
            print(f"coverage-drift: data-missing -- could not generate {args.report}")
            return 9

    try:
        current = _parse_cobertura(args.report)
    except (ET.ParseError, OSError) as e:
        print(f"coverage-drift: data-missing -- parse failed: {e}")
        return 9

    baseline = (
        json.loads(args.baseline.read_text(encoding="utf-8"))
        if args.baseline.exists()
        else {"per_module": {}, "samples": 0}
    )
    diagnostics, new_baseline = _evaluate(current, baseline, args.yellow_pct, args.red_pct)

    overall = max((d["level"] for d in diagnostics), key=_severity, default="GREEN")
    code = _severity(overall)

    print(
        f"coverage-drift: {overall} -- {len(diagnostics)} modules (samples={baseline.get('samples', 0)} prior)",
    )
    # Show only non-green or seeded; full list is in the baseline file
    for d in sorted(diagnostics, key=lambda x: -_severity(x["level"])):
        if d["seeded"]:
            print(f"  [SEED  ] {d['module']}: {d['current']:.1f}% (baseline-seed)")
            continue
        if d["level"] != "GREEN":
            print(
                f"  [{d['level']:6}] {d['module']}: {d['current']:.1f}% "
                f"vs baseline {d['baseline']:.1f}% (drop={d['drop']:.1f}pp)",
            )

    n_green = sum(1 for d in diagnostics if d["level"] == "GREEN" and not d["seeded"])
    n_seed = sum(1 for d in diagnostics if d["seeded"])
    print(f"  ({n_green} modules at-or-above baseline, {n_seed} new modules seeded)")

    if not args.no_update:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(new_baseline, indent=2) + "\n",
            encoding="utf-8",
        )
    return code


if __name__ == "__main__":
    sys.exit(main())

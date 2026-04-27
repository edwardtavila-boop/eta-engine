"""Cross-regime agreement checker: eta_engine vs mnq_bot.

The two codebases share a conceptual lineage but use different regime
taxonomies:

  eta_engine        mnq_bot
  -------------        -------
  TRENDING             RISK-ON
  RANGING              NEUTRAL
  HIGH_VOL             (no direct equivalent)
  LOW_VOL              (no direct equivalent)
  CRISIS               (no direct equivalent)

A label-by-label equality check is therefore meaningless. What IS
meaningful: the two systems' WORST-regime conclusion should point in
the same broad direction. If apex says ``exclude HIGH_VOL`` because
its OOS expectancy is deeply negative, and mnq_bot's by-regime CSV
shows its worst-regime expectancy ALSO negative, the systems agree
at the meta level. If apex says ``HIGH_VOL bad`` but mnq_bot shows
every regime profitable, that's a structural disagreement worth
investigating.

What this script checks
-----------------------
1. apex's ``docs/cross_regime/cross_regime_validation.json`` exists
   and has a regime with OOS ``expectancy_r < 0``.
2. mnq_bot's ``eta_v3_framework/results/by_regime.csv`` exists and
   has a regime with ``avg_r < 0``.
3. (Strong) Both systems agree on the SIGN of their worst-regime
   conclusion.

Inputs
------
--apex-cross-regime PATH (default: ../eta_engine/docs/cross_regime/cross_regime_validation.json)
--mnq-by-regime PATH (default: ../mnq_bot/eta_v3_framework/results/by_regime.csv)

Exit codes
----------
0  AGREE -- both show negative-expectancy regimes
1  YELLOW -- apex shows negatives but mnq doesn't (or vice versa)
2  RED -- both show all-positive expectancies (apex's HIGH_VOL exclusion
       is then a single-source claim, fragile)
9  data missing on either side
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APEX = ROOT / "docs" / "cross_regime" / "cross_regime_validation.json"

# Try a few canonical locations for mnq_bot's by_regime.csv. The cloud
# trigger passes --mnq-by-regime explicitly, so this default only
# matters for local invocation on the operator's machine.
_MNQ_CANDIDATES = [
    Path("C:/Users/edwar/projects/mnq_bot/eta_v3_framework/results/by_regime.csv"),
    Path.home() / "projects" / "mnq_bot" / "eta_v3_framework" / "results" / "by_regime.csv",
    ROOT.parent / "mnq_bot" / "eta_v3_framework" / "results" / "by_regime.csv",
]


def _default_mnq() -> Path:
    """First existing candidate, else the canonical Windows path."""
    for cand in _MNQ_CANDIDATES:
        if cand.exists():
            return cand
    return _MNQ_CANDIDATES[0]


DEFAULT_MNQ = _default_mnq()


def _apex_negative_regimes(path: Path) -> list[tuple[str, float]]:
    """Return [(regime, oos_expectancy_r), ...] for regimes with neg OOS."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[str, float]] = []
    for regime, payload in data.get("per_regime", {}).items():
        oos = payload.get("oos") or {}
        exp = oos.get("expectancy_r")
        if exp is not None and float(exp) < 0:
            out.append((regime, float(exp)))
    return out


def _mnq_negative_regimes(path: Path) -> list[tuple[str, float]]:
    """Return [(regime, avg_r), ...] for regimes with neg avg_r."""
    if not path.exists():
        return []
    out: list[tuple[str, float]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                avg_r = float(row["avg_r"])
            except (KeyError, ValueError):
                continue
            if avg_r < 0:
                out.append((row.get("regime", "<?>"), avg_r))
    return out


def _verdict(apex_neg: list, mnq_neg: list) -> tuple[str, int, str]:
    """Return (verdict_label, exit_code, message)."""
    if apex_neg and mnq_neg:
        return (
            "AGREE",
            0,
            f"both negative: apex={[r for r, _ in apex_neg]} mnq={[r for r, _ in mnq_neg]}",
        )
    if apex_neg and not mnq_neg:
        return (
            "YELLOW",
            1,
            f"apex shows negative regimes {[r for r, _ in apex_neg]} "
            f"but mnq_bot's by_regime.csv shows all positive expectancies",
        )
    if mnq_neg and not apex_neg:
        return (
            "YELLOW",
            1,
            f"mnq_bot shows negative regimes {[r for r, _ in mnq_neg]} but apex's cross_regime_validation has none",
        )
    return (
        "RED",
        2,
        "BOTH systems show all-positive regimes -- apex's HIGH_VOL "
        "exclusion is then a single-source claim, fragile to data update",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--apex-cross-regime", type=Path, default=DEFAULT_APEX)
    p.add_argument("--mnq-by-regime", type=Path, default=DEFAULT_MNQ)
    args = p.parse_args(argv)

    if not args.apex_cross_regime.exists():
        print(f"agreement: data-missing -- {args.apex_cross_regime} not found")
        return 9
    if not args.mnq_by_regime.exists():
        print(f"agreement: data-missing -- {args.mnq_by_regime} not found")
        return 9

    apex_neg = _apex_negative_regimes(args.apex_cross_regime)
    mnq_neg = _mnq_negative_regimes(args.mnq_by_regime)
    verdict, code, msg = _verdict(apex_neg, mnq_neg)

    print(f"agreement: {verdict} -- {msg}")
    return code


if __name__ == "__main__":
    sys.exit(main())

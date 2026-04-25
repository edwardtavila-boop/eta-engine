"""H1 closure: tolerance calibrator for ``BrokerEquityReconciler``.

Reads ``docs/runtime_log.jsonl``, extracts every tick's ``broker_equity``
block, and emits a recommended set of asymmetric tolerance parameters
based on the observed drift histogram.

Why this exists
---------------
The Red Team v0.1.64 review (H1) observed:
  > The deferral rationale claims tolerance will come 'from live-paper
  > empirics.' There is no script in `scripts/` that reads
  > `runtime_log.jsonl` and emits a tolerance recommendation. Operator
  > goes to set the v0.2.x tolerance and there is no tool. They
  > eyeball it and pick $100. Production blow-up at $99 silent
  > commission slip below the threshold.

This script is the tool. It does NOT mutate any config; it only
prints a recommendation. The operator decides whether to update
``configs/kill_switch.yaml`` or ``_amain``'s defaults.

Methodology
-----------
For each tick entry in ``runtime_log.jsonl`` where
``meta.broker_equity.drift_usd is not None`` and the reason is
``broker_below_logical``, ``broker_above_logical``, or
``within_tolerance``, partition by direction:

  * ``below`` direction = drift_usd > 0 (broker reports less than
    logical -- the dangerous direction; cushion is over-stated)
  * ``above`` direction = drift_usd < 0 (broker reports more than
    logical -- usually MTM lag / dividend / rebate; harmless)
  * exact-zero ticks skipped (no signal in either direction)

For each direction compute p50, p95, p99, max of:
  * |drift_usd|
  * |drift_pct_of_logical|

Tolerance recommendations:
  * ``tolerance_below_usd / tolerance_below_pct`` = p99 of |below|
    (tight: if you set the threshold here, you accept being woken
    up roughly 1% of the time -- a meaningful drift cluster)
  * ``tolerance_above_usd / tolerance_above_pct`` = 2 * p99 of |above|
    (loose: in the harmless direction we deliberately give 2x slack
    so MTM lag does not generate alert spam)

Output
------
By default prints a human-readable report. ``--json`` emits a
machine-readable dict for piping into a config-update script.

Usage
-----
    # Default: read docs/runtime_log.jsonl, human report
    python scripts/calibrate_broker_drift_tolerance.py

    # JSON output for downstream tooling
    python scripts/calibrate_broker_drift_tolerance.py --json

    # Custom log file (useful for paper-vs-live A/B)
    python scripts/calibrate_broker_drift_tolerance.py --log path/to/log.jsonl

    # Override percentile (use p95 instead of p99 for tighter alerts)
    python scripts/calibrate_broker_drift_tolerance.py --percentile 0.95

Exit codes
----------
0 -- ran successfully (recommendation printed, even if data was sparse)
2 -- log file missing or unreadable
3 -- log file present but no broker_equity ticks found (calibration
     impossible; operator must run paper longer)
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "docs" / "runtime_log.jsonl"


@dataclass(frozen=True)
class DriftSamples:
    """One direction's worth of drift samples."""

    direction: str   # "below" or "above"
    n: int
    usd: list[float]
    pct: list[float]


@dataclass(frozen=True)
class Stats:
    """Summary statistics for one sample list."""

    n: int
    p50: float
    p95: float
    p99: float
    max_: float


def _percentile(values: list[float], p: float) -> float:
    """Return the p-th percentile of a list. p in [0, 1]."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    # statistics.quantiles requires n >= 2; for our purposes use 100
    # cuts so we can index by integer percentile.
    sorted_vals = sorted(values)
    if p <= 0:
        return sorted_vals[0]
    if p >= 1:
        return sorted_vals[-1]
    # Linear interpolation between adjacent ranks.
    rank = p * (len(sorted_vals) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (rank - lo)


def _summarize(values: list[float]) -> Stats:
    if not values:
        return Stats(n=0, p50=0.0, p95=0.0, p99=0.0, max_=0.0)
    return Stats(
        n=len(values),
        p50=_percentile(values, 0.50),
        p95=_percentile(values, 0.95),
        p99=_percentile(values, 0.99),
        max_=max(values),
    )


def collect(log_path: Path) -> tuple[DriftSamples, DriftSamples]:
    """Walk the JSONL log; return (below, above) drift samples.

    Returns drift values as ABSOLUTE (positive) numbers for both
    directions so downstream stats can be computed uniformly. The
    direction is preserved in the ``DriftSamples.direction`` field.
    """
    below_usd: list[float] = []
    below_pct: list[float] = []
    above_usd: list[float] = []
    above_pct: list[float] = []

    with log_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("kind") != "tick":
                continue
            be = (entry.get("meta") or {}).get("broker_equity")
            if not isinstance(be, dict):
                continue
            drift_usd = be.get("drift_usd")
            drift_pct = be.get("drift_pct_of_logical")
            if drift_usd is None:
                continue  # no_broker_data tick -- no signal
            if drift_usd == 0:
                continue  # exact-zero -- contributes no info either side
            try:
                usd = float(drift_usd)
            except (TypeError, ValueError):
                continue
            try:
                pct = float(drift_pct) if drift_pct is not None else 0.0
            except (TypeError, ValueError):
                pct = 0.0
            if usd > 0:
                below_usd.append(abs(usd))
                below_pct.append(abs(pct))
            else:
                above_usd.append(abs(usd))
                above_pct.append(abs(pct))

    return (
        DriftSamples("below", n=len(below_usd), usd=below_usd, pct=below_pct),
        DriftSamples("above", n=len(above_usd), usd=above_usd, pct=above_pct),
    )


def recommend(
    below: DriftSamples,
    above: DriftSamples,
    *,
    percentile: float = 0.99,
    above_slack: float = 2.0,
) -> dict[str, float | None]:
    """Compute recommended tolerance values from drift samples.

    Below direction: tight, set at the p-th percentile of observed
    |drift|. Above direction: loose, set at ``above_slack`` x the
    p-th percentile so MTM-lag overshoot does not generate alert spam.

    Returns a dict with keys:
      tolerance_below_usd / tolerance_below_pct
      tolerance_above_usd / tolerance_above_pct
    Each value is a float, or ``None`` if there were no samples in
    that direction (caller decides whether to fall back to a default).
    """
    return {
        "tolerance_below_usd": (
            _percentile(below.usd, percentile) if below.usd else None
        ),
        "tolerance_below_pct": (
            _percentile(below.pct, percentile) if below.pct else None
        ),
        "tolerance_above_usd": (
            above_slack * _percentile(above.usd, percentile)
            if above.usd else None
        ),
        "tolerance_above_pct": (
            above_slack * _percentile(above.pct, percentile)
            if above.pct else None
        ),
    }


def _human_report(
    below: DriftSamples,
    above: DriftSamples,
    rec: dict[str, float | None],
    *,
    percentile: float,
    above_slack: float,
) -> str:
    """Render a human-readable report. Returns a string."""
    lines: list[str] = []
    lines.append("BROKER EQUITY DRIFT TOLERANCE CALIBRATOR")
    lines.append("=" * 50)
    lines.append(
        f"Samples: below={below.n}  above={above.n}  "
        f"total_directional={below.n + above.n}",
    )
    lines.append(f"Percentile target: p{int(percentile * 100)}")
    lines.append(f"Above-direction slack: {above_slack:.1f}x")
    lines.append("")
    for label, samples in (("below", below), ("above", above)):
        lines.append(f"  {label.upper()} direction (drift_usd sign = "
                     f"{'+' if label == 'below' else '-'})")
        if samples.n == 0:
            lines.append(f"    (no {label}-direction samples in log)")
            lines.append("")
            continue
        usd_stats = _summarize(samples.usd)
        pct_stats = _summarize(samples.pct)
        lines.append(
            f"    USD  n={usd_stats.n}  p50={usd_stats.p50:.2f}  "
            f"p95={usd_stats.p95:.2f}  p99={usd_stats.p99:.2f}  "
            f"max={usd_stats.max_:.2f}",
        )
        lines.append(
            f"    PCT  n={pct_stats.n}  p50={pct_stats.p50:.5f}  "
            f"p95={pct_stats.p95:.5f}  p99={pct_stats.p99:.5f}  "
            f"max={pct_stats.max_:.5f}",
        )
        lines.append("")

    lines.append("RECOMMENDED TOLERANCES")
    lines.append("-" * 50)
    for k, v in rec.items():
        if v is None:
            lines.append(f"  {k}: <no data -- keep current default>")
        elif "_pct" in k:
            lines.append(f"  {k}: {v:.6f}  ({v * 100:.4f}%)")
        else:
            lines.append(f"  {k}: ${v:.2f}")
    lines.append("")
    if any(v is None for v in rec.values()):
        lines.append(
            "NOTE: at least one direction has zero samples. The "
            "recommendation is partial; run more paper trading and "
            "re-calibrate before tightening live tolerances.",
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help=f"Path to runtime_log.jsonl (default: {DEFAULT_LOG})",
    )
    p.add_argument(
        "--percentile",
        type=float,
        default=0.99,
        help="Percentile target for tolerance recommendation (0..1, default 0.99)",
    )
    p.add_argument(
        "--above-slack",
        type=float,
        default=2.0,
        help="Multiplier applied to above-direction percentile (default 2.0)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the human report",
    )
    args = p.parse_args(argv)

    if not args.log.exists():
        print(f"ERROR: log file not found: {args.log}", file=sys.stderr)
        return 2
    if not (0.0 < args.percentile < 1.0):
        print(
            f"ERROR: --percentile must be in (0, 1), got {args.percentile}",
            file=sys.stderr,
        )
        return 2
    if args.above_slack <= 0:
        print(
            f"ERROR: --above-slack must be > 0, got {args.above_slack}",
            file=sys.stderr,
        )
        return 2

    below, above = collect(args.log)
    rec = recommend(
        below, above,
        percentile=args.percentile,
        above_slack=args.above_slack,
    )

    if below.n == 0 and above.n == 0:
        print(
            f"ERROR: no broker_equity tick samples found in {args.log}. "
            "Run paper trading with the reconciler wired (see "
            "docs/runbooks/broker_equity_drift_response.md) before "
            "calibrating.",
            file=sys.stderr,
        )
        return 3

    if args.json:
        print(json.dumps({
            "log_path": str(args.log),
            "percentile": args.percentile,
            "above_slack": args.above_slack,
            "samples": {
                "below_n": below.n,
                "above_n": above.n,
            },
            "recommendation": rec,
        }, indent=2))
    else:
        print(_human_report(
            below, above, rec,
            percentile=args.percentile,
            above_slack=args.above_slack,
        ))

    return 0


if __name__ == "__main__":
    sys.exit(main())

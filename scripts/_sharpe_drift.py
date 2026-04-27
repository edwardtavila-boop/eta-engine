"""Per-bot Sharpe / Sortino drift alarm.

Reads the latest ``docs/paper_run_report.json`` and compares each bot's
Sharpe + Sortino + expectancy_r against a rolling baseline persisted at
``docs/sharpe_baseline.json``. If a metric has degraded materially since
the baseline, a YELLOW or RED level is emitted.

How the baseline is maintained
------------------------------
The baseline file holds an exponential moving average (alpha=0.2) of
Sharpe / Sortino / expectancy_r per bot, plus the timestamp and the
number of samples observed. Each run of this script:

1. Computes pct-change of (current - baseline) / baseline for each bot.
2. Classifies the worst per-bot degradation in {GREEN, YELLOW, RED}.
3. Writes an updated baseline with the new EMA values folded in (so the
   baseline drifts WITH the live system, not against it -- this catches
   *acceleration* of drift, not slow-and-steady regime change).

Sign-flip detection
-------------------
A baseline Sharpe of +6 followed by a current Sharpe of -1 is treated
as RED regardless of percentage, because a sign flip is qualitatively
different from a magnitude drop. This is the production-mode early
warning before the classic 'drift to break-even' bottoms out.

Thresholds (per bot)
--------------------
* GREEN  -- |change| <= 30%
* YELLOW -- change in (-50%, -30%]
* RED    -- change <= -50% OR sign-flipped sharpe

Aggregate verdict = max severity across bots.

Exit codes
----------
0 GREEN, 1 YELLOW, 2 RED, 9 data missing
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "docs" / "paper_run_report.json"
DEFAULT_BASELINE = ROOT / "docs" / "sharpe_baseline.json"

ALPHA = 0.2  # EMA smoothing
METRICS = ("sharpe", "sortino", "expectancy_r")
YELLOW_DROP = 0.30
RED_DROP = 0.50


def _load_report(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _load_baseline(path: Path) -> dict:
    if not path.exists():
        return {"per_bot": {}, "samples": 0, "last_updated": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"per_bot": {}, "samples": 0, "last_updated": None}


def _ema(prev: float | None, new: float, alpha: float = ALPHA) -> float:
    if prev is None:
        return float(new)
    return alpha * float(new) + (1.0 - alpha) * float(prev)


def _classify_change(baseline: float, current: float) -> tuple[str, float]:
    """Return (level, pct_change). Sign-flip auto-RED."""
    if baseline == 0:
        return ("GREEN", 0.0)
    pct = (current - baseline) / abs(baseline)
    if (baseline > 0 and current < 0) or (baseline < 0 and current > 0):
        return ("RED", pct)
    if pct <= -RED_DROP:
        return ("RED", pct)
    if pct <= -YELLOW_DROP:
        return ("YELLOW", pct)
    return ("GREEN", pct)


def _evaluate(
    report: dict,
    baseline: dict,
) -> tuple[list[dict], dict]:
    """Return (per-bot diagnostics, updated baseline dict)."""
    new_baseline = {
        "per_bot": dict(baseline.get("per_bot", {})),
        "samples": int(baseline.get("samples", 0)) + 1,
        "last_updated": datetime.now(UTC).isoformat(),
    }
    diagnostics = []
    for bot in report.get("per_bot", []):
        name = bot.get("bot", "<?>")
        prev = baseline.get("per_bot", {}).get(name, {})
        cur_metrics = {m: float(bot.get(m, 0.0)) for m in METRICS}
        per_metric = {}
        worst = "GREEN"
        worst_pct = 0.0
        worst_metric = None
        seeded_only = True  # all metrics are first-time-seed
        for m, cur in cur_metrics.items():
            base = prev.get(m)
            if base is None:
                # First time seeing this metric for this bot -- seed only
                per_metric[m] = {"baseline": None, "current": cur, "level": "GREEN", "pct": 0.0}
                continue
            seeded_only = False
            level, pct = _classify_change(float(base), cur)
            per_metric[m] = {
                "baseline": float(base),
                "current": cur,
                "level": level,
                "pct": pct,
            }
            if _severity(level) > _severity(worst):
                worst, worst_pct, worst_metric = level, pct, m
            elif worst_metric is None:
                # Track best comparison metric for display even if all-GREEN
                worst_metric = m
                worst_pct = pct
        diagnostics.append(
            {
                "bot": name,
                "level": worst,
                "worst_metric": worst_metric,
                "worst_pct": worst_pct,
                "metrics": per_metric,
                "seeded_only": seeded_only,
            }
        )
        # Update EMA in-place
        updated = {m: _ema(prev.get(m), cur_metrics[m]) for m in METRICS}
        new_baseline["per_bot"][name] = updated
    return diagnostics, new_baseline


def _severity(level: str) -> int:
    return {"GREEN": 0, "YELLOW": 1, "RED": 2}.get(level, 0)


def _aggregate(diagnostics: list[dict]) -> tuple[str, int]:
    if not diagnostics:
        return ("GREEN", 0)
    worst = max(diagnostics, key=lambda d: _severity(d["level"]))["level"]
    return (worst, _severity(worst))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    p.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    p.add_argument(
        "--no-update",
        action="store_true",
        help="don't persist the updated baseline (useful for dry-runs)",
    )
    args = p.parse_args(argv)

    report = _load_report(args.report)
    if report is None:
        print(f"sharpe-drift: data-missing -- {args.report} not found")
        return 9

    baseline = _load_baseline(args.baseline)
    diagnostics, new_baseline = _evaluate(report, baseline)
    overall, code = _aggregate(diagnostics)

    print(
        f"sharpe-drift: {overall} -- {len(diagnostics)} bots evaluated (samples={baseline.get('samples', 0)} prior)",
    )
    for d in diagnostics:
        if d.get("seeded_only"):
            print(f"  [GREEN ] {d['bot']}: baseline-seed (no prior data)")
            continue
        wm = d["metrics"][d["worst_metric"]]
        print(
            f"  [{d['level']:6}] {d['bot']}: {d['worst_metric']} "
            f"baseline={wm['baseline']:.3f} -> current={wm['current']:.3f} "
            f"({d['worst_pct'] * 100:+.1f}%)",
        )

    if not args.no_update:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(new_baseline, indent=2) + "\n",
            encoding="utf-8",
        )
    return code


if __name__ == "__main__":
    sys.exit(main())

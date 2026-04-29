"""Strategy backtest gauntlet (Tier-4 #16, 2026-04-27).

Runs a candidate Pine spec through the same regression suite the
existing strategies use, scores it against the current champion on a
fixed set of metrics, prints a pass/fail summary.

This is the "doorway" for new strategy ideas. Drop a Pine file in
``mnq_backtest/pine/<name>.pine``, run::

    python scripts/strategy_gauntlet.py --candidate mnq_backtest/pine/<name>.pine

The candidate must beat the champion on at least ``--required-wins``
metrics (default 3 of 5) to pass the gauntlet. Wins on:

  * total_pnl              -- larger is better
  * sharpe                 -- larger is better
  * max_drawdown_abs       -- smaller is better (we negate for comparison)
  * trade_count_in_band    -- closer to baseline (overtrading penalty)
  * pf (profit factor)     -- larger is better

Champion spec is read from ``mnq_backtest/configs/champion_spec.txt``
(default: ``cascade_hunter_v1``).
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("strategy_gauntlet")

WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
MNQ_BACKTEST = WORKSPACE / "mnq_backtest"


@dataclass
class GauntletScore:
    spec_id: str
    metrics: dict[str, float]


def run_backtest(spec_path: Path, *, bars: int = 500) -> GauntletScore:
    """Invoke mnq_backtest's nightly regression on a spec, return metrics.

    For now this is a thin wrapper around the existing
    eta_engine_nightly_regression.py script -- it knows how to load a
    spec + run the harness + emit metrics JSON.
    """
    venv_py = MNQ_BACKTEST / ".venv" / "Scripts" / "python.exe"
    py = str(venv_py) if venv_py.exists() else sys.executable

    # The regression script expects --spec <path>
    cmd = [py, str(MNQ_BACKTEST / "scripts" / "eta_engine_nightly_regression.py"),
           "--spec", str(spec_path), "--bars", str(bars), "--json-out"]
    logger.info("running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=900, check=False,
            cwd=str(MNQ_BACKTEST),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"backtest invocation failed: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"backtest exit={result.returncode}\nstdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )
    # Parse the last JSON object printed
    last_line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "{}"
    try:
        metrics = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse metrics JSON: {exc}") from exc
    return GauntletScore(spec_id=spec_path.stem, metrics=metrics)


def compare(candidate: GauntletScore, champion: GauntletScore) -> dict[str, str]:
    """Per-metric W/L/T verdict.

    Returns dict like ``{"total_pnl": "WIN", "sharpe": "LOSS", ...}``.
    Ties default to TIE. Missing metrics on either side default to TIE.
    """
    higher_better = {"total_pnl", "sharpe", "pf", "win_rate"}
    lower_better = {"max_drawdown_abs", "max_drawdown_pct", "n_kills"}
    verdicts: dict[str, str] = {}
    metrics_to_check = set(higher_better | lower_better)
    metrics_to_check |= set(candidate.metrics.keys()) | set(champion.metrics.keys())

    for k in sorted(metrics_to_check):
        a = candidate.metrics.get(k)
        b = champion.metrics.get(k)
        if a is None or b is None:
            verdicts[k] = "TIE"
            continue
        if k in higher_better:
            verdicts[k] = "WIN" if a > b * 1.001 else "LOSS" if a < b * 0.999 else "TIE"
        elif k in lower_better:
            verdicts[k] = "WIN" if a < b * 0.999 else "LOSS" if a > b * 1.001 else "TIE"
        else:
            # Trade count "in band" -- closer to champion wins
            ratio = a / b if b else 0.0
            verdicts[k] = "WIN" if 0.7 <= ratio <= 1.3 else "LOSS"
    return verdicts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--candidate", type=Path, required=True,
                   help="Path to candidate Pine spec or strategy module")
    p.add_argument("--champion", type=Path,
                   default=MNQ_BACKTEST / "pine" / "cascade_hunter_v1.0_strategy.pine")
    p.add_argument("--bars", type=int, default=500)
    p.add_argument("--required-wins", type=int, default=3,
                   help="Min number of WIN verdicts to pass gauntlet (default 3 of 5 on core metrics)")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.candidate.exists():
        logger.error("candidate not found: %s", args.candidate)
        return 1
    if not args.champion.exists():
        logger.error("champion not found: %s -- can't gauntlet without a baseline", args.champion)
        return 1

    logger.info("running candidate: %s", args.candidate.name)
    cand_score = run_backtest(args.candidate, bars=args.bars)
    logger.info("running champion:  %s", args.champion.name)
    champ_score = run_backtest(args.champion, bars=args.bars)

    verdicts = compare(cand_score, champ_score)
    win_count = sum(1 for v in verdicts.values() if v == "WIN")
    loss_count = sum(1 for v in verdicts.values() if v == "LOSS")
    pass_gauntlet = win_count >= args.required_wins and win_count > loss_count

    if args.json:
        print(json.dumps({
            "ts": datetime.now(UTC).isoformat(),
            "candidate": cand_score.spec_id,
            "champion":  champ_score.spec_id,
            "verdicts":  verdicts,
            "wins":      win_count,
            "losses":    loss_count,
            "pass":      pass_gauntlet,
        }, indent=2))
    else:
        print()
        print(f"  {'METRIC':<22}  {'CANDIDATE':>12}  {'CHAMPION':>12}  VERDICT")
        for k in sorted(verdicts.keys()):
            a = cand_score.metrics.get(k, float('nan'))
            b = champ_score.metrics.get(k, float('nan'))
            print(f"  {k:<22}  {a:>12.4f}  {b:>12.4f}  {verdicts[k]}")
        print()
        print(f"  WINS:   {win_count}")
        print(f"  LOSSES: {loss_count}")
        print(f"  GAUNTLET: {'PASS' if pass_gauntlet else 'FAIL'}")
        print()

    return 0 if pass_gauntlet else 1


if __name__ == "__main__":
    sys.exit(main())

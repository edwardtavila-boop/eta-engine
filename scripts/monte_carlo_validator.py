"""Monte Carlo equity-curve validator (elite framework Layer 5).

For each bot with enough closes, bootstrap N randomized trade sequences
from its actual realized R distribution and compute the percentile band
on the equity curve. The question this answers:

    "Does this bot's equity curve survive worst-case permutations of
    its own trades — or did it get lucky on the trade ordering?"

A robust strategy's 5th-percentile equity curve still finishes positive
(or at least not ruinous). A luck-driven strategy's 5th-percentile
collapses far below zero — meaning if the same trades had hit in a
different order (a natural-variance scenario), it would have blown up.

Outputs per bot:
    actual_final_R         — observed cumulative R over the trade history
    p05_final_R            — 5th-percentile of bootstrapped final R
    p50_final_R            — median bootstrap final R (sanity check)
    p95_final_R            — 95th-percentile (best-case)
    p05_max_drawdown_R     — worst-case max DD across bootstraps
    p_negative             — % of bootstraps that finished negative
    luck_score             — 0..1 where 0 = robust, 1 = pure luck
                             (= p_negative when actual is positive)
    verdict                — ROBUST / FRAGILE / LUCKY / INSUFFICIENT

Usage:
    python -m eta_engine.scripts.monte_carlo_validator
    python -m eta_engine.scripts.monte_carlo_validator --bot btc_optimized
    python -m eta_engine.scripts.monte_carlo_validator --since 2026-05-04T23:31:00 --bootstraps 1000
    python -m eta_engine.scripts.monte_carlo_validator --json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

_TRADE_CLOSES = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\trade_closes.jsonl",
)


def _load_closes(since_iso: str | None = None) -> dict[str, list[float]]:
    """Return {bot_id: [realized_r, ...]} from trade_closes.jsonl."""
    out: dict[str, list[float]] = defaultdict(list)
    if not _TRADE_CLOSES.exists():
        return out
    try:
        with _TRADE_CLOSES.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_iso and str(rec.get("ts", "")) < since_iso:
                    continue
                bid = rec.get("bot_id")
                r = rec.get("realized_r")
                if bid is None or r is None:
                    continue
                with contextlib.suppress(TypeError, ValueError):
                    out[bid].append(float(r))
    except OSError:
        return out
    return out


def _equity_curve_stats(rs: list[float]) -> tuple[float, float]:
    """Return (final_R, max_drawdown_R) for one trade sequence."""
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rs:
        cum += r
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return cum, max_dd


def _percentile(values: list[float], q: float) -> float:
    """Inclusive percentile (linear interp), q in [0,100]."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (q / 100.0) * (len(s) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(s) - 1)
    frac = pos - lower
    return s[lower] * (1 - frac) + s[upper] * frac


def _validate_bot(
    rs: list[float],
    bootstraps: int,
    rng: random.Random,
) -> dict[str, Any]:
    """Bootstrap N random orderings; compute percentile bands."""
    n = len(rs)
    if n < 30:
        return {
            "n": n,
            "verdict": "INSUFFICIENT",
            "reason": f"need >=30 closes, have {n}",
        }

    actual_final, actual_max_dd = _equity_curve_stats(rs)

    # Bootstrap with replacement (resampling the empirical R-distribution).
    # This tests "could a worse-luck draw of the same distribution have
    # ruined this bot?" — captures both ordering AND draw variance.
    finals: list[float] = []
    max_dds: list[float] = []
    n_negative = 0
    for _ in range(bootstraps):
        sample = rng.choices(rs, k=n)
        f, dd = _equity_curve_stats(sample)
        finals.append(f)
        max_dds.append(dd)
        if f < 0:
            n_negative += 1

    p05 = _percentile(finals, 5.0)
    p50 = _percentile(finals, 50.0)
    p95 = _percentile(finals, 95.0)
    p05_dd = _percentile(max_dds, 95.0)  # 95th percentile of DD = worst-case
    p_negative = n_negative / bootstraps

    # Verdict rules:
    # - ROBUST: actual positive AND p05 still positive AND p_negative < 10%
    # - LUCKY:  actual positive BUT p05 deeply negative AND p_negative > 30%
    # - FRAGILE: actual positive AND p05 between (LUCKY threshold and 0)
    # - DEAD:   actual final negative AND p95 also negative
    if actual_final < 0 and p95 < 0:
        verdict = "DEAD"
    elif actual_final > 0 and p05 > 0 and p_negative < 0.10:
        verdict = "ROBUST"
    elif actual_final > 0 and p_negative > 0.30:
        verdict = "LUCKY"
    elif actual_final > 0:
        verdict = "FRAGILE"
    else:
        verdict = "MIXED"

    luck_score = p_negative if actual_final > 0 else 1.0

    return {
        "n": n,
        "actual_final_R": round(actual_final, 4),
        "actual_max_drawdown_R": round(actual_max_dd, 4),
        "p05_final_R": round(p05, 4),
        "p50_final_R": round(p50, 4),
        "p95_final_R": round(p95, 4),
        "p95_max_drawdown_R": round(p05_dd, 4),
        "p_negative": round(p_negative, 4),
        "luck_score": round(luck_score, 4),
        "bootstraps": bootstraps,
        "verdict": verdict,
    }


def analyze(
    since_iso: str | None = None,
    bootstraps: int = 1000,
    seed: int | None = None,
) -> dict[str, Any]:
    rng = random.Random(seed) if seed is not None else random.Random()
    closes_by_bot = _load_closes(since_iso=since_iso)
    bots: dict[str, dict[str, Any]] = {}
    for bot_id, rs in closes_by_bot.items():
        result = _validate_bot(rs, bootstraps=bootstraps, rng=rng)
        result["bot_id"] = bot_id
        bots[bot_id] = result

    verdict_counts: dict[str, int] = defaultdict(int)
    for r in bots.values():
        verdict_counts[r["verdict"]] += 1

    return {
        "n_bots": len(bots),
        "bootstraps_per_bot": bootstraps,
        "verdict_counts": dict(verdict_counts),
        "bots": bots,
        "since_iso": since_iso,
    }


def _print_text(report: dict[str, Any]) -> None:
    print("=" * 102)
    print(
        f" MONTE CARLO VALIDATOR — {report['n_bots']} bots, {report['bootstraps_per_bot']} bootstraps each",
    )
    vc = report["verdict_counts"]
    print(
        f" verdicts: ROBUST={vc.get('ROBUST', 0)}  FRAGILE={vc.get('FRAGILE', 0)}  "
        f"LUCKY={vc.get('LUCKY', 0)}  DEAD={vc.get('DEAD', 0)}  "
        f"MIXED={vc.get('MIXED', 0)}  INSUFFICIENT={vc.get('INSUFFICIENT', 0)}",
    )
    print("=" * 102)
    print(
        f"{'bot_id':<25} {'verdict':<14} {'n':>4} "
        f"{'actual_R':>10} {'p05_R':>9} {'p50_R':>9} {'p95_R':>9} "
        f"{'wc_DD_R':>9} {'p_neg':>7} {'luck':>6}",
    )
    print("-" * 102)

    verdict_order = {
        "ROBUST": 0,
        "FRAGILE": 1,
        "MIXED": 2,
        "LUCKY": 3,
        "DEAD": 4,
        "INSUFFICIENT": 5,
    }
    sorted_bots = sorted(
        report["bots"].values(),
        key=lambda b: (
            verdict_order.get(b["verdict"], 9),
            -float(b.get("actual_final_R", 0) or 0),
        ),
    )
    for r in sorted_bots:
        if r["verdict"] == "INSUFFICIENT":
            print(
                f"{r['bot_id']:<25} {r['verdict']:<14} {r['n']:>4}  (need 30+ closes)",
            )
            continue
        print(
            f"{r['bot_id']:<25} {r['verdict']:<14} {r['n']:>4} "
            f"{r['actual_final_R']:>+10.3f} {r['p05_final_R']:>+9.3f} "
            f"{r['p50_final_R']:>+9.3f} {r['p95_final_R']:>+9.3f} "
            f"{r['p95_max_drawdown_R']:>9.3f} "
            f"{r['p_negative'] * 100:>5.1f}% {r['luck_score']:>6.3f}",
        )
    print("=" * 102)
    print("\nLEGEND:")
    print("  ROBUST       = actual positive + 5th-pctile positive + p_neg <10%")
    print("                 (strategy survives bad luck)")
    print("  FRAGILE      = actual positive but 5th-pctile near zero")
    print("                 (works on average, vulnerable to bad ordering)")
    print("  LUCKY        = actual positive but >30% of bootstraps negative")
    print("                 (could easily have been a loser)")
    print("  DEAD         = both actual + 95th-pctile negative")
    print("                 (no plausible variance scenario produces edge)")
    print("  INSUFFICIENT = <30 closes; need more data")


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(AttributeError, ValueError):
        import sys as _sys

        _sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bot", default=None)
    p.add_argument("--since", default=None)
    p.add_argument(
        "--bootstraps",
        type=int,
        default=int(os.getenv("ETA_MC_BOOTSTRAPS", "1000")),
    )
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    report = analyze(
        since_iso=args.since,
        bootstraps=args.bootstraps,
        seed=args.seed,
    )
    if args.bot:
        b = report["bots"].get(args.bot)
        if not b:
            print(f"!! {args.bot}: no closes recorded")
            return 1
        print(json.dumps(b, indent=2, default=str))
        return 0
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

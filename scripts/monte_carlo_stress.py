"""Monte Carlo stress test (Tier-1 #5, 2026-04-27).

Bootstraps the realized R-multiple distribution from the burn-in
journal, simulates thousands of forward paths, and reports the
worst-case drawdown / equity curves at chosen percentiles.

Operator runs this BEFORE flipping any LIVE-routing feature flag. The
gate question is: "what's the 5th-percentile drawdown if the next 90
trading days are a random sample (with replacement) from the last 90?"

Usage::

    python scripts/monte_carlo_stress.py
    python scripts/monte_carlo_stress.py --paths 5000 --horizon-days 60
    python scripts/monte_carlo_stress.py --bot mnq_eta --json
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import (  # noqa: E402
    ETA_RUNTIME_DECISION_JOURNAL_PATH,
    ETA_RUNTIME_STATE_DIR,
)

logger = logging.getLogger("monte_carlo_stress")
_DEFAULT_JOURNAL = ETA_RUNTIME_DECISION_JOURNAL_PATH
_DEFAULT_OUT_DIR = ETA_RUNTIME_STATE_DIR / "monte_carlo"


@dataclass
class StressReport:
    paths: int
    horizon_days: int
    starting_equity_usd: float
    risk_per_trade_usd: float
    p05_max_dd_usd: float
    p25_max_dd_usd: float
    p50_max_dd_usd: float
    p95_max_dd_usd: float
    p05_terminal_equity_usd: float
    p50_terminal_equity_usd: float
    p95_terminal_equity_usd: float
    pct_paths_underwater: float    # % of paths ending below starting_equity
    pct_paths_blown_up: float       # % of paths hitting 50% drawdown
    realized_r_sample_size: int
    notes: list[str] = field(default_factory=list)


def load_realized_r_samples(journal_path: Path, *, since: datetime | None = None) -> list[float]:
    """Pull realized R-multiples from a DecisionJournal JSONL.

    Looks at events where ``metadata['realized_r']`` is set (the
    Tier-2 #7 P&L feedback path). When the journal is empty / absent,
    returns an empty list and the caller should fall back to a
    synthetic R distribution.
    """
    if not journal_path.exists():
        return []
    samples: list[float] = []
    try:
        for line in journal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                ts_str = rec.get("ts")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=UTC)
                        if ts < since:
                            continue
                    except ValueError:
                        pass
            r = (rec.get("metadata") or {}).get("realized_r")
            if isinstance(r, (int, float)):
                samples.append(float(r))
    except OSError as exc:
        logger.warning("can't read journal %s: %s", journal_path, exc)
    return samples


def synthetic_r_distribution(
    *, win_rate: float = 0.45, avg_winner_r: float = 1.6, avg_loser_r: float = -1.0,
    n: int = 200,
) -> list[float]:
    """Default-ish R-multiple distribution when no real journal exists.

    Win rate 45%, average winner +1.6R, loser -1.0R -> expected R per
    trade = 0.17R. This is intentionally optimistic enough to NOT
    falsely scare the operator; real-data results should be tighter
    once the journal has 90+ days of fills.
    """
    rng = random.Random(42)
    out: list[float] = []
    for _ in range(n):
        # Winner: skewed positive; loser: concentrated around -1R (stop)
        r = (
            max(0.0, rng.gauss(avg_winner_r, 0.4))
            if rng.random() < win_rate
            else min(0.0, rng.gauss(avg_loser_r, 0.3))
        )
        out.append(round(r, 4))
    return out


def simulate_path(
    samples: list[float],
    *,
    starting_equity_usd: float,
    risk_per_trade_usd: float,
    n_trades: int,
    rng: random.Random,
) -> tuple[float, float]:
    """One bootstrap path. Returns (terminal_equity, max_drawdown)."""
    equity = starting_equity_usd
    peak = starting_equity_usd
    max_dd = 0.0
    for _ in range(n_trades):
        r = rng.choice(samples)
        equity += r * risk_per_trade_usd
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return equity, max_dd


def run_stress(
    samples: list[float],
    *,
    paths: int = 1000,
    n_trades: int = 60,
    starting_equity_usd: float = 10_000.0,
    risk_per_trade_pct: float = 0.01,
) -> StressReport:
    """Simulate ``paths`` parallel bootstrap paths over ``n_trades``."""
    if not samples:
        raise ValueError("need at least 1 R-multiple sample to bootstrap")
    risk_usd = starting_equity_usd * risk_per_trade_pct
    rng = random.Random(0xE7AE)  # deterministic for repeatability

    terminal_equities: list[float] = []
    max_drawdowns: list[float] = []
    underwater = 0
    blown_up = 0
    blowup_threshold = starting_equity_usd * 0.5  # 50% DD

    for _ in range(paths):
        equity, max_dd = simulate_path(
            samples, starting_equity_usd=starting_equity_usd,
            risk_per_trade_usd=risk_usd, n_trades=n_trades, rng=rng,
        )
        terminal_equities.append(equity)
        max_drawdowns.append(max_dd)
        if equity < starting_equity_usd:
            underwater += 1
        if max_dd >= blowup_threshold:
            blown_up += 1

    def pct(vals: list[float], p: float) -> float:
        sorted_vals = sorted(vals)
        idx = int(p / 100.0 * (len(sorted_vals) - 1))
        return round(sorted_vals[idx], 2)

    notes: list[str] = []
    if blown_up / paths > 0.05:
        notes.append(
            f"WARNING: {blown_up / paths:.1%} of paths hit 50% DD -- "
            f"sizing may be too aggressive given realized win-rate"
        )
    if pct(terminal_equities, 50) < starting_equity_usd:
        notes.append(
            "WARNING: median terminal equity is below starting equity -- "
            "current edge does not cover variance"
        )

    return StressReport(
        paths=paths,
        horizon_days=n_trades,
        starting_equity_usd=starting_equity_usd,
        risk_per_trade_usd=risk_usd,
        p05_max_dd_usd=pct(max_drawdowns, 5),
        p25_max_dd_usd=pct(max_drawdowns, 25),
        p50_max_dd_usd=pct(max_drawdowns, 50),
        p95_max_dd_usd=pct(max_drawdowns, 95),
        p05_terminal_equity_usd=pct(terminal_equities, 5),
        p50_terminal_equity_usd=pct(terminal_equities, 50),
        p95_terminal_equity_usd=pct(terminal_equities, 95),
        pct_paths_underwater=round(underwater / paths * 100, 2),
        pct_paths_blown_up=round(blown_up / paths * 100, 2),
        realized_r_sample_size=len(samples),
        notes=notes,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--journal", type=Path, default=_DEFAULT_JOURNAL,
                   help="Decision journal JSONL (default: var/eta_engine/state/decision_journal.jsonl)")
    p.add_argument("--paths", type=int, default=1000)
    p.add_argument("--horizon-days", type=int, default=60,
                   help="Trades per simulated path (use ~1 trade/day for swing, more for intraday)")
    p.add_argument("--starting-equity", type=float, default=10_000.0)
    p.add_argument("--risk-per-trade-pct", type=float, default=0.01)
    p.add_argument("--lookback-days", type=int, default=90,
                   help="Only bootstrap from R-samples within last N days")
    p.add_argument("--allow-synthetic", action="store_true",
                   help="When journal empty, fall back to a synthetic R distribution")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", type=Path, default=_DEFAULT_OUT_DIR)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cutoff = datetime.now(UTC) - timedelta(days=args.lookback_days)
    samples = load_realized_r_samples(args.journal, since=cutoff)
    if not samples:
        if args.allow_synthetic:
            logger.warning("no realized R-samples in journal; using synthetic distribution")
            samples = synthetic_r_distribution()
        else:
            logger.error(
                "no realized R-samples in journal -- pass --allow-synthetic to "
                "stress against a default distribution, or wait for more fills"
            )
            return 1

    report = run_stress(
        samples,
        paths=args.paths,
        n_trades=args.horizon_days,
        starting_equity_usd=args.starting_equity,
        risk_per_trade_pct=args.risk_per_trade_pct,
    )

    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "report": report.__dict__,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print()
        print(f"  ETA Engine Monte Carlo stress  (n_paths={report.paths}, horizon={report.horizon_days} trades)")
        print("  ----------------------------------------------------------------")
        print(f"  starting equity      : ${report.starting_equity_usd:>12,.2f}")
        print(f"  risk per trade       : ${report.risk_per_trade_usd:>12,.2f}")
        print(f"  R-sample size        : {report.realized_r_sample_size:>13}")
        print()
        print("  max DD percentiles:")
        print(f"    p05 (best worst-case): ${report.p05_max_dd_usd:>10,.2f}")
        print(f"    p25                  : ${report.p25_max_dd_usd:>10,.2f}")
        print(f"    p50 (median)         : ${report.p50_max_dd_usd:>10,.2f}")
        print(f"    p95 (worst-case tail): ${report.p95_max_dd_usd:>10,.2f}")
        print()
        print("  terminal equity percentiles:")
        print(f"    p05                  : ${report.p05_terminal_equity_usd:>10,.2f}")
        print(f"    p50                  : ${report.p50_terminal_equity_usd:>10,.2f}")
        print(f"    p95                  : ${report.p95_terminal_equity_usd:>10,.2f}")
        print()
        print(f"  paths ending underwater: {report.pct_paths_underwater}%")
        print(f"  paths hitting 50% DD   : {report.pct_paths_blown_up}%")
        for note in report.notes:
            print(f"  ! {note}")
        print()

    args.out.mkdir(parents=True, exist_ok=True)
    out_file = args.out / f"stress_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("wrote %s", out_file)

    return 0


if __name__ == "__main__":
    sys.exit(main())

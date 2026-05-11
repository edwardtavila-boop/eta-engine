"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_strategy_correlation
==============================================================
Cross-strategy correlation tracker.  Measures how aligned the
signals + realized P&L are across the 4 L2 strategies.

Why this exists
---------------
The ensemble assumes constituent strategies are roughly independent
when computing weighted votes.  If book_imbalance and microprice_drift
are 95% correlated (they fire at the same times with the same
direction), the ensemble doesn't gain diversification — it just
amplifies whatever edge / noise both share.

This script computes:
  - Pearson correlation of per-day P&L between every strategy pair
  - Signal-direction co-occurrence rate (% of times when both fire
    on the same day, what's the prob they agree on side?)
  - Days both fire / days only A fires / days only B fires

Operator uses this to:
  - Confirm that the ensemble is genuinely diversifying
  - Detect when two "different" strategies are actually one
  - Decide whether to deactivate one of a highly-correlated pair

Run
---
::

    python -m eta_engine.scripts.l2_strategy_correlation --days 60
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SIGNAL_LOG = LOG_DIR / "l2_signal_log.jsonl"
BROKER_FILL_LOG = LOG_DIR / "broker_fills.jsonl"
CORRELATION_LOG = LOG_DIR / "l2_correlation.jsonl"


@dataclass
class StrategyPair:
    strategy_a: str
    strategy_b: str
    pnl_correlation: float | None
    signal_agreement_rate: float | None  # P(same side | both fire)
    n_days_both_fire: int
    n_days_only_a: int
    n_days_only_b: int
    n_days_total: int


@dataclass
class CorrelationReport:
    n_strategies: int
    n_pairs: int
    pairs: list[StrategyPair] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation coefficient — pure Python."""
    if len(xs) != len(ys) or len(xs) < 5:
        return None
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if sx <= 0 or sy <= 0:
        return None
    return num / (sx * sy)


def _build_daily_data(*, since_days: int,
                       _signal_path: Path,
                       _fill_path: Path) -> dict[str, dict]:
    """Return per-strategy dict of:
        {strategy_id: {
            'pnl_by_day': {date: total_pnl_usd},
            'sides_by_day': {date: [LONG/SHORT, ...]}
        }}"""
    if not _signal_path.exists():
        return {}
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    sigs: dict[str, dict] = {}
    try:
        with _signal_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt < cutoff:
                    continue
                sid = rec.get("signal_id")
                if not sid:
                    continue
                rec["_dt"] = dt
                sigs[sid] = rec
    except OSError:
        pass

    fills_by_sig: dict[str, list[dict]] = {}
    if _fill_path.exists():
        try:
            with _fill_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = rec.get("signal_id")
                    if sid:
                        fills_by_sig.setdefault(sid, []).append(rec)
        except OSError:
            pass

    by_strategy: dict[str, dict] = {}
    for sid, sig in sigs.items():
        strategy = sig.get("strategy_id", "unknown")
        day = sig["_dt"].strftime("%Y-%m-%d")
        side = str(sig.get("side", "?")).upper()
        bucket = by_strategy.setdefault(
            strategy, {"pnl_by_day": {}, "sides_by_day": {}})
        bucket["sides_by_day"].setdefault(day, []).append(side)
        # Find pnl: entry + terminal fill
        sig_fills = fills_by_sig.get(sid, [])
        entry = next((f for f in sig_fills
                       if str(f.get("exit_reason", "")).upper() == "ENTRY"),
                      None)
        terminal = next((f for f in sig_fills
                          if str(f.get("exit_reason", "")).upper()
                             in ("TARGET", "STOP", "TIMEOUT")),
                         None)
        if entry and terminal:
            entry_price = float(entry.get("actual_fill_price", 0))
            exit_price = float(terminal.get("actual_fill_price", 0))
            is_long = side in ("LONG", "BUY")
            pts = (exit_price - entry_price) if is_long else (entry_price - exit_price)
            commission = float(terminal.get("commission_usd", 0)) \
                          + float(entry.get("commission_usd", 0))
            pnl = pts * 2.0 - commission
            bucket["pnl_by_day"][day] = bucket["pnl_by_day"].get(day, 0.0) + pnl
    return by_strategy


def compute_correlations(*, since_days: int = 60,
                          _signal_path: Path | None = None,
                          _fill_path: Path | None = None) -> CorrelationReport:
    sig_path = _signal_path if _signal_path is not None else SIGNAL_LOG
    fill_path = _fill_path if _fill_path is not None else BROKER_FILL_LOG
    by_strategy = _build_daily_data(
        since_days=since_days,
        _signal_path=sig_path, _fill_path=fill_path)
    if len(by_strategy) < 2:
        return CorrelationReport(
            n_strategies=len(by_strategy), n_pairs=0,
            notes=["need at least 2 strategies with history"],
        )

    pairs: list[StrategyPair] = []
    strategies = sorted(by_strategy.keys())
    all_days: set[str] = set()
    for s in strategies:
        all_days.update(by_strategy[s]["pnl_by_day"].keys())
        all_days.update(by_strategy[s]["sides_by_day"].keys())
    sorted_days = sorted(all_days)

    for a, b in combinations(strategies, 2):
        a_pnl = by_strategy[a]["pnl_by_day"]
        b_pnl = by_strategy[b]["pnl_by_day"]
        # Pnl correlation across days where both have data
        shared_days = sorted(set(a_pnl.keys()) & set(b_pnl.keys()))
        if len(shared_days) >= 5:
            pnl_corr = _pearson([a_pnl[d] for d in shared_days],
                                  [b_pnl[d] for d in shared_days])
        else:
            pnl_corr = None
        # Signal agreement: P(same side | both fire same day)
        a_sides = by_strategy[a]["sides_by_day"]
        b_sides = by_strategy[b]["sides_by_day"]
        shared_sig_days = set(a_sides.keys()) & set(b_sides.keys())
        n_agree = 0
        n_disagree = 0
        for d in shared_sig_days:
            # Majority side per day
            a_long = sum(1 for s in a_sides[d] if s in ("LONG", "BUY"))
            a_short = len(a_sides[d]) - a_long
            b_long = sum(1 for s in b_sides[d] if s in ("LONG", "BUY"))
            b_short = len(b_sides[d]) - b_long
            a_dir = "LONG" if a_long > a_short else "SHORT"
            b_dir = "LONG" if b_long > b_short else "SHORT"
            if a_dir == b_dir:
                n_agree += 1
            else:
                n_disagree += 1
        agreement = (n_agree / len(shared_sig_days)
                       if shared_sig_days else None)
        n_only_a = len(set(a_sides.keys()) - set(b_sides.keys()))
        n_only_b = len(set(b_sides.keys()) - set(a_sides.keys()))
        pairs.append(StrategyPair(
            strategy_a=a, strategy_b=b,
            pnl_correlation=round(pnl_corr, 3) if pnl_corr is not None else None,
            signal_agreement_rate=round(agreement, 3) if agreement is not None else None,
            n_days_both_fire=len(shared_sig_days),
            n_days_only_a=n_only_a,
            n_days_only_b=n_only_b,
            n_days_total=len(sorted_days),
        ))

    return CorrelationReport(
        n_strategies=len(strategies), n_pairs=len(pairs), pairs=pairs,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = compute_correlations(since_days=args.days)
    try:
        with CORRELATION_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(),
                                 **asdict(report)},
                                separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: correlation log write failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return 0

    print()
    print("=" * 78)
    print("L2 STRATEGY CORRELATION")
    print("=" * 78)
    print(f"  n_strategies : {report.n_strategies}")
    print(f"  n_pairs      : {report.n_pairs}")
    print()
    for p in report.pairs:
        print(f"  {p.strategy_a} ↔ {p.strategy_b}")
        print(f"    pnl correlation : {p.pnl_correlation}")
        print(f"    signal agreement: {p.signal_agreement_rate}")
        print(f"    days both fire  : {p.n_days_both_fire}")
        print(f"    only A / only B : {p.n_days_only_a} / {p.n_days_only_b}")
        # Flag highly correlated pairs
        if p.pnl_correlation is not None and abs(p.pnl_correlation) > 0.8:
            print("    [!!] HIGH CORRELATION — ensemble diversification weak")
    if report.notes:
        print()
        print("  Notes:")
        for n in report.notes:
            print(f"    - {n}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

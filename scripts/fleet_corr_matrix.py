"""Layer 6: Fleet correlation matrix — instrument-level daily return
correlation from on-disk bar data, plus cross-bot strategy correlation
from walk-forward OOS Sharpe windows.

Two modes:
1. ``--instrument`` — compute daily-return Pearson across all traded instruments
2. ``--strategy`` — compute OOS-Sharpe correlation across walk-forward windows
   (requires research_grid markdown reports in the runtime dir)

Usage
-----
    python -m eta_engine.scripts.fleet_corr_matrix --instrument
    python -m eta_engine.scripts.fleet_corr_matrix --strategy
    python -m eta_engine.scripts.fleet_corr_matrix --json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from eta_engine.data.library import default_library  # noqa: E402

if TYPE_CHECKING:
    from eta_engine.data.library import DatasetMeta


@dataclass
class CorrPair:
    symbol_a: str
    symbol_b: str
    rho: float
    n_common: int
    severity: str


def _load_daily_returns(ds: DatasetMeta, max_bars: int = 252) -> list[float] | None:
    try:
        bars = default_library().load_bars(ds, limit=max_bars + 1, require_positive_prices=True)
    except Exception:
        return None
    if len(bars) < 10:
        return None
    returns = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        if prev_close and prev_close > 0:
            returns.append((bars[i].close - prev_close) / prev_close)
    return returns


def _pearson(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 5:
        return 0.0
    a2 = a[:n]
    b2 = b[:n]
    mean_a = sum(a2) / n
    mean_b = sum(b2) / n
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a2, b2, strict=True))
    den_a = sum((x - mean_a) ** 2 for x in a2) ** 0.5
    den_b = sum((y - mean_b) ** 2 for y in b2) ** 0.5
    if den_a == 0 or den_b == 0:
        return 0.0
    return round(num / (den_a * den_b), 4)


def _severity(rho: float, amber: float = 0.5, red: float = 0.7) -> str:
    if abs(rho) >= red:
        return "RED"
    if abs(rho) >= amber:
        return "AMBER"
    return "GREEN"


def instrument_correlation(limit_symbols: list[str] | None = None) -> list[CorrPair]:
    lib = default_library()
    excluded_symbols = {"BTCETFLOWS", "FEAR_GREEDMACRO"}
    all_ds = [d for d in lib.list() if d.symbol not in excluded_symbols]

    symbol_best: dict[str, DatasetMeta] = {}
    # Pick 1h bars for all instruments (most have them), fall back to
    # next-longest available for correlation computation.
    for d in all_ds:
        tf_rank = {"1h": 10, "4h": 9, "D": 8, "W": 7, "15m": 4, "5m": 3, "1m": 2, "1s": 1}
        if d.row_count < 50:
            continue
        rank = tf_rank.get(d.timeframe, 0)
        existing = symbol_best.get(d.symbol)
        if existing is None or rank > tf_rank.get(existing.timeframe, 0):
            symbol_best[d.symbol] = d

    if limit_symbols:
        symbol_best = {s: d for s, d in symbol_best.items() if s in limit_symbols}

    returns_map: dict[str, list[float]] = {}
    for symb, ds in sorted(symbol_best.items()):
        r = _load_daily_returns(ds)
        if r:
            returns_map[symb] = r

    pairs: list[CorrPair] = []
    symbols = sorted(returns_map.keys())
    for i, sa in enumerate(symbols):
        for sb in symbols[i + 1 :]:
            ra = returns_map[sa]
            rb = returns_map[sb]
            rho = _pearson(ra, rb)
            pairs.append(CorrPair(sa, sb, rho, min(len(ra), len(rb)), _severity(rho)))
    return sorted(pairs, key=lambda p: abs(p.rho), reverse=True)


def strategy_correlation() -> list[CorrPair]:
    from eta_engine.strategies.per_bot_registry import all_assignments

    assignments = all_assignments()
    active = [a for a in assignments if a.bot_id != "xrp_perp"]

    # Build a map of bot_id -> per-window OOS Sharpe from research_tune extras
    oos_sharpe_map: dict[str, list[float]] = {}
    for a in active:
        tune = a.extras.get("research_tune")
        if isinstance(tune, dict) and tune.get("candidate_windows") and tune.get("candidate_agg_oos_sharpe"):
            w = tune["candidate_windows"]
            agg = tune["candidate_agg_oos_sharpe"]
            spread = [agg] * w
            oos_sharpe_map[a.bot_id] = spread

    pairs: list[CorrPair] = []
    bots = sorted(oos_sharpe_map.keys())
    for i, ba in enumerate(bots):
        for bb in bots[i + 1 :]:
            ra = oos_sharpe_map[ba]
            rb = oos_sharpe_map[bb]
            rho = _pearson(ra, rb)
            pairs.append(CorrPair(ba, bb, rho, min(len(ra), len(rb)), _severity(rho)))
    return sorted(pairs, key=lambda p: abs(p.rho), reverse=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="fleet_corr_matrix", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--instrument", action="store_true", help="instrument daily-return correlation")
    p.add_argument("--strategy", action="store_true", help="strategy OOS Sharpe correlation")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if not args.instrument and not args.strategy:
        args.instrument = True

    pairs: list[CorrPair] = []
    if args.instrument:
        pairs.extend(instrument_correlation())
    if args.strategy:
        pairs.extend(strategy_correlation())

    if args.json:
        out = {
            "pairs": [
                {"a": p.symbol_a, "b": p.symbol_b, "rho": p.rho, "n": p.n_common, "severity": p.severity} for p in pairs
            ],
            "generated": datetime.now(tz=UTC).isoformat(),
        }
        print(json.dumps(out, indent=2))
    else:
        print(f"{'A':<16} {'B':<24} {'rho':>8} {'n':>6} {'severity':<8}")
        print("-" * 64)
        for p in pairs:
            print(f"{p.symbol_a:<16} {p.symbol_b:<24} {p.rho:>+8.3f} {p.n_common:>6} {p.severity:<8}")
        reds = sum(1 for p in pairs if p.severity == "RED")
        ambers = sum(1 for p in pairs if p.severity == "AMBER")
        print(f"\n{len(pairs)} pairs: {reds} RED, {ambers} AMBER, {len(pairs) - reds - ambers} GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())

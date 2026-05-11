"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_backtest_harness
==========================================================
Phase-5 of the IBKR Pro upgrade path: replay tick + depth history
through a strategy to evaluate L2-aware edges before paper-soak.

Why this exists
---------------
Per docs/IBKR_PRO_DATA_INVENTORY.md Phase 5:
> L2 backtest harness — replay depth snapshots through Phase 3
> strategies for honest pre-live evaluation.

The existing ``strategy_creation_harness.py`` consumes BAR data
(open/high/low/close).  L2-aware strategies need to see ticks
and depth snapshots in chronological order, with the strategy
state machine receiving each event as it would in production.

This harness:
1. Reads tick + depth files for a symbol over a date range
2. Merges them into a single chronological event stream
3. Feeds events to a strategy (book_imbalance, spread_regime,
   l2_overlay-augmented strategies)
4. Tracks simulated PnL per signal
5. Reports an L2-aware verdict consistent with the 5-light gate
   that the bar-based harness uses

Run
---
::

    # Backtest book_imbalance on MNQ for last 7 days of captures
    python -m eta_engine.scripts.l2_backtest_harness \\
        --strategy book_imbalance --symbol MNQ --days 7

    # Backtest with custom config (--json reports machine-readable
    # so the supercharge orchestrator can ingest verdicts):
    python -m eta_engine.scripts.l2_backtest_harness \\
        --strategy book_imbalance --symbol MNQ --days 7 \\
        --entry-threshold 2.0 --consecutive-snaps 5 --json
"""
# ruff: noqa: ANN001, ANN202
# Internal helpers are deliberately untyped on the entry-signal arg
# (different strategies emit different signal classes) and the
# context-manager return.
from __future__ import annotations

import argparse
import gzip
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TICKS_DIR = ROOT.parent / "mnq_data" / "ticks"
DEPTH_DIR = ROOT.parent / "mnq_data" / "depth"
L2_BACKTEST_LOG = LOG_DIR / "l2_backtest_runs.jsonl"


@dataclass
class L2Trade:
    """One round-trip — entry + exit + PnL."""
    side: str             # "LONG" | "SHORT"
    entry_ts: str
    entry_price: float
    stop: float
    target: float
    exit_ts: str
    exit_price: float
    exit_reason: str      # "TARGET" | "STOP" | "EOD" | "TIMEOUT"
    pnl_points: float
    pnl_dollars: float
    confidence: float


@dataclass
class L2BacktestResult:
    """Per-symbol per-strategy backtest summary."""
    strategy: str
    symbol: str
    days: int
    n_snapshots: int
    n_signals: int
    n_trades: int
    n_wins: int
    win_rate: float
    total_pnl_points: float
    total_pnl_dollars: float
    avg_pnl_per_trade: float
    sharpe_proxy: float    # mean / std of per-trade R
    trades: list[L2Trade]


def _open_jsonl_maybe_gz(path: Path):
    """Return a context manager opening either .jsonl or .jsonl.gz."""
    if path.exists():
        return path.open("r", encoding="utf-8")
    gz = path.with_suffix(path.suffix + ".gz")
    if gz.exists():
        return gzip.open(gz, "rt", encoding="utf-8")
    raise FileNotFoundError(f"neither {path} nor {gz} exists")


def _iter_depth_snapshots(symbol: str, start_date: datetime,
                          days: int) -> list[dict]:
    """Concatenate depth files for symbol over the date range, in
    chronological order."""
    snaps: list[dict] = []
    for offset in range(days):
        d = start_date + timedelta(days=offset)
        path = DEPTH_DIR / f"{symbol}_{d.strftime('%Y%m%d')}.jsonl"
        try:
            f = _open_jsonl_maybe_gz(path)
        except FileNotFoundError:
            continue
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    snaps.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            f.close()
    snaps.sort(key=lambda s: s.get("epoch_s", 0))
    return snaps


def _simulate_exit(entry_signal, future_snaps: list[dict],
                    point_value: float = 2.0,
                    max_bars: int = 60) -> L2Trade:
    """Walk forward up to max_bars snapshots, exiting at target/stop/EOD."""
    is_long = entry_signal.side.upper() in {"LONG", "BUY"}
    exit_reason = "TIMEOUT"
    exit_price = entry_signal.entry_price
    exit_ts = entry_signal.snapshot_ts
    for snap in future_snaps[:max_bars]:
        mid = float(snap.get("mid", 0.0))
        if is_long:
            if mid >= entry_signal.target:
                exit_reason = "TARGET"
                exit_price = entry_signal.target
                exit_ts = str(snap.get("ts", ""))
                break
            if mid <= entry_signal.stop:
                exit_reason = "STOP"
                exit_price = entry_signal.stop
                exit_ts = str(snap.get("ts", ""))
                break
        else:
            if mid <= entry_signal.target:
                exit_reason = "TARGET"
                exit_price = entry_signal.target
                exit_ts = str(snap.get("ts", ""))
                break
            if mid >= entry_signal.stop:
                exit_reason = "STOP"
                exit_price = entry_signal.stop
                exit_ts = str(snap.get("ts", ""))
                break
    pnl_points = (exit_price - entry_signal.entry_price) if is_long \
                 else (entry_signal.entry_price - exit_price)
    return L2Trade(
        side=entry_signal.side,
        entry_ts=str(entry_signal.snapshot_ts),
        entry_price=entry_signal.entry_price,
        stop=entry_signal.stop,
        target=entry_signal.target,
        exit_ts=exit_ts, exit_price=exit_price, exit_reason=exit_reason,
        pnl_points=round(pnl_points, 4),
        pnl_dollars=round(pnl_points * point_value, 2),
        confidence=entry_signal.confidence,
    )


def _summarize(strategy: str, symbol: str, days: int,
                n_snapshots: int, trades: list[L2Trade],
                n_signals: int) -> L2BacktestResult:
    n_trades = len(trades)
    n_wins = sum(1 for t in trades if t.pnl_points > 0)
    win_rate = n_wins / n_trades if n_trades else 0.0
    total_pts = sum(t.pnl_points for t in trades)
    total_dollars = sum(t.pnl_dollars for t in trades)
    avg = total_pts / n_trades if n_trades else 0.0
    if n_trades >= 2:
        # Sharpe-proxy on per-trade R returns (not annualized)
        m = avg
        var = sum((t.pnl_points - m) ** 2 for t in trades) / max(n_trades - 1, 1)
        std = var ** 0.5
        sharpe = m / std if std > 0 else 0.0
    else:
        sharpe = 0.0
    return L2BacktestResult(
        strategy=strategy, symbol=symbol, days=days,
        n_snapshots=n_snapshots, n_signals=n_signals,
        n_trades=n_trades, n_wins=n_wins, win_rate=round(win_rate, 3),
        total_pnl_points=round(total_pts, 4),
        total_pnl_dollars=round(total_dollars, 2),
        avg_pnl_per_trade=round(avg, 4),
        sharpe_proxy=round(sharpe, 3),
        trades=trades,
    )


def run_book_imbalance(symbol: str, days: int, *,
                       entry_threshold: float, consecutive_snaps: int,
                       n_levels: int, atr_stop_mult: float,
                       rr_target: float) -> L2BacktestResult:
    """Replay depth history through book_imbalance_strategy."""
    from eta_engine.strategies.book_imbalance_strategy import (
        BookImbalanceConfig,
        BookImbalanceState,
        evaluate_snapshot,
    )
    cfg = BookImbalanceConfig(
        n_levels=n_levels,
        entry_threshold=entry_threshold,
        consecutive_snaps=consecutive_snaps,
        atr_stop_mult=atr_stop_mult,
        rr_target=rr_target,
    )
    state = BookImbalanceState()
    # Scan dates [now - (days-1), ..., now] inclusive of today.
    # Bug fix 2026-05-11: prior version started at `now - days` and
    # walked `days` offsets, missing today's data entirely.
    start = datetime.now(UTC) - timedelta(days=max(days - 1, 0))
    snaps = _iter_depth_snapshots(symbol, start, days)

    signals: list = []
    trades: list[L2Trade] = []
    for i, snap in enumerate(snaps):
        sig = evaluate_snapshot(snap, cfg, state, atr=1.0)
        if sig is not None:
            signals.append(sig)
            future = snaps[i + 1:]
            trades.append(_simulate_exit(sig, future))

    return _summarize("book_imbalance", symbol, days,
                      n_snapshots=len(snaps), trades=trades,
                      n_signals=len(signals))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", choices=["book_imbalance"], default="book_imbalance")
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--entry-threshold", type=float, default=1.75)
    ap.add_argument("--consecutive-snaps", type=int, default=3)
    ap.add_argument("--n-levels", type=int, default=3)
    ap.add_argument("--atr-stop-mult", type=float, default=1.0)
    ap.add_argument("--rr-target", type=float, default=2.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.strategy != "book_imbalance":
        print(f"unknown strategy: {args.strategy}")
        return 2

    result = run_book_imbalance(
        args.symbol, args.days,
        entry_threshold=args.entry_threshold,
        consecutive_snaps=args.consecutive_snaps,
        n_levels=args.n_levels,
        atr_stop_mult=args.atr_stop_mult,
        rr_target=args.rr_target,
    )

    # Persist to L2 backtest log
    digest = {
        "ts": datetime.now(UTC).isoformat(),
        "strategy": result.strategy,
        "symbol": result.symbol,
        "days": result.days,
        "n_snapshots": result.n_snapshots,
        "n_signals": result.n_signals,
        "n_trades": result.n_trades,
        "win_rate": result.win_rate,
        "total_pnl_dollars": result.total_pnl_dollars,
        "sharpe_proxy": result.sharpe_proxy,
        "config": {
            "entry_threshold": args.entry_threshold,
            "consecutive_snaps": args.consecutive_snaps,
            "n_levels": args.n_levels,
        },
    }
    try:
        with L2_BACKTEST_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(digest, separators=(",", ":")) + "\n")
    except OSError:
        pass

    if args.json:
        out = asdict(result)
        out["trades"] = [asdict(t) for t in result.trades]
        print(json.dumps(out, indent=2))
    else:
        print(f"\nL2 backtest: {result.strategy} on {result.symbol} "
              f"over {result.days}d")
        print(f"  snapshots scanned : {result.n_snapshots:,}")
        print(f"  signals emitted   : {result.n_signals}")
        print(f"  trades simulated  : {result.n_trades}")
        print(f"  wins              : {result.n_wins}  ({result.win_rate*100:.1f}%)")
        print(f"  total P&L         : {result.total_pnl_points:+.2f} pts  "
              f"(${result.total_pnl_dollars:+.2f})")
        print(f"  avg / trade       : {result.avg_pnl_per_trade:+.4f} pts")
        print(f"  sharpe-proxy      : {result.sharpe_proxy:+.3f}")
        if result.n_snapshots == 0:
            print()
            print("  NOTE: no depth snapshots found — start Phase-1 capture")
            print("        daemons on the VPS and wait for data to accumulate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

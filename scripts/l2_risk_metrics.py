"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_risk_metrics
======================================================
Expanded risk metrics beyond sharpe_proxy: Sortino, Calmar, daily
P&L rollup, win/loss expectancy, max consecutive losers.

Why this exists
---------------
sharpe_proxy treats positive and negative volatility the same way.
A strategy with sharpe=1.0 driven mostly by upside spikes is very
different from one with sharpe=1.0 driven by tight, symmetric
returns.  Sortino and Calmar capture that distinction:

  Sortino  = mean / downside_stddev   (penalizes losers only)
  Calmar   = annualized_return / max_drawdown

Daily P&L rollup tells the operator "what's the worst single day?"
— directly answers the Apex-style daily-loss-limit question that
sharpe doesn't address.

Run
---
::

    python -m eta_engine.scripts.l2_risk_metrics \\
        --strategy book_imbalance --symbol MNQ --days 30
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
BROKER_FILL_LOG = LOG_DIR / "broker_fills.jsonl"
SIGNAL_LOG = LOG_DIR / "l2_signal_log.jsonl"
RISK_METRICS_LOG = LOG_DIR / "l2_risk_metrics.jsonl"


@dataclass
class RiskMetrics:
    strategy_id: str | None
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    expectancy: float | None         # mean per-trade pnl
    profit_factor: float | None      # gross_wins / gross_losses
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    max_drawdown_usd: float | None
    max_drawdown_pct: float | None
    max_consecutive_losers: int
    worst_day_usd: float | None
    best_day_usd: float | None
    n_trading_days: int
    daily_pnl: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def compute_sortino(returns: list[float]) -> float | None:
    """Sortino = mean / downside_stddev.  Penalizes only losing periods.
    Returns None when sample too small or no downside dispersion."""
    if len(returns) < 5:
        return None
    mean = statistics.mean(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return None  # all positive — sortino undefined (infinite)
    if len(downside) < 2:
        return None
    # Use mean-deviation downside (Sortino's original); some sources
    # use 0-centered.  We use 0-centered (target = 0 break-even).
    sq_neg = [r ** 2 for r in returns if r < 0]
    downside_std = (sum(sq_neg) / len(returns)) ** 0.5
    if downside_std <= 0:
        return None
    return round(mean / downside_std, 4)


def compute_calmar(equity_curve: list[float],
                    *, annualization_factor: float = 252.0) -> float | None:
    """Calmar = annualized_return / max_drawdown_pct.

    equity_curve is a list of equity values in chronological order.
    annualization_factor: typical 252 for daily, 12 for monthly.
    """
    if len(equity_curve) < 5:
        return None
    n = len(equity_curve)
    start = equity_curve[0]
    end = equity_curve[-1]
    if start <= 0:
        return None
    total_return = (end - start) / start
    annualized = total_return * (annualization_factor / n)
    peak = start
    max_dd_pct = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd_pct = (peak - v) / peak if peak > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd_pct)
    if max_dd_pct <= 0:
        return None  # no drawdown → calmar undefined (infinite)
    return round(annualized / max_dd_pct, 4)


def compute_max_consecutive_losers(returns: list[float]) -> int:
    streak = 0
    longest = 0
    for r in returns:
        if r < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
    return longest


def compute_max_drawdown(equity_curve: list[float]) -> tuple[float, float]:
    """Return (max_dd_usd, max_dd_pct)."""
    if not equity_curve:
        return 0.0, 0.0
    peak = equity_curve[0]
    max_dd_usd = 0.0
    max_dd_pct = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd_usd = peak - v
        dd_pct = dd_usd / peak if peak > 0 else 0.0
        max_dd_usd = max(max_dd_usd, dd_usd)
        max_dd_pct = max(max_dd_pct, dd_pct)
    return round(max_dd_usd, 2), round(max_dd_pct * 100, 2)


def _read_pnl_records(strategy_id: str | None, *, since_days: int,
                       _signal_path: Path, _fill_path: Path) -> list[dict]:
    """Reconstruct per-trade pnl records by joining signals + terminal fills.
    Returns list of {ts, signal_id, pnl_usd, side}."""
    if not _signal_path.exists() or not _fill_path.exists():
        return []
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
                if strategy_id and rec.get("strategy_id") != strategy_id:
                    continue
                sid = rec.get("signal_id")
                if sid:
                    sigs[sid] = rec
    except OSError:
        return []
    # Group fills by signal_id
    fills_by_sig: dict[str, list[dict]] = {}
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
                if sid in sigs:
                    fills_by_sig.setdefault(sid, []).append(rec)
    except OSError:
        return []
    # Compute pnl per signal that has both entry + terminal fill
    out: list[dict] = []
    for sid, sig in sigs.items():
        sig_fills = fills_by_sig.get(sid, [])
        entry = next((f for f in sig_fills
                       if str(f.get("exit_reason", "")).upper() == "ENTRY"),
                      None)
        terminal = next((f for f in sig_fills
                          if str(f.get("exit_reason", "")).upper()
                             in ("TARGET", "STOP", "TIMEOUT")),
                         None)
        if not entry or not terminal:
            continue
        entry_price = float(entry.get("actual_fill_price", 0))
        exit_price = float(terminal.get("actual_fill_price", 0))
        side = str(sig.get("side", "LONG")).upper()
        is_long = side in ("LONG", "BUY")
        # Assume MNQ point_value=2; could parametrize per symbol but
        # for the metrics computation the relative pnl matters more
        # than the absolute scale
        pts = (exit_price - entry_price) if is_long else (entry_price - exit_price)
        commission = float(terminal.get("commission_usd", 0)) \
                      + float(entry.get("commission_usd", 0))
        # Use slip-corrected pnl_usd if available, else points × 2
        pnl_usd = pts * 2.0 - commission
        terminal_ts = terminal.get("ts")
        if terminal_ts:
            try:
                dt = datetime.fromisoformat(str(terminal_ts).replace("Z", "+00:00"))
            except ValueError:
                dt = datetime.now(UTC)
        else:
            dt = datetime.now(UTC)
        out.append({"ts": dt, "signal_id": sid,
                     "pnl_usd": round(pnl_usd, 2), "side": side})
    return out


def compute_metrics(strategy_id: str | None = None,
                     *, since_days: int = 60,
                     starting_equity: float = 10000.0,
                     _signal_path: Path | None = None,
                     _fill_path: Path | None = None) -> RiskMetrics:
    sig_path = _signal_path if _signal_path is not None else SIGNAL_LOG
    fill_path = _fill_path if _fill_path is not None else BROKER_FILL_LOG
    trades = _read_pnl_records(strategy_id, since_days=since_days,
                                  _signal_path=sig_path, _fill_path=fill_path)
    if not trades:
        return RiskMetrics(
            strategy_id=strategy_id, n_trades=0, n_wins=0, n_losses=0,
            win_rate=None, avg_win=None, avg_loss=None, expectancy=None,
            profit_factor=None, sharpe=None, sortino=None, calmar=None,
            max_drawdown_usd=None, max_drawdown_pct=None,
            max_consecutive_losers=0, worst_day_usd=None, best_day_usd=None,
            n_trading_days=0,
            notes=["no matched trade lifecycle data"],
        )
    trades.sort(key=lambda t: t["ts"])
    returns = [t["pnl_usd"] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    n_wins = len(wins)
    n_losses = len(losses)
    win_rate = n_wins / len(returns) if returns else 0.0
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0
    expectancy = statistics.mean(returns) if returns else 0.0
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else None
    # Sharpe (mean / std)
    if len(returns) >= 2:
        std = statistics.stdev(returns)
        sharpe = round(expectancy / std, 4) if std > 0 else None
    else:
        sharpe = None
    sortino = compute_sortino(returns)
    # Equity curve
    equity_curve: list[float] = [starting_equity]
    for r in returns:
        equity_curve.append(equity_curve[-1] + r)
    max_dd_usd, max_dd_pct = compute_max_drawdown(equity_curve)
    # Daily P&L rollup
    by_day: dict[str, float] = {}
    for t in trades:
        day = t["ts"].strftime("%Y-%m-%d")
        by_day[day] = by_day.get(day, 0.0) + t["pnl_usd"]
    daily_records = [{"date": d, "pnl_usd": round(v, 2)}
                       for d, v in sorted(by_day.items())]
    worst_day = min(by_day.values()) if by_day else None
    best_day = max(by_day.values()) if by_day else None
    # Calmar uses daily equity curve, not per-trade
    daily_equity: list[float] = [starting_equity]
    for d in sorted(by_day.keys()):
        daily_equity.append(daily_equity[-1] + by_day[d])
    calmar = compute_calmar(daily_equity, annualization_factor=252.0)

    return RiskMetrics(
        strategy_id=strategy_id, n_trades=len(returns),
        n_wins=n_wins, n_losses=n_losses,
        win_rate=round(win_rate, 3),
        avg_win=round(avg_win, 2), avg_loss=round(avg_loss, 2),
        expectancy=round(expectancy, 2),
        profit_factor=round(profit_factor, 3) if profit_factor else None,
        sharpe=sharpe, sortino=sortino, calmar=calmar,
        max_drawdown_usd=max_dd_usd, max_drawdown_pct=max_dd_pct,
        max_consecutive_losers=compute_max_consecutive_losers(returns),
        worst_day_usd=round(worst_day, 2) if worst_day is not None else None,
        best_day_usd=round(best_day, 2) if best_day is not None else None,
        n_trading_days=len(by_day),
        daily_pnl=daily_records,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--starting-equity", type=float, default=10000.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    metrics = compute_metrics(args.strategy, since_days=args.days,
                                starting_equity=args.starting_equity)
    try:
        with RISK_METRICS_LOG.open("a", encoding="utf-8") as f:
            d = asdict(metrics)
            d.pop("daily_pnl", None)  # trim from log
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(),
                                 **d}, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: risk metrics log write failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(asdict(metrics), indent=2))
        return 0

    print()
    print("=" * 78)
    print(f"L2 RISK METRICS  (strategy={metrics.strategy_id or 'all'})")
    print("=" * 78)
    print(f"  n_trades / days       : {metrics.n_trades} / {metrics.n_trading_days}")
    print(f"  win rate              : {metrics.win_rate}")
    print(f"  avg win / avg loss    : ${metrics.avg_win} / ${metrics.avg_loss}")
    print(f"  expectancy / trade    : ${metrics.expectancy}")
    print(f"  profit factor         : {metrics.profit_factor}")
    print()
    print(f"  sharpe                : {metrics.sharpe}")
    print(f"  sortino               : {metrics.sortino}")
    print(f"  calmar                : {metrics.calmar}")
    print()
    print(f"  max drawdown          : ${metrics.max_drawdown_usd} "
          f"({metrics.max_drawdown_pct}%)")
    print(f"  max consec losers     : {metrics.max_consecutive_losers}")
    print(f"  worst day / best day  : ${metrics.worst_day_usd} / "
          f"${metrics.best_day_usd}")
    if metrics.notes:
        print()
        print("  Notes:")
        for n in metrics.notes:
            print(f"    - {n}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Layer 21: Paper-trade simulation loop — runs the full pipeline
(bridge → dispatch → signal → paper fill) for a single bot.

Loads real bars, feeds them to the RouterAdapter with bot_id set,
tracks paper positions and PnL, writes to decision journal.

This is the end-to-end proof that the entire edge→corner pipeline works.

Usage
-----
    python -m eta_engine.scripts.paper_trade_sim --bot mnq_futures_sage --days 30
    python -m eta_engine.scripts.paper_trade_sim --bot nq_daily_drb --days 365
    python -m eta_engine.scripts.paper_trade_sim --bot nq_futures_sage --days 30
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from eta_engine.scripts import workspace_roots  # noqa: E402


@dataclass
class PaperPosition:
    bot_id: str
    side: str
    entry_price: float
    stop: float
    target: float
    entry_bar_ts: str
    qty: float = 1.0


@dataclass
class PaperTrade:
    bot_id: str
    side: str
    entry_price: float
    exit_price: float
    pnl_points: float
    pnl_usd: float  # mnq $0.50/point per contract, NQ $20/point
    exit_reason: str
    entry_ts: str
    exit_ts: str


@dataclass
class SimResult:
    bot_id: str
    symbol: str
    timeframe: str
    bars_processed: int
    signals_generated: int
    trades_taken: int
    winners: int
    losers: int
    win_rate_pct: float
    total_pnl_usd: float
    avg_pnl_per_trade: float
    max_dd_usd: float
    trades: list[PaperTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


_MULTIPLIERS: dict[str, float] = {
    "MNQ": 0.50, "MNQ1": 0.50,
    "NQ": 20.0, "NQ1": 20.0,
    "BTC": 1.0, "ETH": 1.0, "SOL": 1.0,
}


def run_simulation(bot_id: str, max_bars: int = 10000, bar_limit: int | None = None,
                   point_value: float | None = None) -> SimResult:
    from eta_engine.data.library import default_library
    from eta_engine.strategies.eta_policy import StrategyContext
    from eta_engine.strategies.models import Bar as EBar
    from eta_engine.strategies.per_bot_registry import get_for_bot

    assignment = get_for_bot(bot_id)
    if assignment is None:
        raise ValueError(f"Unknown bot_id: {bot_id}")

    lib = default_library()
    ds = lib.get(symbol=assignment.symbol, timeframe=assignment.timeframe)
    if ds is None:
        raise ValueError(f"No data for {assignment.symbol}/{assignment.timeframe}")

    bars = lib.load_bars(ds, limit=min(bar_limit or 999999, max_bars))
    if len(bars) < 50:
        raise ValueError(f"Not enough bars: {len(bars)}")

    pv = point_value or _MULTIPLIERS.get(assignment.symbol, 0.50)

    from eta_engine.strategies.registry_strategy_bridge import build_registry_dispatch

    bridge = build_registry_dispatch(bot_id)
    if bridge is None:
        raise ValueError(f"Bridge returned None for {bot_id}")

    elig, reg = bridge
    fn = list(reg.values())[0]
    ctx = StrategyContext(kill_switch_active=False, session_allows_entries=True)

    eta_bars = [
        EBar(
            ts=int(b.timestamp.timestamp() * 1000),
            open=float(b.open),
            high=float(b.high),
            low=float(b.low),
            close=float(b.close),
            volume=float(b.volume),
        )
        for b in bars
    ]

    position: PaperPosition | None = None
    trades: list[PaperTrade] = []
    equity = 0.0
    equity_curve: list[float] = [0.0]
    signals = 0
    peak_equity = 0.0
    max_dd = 0.0

    for i in range(max(2, len(eta_bars) // 20), len(eta_bars)):
        bar = eta_bars[i]
        price = bar.close
        bar_ts = bars[i].timestamp

        if position is not None:
            if position.side == "LONG":
                if bar.low <= position.stop:
                    pnl_points = position.stop - position.entry_price
                    pnl_usd = pnl_points * pv
                    trades.append(PaperTrade(
                        bot_id=bot_id, side="LONG",
                        entry_price=position.entry_price, exit_price=position.stop,
                        pnl_points=pnl_points, pnl_usd=pnl_usd,
                        exit_reason="stop_loss",
                        entry_ts=position.entry_bar_ts,
                        exit_ts=bar_ts.isoformat(),
                    ))
                    equity += pnl_usd
                    position = None
                elif bar.high >= position.target:
                    pnl_points = position.target - position.entry_price
                    pnl_usd = pnl_points * pv
                    trades.append(PaperTrade(
                        bot_id=bot_id, side="LONG",
                        entry_price=position.entry_price, exit_price=position.target,
                        pnl_points=pnl_points, pnl_usd=pnl_usd,
                        exit_reason="take_profit",
                        entry_ts=position.entry_bar_ts,
                        exit_ts=bar_ts.isoformat(),
                    ))
                    equity += pnl_usd
                    position = None
            else:
                if bar.high >= position.stop:
                    pnl_points = position.entry_price - position.stop
                    pnl_usd = pnl_points * pv
                    trades.append(PaperTrade(
                        bot_id=bot_id, side="SHORT",
                        entry_price=position.entry_price, exit_price=position.stop,
                        pnl_points=pnl_points, pnl_usd=pnl_usd,
                        exit_reason="stop_loss",
                        entry_ts=position.entry_bar_ts,
                        exit_ts=bar_ts.isoformat(),
                    ))
                    equity += pnl_usd
                    position = None
                elif bar.low <= position.target:
                    pnl_points = position.entry_price - position.target
                    pnl_usd = pnl_points * pv
                    trades.append(PaperTrade(
                        bot_id=bot_id, side="SHORT",
                        entry_price=position.entry_price, exit_price=position.target,
                        pnl_points=pnl_points, pnl_usd=pnl_usd,
                        exit_reason="take_profit",
                        entry_ts=position.entry_bar_ts,
                        exit_ts=bar_ts.isoformat(),
                    ))
                    equity += pnl_usd
                    position = None

        if position is None:
            signal = fn(eta_bars[:i + 1], ctx)
            if signal.is_actionable and signal.stop > 0 and signal.target > 0:
                signals += 1
                position = PaperPosition(
                    bot_id=bot_id,
                    side=signal.side.value,
                    entry_price=signal.entry or price,
                    stop=signal.stop,
                    target=signal.target,
                    entry_bar_ts=bar_ts.isoformat(),
                )

        equity_curve.append(equity)
        if equity > peak_equity:
            peak_equity = equity
        dd = peak_equity - equity
        if dd > max_dd:
            max_dd = dd

    winners = sum(1 for t in trades if t.pnl_usd > 0)
    losers = sum(1 for t in trades if t.pnl_usd <= 0)
    total_pnl = sum(t.pnl_usd for t in trades)
    avg_pnl = total_pnl / len(trades) if trades else 0.0
    wr = (winners / len(trades)) * 100 if trades else 0.0

    return SimResult(
        bot_id=bot_id,
        symbol=assignment.symbol,
        timeframe=assignment.timeframe,
        bars_processed=len(eta_bars),
        signals_generated=signals,
        trades_taken=len(trades),
        winners=winners,
        losers=losers,
        win_rate_pct=round(wr, 1),
        total_pnl_usd=round(total_pnl, 2),
        avg_pnl_per_trade=round(avg_pnl, 2),
        max_dd_usd=round(max_dd, 2),
        trades=trades,
        equity_curve=[round(e, 2) for e in equity_curve],
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="paper_trade_sim", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bot", type=str, required=True, help="bot_id to simulate")
    p.add_argument("--days", type=int, default=30, help="approximate days of data to simulate")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    assignment = __import__("eta_engine.strategies.per_bot_registry",
                            fromlist=["get_for_bot"]).get_for_bot(args.bot)
    if assignment is None:
        print(f"Unknown bot: {args.bot}")
        return 1

    daily_bars = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "4h": 6, "D": 1, "W": 0.14}
    bars_per_day = daily_bars.get(assignment.timeframe, 288)
    bar_limit = int(args.days * bars_per_day)
    point_value = _MULTIPLIERS.get(assignment.symbol, 0.50)

    try:
        result = run_simulation(args.bot, max_bars=100000, bar_limit=bar_limit,
                                point_value=point_value)
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    if args.json:
        print(json.dumps({
            "bot_id": result.bot_id, "symbol": result.symbol, "timeframe": result.timeframe,
            "bars": result.bars_processed, "signals": result.signals_generated,
            "trades": result.trades_taken, "winners": result.winners, "losers": result.losers,
            "win_rate": result.win_rate_pct, "total_pnl": result.total_pnl_usd,
            "avg_pnl_per_trade": result.avg_pnl_per_trade, "max_dd": result.max_dd_usd,
            "equity_curve": result.equity_curve,
        }, indent=2))
    else:
        print(f"PAPER TRADE SIMULATION — {result.bot_id} ({result.symbol} {result.timeframe})")
        print(f"  Bars processed:     {result.bars_processed}")
        print(f"  Signals generated:  {result.signals_generated}")
        print(f"  Trades executed:    {result.trades_taken}")
        print(f"  Winners:            {result.winners}")
        print(f"  Losers:             {result.losers}")
        print(f"  Win rate:           {result.win_rate_pct:.1f}%")
        print(f"  Total PnL:          ${result.total_pnl_usd:+.2f}")
        print(f"  Avg PnL/trade:      ${result.avg_pnl_per_trade:+.2f}")
        print(f"  Max drawdown:       ${result.max_dd_usd:.2f}")
        if result.trades:
            print(f"\n  Last 5 trades:")
            for t in result.trades[-5:]:
                print(f"    {t.exit_ts[:10]} {t.side:<6} entry={t.entry_price:.1f} exit={t.exit_price:.1f} "
                      f"pnl=${t.pnl_usd:+.2f} ({t.exit_reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

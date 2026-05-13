"""Layer 21: Paper-trade simulation loop with REALISTIC fills.

Loads real bars, feeds them to the registry strategy bridge, simulates
fills with adverse slippage on entries / stops, charges commissions,
resolves same-bar straddles probabilistically, and tags trades to
RTH / overnight session buckets so the post-hoc analysis can spot
where the live-vs-paper gap will widen.

The legacy zero-friction path remains available via ``--mode legacy``
for A/B comparison only — it is NOT a defensible production estimate.

Usage
-----
    python -m eta_engine.scripts.paper_trade_sim --bot mnq_futures_sage --days 30
    python -m eta_engine.scripts.paper_trade_sim --bot nq_daily_drb --days 365
    python -m eta_engine.scripts.paper_trade_sim --bot nq_futures_sage --days 30 --mode pessimistic
    python -m eta_engine.scripts.paper_trade_sim --bot vwap_mr_mnq --days 90 --walk-forward
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    import contextlib

    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


from eta_engine.feeds.funding_ledger import FundingLedger  # noqa: E402
from eta_engine.feeds.instrument_specs import (  # noqa: E402
    get_spec,
    is_rth_session,
)
from eta_engine.feeds.realistic_fill_sim import (  # noqa: E402
    BarOHLCV,
    Mode,
    RealisticFillSim,
)
from eta_engine.feeds.signal_validator import (  # noqa: E402
    validate_signal,
)


@dataclass
class PaperPosition:
    bot_id: str
    side: str
    entry_price: float
    stop: float
    target: float
    entry_bar_ts: str
    qty: float = 1.0
    entry_slippage_ticks: float = 0.0
    entry_session_rth: bool = True


@dataclass
class PaperTrade:
    bot_id: str
    side: str
    entry_price: float
    exit_price: float
    qty: float
    pnl_points: float
    gross_pnl_usd: float  # before commission, after slippage
    commission_usd: float  # round-trip
    net_pnl_usd: float  # gross - commission - funding
    exit_reason: str
    entry_ts: str
    exit_ts: str
    session_rth: bool  # True if entry+exit both during RTH
    entry_slippage_ticks: float
    exit_slippage_ticks: float
    funding_cost_usd: float = 0.0  # Crypto perp funding cost (positive = paid).
    # Always 0.0 unless funding_cost_enabled=True
    # AND the symbol is a perpetual swap. See
    # eta_engine.feeds.funding_ledger.


@dataclass
class SimResult:
    bot_id: str
    symbol: str
    timeframe: str
    mode: str
    bars_processed: int
    signals_generated: int
    signals_rejected: int  # hard-validation failures (stop on wrong side, RR absurd, etc.)
    trades_taken: int
    winners: int
    losers: int
    win_rate_pct: float
    gross_pnl_usd: float  # before commissions, after slippage
    total_commission_usd: float
    total_pnl_usd: float  # net of commissions AND funding (when enabled)
    avg_pnl_per_trade: float
    max_dd_usd: float
    rth_trades: int
    overnight_trades: int
    rth_pnl_usd: float
    overnight_pnl_usd: float
    straddle_resolutions: int  # how many bars triggered straddle resolver
    rejection_codes: dict[str, int] = field(default_factory=dict)
    trades: list[PaperTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    total_funding_cost_usd: float = 0.0  # Sum of per-trade funding costs.
    # Always 0.0 unless funding_cost_enabled.


# Legacy multiplier table — kept for back-compat with external callers
# that pass an explicit point_value override.  PRIMARY path now uses
# instrument_specs.get_spec() which has VERIFIED CME contract multipliers.
# The old MNQ value (0.50) is wrong by 4x; do NOT trust this dict for
# new code.
_LEGACY_MULTIPLIERS: dict[str, float] = {
    "MNQ": 2.00,
    "MNQ1": 2.00,
    "NQ": 20.0,
    "NQ1": 20.0,
    "ES": 50.0,
    "ES1": 50.0,
    "BTC": 5.0,
    "ETH": 50.0,
    "MBT": 0.10,
    "MET": 0.10,
    "SOL": 1.0,
    "XRP": 1.0,
}


def _bar_to_ohlcv(b: object, ts_iso: str) -> BarOHLCV:
    return BarOHLCV(
        open=float(b.open),
        high=float(b.high),
        low=float(b.low),
        close=float(b.close),
        volume=float(b.volume),
        ts_iso=ts_iso,
    )


def run_simulation(  # noqa: PLR0915 — single coherent loop, intentionally inline
    bot_id: str,
    max_bars: int = 10000,
    bar_limit: int | None = None,
    point_value: float | None = None,
    mode: Mode = "realistic",
    seed: int = 0,
    is_fraction: float | None = None,
    eval_oos: bool = False,
    skip_days: int = 0,
    funding_cost_enabled: bool = False,
    funding_provider: object | None = None,
) -> SimResult:
    """Replay historical bars with realistic fills.

    Parameters
    ----------
    mode: ``realistic`` (default), ``pessimistic``, or ``legacy``
    seed: RNG seed for the straddle/thin-bar resolvers
    is_fraction: if set, only the first ``is_fraction`` of bars are used
        as the "trading" window; remainder is ignored.  Combined with
        ``eval_oos=True``, swaps to the OOS window so a caller can score
        IS and OOS independently.
    funding_cost_enabled: when True AND the bot's symbol is a perpetual
        swap, deduct 8h funding settlements from each trade's net PnL
        via ``eta_engine.feeds.funding_ledger.FundingLedger``. DEFAULT
        OFF for back-compat with all pre-funding-ledger paper-soak runs.
    funding_provider: required when ``funding_cost_enabled`` and the
        symbol is perp. Anything that exposes ``rate_at(datetime) ->
        float`` or is a callable ``(datetime) -> float``. When None
        and funding is enabled, every settlement returns 0.0 (no-op
        ledger; useful as a smoke baseline).
    """
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

    bars = lib.load_bars(
        ds,
        limit=min(bar_limit or 999999, max_bars),
        limit_from="tail",
        require_positive_prices=True,
    )
    if len(bars) < 50:
        raise ValueError(f"Not enough bars: {len(bars)}")

    # Skip N most recent bars to advance the window backward in time
    if skip_days > 0 and bar_limit is not None:
        daily_bars_map = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "4h": 6, "D": 1, "W": 0.14}
        skip_bpd = daily_bars_map.get(assignment.timeframe, 288)
        skip_extra = int(skip_days * skip_bpd)
        needed = min(bar_limit + skip_extra, 200000)
        all_bars = lib.load_bars(ds, limit=needed, limit_from="tail", require_positive_prices=True)
        if len(all_bars) > skip_extra:
            bars = all_bars[: len(all_bars) - skip_extra]

    spec = get_spec(assignment.symbol)
    # Allow caller override but prefer the spec table.
    # 2026-05-07: switched from ``spec.point_value`` to
    # ``effective_point_value`` to resolve the BTC/ETH spot-vs-futures
    # ambiguity (get_spec("BTC") returns the CME spec at 5.0 but spot
    # bots want 1.0). See umbrella fix in commit log.
    if point_value is not None:
        pv = point_value
    else:
        try:
            from eta_engine.feeds.instrument_specs import effective_point_value

            pv = float(effective_point_value(assignment.symbol, route="auto") or spec.point_value)
        except Exception:  # noqa: BLE001
            pv = spec.point_value

    fill_sim = RealisticFillSim(mode=mode, seed=seed)

    # Funding ledger is opt-in. We instantiate it only when both the
    # config flag is on AND the symbol is a perpetual swap, so non-perp
    # bots pay no per-trade overhead and the default behavior is
    # bit-for-bit identical to pre-funding-ledger runs.
    funding_active = bool(
        funding_cost_enabled and getattr(spec, "is_perpetual", False),
    )
    funding_ledger = FundingLedger() if funding_active else None
    if funding_active and funding_provider is None:
        # Caller asked for funding but supplied no rate source -> default
        # to a zero-rate stub. This keeps the code path live (so the
        # PaperTrade.funding_cost_usd field is populated) without
        # silently fabricating non-zero costs from no data.
        funding_provider = lambda _ts: 0.0  # noqa: E731

    from eta_engine.strategies.registry_strategy_bridge import (
        build_registry_dispatch,
        clear_strategy_cache,
    )

    clear_strategy_cache()

    bridge = build_registry_dispatch(bot_id)
    if bridge is None:
        raise ValueError(f"Bridge returned None for {bot_id}")

    _, reg = bridge
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

    # IS / OOS split: caller gets contiguous halves (or any fraction).
    # The strategy still warms up on whatever bars are inside the chosen
    # window, so an IS run and an OOS run on the same data produce
    # legitimate disjoint test windows.
    if is_fraction is not None and 0.1 <= is_fraction <= 0.9:
        split_idx = int(len(eta_bars) * is_fraction)
        if eval_oos:
            eta_bars = eta_bars[split_idx:]
            bars = bars[split_idx:]
        else:
            eta_bars = eta_bars[:split_idx]
            bars = bars[:split_idx]

    position: PaperPosition | None = None
    pending_entry_signal = None  # holds the signal object until next bar's open
    trades: list[PaperTrade] = []

    starting_equity = 10000.0
    equity = starting_equity
    equity_curve: list[float] = [starting_equity]
    signals = 0
    signals_rejected = 0
    rejection_codes: dict[str, int] = {}
    peak_equity = starting_equity
    max_dd = 0.0
    straddle_count = 0

    # Loop offset: skip the first 5% of loaded bars so the strategy has
    # buffer to warm up.  Strategies have their own warmup_bars too;
    # this is just a floor.
    loop_start = max(2, len(eta_bars) // 20)

    for i in range(loop_start, len(eta_bars)):
        bar_eta = eta_bars[i]
        bar_ts = bars[i].timestamp
        bar_ts_iso = bar_ts.isoformat()
        bar_ohlcv = _bar_to_ohlcv(bar_eta, bar_ts_iso)

        # Track volume window for thin-bar slippage detection
        fill_sim.feed_bar_volume(bar_eta.volume)

        # --- 1. Realize a pending market-on-next-open entry ---------
        if pending_entry_signal is not None and position is None:
            sig = pending_entry_signal
            entry_fill = fill_sim.simulate_entry(
                side=sig.side.value,
                entry_bar=bar_ohlcv,
                spec=spec,
            )
            # Recompute qty from the FILLED entry price so risk-per-trade
            # honors the actual stop distance, not the signal's idealized one.
            stop_dist = abs(entry_fill.fill_price - sig.stop)
            if stop_dist <= 0:
                pending_entry_signal = None
            else:
                base_risk_pct = 0.01
                risk_usd = peak_equity * base_risk_pct * max(0.25, min(sig.risk_mult, 1.5))
                qty = risk_usd / (stop_dist * pv)
                qty = max(qty, 0.01)
                # Bug fix 2026-05-05: also cap qty by max-notional so the
                # harness mirrors the live notional ceiling enforced by
                # signal_validator.  Without this cap, low-vol bars (small
                # stop_dist) produce qty so large that notional > 50x
                # equity and the trade gets validator-rejected — the
                # rejection was the harness's own sizing, not the
                # strategy's bug.
                from eta_engine.feeds.signal_validator import (
                    MAX_QTY_NOTIONAL_PCT_OF_EQUITY,
                )

                if entry_fill.fill_price > 0 and pv > 0:
                    max_qty_by_notional = (
                        0.95 * MAX_QTY_NOTIONAL_PCT_OF_EQUITY * peak_equity / (entry_fill.fill_price * pv)
                    )
                    qty = min(qty, max_qty_by_notional)
                qty = max(qty, 0.01)

                # HARD VALIDATION — reject malformed signals before they
                # become positions.  Catches stop-on-wrong-side, RR
                # absurdity, stop-too-far, and notional-cap breaches.
                vr = validate_signal(
                    side=sig.side.value,
                    entry=entry_fill.fill_price,
                    stop=sig.stop,
                    target=sig.target,
                    qty=qty,
                    equity=peak_equity,
                    point_value=pv,
                    spec_symbol=spec.symbol,
                )
                if not vr.ok:
                    signals_rejected += 1
                    for f in vr.failures:
                        rejection_codes[f.code] = rejection_codes.get(f.code, 0) + 1
                    pending_entry_signal = None
                else:
                    position = PaperPosition(
                        bot_id=bot_id,
                        side=sig.side.value,
                        entry_price=entry_fill.fill_price,
                        stop=sig.stop,
                        target=sig.target,
                        entry_bar_ts=bar_ts_iso,
                        qty=round(qty, 4),
                        entry_slippage_ticks=entry_fill.slippage_ticks,
                        entry_session_rth=is_rth_session(bar_ts_iso, spec.symbol),
                    )
                    pending_entry_signal = None
                    # Continue: position can NOT also exit on its own entry bar
                    continue

        # --- 2. Manage existing position: try to exit on this bar ---
        if position is not None:
            exit_fill = fill_sim.simulate_exit(
                side=position.side,
                position_entry=position.entry_price,
                stop_price=position.stop,
                target_price=position.target,
                bar=bar_ohlcv,
                spec=spec,
            )
            if exit_fill.exit_reason != "no_exit":
                if "straddle" in exit_fill.exit_reason:
                    straddle_count += 1
                # Compute PnL with proper qty propagation
                qty = position.qty
                if position.side == "LONG":
                    pnl_points = (exit_fill.fill_price - position.entry_price) * qty
                else:
                    pnl_points = (position.entry_price - exit_fill.fill_price) * qty
                gross_pnl_usd = pnl_points * pv
                commission = fill_sim.commission_for_trade(spec, qty, exit_fill.fill_price)

                # Funding cost: opt-in, perp-only. funding_ledger guards
                # all the edge cases (non-perp, zero qty, malformed
                # window) so this branch stays small.
                funding_cost_usd = 0.0
                if funding_ledger is not None:
                    try:
                        from datetime import datetime as _dt

                        entry_dt = _dt.fromisoformat(position.entry_bar_ts)
                        exit_dt = _dt.fromisoformat(bar_ts_iso)
                        funding_cost_usd = funding_ledger.compute_funding_cost(
                            symbol=spec.symbol,
                            side=position.side,
                            qty=qty,
                            entry_price=position.entry_price,
                            entry_ts=entry_dt,
                            exit_ts=exit_dt,
                            funding_provider=funding_provider,
                        )
                    except (ValueError, TypeError):
                        # Defensive: malformed timestamp shouldn't blow
                        # up the trade — log via the trade's $0 cost.
                        funding_cost_usd = 0.0

                net_pnl_usd = gross_pnl_usd - commission - funding_cost_usd

                exit_session_rth = is_rth_session(bar_ts_iso, spec.symbol)
                trade = PaperTrade(
                    bot_id=bot_id,
                    side=position.side,
                    entry_price=position.entry_price,
                    exit_price=exit_fill.fill_price,
                    qty=qty,
                    pnl_points=pnl_points,
                    gross_pnl_usd=gross_pnl_usd,
                    commission_usd=commission,
                    net_pnl_usd=net_pnl_usd,
                    exit_reason=exit_fill.exit_reason,
                    entry_ts=position.entry_bar_ts,
                    exit_ts=bar_ts_iso,
                    session_rth=position.entry_session_rth and exit_session_rth,
                    entry_slippage_ticks=position.entry_slippage_ticks,
                    exit_slippage_ticks=exit_fill.slippage_ticks,
                    funding_cost_usd=funding_cost_usd,
                )
                trades.append(trade)
                equity += net_pnl_usd
                position = None

        # --- 3. Generate a NEW signal if flat -----------------------
        if position is None and pending_entry_signal is None:
            signal = fn(eta_bars[: i + 1], ctx)
            if signal.is_actionable and signal.stop > 0 and signal.target > 0:
                signals += 1
                pending_entry_signal = signal

        # --- 4. Update equity curve / drawdown ---------------------
        equity_curve.append(equity)
        if equity > peak_equity:
            peak_equity = equity
        dd = peak_equity - equity
        if dd > max_dd:
            max_dd = dd

    winners = sum(1 for t in trades if t.net_pnl_usd > 0)
    losers = sum(1 for t in trades if t.net_pnl_usd <= 0)
    gross_pnl = sum(t.gross_pnl_usd for t in trades)
    total_comm = sum(t.commission_usd for t in trades)
    total_funding = sum(t.funding_cost_usd for t in trades)
    net_pnl = sum(t.net_pnl_usd for t in trades)
    avg_pnl = net_pnl / len(trades) if trades else 0.0
    wr = (winners / len(trades)) * 100 if trades else 0.0
    rth_trades = sum(1 for t in trades if t.session_rth)
    overnight_trades = len(trades) - rth_trades
    rth_pnl = sum(t.net_pnl_usd for t in trades if t.session_rth)
    overnight_pnl = sum(t.net_pnl_usd for t in trades if not t.session_rth)

    return SimResult(
        bot_id=bot_id,
        symbol=assignment.symbol,
        timeframe=assignment.timeframe,
        mode=mode,
        bars_processed=len(eta_bars),
        signals_generated=signals,
        signals_rejected=signals_rejected,
        trades_taken=len(trades),
        winners=winners,
        losers=losers,
        win_rate_pct=round(wr, 1),
        gross_pnl_usd=round(gross_pnl, 2),
        total_commission_usd=round(total_comm, 2),
        total_pnl_usd=round(net_pnl, 2),
        avg_pnl_per_trade=round(avg_pnl, 2),
        max_dd_usd=round(max_dd, 2),
        rth_trades=rth_trades,
        overnight_trades=overnight_trades,
        rth_pnl_usd=round(rth_pnl, 2),
        overnight_pnl_usd=round(overnight_pnl, 2),
        straddle_resolutions=straddle_count,
        rejection_codes=rejection_codes,
        trades=trades,
        equity_curve=[round(e, 2) for e in equity_curve],
        total_funding_cost_usd=round(total_funding, 2),
    )


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915
    p = argparse.ArgumentParser(
        prog="paper_trade_sim", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--bot", type=str, required=True, help="bot_id to simulate")
    p.add_argument("--days", type=int, default=30, help="approximate days of data to simulate")
    p.add_argument(
        "--mode",
        type=str,
        default="realistic",
        choices=["realistic", "pessimistic", "legacy"],
        help="fill realism mode (default: realistic)",
    )
    p.add_argument("--seed", type=int, default=0, help="RNG seed for straddle/thin-bar resolvers")
    p.add_argument(
        "--walk-forward",
        action="store_true",
        help="run two simulations: IS (first 70%% of bars) + OOS (last 30%%) and report both",
    )
    p.add_argument(
        "--skip-days",
        type=int,
        default=0,
        help="skip the most recent N days of data before loading (advances the simulation window backward in time)",
    )
    p.add_argument("--is-fraction", type=float, default=0.7, help="train fraction for --walk-forward (default 0.7)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    assignment = __import__(
        "eta_engine.strategies.per_bot_registry",
        fromlist=["get_for_bot"],
    ).get_for_bot(args.bot)
    if assignment is None:
        print(f"Unknown bot: {args.bot}")
        return 1

    daily_bars = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "4h": 6, "D": 1, "W": 0.14}
    bars_per_day = daily_bars.get(assignment.timeframe, 288)
    bar_limit = int(args.days * bars_per_day)

    spec = get_spec(assignment.symbol)
    # 2026-05-07: ``effective_point_value`` resolves the spot vs CME-
    # futures ambiguity for BTC/ETH (see umbrella fix). Falls back to
    # ``spec.point_value`` for futures and unknown symbols.
    try:
        from eta_engine.feeds.instrument_specs import effective_point_value

        pv = float(effective_point_value(assignment.symbol, route="auto") or spec.point_value)
    except Exception:  # noqa: BLE001
        pv = spec.point_value

    def _run(eval_oos: bool, is_fraction: float | None) -> SimResult:
        return run_simulation(
            args.bot,
            max_bars=100000,
            bar_limit=bar_limit,
            point_value=pv,
            mode=args.mode,
            seed=args.seed,
            is_fraction=is_fraction,
            eval_oos=eval_oos,
            skip_days=args.skip_days,
        )

    try:
        if args.walk_forward:
            is_result = _run(eval_oos=False, is_fraction=args.is_fraction)
            oos_result = _run(eval_oos=True, is_fraction=args.is_fraction)
        else:
            is_result = _run(eval_oos=False, is_fraction=None)
            oos_result = None
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    if args.json:

        def _result_to_dict(r: SimResult) -> dict:
            return {
                "bot_id": r.bot_id,
                "symbol": r.symbol,
                "timeframe": r.timeframe,
                "mode": r.mode,
                "bars": r.bars_processed,
                "signals": r.signals_generated,
                "signals_rejected": r.signals_rejected,
                "rejection_codes": r.rejection_codes,
                "trades": r.trades_taken,
                "winners": r.winners,
                "losers": r.losers,
                "win_rate": r.win_rate_pct,
                "gross_pnl": r.gross_pnl_usd,
                "total_commission": r.total_commission_usd,
                "total_funding_cost": r.total_funding_cost_usd,
                "total_pnl": r.total_pnl_usd,
                "avg_pnl_per_trade": r.avg_pnl_per_trade,
                "max_dd": r.max_dd_usd,
                "rth_trades": r.rth_trades,
                "overnight_trades": r.overnight_trades,
                "rth_pnl": r.rth_pnl_usd,
                "overnight_pnl": r.overnight_pnl_usd,
                "straddle_resolutions": r.straddle_resolutions,
                "equity_curve": r.equity_curve,
            }

        out = {"in_sample": _result_to_dict(is_result)}
        if oos_result is not None:
            out["out_of_sample"] = _result_to_dict(oos_result)
        print(json.dumps(out, indent=2))
        return 0

    def _print(r: SimResult, label: str) -> None:
        print(f"\nPAPER TRADE SIM [{label}] — {r.bot_id} ({r.symbol} {r.timeframe})  mode={r.mode}")
        print(f"  Bars processed:      {r.bars_processed}")
        print(f"  Signals generated:   {r.signals_generated}")
        if r.signals_rejected > 0:
            codes = ", ".join(f"{k}={v}" for k, v in sorted(r.rejection_codes.items()))
            print(f"  Signals REJECTED:    {r.signals_rejected}  ({codes})")
            print("  >>> WARNING: rejected signals indicate STRATEGY BUGS — fix before going live")
        print(f"  Trades executed:     {r.trades_taken}  (RTH={r.rth_trades}, overnight={r.overnight_trades})")
        print(f"  Winners / Losers:    {r.winners} / {r.losers}")
        print(f"  Win rate:            {r.win_rate_pct:.1f}%")
        print(f"  Gross PnL (post-slip): ${r.gross_pnl_usd:+.2f}")
        print(f"  Commissions:         -${r.total_commission_usd:.2f}")
        if r.total_funding_cost_usd != 0.0:
            print(
                f"  Funding (perp 8h):   {-r.total_funding_cost_usd:+.2f}  "
                f"(positive number = paid by trader, deducted from net)"
            )
        print(f"  NET PnL:             ${r.total_pnl_usd:+.2f}")
        print(f"  Avg net per trade:   ${r.avg_pnl_per_trade:+.2f}")
        print(f"  Max drawdown:        ${r.max_dd_usd:.2f}")
        print(f"  RTH PnL:             ${r.rth_pnl_usd:+.2f}")
        print(f"  Overnight PnL:       ${r.overnight_pnl_usd:+.2f}")
        print(f"  Straddle resolutions:{r.straddle_resolutions} of {r.trades_taken} trades")
        if r.trades:
            print("  Last 5 trades:")
            for t in r.trades[-5:]:
                tag = "RTH" if t.session_rth else "ON"
                fund_str = f" funding=${t.funding_cost_usd:+.2f}" if t.funding_cost_usd != 0.0 else ""
                print(
                    f"    {t.exit_ts[:16]} {t.side:<5} {tag} qty={t.qty:6.2f} "
                    f"entry={t.entry_price:.2f} exit={t.exit_price:.2f} "
                    f"net=${t.net_pnl_usd:+8.2f}  ({t.exit_reason}, "
                    f"slip e={t.entry_slippage_ticks:.1f}t/x={t.exit_slippage_ticks:.1f}t"
                    f"{fund_str})"
                )

    _print(is_result, "IN-SAMPLE" if oos_result is not None else "FULL WINDOW")
    if oos_result is not None:
        _print(oos_result, "OUT-OF-SAMPLE")
        # Compare summary
        print("\n--- IS vs OOS comparison ---")
        print(f"  Win-rate gap (OOS - IS):     {oos_result.win_rate_pct - is_result.win_rate_pct:+.1f} pp")
        is_avg = is_result.avg_pnl_per_trade
        oos_avg = oos_result.avg_pnl_per_trade
        if is_avg != 0:
            avg_decay = (oos_avg - is_avg) / abs(is_avg) * 100
            print(f"  Avg-PnL decay:               {avg_decay:+.1f}%")
        if oos_result.trades_taken < 10:
            print("  WARNING: OOS sample < 10 trades — not statistically meaningful.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

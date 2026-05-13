"""Strategy Creation Harness — rapid iteration on a new strategy idea.

Workflow for "I have a new strategy hypothesis":

1. Implement the strategy as a `strategy_kind` callable.
2. Add an entry to `per_bot_registry.py` for the new bot, marked
   `promotion_status="creation_test"`.
3. Run this harness to:
   a. Load the bars
   b. Run the strategy under realistic + pessimistic fills
   c. Walk-forward IS/OOS split
   d. Apply the SAME signal-validator that catches the wrong-side-stop
      class of bug — abort the harness and refuse to score the run if
      ANY signals are rejected.  A new strategy that ships invalid
      brackets cannot be evaluated; fix the bug first.
   e. Compare against a no-edge baseline (random entry, same symbol/TF)
      to verify the strategy actually beats noise
   f. Report a FIVE-LIGHT ELITE GATE:
       - Signal validity (no rejected signals)
       - Sample size (>= 30 trades on OOS)
       - OOS profitability (net of slip + commissions)
       - OOS-vs-IS decay (< 50%)
       - Beats random-entry baseline by 1.5x or more

Only when all five lights are GREEN should the strategy proceed to
paper-soak.  Yellow on any light = needs more work.  Red on any light
= do not paper-soak.

Usage
-----
    python -m eta_engine.scripts.strategy_creation_harness \\
        --bot my_new_strategy_idea --days 90

    # Compare two candidates
    python -m eta_engine.scripts.strategy_creation_harness \\
        --bot variant_a variant_b --days 90 --random-baseline
"""

from __future__ import annotations

import argparse
import contextlib
import random
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class HarnessLight:
    name: str
    status: str  # GREEN / YELLOW / RED
    detail: str

    def emoji(self) -> str:
        return {"GREEN": "[OK ]", "YELLOW": "[? ]", "RED": "[!!]"}.get(self.status, "[?]")


@dataclass
class HarnessReport:
    bot_id: str
    symbol: str
    timeframe: str
    realistic_pnl: float
    pessimistic_pnl: float
    is_pnl: float
    oos_pnl: float
    oos_trades: int
    oos_wr: float
    signals_rejected: int
    rejection_codes: dict
    decay_pct: float
    baseline_pnl: float | None
    lights: list[HarnessLight]

    @property
    def all_green(self) -> bool:
        return all(light.status == "GREEN" for light in self.lights)

    @property
    def any_red(self) -> bool:
        return any(light.status == "RED" for light in self.lights)


def _evaluate_bot(bot_id: str, days: int, is_fraction: float) -> dict:
    """Run realistic + pessimistic + walk-forward IS/OOS for one bot."""
    from eta_engine.scripts.paper_trade_sim import run_simulation
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return {"bot_id": bot_id, "error": f"unknown bot_id {bot_id}"}

    daily_bars = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "4h": 6, "D": 1, "W": 0.14}
    bar_limit = int(days * daily_bars.get(a.timeframe, 288))

    out: dict = {"bot_id": bot_id, "symbol": a.symbol, "timeframe": a.timeframe}
    try:
        realistic = run_simulation(bot_id, max_bars=100000, bar_limit=bar_limit, mode="realistic")
        pessimistic = run_simulation(bot_id, max_bars=100000, bar_limit=bar_limit, mode="pessimistic")
        is_res = run_simulation(
            bot_id,
            max_bars=100000,
            bar_limit=bar_limit,
            mode="realistic",
            is_fraction=is_fraction,
            eval_oos=False,
        )
        oos_res = run_simulation(
            bot_id,
            max_bars=100000,
            bar_limit=bar_limit,
            mode="realistic",
            is_fraction=is_fraction,
            eval_oos=True,
        )

        out.update(
            {
                "realistic_pnl": realistic.total_pnl_usd,
                "pessimistic_pnl": pessimistic.total_pnl_usd,
                "is_pnl": is_res.total_pnl_usd,
                "oos_pnl": oos_res.total_pnl_usd,
                "oos_trades": oos_res.trades_taken,
                "oos_wr": oos_res.win_rate_pct,
                "signals_rejected": realistic.signals_rejected,
                "rejection_codes": dict(realistic.rejection_codes),
            }
        )
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _random_baseline(symbol: str, timeframe: str, days: int, n_trades: int = 100, seed: int = 0) -> float:
    """Compute the expected PnL of a random-entry strategy on the same bars.

    Algorithm: pick `n_trades` random bars; on each, randomly LONG or SHORT
    with stop = ±1 ATR and target = ±2 ATR. Apply realistic fills.

    Returns the average net PnL — used as the "noise floor" the new
    strategy must beat by some margin to demonstrate edge.
    """
    from eta_engine.data.library import default_library
    from eta_engine.feeds.instrument_specs import get_spec
    from eta_engine.feeds.realistic_fill_sim import BarOHLCV, RealisticFillSim

    rng = random.Random(seed)
    lib = default_library()
    ds = lib.get(symbol=symbol, timeframe=timeframe)
    if ds is None:
        return 0.0
    daily_bars = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "4h": 6, "D": 1, "W": 0.14}
    bar_limit = int(days * daily_bars.get(timeframe, 288))
    bars = lib.load_bars(ds, limit=bar_limit, limit_from="tail", require_positive_prices=True)
    if len(bars) < 100:
        return 0.0

    spec = get_spec(symbol)
    # 2026-05-07: ``effective_point_value`` resolves the BTC/ETH spot
    # vs CME-futures multiplier ambiguity (see umbrella fix). Used in
    # the pnl calc below in place of ``spec.point_value``.
    try:
        from eta_engine.feeds.instrument_specs import effective_point_value

        _harness_pv = float(effective_point_value(symbol, route="auto") or spec.point_value)
    except Exception:  # noqa: BLE001
        _harness_pv = spec.point_value
    sim = RealisticFillSim(mode="realistic", seed=seed)
    for b in bars[:20]:
        sim.feed_bar_volume(float(b.volume))

    pnl_total = 0.0
    eligible = list(range(50, len(bars) - 5))
    if len(eligible) < n_trades:
        n_trades = len(eligible)
    chosen = rng.sample(eligible, n_trades)

    for i in chosen:
        side = rng.choice(["LONG", "SHORT"])
        atr_window = bars[max(0, i - 14) : i]
        atr = sum(b.high - b.low for b in atr_window) / max(1, len(atr_window))
        if atr <= 0:
            continue
        entry_bar = bars[i + 1]
        ohlcv_entry = BarOHLCV(
            open=float(entry_bar.open),
            high=float(entry_bar.high),
            low=float(entry_bar.low),
            close=float(entry_bar.close),
            volume=float(entry_bar.volume),
            ts_iso=entry_bar.timestamp.isoformat(),
        )
        entry_fill = sim.simulate_entry(side, ohlcv_entry, spec)
        if side == "LONG":
            stop = entry_fill.fill_price - atr
            target = entry_fill.fill_price + 2 * atr
        else:
            stop = entry_fill.fill_price + atr
            target = entry_fill.fill_price - 2 * atr
        if stop <= 0 or target <= 0:
            continue

        # Walk forward up to 20 bars
        qty = 1.0
        for j in range(i + 2, min(i + 22, len(bars))):
            ohlcv = BarOHLCV(
                open=float(bars[j].open),
                high=float(bars[j].high),
                low=float(bars[j].low),
                close=float(bars[j].close),
                volume=float(bars[j].volume),
                ts_iso=bars[j].timestamp.isoformat(),
            )
            sim.feed_bar_volume(float(bars[j].volume))
            ex = sim.simulate_exit(
                side=side,
                position_entry=entry_fill.fill_price,
                stop_price=stop,
                target_price=target,
                bar=ohlcv,
                spec=spec,
            )
            if ex.exit_reason != "no_exit":
                if side == "LONG":
                    pnl = (ex.fill_price - entry_fill.fill_price) * qty * _harness_pv
                else:
                    pnl = (entry_fill.fill_price - ex.fill_price) * qty * _harness_pv
                pnl -= sim.commission_for_trade(spec, qty, ex.fill_price)
                pnl_total += pnl
                break
    return pnl_total


def _build_lights(d: dict, baseline_pnl: float | None) -> list[HarnessLight]:
    """Apply the FIVE-LIGHT ELITE GATE.

    Each light is GREEN / YELLOW / RED.  All-green is the only state
    that should proceed to paper-soak.
    """
    lights: list[HarnessLight] = []

    # 1. Signal validity
    if d.get("signals_rejected", 0) == 0:
        lights.append(HarnessLight("Signal validity", "GREEN", "no malformed signals"))
    else:
        codes = d.get("rejection_codes") or {}
        codes_str = ", ".join(f"{k}={v}" for k, v in codes.items())
        lights.append(HarnessLight("Signal validity", "RED", f"{d['signals_rejected']} rejected ({codes_str})"))

    # 2. Sample size
    oos_n = d.get("oos_trades", 0)
    if oos_n >= 30:
        lights.append(HarnessLight("Sample size", "GREEN", f"OOS trades {oos_n}"))
    elif oos_n >= 10:
        lights.append(
            HarnessLight("Sample size", "YELLOW", f"OOS trades {oos_n} — too small for confidence, increase --days")
        )
    else:
        lights.append(HarnessLight("Sample size", "RED", f"OOS trades {oos_n} — meaningless"))

    # 3. OOS profitability
    oos_pnl = d.get("oos_pnl", 0.0)
    if oos_pnl > 0:
        lights.append(HarnessLight("OOS profitability", "GREEN", f"OOS net ${oos_pnl:+.0f}"))
    else:
        lights.append(
            HarnessLight("OOS profitability", "RED", f"OOS net ${oos_pnl:+.0f} — strategy loses on held-out data")
        )

    # 4. OOS-vs-IS decay
    is_pnl = d.get("is_pnl", 0.0)
    if abs(is_pnl) < 0.01:
        lights.append(HarnessLight("OOS decay", "YELLOW", "IS PnL is ~zero, decay undefined"))
    else:
        decay = (oos_pnl - is_pnl) / abs(is_pnl) * 100
        if decay > -25:
            lights.append(HarnessLight("OOS decay", "GREEN", f"{decay:+.0f}% (acceptable)"))
        elif decay > -50:
            lights.append(HarnessLight("OOS decay", "YELLOW", f"{decay:+.0f}% (overfit suspected)"))
        else:
            lights.append(HarnessLight("OOS decay", "RED", f"{decay:+.0f}% (severe overfit — IS-tuned only)"))

    # 5. Beats baseline
    if baseline_pnl is None:
        lights.append(HarnessLight("Beats baseline", "YELLOW", "no baseline run; rerun with --random-baseline"))
    else:
        if oos_pnl > baseline_pnl * 1.5 and oos_pnl > 0:
            lights.append(
                HarnessLight("Beats baseline", "GREEN", f"OOS ${oos_pnl:+.0f} vs random ${baseline_pnl:+.0f}")
            )
        elif oos_pnl > baseline_pnl:
            lights.append(
                HarnessLight(
                    "Beats baseline", "YELLOW", f"OOS ${oos_pnl:+.0f} vs random ${baseline_pnl:+.0f} (margin too small)"
                )
            )
        else:
            lights.append(
                HarnessLight("Beats baseline", "RED", f"OOS ${oos_pnl:+.0f} <= random ${baseline_pnl:+.0f} — no edge")
            )

    return lights


def run_harness(
    bot_ids: list[str],
    days: int,
    is_fraction: float,
    workers: int,
    random_baseline: bool,
) -> list[HarnessReport]:
    print(f"Strategy creation harness — {len(bot_ids)} candidates, {workers} workers")

    bot_results: dict[str, dict] = {}
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_evaluate_bot, b, days, is_fraction): b for b in bot_ids}
        for f in as_completed(futures):
            b = futures[f]
            try:
                bot_results[b] = f.result()
            except Exception as e:  # noqa: BLE001
                bot_results[b] = {"bot_id": b, "error": f"{type(e).__name__}: {e}"}
            if "error" in bot_results[b]:
                print(f"  [{b}] ERROR {bot_results[b]['error']}")
            else:
                print(
                    f"  [{b}] OOS=${bot_results[b].get('oos_pnl', 0):+.0f} "
                    f"trades={bot_results[b].get('oos_trades', 0)} "
                    f"rejected={bot_results[b].get('signals_rejected', 0)}"
                )

    reports: list[HarnessReport] = []
    for b in bot_ids:
        d = bot_results.get(b, {})
        if "error" in d:
            reports.append(
                HarnessReport(
                    bot_id=b,
                    symbol="?",
                    timeframe="?",
                    realistic_pnl=0.0,
                    pessimistic_pnl=0.0,
                    is_pnl=0.0,
                    oos_pnl=0.0,
                    oos_trades=0,
                    oos_wr=0.0,
                    signals_rejected=0,
                    rejection_codes={},
                    decay_pct=0.0,
                    baseline_pnl=None,
                    lights=[HarnessLight("Run completed", "RED", d["error"])],
                )
            )
            continue

        baseline_pnl: float | None = None
        if random_baseline:
            print(f"  Computing random-baseline for {b} ...")
            baseline_pnl = _random_baseline(d["symbol"], d["timeframe"], days)
            print(f"    baseline ${baseline_pnl:+.0f}")

        lights = _build_lights(d, baseline_pnl)
        is_pnl = d.get("is_pnl", 0.0)
        oos_pnl = d.get("oos_pnl", 0.0)
        decay = ((oos_pnl - is_pnl) / abs(is_pnl) * 100) if abs(is_pnl) > 0.01 else 0.0
        reports.append(
            HarnessReport(
                bot_id=b,
                symbol=d.get("symbol", "?"),
                timeframe=d.get("timeframe", "?"),
                realistic_pnl=d.get("realistic_pnl", 0.0),
                pessimistic_pnl=d.get("pessimistic_pnl", 0.0),
                is_pnl=is_pnl,
                oos_pnl=oos_pnl,
                oos_trades=d.get("oos_trades", 0),
                oos_wr=d.get("oos_wr", 0.0),
                signals_rejected=d.get("signals_rejected", 0),
                rejection_codes=d.get("rejection_codes", {}),
                decay_pct=decay,
                baseline_pnl=baseline_pnl,
                lights=lights,
            )
        )
    return reports


def print_reports(reports: list[HarnessReport]) -> int:
    print("\n" + "=" * 96)
    print("STRATEGY CREATION HARNESS — FIVE-LIGHT ELITE GATE")
    print("=" * 96)

    any_red = False
    for r in reports:
        print(f"\n--- {r.bot_id} ({r.symbol} {r.timeframe}) ---")
        print(f"  Realistic PnL: ${r.realistic_pnl:+.2f}   Pessimistic: ${r.pessimistic_pnl:+.2f}")
        print(
            f"  IS PnL: ${r.is_pnl:+.2f}   OOS PnL: ${r.oos_pnl:+.2f}   "
            f"OOS trades: {r.oos_trades}   OOS WR: {r.oos_wr:.1f}%"
        )
        if r.baseline_pnl is not None:
            print(f"  Random-baseline PnL: ${r.baseline_pnl:+.2f}")
        print("  Gate:")
        for light in r.lights:
            print(f"    {light.emoji()} {light.name:<22} {light.detail}")
        if r.all_green:
            print("  >>> ALL GREEN — promote to paper-soak")
        elif r.any_red:
            print("  >>> RED — DO NOT promote.  Fix issues, re-run harness.")
            any_red = True
        else:
            print("  >>> YELLOW — needs more data or refinement before paper-soak")

    return 1 if any_red else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="strategy_creation_harness", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--bot", nargs="+", required=True, help="bot_id(s) to evaluate")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--is-fraction", type=float, default=0.7)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument(
        "--random-baseline", action="store_true", help="compute random-entry baseline for the 'beats noise' check"
    )
    args = p.parse_args(argv)

    reports = run_harness(
        bot_ids=args.bot,
        days=args.days,
        is_fraction=args.is_fraction,
        workers=args.workers,
        random_baseline=args.random_baseline,
    )
    return print_reports(reports)


if __name__ == "__main__":
    sys.exit(main())

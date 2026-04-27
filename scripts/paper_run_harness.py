"""
EVOLUTIONARY TRADING ALGO  //  scripts.paper_run_harness
============================================
4-week synthetic paper-trading harness.

Drives all 6 bots (MNQ, NQ, CRYPTO_SEED, ETH_PERP, SOL_PERP, XRP_PERP)
through a synthetic bar stream per asset class and evaluates the
paper_phase_requirements gate defined in firm_spec_paper_promotion_v1.json.

Purpose:
  - Prove the full pipeline runs end-to-end without live credentials.
  - Produce per-bot metrics (expectancy_R, win_rate, max_dd_pct, trades).
  - Evaluate the paper-phase gate (trades >= 30, expectancy >= 0.30R,
    max_dd <= 15%, kill_switch_rate <= 1 per 30d, telemetry_gap = 0).
  - Emit docs/paper_run_report.json and docs/paper_run_tearsheet.txt.

Usage:
    python -m eta_engine.scripts.paper_run_harness [--weeks 4] [--seed 11]
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


# ── Per-bot spec: aligned with firm_spec_paper_promotion_v1.json ──

BOT_PLAN: dict[str, dict[str, Any]] = {
    "mnq": {
        "symbol": "MNQ",
        "capital": 5_000.0,
        "start_price": 17_500.0,
        "drift": 0.0006,
        "vol": 0.0055,
        "interval_minutes": 5,
        "confluence_threshold": 6.0,
        "max_trades_per_day": 6,
        "risk_per_trade_pct": 0.01,
    },
    "nq": {
        "symbol": "NQ",
        "capital": 12_000.0,
        "start_price": 17_500.0,
        "drift": 0.0006,
        "vol": 0.0050,
        "interval_minutes": 5,
        "confluence_threshold": 6.5,
        "max_trades_per_day": 5,
        "risk_per_trade_pct": 0.008,
    },
    "crypto_seed": {
        "symbol": "BTCUSDT",
        "capital": 2_000.0,
        "start_price": 65_000.0,
        "drift": 0.0003,
        "vol": 0.0090,
        "interval_minutes": 15,
        "confluence_threshold": 5.5,
        "max_trades_per_day": 8,
        "risk_per_trade_pct": 0.005,
    },
    "eth_perp": {
        "symbol": "ETHUSDT",
        "capital": 3_000.0,
        "start_price": 3_500.0,
        "drift": 0.0004,
        "vol": 0.0085,
        "interval_minutes": 5,
        "confluence_threshold": 5.0,
        "max_trades_per_day": 6,
        "risk_per_trade_pct": 0.01,
    },
    "sol_perp": {
        "symbol": "SOLUSDT",
        "capital": 3_000.0,
        "start_price": 180.0,
        "drift": 0.0005,
        "vol": 0.0110,
        "interval_minutes": 5,
        "confluence_threshold": 5.0,
        "max_trades_per_day": 6,
        "risk_per_trade_pct": 0.01,
    },
    "xrp_perp": {
        "symbol": "XRPUSDT",
        "capital": 2_000.0,
        "start_price": 0.62,
        "drift": 0.0003,
        "vol": 0.0120,
        "interval_minutes": 5,
        "confluence_threshold": 5.0,
        "max_trades_per_day": 6,
        "risk_per_trade_pct": 0.01,
    },
}


# ── Paper-phase gate thresholds (mirrors firm_spec_paper_promotion_v1.json) ──


@dataclass
class PaperPhaseRequirements:
    min_weeks: int = 4
    min_trades_per_bot: int = 30
    expectancy_r_required: float = 0.30
    max_dd_during_paper_pct: float = 15.0
    kill_switch_rate_max: float = 1.0  # events per 30 calendar days
    telemetry_gap_tolerance_minutes: int = 5


# ── Per-bot result container ──


@dataclass
class BotPaperResult:
    bot: str
    symbol: str
    n_trades: int
    win_rate: float
    expectancy_r: float
    avg_win_r: float
    avg_loss_r: float
    profit_factor: float
    max_dd_pct: float
    total_return_pct: float
    final_equity_usd: float
    starting_equity_usd: float
    sharpe: float
    sortino: float
    killed: bool
    kill_events: int
    telemetry_gaps: int
    gate_pass: bool = False
    gate_failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot": self.bot,
            "symbol": self.symbol,
            "n_trades": self.n_trades,
            "win_rate": round(self.win_rate, 4),
            "expectancy_r": round(self.expectancy_r, 4),
            "avg_win_r": round(self.avg_win_r, 4),
            "avg_loss_r": round(self.avg_loss_r, 4),
            "profit_factor": round(self.profit_factor, 4),
            "max_dd_pct": round(self.max_dd_pct, 4),
            "total_return_pct": round(self.total_return_pct, 4),
            "final_equity_usd": round(self.final_equity_usd, 2),
            "starting_equity_usd": round(self.starting_equity_usd, 2),
            "sharpe": round(self.sharpe, 4),
            "sortino": round(self.sortino, 4),
            "killed": self.killed,
            "kill_events": self.kill_events,
            "telemetry_gaps": self.telemetry_gaps,
            "gate_pass": self.gate_pass,
            "gate_failures": list(self.gate_failures),
        }


# ── Harness ──


def _ctx_builder(bar, hist):  # noqa: ANN001
    """Rich synthetic context that drives meaningful confluence."""
    from eta_engine.core.data_pipeline import FundingRate

    now = bar.timestamp
    tail = hist[-20:] if len(hist) >= 20 else hist
    ema_series = [b.close for b in tail[:: max(1, len(tail) // 5)]] if len(tail) >= 2 else [bar.close * 0.98, bar.close]
    return {
        "daily_ema": ema_series,
        "h4_struct": "HH_HL",
        "bias": 1,
        "atr_history": [max(bar.high - bar.low, 0.01)] * 10,
        "atr_current": max(bar.high - bar.low, 0.01),
        "funding_history": [
            FundingRate(
                timestamp=now,
                symbol=bar.symbol,
                rate=-0.0006,
                predicted_rate=-0.0006,
            )
        ]
        * 8,
        "onchain": {
            "whale_transfers": 40,
            "whale_transfers_baseline": 20,
            "exchange_netflow_usd": -25_000_000.0,
            "active_addresses": 1300,
            "active_addresses_baseline": 1000,
        },
        "sentiment": {
            "galaxy_score": 80.0,
            "alt_rank": 18,
            "social_volume": 500,
            "social_volume_baseline": 200,
            "fear_greed": 22,
        },
    }


def _run_one_bot(
    bot_name: str,
    plan: dict[str, Any],
    weeks: int,
    seed: int,
    reqs: PaperPhaseRequirements,
    bar_mode: str = "gbm",
) -> BotPaperResult:
    from eta_engine.backtest import (
        BacktestConfig,
        BacktestEngine,
        BarReplay,
    )
    from eta_engine.features.pipeline import FeaturePipeline

    interval = int(plan["interval_minutes"])
    # 7d/week * 24h * (60 / interval_min) bars per week
    n_bars = int(weeks * 7 * 24 * (60 / interval))
    start = datetime(2026, 4, 1, tzinfo=UTC)
    if bar_mode == "jump":
        bars = BarReplay.synthetic_bars_jump(
            n=n_bars,
            start_price=float(plan["start_price"]),
            drift=float(plan["drift"]),
            vol=float(plan["vol"]),
            symbol=str(plan["symbol"]),
            seed=seed,
            start=start,
            interval_minutes=interval,
            jump_intensity=float(plan.get("jump_intensity", 0.02)),
            jump_mean=float(plan.get("jump_mean", 0.0)),
            jump_vol=float(plan.get("jump_vol", 0.015)),
            regime_persist=int(plan.get("regime_persist", 48)),
            bull_drift_boost=float(plan.get("bull_drift_boost", 0.0015)),
            bear_drift_penalty=float(plan.get("bear_drift_penalty", 0.0015)),
        )
    else:
        bars = BarReplay.synthetic_bars(
            n=n_bars,
            start_price=float(plan["start_price"]),
            drift=float(plan["drift"]),
            vol=float(plan["vol"]),
            symbol=str(plan["symbol"]),
            seed=seed,
            start=start,
            interval_minutes=interval,
        )
    cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=str(plan["symbol"]),
        initial_equity=float(plan["capital"]),
        risk_per_trade_pct=float(plan["risk_per_trade_pct"]),
        confluence_threshold=float(plan["confluence_threshold"]),
        max_trades_per_day=int(plan["max_trades_per_day"]),
    )
    pipe = FeaturePipeline.default()
    engine = BacktestEngine(
        pipe,
        cfg,
        ctx_builder=_ctx_builder,
        strategy_id=f"apex_paper_{bot_name}",
    )
    res = engine.run(bars)

    killed = res.max_dd_pct >= 20.0  # mirrors BotConfig.max_dd_kill_pct envelope
    kill_events = 1 if killed else 0
    # Telemetry gap simulation — synthetic bars emit every interval so gaps=0.
    # Real paper runs replace this with wall-clock delta over heartbeat stream.
    tel_gaps = 0

    r = BotPaperResult(
        bot=bot_name,
        symbol=str(plan["symbol"]),
        n_trades=res.n_trades,
        win_rate=res.win_rate,
        expectancy_r=res.expectancy_r,
        avg_win_r=res.avg_win_r,
        avg_loss_r=res.avg_loss_r,
        profit_factor=res.profit_factor,
        max_dd_pct=res.max_dd_pct,
        total_return_pct=res.total_return_pct,
        final_equity_usd=float(plan["capital"]) * (1.0 + res.total_return_pct / 100.0),
        starting_equity_usd=float(plan["capital"]),
        sharpe=res.sharpe,
        sortino=res.sortino,
        killed=killed,
        kill_events=kill_events,
        telemetry_gaps=tel_gaps,
    )
    _apply_gate(r, reqs, weeks)
    return r


def _apply_gate(
    r: BotPaperResult,
    reqs: PaperPhaseRequirements,
    weeks: int,
) -> None:
    fails: list[str] = []
    if weeks < reqs.min_weeks:
        fails.append(
            f"weeks={weeks} < min_weeks={reqs.min_weeks}",
        )
    if r.n_trades < reqs.min_trades_per_bot:
        fails.append(
            f"n_trades={r.n_trades} < min_trades_per_bot={reqs.min_trades_per_bot}",
        )
    if r.expectancy_r < reqs.expectancy_r_required:
        fails.append(
            f"expectancy_r={r.expectancy_r:.3f} < required={reqs.expectancy_r_required}",
        )
    if r.max_dd_pct > reqs.max_dd_during_paper_pct:
        fails.append(
            f"max_dd_pct={r.max_dd_pct:.2f} > max={reqs.max_dd_during_paper_pct}",
        )
    # kill_switch_rate is per-30d. weeks/4.29 gives months. We demand <= 1/mo.
    months = weeks / (30.0 / 7.0)
    rate = r.kill_events / max(months, 1e-9)
    if rate > reqs.kill_switch_rate_max:
        fails.append(
            f"kill_switch_rate={rate:.2f}/mo > max={reqs.kill_switch_rate_max}",
        )
    if r.telemetry_gaps > 0:
        fails.append(
            f"telemetry_gaps={r.telemetry_gaps} > 0",
        )
    r.gate_failures = fails
    r.gate_pass = not fails


# ── Aggregate ──


@dataclass
class AggregateResult:
    total_bots: int
    bots_gate_pass: int
    total_trades: int
    total_final_equity_usd: float
    total_starting_equity_usd: float
    blended_expectancy_r: float
    blended_win_rate: float
    blended_max_dd_pct: float
    any_killed: bool
    promotion_verdict: str  # "GO" | "MODIFY" | "KILL"
    verdict_reason: str


def _aggregate(
    per_bot: list[BotPaperResult],
    reqs: PaperPhaseRequirements,
    weeks: int,
) -> AggregateResult:
    n_trades = sum(b.n_trades for b in per_bot)
    start_cap = sum(b.starting_equity_usd for b in per_bot)
    end_cap = sum(b.final_equity_usd for b in per_bot)
    passing = sum(1 for b in per_bot if b.gate_pass)
    any_killed = any(b.killed for b in per_bot)
    # Blend expectancy by trade count
    blended_exp = sum(b.expectancy_r * b.n_trades for b in per_bot) / n_trades if n_trades > 0 else 0.0
    blended_wr = sum(b.win_rate * b.n_trades for b in per_bot) / n_trades if n_trades > 0 else 0.0
    blended_dd = max((b.max_dd_pct for b in per_bot), default=0.0)

    # Verdict:
    #   - KILL:   any bot hit kill + at fewer than min_weeks
    #   - MODIFY: any bot failed gate but no kill
    #   - GO:     every bot passed gate
    if any_killed and weeks >= reqs.min_weeks:
        verdict = "MODIFY"
        reason = f"{sum(1 for b in per_bot if b.killed)} bot(s) hit DD kill during paper"
    elif passing == len(per_bot):
        verdict = "GO"
        reason = "All bots cleared paper_phase_requirements gate"
    else:
        failing = [b.bot for b in per_bot if not b.gate_pass]
        verdict = "MODIFY"
        reason = f"Bots failing gate: {', '.join(failing)}"
    return AggregateResult(
        total_bots=len(per_bot),
        bots_gate_pass=passing,
        total_trades=n_trades,
        total_final_equity_usd=round(end_cap, 2),
        total_starting_equity_usd=round(start_cap, 2),
        blended_expectancy_r=round(blended_exp, 4),
        blended_win_rate=round(blended_wr, 4),
        blended_max_dd_pct=round(blended_dd, 4),
        any_killed=any_killed,
        promotion_verdict=verdict,
        verdict_reason=reason,
    )


# ── Report + tearsheet ──


def _write_report(
    per_bot: list[BotPaperResult],
    agg: AggregateResult,
    weeks: int,
    seed: int,
    reqs: PaperPhaseRequirements,
    out_dir: Path,
    label: str = "",
    bar_mode: str = "gbm",
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{label}" if label else ""
    report = {
        "kind": "apex_paper_run_report",
        "generated_at": datetime.now(UTC).isoformat(),
        "label": label,
        "bar_mode": bar_mode,
        "weeks": weeks,
        "seed": seed,
        "requirements": {
            "min_weeks": reqs.min_weeks,
            "min_trades_per_bot": reqs.min_trades_per_bot,
            "expectancy_r_required": reqs.expectancy_r_required,
            "max_dd_during_paper_pct": reqs.max_dd_during_paper_pct,
            "kill_switch_rate_max": reqs.kill_switch_rate_max,
            "telemetry_gap_tolerance_minutes": reqs.telemetry_gap_tolerance_minutes,
        },
        "per_bot": [b.to_dict() for b in per_bot],
        "aggregate": {
            "total_bots": agg.total_bots,
            "bots_gate_pass": agg.bots_gate_pass,
            "total_trades": agg.total_trades,
            "total_final_equity_usd": agg.total_final_equity_usd,
            "total_starting_equity_usd": agg.total_starting_equity_usd,
            "blended_expectancy_r": agg.blended_expectancy_r,
            "blended_win_rate": agg.blended_win_rate,
            "blended_max_dd_pct": agg.blended_max_dd_pct,
            "any_killed": agg.any_killed,
            "promotion_verdict": agg.promotion_verdict,
            "verdict_reason": agg.verdict_reason,
        },
    }
    rp = out_dir / f"paper_run_report{suffix}.json"
    rp.write_text(json.dumps(report, indent=2))

    ts_lines: list[str] = []
    ts_lines.append(f"EVOLUTIONARY TRADING ALGO -- Paper Run Tearsheet  ({label or 'default'}, {bar_mode})")
    ts_lines.append("=" * 96)
    ts_lines.append(
        f"Weeks: {weeks}    Seed: {seed}    Generated: {datetime.now(UTC).isoformat()}",
    )
    ts_lines.append("-" * 96)
    ts_lines.append(
        f"{'BOT':<12} {'SYM':<10} {'TRD':>5} {'WIN%':>6} "
        f"{'EXP_R':>7} {'PF':>6} {'DD%':>6} {'RET%':>8} "
        f"{'EQ_END':>10} {'KILL':>5} {'GATE':>6}",
    )
    ts_lines.append("-" * 96)
    for b in per_bot:
        ts_lines.append(
            f"{b.bot:<12} {b.symbol:<10} {b.n_trades:>5} "
            f"{b.win_rate * 100:>5.1f}% {b.expectancy_r:>7.3f} "
            f"{b.profit_factor:>6.2f} {b.max_dd_pct:>5.2f}% "
            f"{b.total_return_pct:>7.2f}% {b.final_equity_usd:>10.2f} "
            f"{'YES' if b.killed else '-':>5} "
            f"{'PASS' if b.gate_pass else 'FAIL':>6}",
        )
    ts_lines.append("-" * 96)
    ts_lines.append(
        f"Aggregate: bots={agg.total_bots} pass={agg.bots_gate_pass}/{agg.total_bots} "
        f"trades={agg.total_trades} "
        f"equity=${agg.total_starting_equity_usd:,.0f} -> ${agg.total_final_equity_usd:,.0f}",
    )
    ts_lines.append(
        f"Blended: expectancy={agg.blended_expectancy_r:.3f}R  "
        f"winrate={agg.blended_win_rate * 100:.1f}%  "
        f"max_dd={agg.blended_max_dd_pct:.2f}%",
    )
    ts_lines.append(
        f"Promotion verdict: {agg.promotion_verdict}  ({agg.verdict_reason})",
    )
    ts_lines.append("=" * 96)
    for b in per_bot:
        if b.gate_failures:
            ts_lines.append(f"[{b.bot}] fails:")
            for f in b.gate_failures:
                ts_lines.append(f"   - {f}")
    tp = out_dir / f"paper_run_tearsheet{suffix}.txt"
    tp.write_text("\n".join(ts_lines) + "\n")
    return rp, tp


# ── Entry ──


def _merge_overrides(
    plans: dict[str, dict[str, Any]],
    overrides_path: Path | None,
) -> dict[str, dict[str, Any]]:
    """Deep-merge overrides JSON into BOT_PLAN. Shape: {bot: {param: value}}."""
    merged: dict[str, dict[str, Any]] = {b: dict(p) for b, p in plans.items()}
    if overrides_path is None:
        return merged
    if not overrides_path.exists():
        raise FileNotFoundError(f"overrides file not found: {overrides_path}")
    raw = json.loads(overrides_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(
            f"overrides JSON must be object of {{bot: {{param: value}}}}, got {type(raw).__name__}",
        )
    for bot, params in raw.items():
        if bot not in merged:
            continue
        if not isinstance(params, dict):
            continue
        for k, v in params.items():
            # Strip non-plan keys like 'expected_expectancy_r'
            if k in merged[bot] or k in (
                "jump_intensity",
                "jump_mean",
                "jump_vol",
                "regime_persist",
                "bull_drift_boost",
                "bear_drift_penalty",
            ):
                merged[bot][k] = v
    return merged


def main() -> int:
    p = argparse.ArgumentParser(description="Evolutionary Trading Algo paper-run harness")
    p.add_argument("--weeks", type=int, default=4)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "docs",
        help="Report output directory",
    )
    p.add_argument(
        "--overrides",
        type=Path,
        default=None,
        help="JSON file: {bot: {param: value}} merged into BOT_PLAN",
    )
    p.add_argument(
        "--bots-subset",
        type=str,
        default="",
        help="Comma-separated bot names to run (default: all)",
    )
    p.add_argument(
        "--label",
        type=str,
        default="",
        help="Suffix for output files (paper_run_report_<label>.json)",
    )
    p.add_argument(
        "--bar-mode",
        choices=("gbm", "jump"),
        default="gbm",
        help="Synthetic bar generator (gbm=GBM, jump=jump-diffusion+regime)",
    )
    args = p.parse_args()

    reqs = PaperPhaseRequirements()
    weeks = max(1, int(args.weeks))

    plans = _merge_overrides(BOT_PLAN, args.overrides)
    subset = {s.strip() for s in args.bots_subset.split(",") if s.strip()}
    if subset:
        plans = {b: p for b, p in plans.items() if b in subset}
    if not plans:
        print("No bots selected after subset filter.")
        return 2

    label = args.label
    if args.overrides and not label:
        label = args.overrides.stem
    bar_mode = str(args.bar_mode)

    print("EVOLUTIONARY TRADING ALGO -- Paper-Run Harness")
    print("=" * 96)
    print(
        f"Weeks: {weeks}   Seed: {args.seed}   Bots: {len(plans)}   "
        f"Starting capital: ${sum(p['capital'] for p in plans.values()):,.0f}   "
        f"Bar mode: {bar_mode}   Label: {label or '-'}"
    )
    if args.overrides:
        print(f"Overrides: {args.overrides}")
    print("-" * 96)

    results: list[BotPaperResult] = []
    # Different seeds per bot so correlation doesn't collapse
    for i, (bot, plan) in enumerate(plans.items()):
        s = int(args.seed) + i * 7
        r = _run_one_bot(bot, plan, weeks, s, reqs, bar_mode=bar_mode)
        results.append(r)
        print(
            f"[{bot:<12}] trades={r.n_trades:>3} "
            f"win={r.win_rate * 100:>5.1f}% "
            f"exp={r.expectancy_r:>+6.3f}R  "
            f"dd={r.max_dd_pct:>5.2f}% "
            f"ret={r.total_return_pct:>+6.2f}%  "
            f"kill={'Y' if r.killed else '-'}  "
            f"gate={'PASS' if r.gate_pass else 'FAIL'}",
        )

    agg = _aggregate(results, reqs, weeks)
    rp, tp = _write_report(
        results,
        agg,
        weeks,
        int(args.seed),
        reqs,
        args.out_dir,
        label=label,
        bar_mode=bar_mode,
    )

    print("-" * 96)
    print(
        f"Aggregate: {agg.bots_gate_pass}/{agg.total_bots} pass  "
        f"trades={agg.total_trades}  "
        f"blended_exp={agg.blended_expectancy_r:+.3f}R  "
        f"blended_dd={agg.blended_max_dd_pct:.2f}%",
    )
    print(f"Verdict:   {agg.promotion_verdict}   ({agg.verdict_reason})")
    print(f"Report:    {rp}")
    print(f"Tearsheet: {tp}")
    print("=" * 96)
    return 0 if agg.promotion_verdict != "KILL" else 1


if __name__ == "__main__":
    sys.exit(main())

"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_walk_forward_demo
================================================
Spin up a 4-window anchored walk-forward run on synthetic bars.
Prints the IS/OOS table + DSR + pass/fail gate.

Usage:
    python -m eta_engine.scripts.run_walk_forward_demo
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def _ctx(bar, hist) -> dict:  # noqa: ANN001
    from eta_engine.core.data_pipeline import FundingRate

    now = bar.timestamp
    # Synthetic regime label so the tearsheet's regime breakdown
    # surfaces something meaningful in the demo. In a real run, the
    # ctx_builder calls into eta_engine.brain regime classifier.
    if hist and len(hist) >= 20:
        recent = hist[-20:]
        ret = (recent[-1].close - recent[0].close) / recent[0].close
        if ret > 0.005:
            regime = "trending_up"
        elif ret < -0.005:
            regime = "trending_down"
        else:
            regime = "choppy"
    else:
        regime = "warmup"
    return {
        "daily_ema": [3000, 3100, 3200, 3300, 3400],
        "h4_struct": "HH_HL",
        "bias": 1,
        "atr_history": [20] * 10,
        "atr_current": 20.0,
        "funding_history": [FundingRate(timestamp=now, symbol=bar.symbol, rate=-0.0008, predicted_rate=-0.0008)] * 8,
        "regime": regime,
        "onchain": {
            "whale_transfers": 40,
            "whale_transfers_baseline": 20,
            "exchange_netflow_usd": -30_000_000.0,
            "active_addresses": 1300,
            "active_addresses_baseline": 1000,
        },
        "sentiment": {
            "galaxy_score": 85.0,
            "alt_rank": 15,
            "social_volume": 600,
            "social_volume_baseline": 200,
            "fear_greed": 20,
        },
    }


def _explain_gate(res, wf) -> list[str]:  # noqa: ANN001
    """Return a list of human-readable reasons why the strict gate failed.

    Empty list = gate passed cleanly.

    The gate is composed of multiple criteria; rather than make the
    operator infer from the table which one tripped, surface each
    failing condition explicitly. New criteria added to walk_forward
    should be reflected here so the explanation stays current.
    """
    reasons: list[str] = []
    # Per-window degradation: any window above the soft 50% line is
    # worth calling out even if the strict gate doesn't fail on it
    # alone, since high per-window degradation usually masks the real
    # cause of an aggregate-DSR pass-fail.
    high_deg_windows = [
        w for w in res.windows
        if w.get("degradation_pct", 0.0) > 0.50
    ]
    if high_deg_windows:
        idxs = ", ".join(str(w["window"]) for w in high_deg_windows)
        reasons.append(
            f"OOS degradation > 50% in window(s): {idxs} "
            f"(strategy IS-overfits in those folds)"
        )
    if res.fold_dsr_median <= 0.5:
        reasons.append(
            f"Per-fold DSR median {res.fold_dsr_median:.3f} <= 0.5 threshold"
        )
    if res.fold_dsr_pass_fraction < wf.fold_dsr_min_pass_fraction:
        reasons.append(
            f"Per-fold DSR pass fraction {res.fold_dsr_pass_fraction * 100:.1f}% "
            f"< {wf.fold_dsr_min_pass_fraction * 100:.0f}% threshold"
        )
    low_trade_windows = [
        w for w in res.windows
        if w.get("oos_trades", 0) < wf.min_trades_per_window
    ]
    if low_trade_windows:
        idxs = ", ".join(str(w["window"]) for w in low_trade_windows)
        reasons.append(
            f"OOS trade count below min ({wf.min_trades_per_window}) "
            f"in window(s): {idxs}"
        )
    return reasons


def main() -> int:
    from eta_engine.backtest import (
        BacktestConfig,
        BarReplay,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.features.pipeline import FeaturePipeline

    bars = BarReplay.synthetic_bars(
        n=4 * 24 * 30,
        drift=0.0010,
        vol=0.004,
        seed=11,
        start=datetime(2025, 1, 1, tzinfo=UTC),
        interval_minutes=15,
    )
    cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=bars[0].symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=7.0,
        max_trades_per_day=10,
    )
    # strict_fold_dsr_gate: adds per-fold robustness check (median fold DSR > 0.5
    # AND >= 50% of folds pass). Turned ON here so the demo surfaces the per-fold
    # layer and a luck-of-the-windows pass no longer slips through on aggregate
    # DSR alone. See backtest/walk_forward.py for gate semantics.
    wf = WalkForwardConfig(
        window_days=7,
        step_days=5,
        anchored=True,
        oos_fraction=0.3,
        min_trades_per_window=1,
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )
    res = WalkForwardEngine().run(
        bars=bars,
        pipeline=FeaturePipeline.default(),
        config=wf,
        base_backtest_config=cfg,
        ctx_builder=_ctx,
    )

    print("EVOLUTIONARY TRADING ALGO -- Walk-Forward Demo (strict per-fold DSR gate)")
    print("=" * 82)
    print(f"Bars: {len(bars)}  anchored={wf.anchored}  windows={len(res.windows)}")
    print("-" * 82)
    print(
        f"{'#':>2} {'IS_Sh':>7} {'OOS_Sh':>7} {'IS_tr':>6} {'OOS_tr':>6} "
        f"{'IS_ret%':>8} {'OOS_ret%':>9} {'deg%':>6} {'DSR':>6}"
    )
    print("-" * 82)
    for w in res.windows:
        print(
            f"{w['window']:>2} {w['is_sharpe']:>7.3f} {w['oos_sharpe']:>7.3f} "
            f"{w['is_trades']:>6} {w['oos_trades']:>6} "
            f"{w['is_return_pct']:>8.2f} {w['oos_return_pct']:>9.2f} "
            f"{w['degradation_pct'] * 100:>6.1f} {w.get('oos_dsr', 0.0):>6.3f}"
        )
    print("-" * 82)
    print(f"Aggregate IS Sharpe:         {res.aggregate_is_sharpe:>8.4f}")
    print(f"Aggregate OOS Sharpe:        {res.aggregate_oos_sharpe:>8.4f}")
    print(f"OOS degradation (avg):       {res.oos_degradation_avg * 100:>7.2f}%")
    print(f"Aggregate Deflated Sharpe:   {res.deflated_sharpe:>8.4f}")
    print(f"Per-fold DSR median:         {res.fold_dsr_median:>8.4f}")
    print(
        f"Per-fold DSR pass fraction:  {res.fold_dsr_pass_fraction * 100:>7.2f}% "
        f"(threshold: {wf.fold_dsr_min_pass_fraction * 100:.0f}%)",
    )
    verdict = "PASS" if res.pass_gate else "FAIL"
    print(f"Gate (strict: DSR+deg+trades + median+pass-frac): {verdict}")
    if not res.pass_gate:
        reasons = _explain_gate(res, wf)
        if reasons:
            print("Why it failed:")
            for r in reasons:
                print(f"  - {r}")
        else:
            # Aggregate gate failed but no per-window reason flagged —
            # likely a strict-DSR aggregate threshold or trade-floor
            # check inside walk_forward.py. Surface what we can.
            print("Why it failed:")
            print(
                f"  - Aggregate DSR {res.deflated_sharpe:.3f} or "
                f"OOS degradation {res.oos_degradation_avg * 100:.1f}% "
                f"failed an aggregate threshold (see backtest/walk_forward.py)"
            )
    print("=" * 82)
    return 0


if __name__ == "__main__":
    sys.exit(main())

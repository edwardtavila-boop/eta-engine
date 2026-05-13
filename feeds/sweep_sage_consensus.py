"""
EVOLUTIONARY TRADING ALGO  //  scripts.sweep_sage_consensus
=============================================================
Restrictive parameter sweep for the pure SageConsensusStrategy.

Initial walk-forward at default thresholds (conv=0.55, align=0.65,
min_bars_between=6, max_trades_per_day=3) on MNQ 5m showed heavy
IS overfit (W0: IS +2.08 / OOS -0.00, W1: IS +1.80 / OOS -2.30).

The diagnosis: too many trades fire (93 IS / 162 IS in W0/W1)
because the gate is too permissive. Sage's ensemble signals
inflate IS Sharpe via small noise but don't replicate OOS.

The fix to test: HIGHER conviction (0.70-0.85) + much longer
cooldowns (24-72 bars between trades) + 1 trade/day cap. This
forces the strategy to fire only on the strongest, most spaced-
out signals — the regime where multi-school agreement actually
predicts OOS continuation.

Grid:
  * min_conviction         ∈ {0.65, 0.70, 0.75, 0.80, 0.85}
  * min_alignment          ∈ {0.70, 0.80}
  * min_bars_between_trades∈ {12, 24, 48}
  * max_trades_per_day     ∈ {1, 2}

Total = 5 × 2 × 3 × 2 = 60 cells.

Output: docs/research_log/sage_consensus_sweep_<ts>.{md,json}.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def _build_grid() -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for conv, align, cooldown, max_trades in itertools.product(
        (0.65, 0.70, 0.75, 0.80, 0.85),
        (0.70, 0.80),
        (12, 24, 48),
        (1, 2),
    ):
        cells.append(
            {
                "min_conviction": conv,
                "min_alignment": align,
                "min_bars_between_trades": cooldown,
                "max_trades_per_day": max_trades,
            }
        )
    return cells


GRID = _build_grid()


def _run_cell(cell: dict[str, Any]) -> dict[str, Any]:
    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.strategies.sage_consensus_strategy import (
        SageConsensusConfig,
        SageConsensusStrategy,
    )

    symbol = os.environ.get("SAGE_CONS_SYMBOL", "MNQ1")
    timeframe = os.environ.get("SAGE_CONS_TIMEFRAME", "5m")
    ds = default_library().get(symbol=symbol, timeframe=timeframe)
    if ds is None:
        return {"error": f"no dataset for {symbol}/{timeframe}"}
    bars = default_library().load_bars(ds, require_positive_prices=True)
    if not bars:
        return {"error": f"no tradable positive-price bars for {symbol}/{timeframe}"}

    backtest_cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=ds.symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )
    wf = WalkForwardConfig(
        window_days=int(os.environ.get("WF_WINDOW_DAYS", "60")),
        step_days=int(os.environ.get("WF_STEP_DAYS", "30")),
        anchored=True,
        oos_fraction=0.3,
        # Lift the floor from 3 to 5: the failure mode at default
        # thresholds was over-trading, so we want windows with 5+
        # OOS trades to be confidence-bearing.
        min_trades_per_window=5,
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )
    cfg = SageConsensusConfig(
        min_conviction=cell["min_conviction"],
        min_alignment=cell["min_alignment"],
        min_consensus=0.30,
        sage_lookback_bars=200,
        atr_period=14,
        atr_stop_mult=1.5,
        rr_target=2.0,
        risk_per_trade_pct=0.01,
        min_bars_between_trades=cell["min_bars_between_trades"],
        max_trades_per_day=cell["max_trades_per_day"],
        warmup_bars=60,
        instrument_class="futures",
        apply_edge_weights=False,
    )

    res = WalkForwardEngine().run(
        bars=bars,
        pipeline=FeaturePipeline.default(),
        config=wf,
        base_backtest_config=backtest_cfg,
        ctx_builder=lambda b, h: {},
        strategy_factory=lambda: SageConsensusStrategy(cfg),
    )
    n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0) > 0)
    total_oos_trades = sum(w.get("oos_trades", 0) for w in res.windows)
    agg_oos_clipped = max(-50.0, min(50.0, res.aggregate_oos_sharpe))
    return {
        **cell,
        "windows": len(res.windows),
        "agg_oos_sharpe": agg_oos_clipped,
        "agg_oos_sharpe_raw": res.aggregate_oos_sharpe,
        "n_pos_oos": n_pos,
        "fold_dsr_median": res.fold_dsr_median,
        "fold_dsr_pass_fraction": res.fold_dsr_pass_fraction,
        "pass_gate": res.pass_gate,
        "total_oos_trades": total_oos_trades,
    }


def _render_md(rows: list[dict[str, Any]]) -> str:
    rows = [r for r in rows if "error" not in r]
    rows.sort(
        key=lambda r: (r.get("agg_oos_sharpe", -999), r.get("fold_dsr_pass_fraction", 0)),
        reverse=True,
    )
    top = rows[:30]
    lines = [
        "# Sage consensus sweep -- MNQ 5m, 60/30",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        f"Total cells: {len(rows)}",
        "",
        "Showing top 30 by agg OOS Sharpe (clipped to [-50, 50]).",
        "",
        "| conv | align | cooldown | max/day | windows | agg_OOS_Sh | +OOS | DSR_med | DSR_pass | trades | gate |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for r in top:
        lines.append(
            f"| {r['min_conviction']:.2f} | {r['min_alignment']:.2f} | "
            f"{r['min_bars_between_trades']} | "
            f"{r['max_trades_per_day']} | {r['windows']} | "
            f"{r.get('agg_oos_sharpe', 0):.3f} | "
            f"{r.get('n_pos_oos', 0)}/{r.get('windows', 0)} | "
            f"{r.get('fold_dsr_median', 0):.3f} | "
            f"{r.get('fold_dsr_pass_fraction', 0) * 100:.0f}% | "
            f"{r.get('total_oos_trades', 0)} | "
            f"{'PASS' if r.get('pass_gate') else 'FAIL'} |"
        )
    return "\n".join(lines)


def main() -> int:
    out_md = ROOT / "docs" / "research_log" / f"sage_consensus_sweep_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md"
    out_json = out_md.with_suffix(".json")
    rows: list[dict[str, Any]] = []
    n_pass = 0
    for i, cell in enumerate(GRID, 1):
        if i % 10 == 0:
            print(f"[{i}/{len(GRID)}] passes so far: {n_pass}")
        try:
            row = _run_cell(cell)
            if row.get("pass_gate") or row.get("agg_oos_sharpe", 0) > 1.0:
                if row.get("pass_gate"):
                    n_pass += 1
                print(
                    f"[{i}/{len(GRID)}] {'PASS' if row.get('pass_gate') else 'NEAR'}: "
                    f"conv={cell['min_conviction']:.2f} align={cell['min_alignment']:.2f} "
                    f"cd={cell['min_bars_between_trades']} max={cell['max_trades_per_day']} -> "
                    f"OOS Sh {row.get('agg_oos_sharpe', 0):.3f}, "
                    f"DSR_pass {row.get('fold_dsr_pass_fraction', 0) * 100:.0f}%, "
                    f"trades={row.get('total_oos_trades', 0)}"
                )
        except Exception as e:  # noqa: BLE001
            row = {**cell, "error": str(e)}
        rows.append(row)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_render_md(rows), encoding="utf-8")
    out_json.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print(f"\n[ok] wrote {out_md}")
    print(f"[summary] {n_pass}/{len(GRID)} cells passed strict gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())

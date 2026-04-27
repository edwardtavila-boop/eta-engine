"""
EVOLUTIONARY TRADING ALGO  //  scripts.sweep_drb_params
========================================================
Parameter grid for the Daily Range Breakout strategy on NQ daily.

Earlier results (2026-04-27): walk-forward at 365/180 windows on
27y of NQ daily history produced agg OOS Sharpe +0.62 to +0.74
across lookbacks 1/5/10. DSR pass 44% — close to the 50% gate
but still under.

This sweep searches a richer grid:

    lookback_days  ∈ {1, 3, 5, 10, 15}
    rr_target      ∈ {1.5, 2.0, 2.5, 3.0}
    atr_stop_mult  ∈ {1.0, 1.5, 2.0}
    ema_bias       ∈ {0, 50, 100, 200}
    min_range_pts  ∈ {0.0, 50.0, 100.0}

    Total = 5 × 4 × 3 × 4 × 3 = 720 cells

Daily bars are cheap so the sweep finishes in minutes rather than
hours. The output mirrors sweep_sage_gated_orb — sorted MD table
+ raw JSON in docs/research_log/.
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


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------


def _build_grid() -> list[dict[str, Any]]:
    cells = []
    for lb, rr, atr_mult, ema, min_rng in itertools.product(
        (1, 3, 5, 10, 15),
        (1.5, 2.0, 2.5, 3.0),
        (1.0, 1.5, 2.0),
        (0, 50, 100, 200),
        (0.0, 50.0, 100.0),
    ):
        cells.append({
            "lookback_days": lb,
            "rr_target": rr,
            "atr_stop_mult": atr_mult,
            "ema_bias_period": ema,
            "min_range_pts": min_rng,
        })
    return cells


GRID = _build_grid()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_cell(cell: dict[str, Any]) -> dict[str, Any]:
    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.strategies.drb_strategy import DRBConfig, DRBStrategy

    symbol = os.environ.get("DRB_SYMBOL", "NQ1")
    timeframe = os.environ.get("DRB_TIMEFRAME", "D")
    ds = default_library().get(symbol=symbol, timeframe=timeframe)
    if ds is None:
        return {"error": f"no dataset for {symbol}/{timeframe}"}
    bars = default_library().load_bars(ds)

    backtest_cfg = BacktestConfig(
        start_date=bars[0].timestamp, end_date=bars[-1].timestamp,
        symbol=ds.symbol, initial_equity=10_000.0,
        risk_per_trade_pct=0.01, confluence_threshold=0.0,
        max_trades_per_day=10,
    )
    wf = WalkForwardConfig(
        window_days=int(os.environ.get("WF_WINDOW_DAYS", "365")),
        step_days=int(os.environ.get("WF_STEP_DAYS", "180")),
        anchored=True,
        oos_fraction=0.3,
        min_trades_per_window=int(os.environ.get("WF_MIN_TRADES", "5")),
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )
    cfg = DRBConfig(
        lookback_days=cell["lookback_days"],
        rr_target=cell["rr_target"],
        atr_period=14,
        atr_stop_mult=cell["atr_stop_mult"],
        risk_per_trade_pct=0.01,
        max_trades_per_day=1,
        ema_bias_period=cell["ema_bias_period"],
        min_range_pts=cell["min_range_pts"],
    )

    res = WalkForwardEngine().run(
        bars=bars,
        pipeline=FeaturePipeline.default(),
        config=wf,
        base_backtest_config=backtest_cfg,
        ctx_builder=lambda b, h: {},
        strategy_factory=lambda: DRBStrategy(cfg),
    )
    n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0) > 0)
    total_oos_trades = sum(w.get("oos_trades", 0) for w in res.windows)
    return {
        **cell,
        "windows": len(res.windows),
        "agg_oos_sharpe": res.aggregate_oos_sharpe,
        "n_pos_oos": n_pos,
        "fold_dsr_median": res.fold_dsr_median,
        "fold_dsr_pass_fraction": res.fold_dsr_pass_fraction,
        "pass_gate": res.pass_gate,
        "total_oos_trades": total_oos_trades,
    }


def _render_md(rows: list[dict[str, Any]]) -> str:
    rows = [r for r in rows if "error" not in r]
    rows.sort(key=lambda r: (r.get("agg_oos_sharpe", -999), r.get("fold_dsr_pass_fraction", 0)), reverse=True)
    top = rows[:30]  # keep table readable
    lines = [
        "# DRB sweep — NQ daily, 365/180",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        f"Total cells: {len(rows)}",
        "",
        "Showing top 30 by agg OOS Sharpe.",
        "",
        "| lb | rr | atr | ema | min_rng | windows | agg_OOS_Sh | +OOS | DSR_med | DSR_pass | trades | gate |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for r in top:
        lines.append(
            f"| {r['lookback_days']} | {r['rr_target']:.1f} | "
            f"{r['atr_stop_mult']:.1f} | {r['ema_bias_period']} | "
            f"{r['min_range_pts']:.0f} | {r['windows']} | "
            f"{r.get('agg_oos_sharpe', 0):.3f} | "
            f"{r.get('n_pos_oos', 0)}/{r.get('windows', 0)} | "
            f"{r.get('fold_dsr_median', 0):.3f} | "
            f"{r.get('fold_dsr_pass_fraction', 0)*100:.0f}% | "
            f"{r.get('total_oos_trades', 0)} | "
            f"{'PASS' if r.get('pass_gate') else 'FAIL'} |"
        )
    return "\n".join(lines)


def main() -> int:
    out_md = (
        ROOT / "docs" / "research_log"
        / f"drb_sweep_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md"
    )
    out_json = out_md.with_suffix(".json")
    rows: list[dict[str, Any]] = []
    n_pass = 0
    for i, cell in enumerate(GRID, 1):
        if i % 50 == 0:
            print(f"[{i}/{len(GRID)}] passes so far: {n_pass}")
        try:
            row = _run_cell(cell)
            if row.get("pass_gate"):
                n_pass += 1
                print(
                    f"[{i}/{len(GRID)}] PASS: lb={cell['lookback_days']} "
                    f"rr={cell['rr_target']:.1f} atr={cell['atr_stop_mult']:.1f} "
                    f"ema={cell['ema_bias_period']} -> "
                    f"OOS Sh {row.get('agg_oos_sharpe', 0):.3f}, "
                    f"DSR_pass {row.get('fold_dsr_pass_fraction', 0)*100:.0f}%"
                )
        except Exception as e:  # noqa: BLE001
            row = {**cell, "error": str(e)}
        rows.append(row)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_render_md(rows), encoding="utf-8")
    out_json.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print(f"\n[ok] wrote {out_md}")
    print(f"[ok] wrote {out_json}")
    print(f"[summary] {n_pass}/{len(GRID)} cells passed the strict gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())

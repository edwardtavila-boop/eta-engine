"""
EVOLUTIONARY TRADING ALGO  //  scripts.sweep_sage_gated_orb
============================================================
Small parameter grid for the sage-overlay thresholds on top of
the promoted ORB baseline. Searches for thresholds that hold
across BOTH walk-forward windows on MNQ 5m (avoiding the
overfit IS / blow-up OOS pattern of the unguarded sage strategy).

Grid: min_conviction × min_alignment × range_minutes (3×3×2 = 18
cells). Each cell runs the same WalkForwardConfig the ORB
baseline used (60d/30d, anchored, OOS 0.3, gate strict).

Output: docs/research_log/sage_gated_orb_sweep_<ts>.md with the
top cells by aggregate OOS Sharpe + DSR pass.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------

# Keep small — sage runs ~22 schools per bar, so the inner loop is heavy.
# 18 cells × ~2 windows × ~20k bars = manageable but not free.
GRID: list[dict[str, Any]] = []
for conv in (0.45, 0.55, 0.65):
    for align in (0.55, 0.65, 0.75):
        for rng in (15, 30):
            GRID.append({
                "min_conviction": conv,
                "min_alignment": align,
                "range_minutes": rng,
            })


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_cell(cell: dict[str, Any]) -> dict[str, Any]:
    """Run one cell's walk-forward. Returns the metrics dict."""
    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.strategies.orb_strategy import ORBConfig
    from eta_engine.strategies.sage_consensus_strategy import SageConsensusConfig
    from eta_engine.strategies.sage_gated_orb_strategy import (
        SageGatedORBConfig,
        SageGatedORBStrategy,
    )

    symbol = os.environ.get("MNQ_SYMBOL", "MNQ1")
    timeframe = os.environ.get("MNQ_TIMEFRAME", "5m")
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
        window_days=60, step_days=30, anchored=True,
        oos_fraction=0.3, min_trades_per_window=3,
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )
    cfg = SageGatedORBConfig(
        orb=ORBConfig(
            range_minutes=cell["range_minutes"],
            rth_open_local=time(9, 30),
            rth_close_local=time(16, 0),
            max_entry_local=time(11, 0),
            flatten_at_local=time(15, 55),
            timezone_name="America/New_York",
            ema_bias_period=200, rr_target=2.0,
            atr_period=14, atr_stop_mult=2.0,
            risk_per_trade_pct=0.01, max_trades_per_day=1,
        ),
        sage=SageConsensusConfig(
            min_conviction=cell["min_conviction"],
            min_alignment=cell["min_alignment"],
            min_consensus=0.30,
            sage_lookback_bars=200,
            atr_period=14, atr_stop_mult=1.5, rr_target=2.0,
            risk_per_trade_pct=0.01,
            min_bars_between_trades=6, max_trades_per_day=1,
            warmup_bars=60, instrument_class="futures",
            apply_edge_weights=False,
        ),
        overlay_enabled=True,
    )

    res = WalkForwardEngine().run(
        bars=bars,
        pipeline=FeaturePipeline.default(),
        config=wf,
        base_backtest_config=backtest_cfg,
        ctx_builder=lambda b, h: {},
        strategy_factory=lambda: SageGatedORBStrategy(cfg),
    )
    n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0) > 0)
    return {
        **cell,
        "windows": len(res.windows),
        "agg_oos_sharpe": res.aggregate_oos_sharpe,
        "n_pos_oos": n_pos,
        "fold_dsr_median": res.fold_dsr_median,
        "fold_dsr_pass_fraction": res.fold_dsr_pass_fraction,
        "pass_gate": res.pass_gate,
    }


def _render_md(rows: list[dict[str, Any]]) -> str:
    """Markdown table sorted by agg_oos_sharpe desc."""
    rows = [r for r in rows if "error" not in r]
    rows.sort(key=lambda r: r.get("agg_oos_sharpe", -999), reverse=True)
    lines = [
        "# sage-gated ORB sweep — MNQ 5m",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        f"Cells: {len(rows)}",
        "",
        "| conv | align | range | windows | agg_OOS_Sh | +OOS | DSR_med | DSR_pass | gate |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['min_conviction']:.2f} | {r['min_alignment']:.2f} | "
            f"{r['range_minutes']} | {r['windows']} | "
            f"{r.get('agg_oos_sharpe', 0):.3f} | "
            f"{r.get('n_pos_oos', 0)}/{r.get('windows', 0)} | "
            f"{r.get('fold_dsr_median', 0):.3f} | "
            f"{r.get('fold_dsr_pass_fraction', 0)*100:.0f}% | "
            f"{'PASS' if r.get('pass_gate') else 'FAIL'} |"
        )
    return "\n".join(lines)


def main() -> int:
    out_md = (
        ROOT / "docs" / "research_log"
        / f"sage_gated_orb_sweep_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md"
    )
    out_json = out_md.with_suffix(".json")
    rows: list[dict[str, Any]] = []
    for i, cell in enumerate(GRID, 1):
        print(f"[{i}/{len(GRID)}] cell={cell}")
        try:
            row = _run_cell(cell)
            print(
                f"  -> agg_OOS_Sh={row.get('agg_oos_sharpe', 0):.3f}  "
                f"DSR_pass={row.get('fold_dsr_pass_fraction', 0)*100:.0f}%  "
                f"gate={'PASS' if row.get('pass_gate') else 'FAIL'}"
            )
        except Exception as e:  # noqa: BLE001 - sweep should not crash
            print(f"  -> ERROR: {e!r}")
            row = {**cell, "error": str(e)}
        rows.append(row)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_render_md(rows), encoding="utf-8")
    out_json.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print(f"\n[ok] wrote {out_md}")
    print(f"[ok] wrote {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

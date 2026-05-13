"""
EVOLUTIONARY TRADING ALGO  //  scripts.sweep_crypto_sage_orb
==============================================================
Crypto-specific sage-overlay sweep on BTC 1h.

The MNQ sage sweep (sweep_sage_gated_orb.py) found conv=0.65 wins
for index futures. On BTC the same conv=0.65 produces numerical
blowup (-2.7e15 OOS Sharpe) because the overlay filters trade
count down to 1-5 per OOS window — too few for stable Sharpe.

This sweep searches a CRYPTO-tuned region:

  * min_conviction      ∈ {0.35, 0.40, 0.45, 0.50, 0.55}
  * min_alignment       ∈ {0.50, 0.60}
  * range_minutes       ∈ {30, 60, 90}
  * sage_lookback_bars  ∈ {200, 400, 600}
  * instrument_class    ∈ {"crypto", "futures"}

Total = 5 × 2 × 3 × 3 × 2 = 180 cells.

A probe at conv=0.45 + lookback=400 already produced agg IS Sh
+2.25, agg OOS Sh +1.47, 6/9 +OOS, DSR pass 44% — close to the
strict 50% gate. The full sweep should find a passing region
nearby.

Output: docs/research_log/crypto_sage_sweep_<ts>.{md,json} with
the top cells sorted by agg OOS Sharpe + DSR pass.
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
    cells: list[dict[str, Any]] = []
    for conv, align, rng, lb, ic in itertools.product(
        (0.35, 0.40, 0.45, 0.50, 0.55),
        (0.50, 0.60),
        (30, 60, 90),
        (200, 400, 600),
        ("crypto", "futures"),
    ):
        cells.append(
            {
                "min_conviction": conv,
                "min_alignment": align,
                "range_minutes": rng,
                "sage_lookback_bars": lb,
                "instrument_class": ic,
            }
        )
    return cells


GRID = _build_grid()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_cell(cell: dict[str, Any]) -> dict[str, Any]:
    from datetime import time

    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.strategies.crypto_orb_strategy import CryptoORBConfig
    from eta_engine.strategies.sage_consensus_strategy import SageConsensusConfig
    from eta_engine.strategies.sage_gated_orb_strategy import (
        SageGatedORBConfig,
        SageGatedORBStrategy,
    )

    symbol = os.environ.get("CRYPTO_SAGE_SYMBOL", "BTC")
    timeframe = os.environ.get("CRYPTO_SAGE_TIMEFRAME", "1h")
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
        window_days=int(os.environ.get("WF_WINDOW_DAYS", "90")),
        step_days=int(os.environ.get("WF_STEP_DAYS", "30")),
        anchored=True,
        oos_fraction=0.3,
        # Higher floor — refuse to score windows with <5 OOS trades
        # (otherwise tiny Ns produce unstable Sharpes that pollute
        # the aggregate).
        min_trades_per_window=5,
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )
    cfg = SageGatedORBConfig(
        orb=CryptoORBConfig(
            range_minutes=cell["range_minutes"],
            # Re-pin the rest of CryptoORBConfig defaults explicitly
            # so the cell is self-contained.
            rth_open_local=time(0, 0),
            rth_close_local=time(23, 59),
            max_entry_local=time(6, 0),
            flatten_at_local=time(23, 55),
            timezone_name="UTC",
            atr_stop_mult=2.5,
            rr_target=2.5,
            ema_bias_period=100,
            max_trades_per_day=2,
        ),
        sage=SageConsensusConfig(
            min_conviction=cell["min_conviction"],
            min_alignment=cell["min_alignment"],
            min_consensus=0.30,
            sage_lookback_bars=cell["sage_lookback_bars"],
            atr_period=14,
            atr_stop_mult=1.5,
            rr_target=2.0,
            risk_per_trade_pct=0.01,
            min_bars_between_trades=6,
            max_trades_per_day=2,
            warmup_bars=60,
            instrument_class=cell["instrument_class"],
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
    total_oos_trades = sum(w.get("oos_trades", 0) for w in res.windows)
    # Clip the aggregate Sharpe so a single window's numerical blowup
    # doesn't poison cross-cell sorting. Real-world Sharpes well
    # outside [-50, 50] are physically implausible.
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


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _render_md(rows: list[dict[str, Any]]) -> str:
    rows = [r for r in rows if "error" not in r]
    rows.sort(
        key=lambda r: (r.get("agg_oos_sharpe", -999), r.get("fold_dsr_pass_fraction", 0)),
        reverse=True,
    )
    top = rows[:30]
    lines = [
        "# Crypto sage-overlay sweep -- BTC 1h, 90/30",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        f"Total cells: {len(rows)}",
        "",
        "Showing top 30 by agg OOS Sharpe (clipped to [-50, 50]).",
        "",
        (
            "| conv | align | range | lookback | instr | windows "
            "| agg_OOS_Sh | +OOS | DSR_med | DSR_pass | trades | gate |"
        ),
        "|---:|---:|---:|---:|:---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for r in top:
        lines.append(
            f"| {r['min_conviction']:.2f} | {r['min_alignment']:.2f} | "
            f"{r['range_minutes']} | {r['sage_lookback_bars']} | "
            f"{r['instrument_class']} | {r['windows']} | "
            f"{r.get('agg_oos_sharpe', 0):.3f} | "
            f"{r.get('n_pos_oos', 0)}/{r.get('windows', 0)} | "
            f"{r.get('fold_dsr_median', 0):.3f} | "
            f"{r.get('fold_dsr_pass_fraction', 0) * 100:.0f}% | "
            f"{r.get('total_oos_trades', 0)} | "
            f"{'PASS' if r.get('pass_gate') else 'FAIL'} |"
        )
    return "\n".join(lines)


def main() -> int:
    out_md = ROOT / "docs" / "research_log" / f"crypto_sage_sweep_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md"
    out_json = out_md.with_suffix(".json")
    rows: list[dict[str, Any]] = []
    n_pass = 0
    for i, cell in enumerate(GRID, 1):
        if i % 25 == 0:
            print(f"[{i}/{len(GRID)}] passes so far: {n_pass}")
        try:
            row = _run_cell(cell)
            if row.get("pass_gate") or row.get("agg_oos_sharpe", 0) > 1.5:
                n_pass += 1 if row.get("pass_gate") else 0
                print(
                    f"[{i}/{len(GRID)}] {'PASS' if row.get('pass_gate') else 'NEAR'}: "
                    f"conv={cell['min_conviction']:.2f} align={cell['min_alignment']:.2f} "
                    f"rng={cell['range_minutes']} lb={cell['sage_lookback_bars']} "
                    f"ic={cell['instrument_class']} -> "
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

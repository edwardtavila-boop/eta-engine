"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_foundation_supercharge_sweep
========================================================================
Multi-cell sweep over the foundation strategy coverage matrix.

User mandate (2026-04-27):
"to be marked truly done make sure all strategies are now reflecting
our new strategy logic and they're optimized for most trades at
highest oos and is they need to be supercharged".

Mechanic
--------
For each (asset, strategy) cell in the coverage matrix, run a
focused parameter sweep and record:
* Aggregate OOS Sharpe
* Aggregate IS Sharpe
* Total OOS trades
* Positive-OOS fold fraction
* Per-fold DSR pass fraction
* Composite score = OOS_Sharpe * sqrt(trade_count) (rewards both
  edge AND volume — a +2.0 Sharpe with 100 trades beats +3.0 with
  10 trades on this metric since the latter is statistically thin)
* Gate pass

Then print the top config per cell so the user can pick the
ones to promote.

Cells covered
-------------
* BTC + ETH + SOL × {compression, sweep, regime_trend}
* MNQ + NQ × {compression, sweep, regime_trend, ORB}

(ORB has its own existing harness; included here for parity.)

Usage
-----
    python -m eta_engine.scripts.run_foundation_supercharge_sweep
    python -m eta_engine.scripts.run_foundation_supercharge_sweep \\
        --assets BTC --strategies compression
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

DEFAULT_ASSETS = ("BTC", "ETH", "SOL", "MNQ1", "NQ1")
DEFAULT_STRATEGIES = ("compression", "sweep")
DEFAULT_OUT_JSON = (
    ROOT / "docs" / "research_log" / "foundation_supercharge_sweep_results.json"
)


def _slug(parts: list[str]) -> str:
    return "-".join(p.lower().replace("/", "-") for p in parts)


def _resolve_out_json(
    out_json: Path | None,
    assets: list[str],
    strategies: list[str],
) -> Path:
    if out_json is not None:
        return out_json
    if assets == list(DEFAULT_ASSETS) and strategies == list(DEFAULT_STRATEGIES):
        return DEFAULT_OUT_JSON
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    scope = f"{_slug(assets)}_{_slug(strategies)}"
    return (
        ROOT
        / "docs"
        / "research_log"
        / f"foundation_supercharge_sweep_results_{scope}_{stamp}.json"
    )


# ---------------------------------------------------------------------------
# Sweep grid per strategy
# ---------------------------------------------------------------------------


def _compression_sweep_grid() -> list[dict]:
    """Compression-breakout parameter grid (4 cells)."""
    return [
        {"bb_width_max_percentile": 0.30, "rr_target": 2.0, "atr_stop_mult_factor": 1.0},
        {"bb_width_max_percentile": 0.30, "rr_target": 2.5, "atr_stop_mult_factor": 1.0},
        {"bb_width_max_percentile": 0.40, "rr_target": 2.5, "atr_stop_mult_factor": 1.0},
        {"bb_width_max_percentile": 0.50, "rr_target": 2.0, "atr_stop_mult_factor": 1.0},
    ]


def _compression_tight_grid() -> list[dict]:
    """Tighter compression sweep — fewer but higher-quality entries.

    Built 2026-04-27 after the default sweep showed BTC compression
    at +0.50 OOS / 358 trades but DSR pass only 28% (close-but-not).
    The hypothesis: tightening volume + close-location + cooldown
    gates removes marginal trades and lifts per-fold DSR.
    """
    return [
        {
            "bb_width_max_percentile": 0.30, "rr_target": 2.5,
            "atr_stop_mult_factor": 1.0,
            "min_volume_z": 1.0, "min_close_location": 0.80,
            "min_bars_between_trades": 24,
        },
        {
            "bb_width_max_percentile": 0.20, "rr_target": 2.5,
            "atr_stop_mult_factor": 1.0,
            "min_volume_z": 0.5, "min_close_location": 0.70,
            "min_bars_between_trades": 12,
        },
        {
            "bb_width_max_percentile": 0.30, "rr_target": 3.0,
            "atr_stop_mult_factor": 1.0,
            "min_volume_z": 0.5, "min_close_location": 0.70,
            "min_bars_between_trades": 12,
        },
        {
            "bb_width_max_percentile": 0.30, "rr_target": 2.5,
            "atr_stop_mult_factor": 1.0,
            "min_volume_z": 0.8, "min_close_location": 0.80,
            "min_bars_between_trades": 24,
        },
        {
            "bb_width_max_percentile": 0.20, "rr_target": 3.0,
            "atr_stop_mult_factor": 1.2,   # wider stop for more durability
            "min_volume_z": 1.0, "min_close_location": 0.75,
            "min_bars_between_trades": 24,
        },
    ]


def _sweep_sweep_grid() -> list[dict]:
    """Sweep-reclaim parameter grid (4 cells)."""
    return [
        {"min_wick_pct_factor": 1.0, "rr_target": 2.0, "level_lookback_factor": 1.0},
        {"min_wick_pct_factor": 0.7, "rr_target": 2.0, "level_lookback_factor": 1.0},
        {"min_wick_pct_factor": 1.0, "rr_target": 2.5, "level_lookback_factor": 1.0},
        {"min_wick_pct_factor": 0.7, "rr_target": 2.5, "level_lookback_factor": 1.5},
    ]


# ---------------------------------------------------------------------------
# Strategy + preset wiring
# ---------------------------------------------------------------------------


def _build_compression_factory(symbol: str, params: dict) -> Any:  # noqa: ANN401
    from dataclasses import replace

    from eta_engine.strategies.compression_breakout_strategy import (
        CompressionBreakoutStrategy,
        btc_compression_preset,
        eth_compression_preset,
        mnq_compression_preset,
        nq_compression_preset,
        sol_compression_preset,
    )

    preset_map = {
        "BTC": btc_compression_preset, "ETH": eth_compression_preset,
        "SOL": sol_compression_preset, "MNQ1": mnq_compression_preset,
        "NQ1": nq_compression_preset,
    }
    factory = preset_map.get(symbol)
    if factory is None:
        return None
    base_cfg = factory()
    overrides: dict = {
        "bb_width_max_percentile": params["bb_width_max_percentile"],
        "rr_target": params["rr_target"],
        "atr_stop_mult": base_cfg.atr_stop_mult * params["atr_stop_mult_factor"],
    }
    # Optional tight-grid knobs
    for k in ("min_volume_z", "min_close_location",
              "min_bars_between_trades"):
        if k in params:
            overrides[k] = params[k]
    cfg = replace(base_cfg, **overrides)
    return lambda: CompressionBreakoutStrategy(cfg)


def _build_sweep_factory(symbol: str, params: dict) -> Any:  # noqa: ANN401
    from dataclasses import replace

    from eta_engine.strategies.sweep_reclaim_strategy import (
        SweepReclaimStrategy,
        btc_daily_sweep_preset,
        eth_daily_sweep_preset,
        mnq_intraday_sweep_preset,
        nq_intraday_sweep_preset,
        sol_daily_sweep_preset,
    )

    preset_map = {
        "BTC": btc_daily_sweep_preset, "ETH": eth_daily_sweep_preset,
        "SOL": sol_daily_sweep_preset, "MNQ1": mnq_intraday_sweep_preset,
        "NQ1": nq_intraday_sweep_preset,
    }
    factory = preset_map.get(symbol)
    if factory is None:
        return None
    base_cfg = factory()
    cfg = replace(
        base_cfg,
        min_wick_pct=base_cfg.min_wick_pct * params["min_wick_pct_factor"],
        rr_target=params["rr_target"],
        level_lookback=int(base_cfg.level_lookback * params["level_lookback_factor"]),
    )
    return lambda: SweepReclaimStrategy(cfg)


# ---------------------------------------------------------------------------
# WF runner
# ---------------------------------------------------------------------------


def _run_wf(symbol: str, timeframe: str, factory: Any, *,  # noqa: ANN401
            window_days: int, step_days: int) -> dict:
    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline

    ds = default_library().get(symbol=symbol, timeframe=timeframe)
    if ds is None:
        return {"err": f"no dataset for {symbol}/{timeframe}"}
    bars = default_library().load_bars(ds, require_positive_prices=True)
    if not bars:
        return {"err": f"no tradable positive-price bars for {symbol}/{timeframe}"}

    backtest_cfg = BacktestConfig(
        start_date=bars[0].timestamp, end_date=bars[-1].timestamp,
        symbol=ds.symbol, initial_equity=10_000.0,
        risk_per_trade_pct=0.005, confluence_threshold=0.0,
        max_trades_per_day=10,
    )
    wf = WalkForwardConfig(
        window_days=window_days, step_days=step_days,
        anchored=True, oos_fraction=0.3,
        min_trades_per_window=3,
        strict_fold_dsr_gate=True, fold_dsr_min_pass_fraction=0.5,
    )

    res = WalkForwardEngine().run(
        bars=bars, pipeline=FeaturePipeline.default(),
        config=wf, base_backtest_config=backtest_cfg,
        ctx_builder=lambda b, h: {}, strategy_factory=factory,
    )

    n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0) > 0)
    n_total = len(res.windows)
    n_oos = sum(w.get("oos_trades", 0) for w in res.windows)
    n_is = sum(w.get("is_trades", 0) for w in res.windows)
    composite = res.aggregate_oos_sharpe * math.sqrt(max(n_oos, 1))
    return {
        "windows": n_total,
        "is_sharpe": res.aggregate_is_sharpe,
        "oos_sharpe": res.aggregate_oos_sharpe,
        "is_trades": n_is,
        "oos_trades": n_oos,
        "pos_oos_frac": n_pos / max(n_total, 1),
        "deg_avg": res.oos_degradation_avg,
        "dsr_pass_frac": res.fold_dsr_pass_fraction,
        "gate_pass": res.pass_gate,
        "composite_score": composite,
    }


# ---------------------------------------------------------------------------
# Sweep driver
# ---------------------------------------------------------------------------


_TIMEFRAME_BY_ASSET = {
    "BTC": "1h", "ETH": "1h", "SOL": "1h",
    "MNQ1": "5m", "NQ1": "5m",
}

_WF_CFG_BY_ASSET_TF = {
    ("BTC", "1h"): (90, 30),
    ("ETH", "1h"): (90, 30),
    ("SOL", "1h"): (90, 30),
    ("MNQ1", "5m"): (60, 30),
    ("NQ1", "5m"): (60, 30),
}


def _print_cell_table(asset: str, strategy: str, results: list[dict]) -> None:
    print(f"\n=== {asset} × {strategy} ===")
    print(f"{'#':>2}  {'IS_Sh':>7}  {'OOS_Sh':>7}  {'IS_tr':>5}  {'OOS_tr':>6}"
          f"  {'+OOS%':>5}  {'DSR%':>5}  {'comp':>6}  {'gate':>5}  params")
    for i, r in enumerate(results):
        if "err" in r:
            print(f"{i:>2}  ERR: {r['err']}")
            continue
        gate = "PASS" if r["gate_pass"] else "FAIL"
        print(
            f"{i:>2}  {r['is_sharpe']:>7.3f}  {r['oos_sharpe']:>7.3f}"
            f"  {r['is_trades']:>5}  {r['oos_trades']:>6}"
            f"  {r['pos_oos_frac'] * 100:>4.0f}%"
            f"  {r['dsr_pass_frac'] * 100:>4.0f}%"
            f"  {r['composite_score']:>6.2f}"
            f"  {gate:>5}  {r['params']}"
        )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--assets", default="BTC,ETH,SOL,MNQ1,NQ1",
        help="Comma-separated asset symbols",
    )
    p.add_argument(
        "--strategies", default="compression,sweep",
        help="Comma-separated: compression, sweep",
    )
    p.add_argument(
        "--out-json", type=Path,
        default=None,
        help=(
            "Output JSON path. Defaults to the canonical aggregate artifact "
            "only for the full default asset/strategy sweep; scoped sweeps "
            "write timestamped scoped artifacts unless this is explicit."
        ),
    )
    args = p.parse_args()

    assets = [a.strip() for a in args.assets.split(",") if a.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    out_json = _resolve_out_json(args.out_json, assets, strategies)

    print(f"[supercharge-sweep] assets={assets} strategies={strategies}")
    print(f"[supercharge-sweep] timestamp={datetime.now(UTC).isoformat()}")
    if args.out_json is None and out_json != DEFAULT_OUT_JSON:
        print(
            "[supercharge-sweep] scoped run detected; writing scoped artifact "
            f"instead of clobbering {DEFAULT_OUT_JSON}",
        )

    all_results: dict = {}
    for asset in assets:
        timeframe = _TIMEFRAME_BY_ASSET.get(asset)
        if timeframe is None:
            print(f"WARN: unknown asset '{asset}'; skipping")
            continue
        window_days, step_days = _WF_CFG_BY_ASSET_TF[(asset, timeframe)]
        for strategy in strategies:
            cell_key = f"{asset}/{strategy}"
            if strategy == "compression":
                grid = _compression_sweep_grid()
                builder = _build_compression_factory
            elif strategy == "compression_tight":
                grid = _compression_tight_grid()
                builder = _build_compression_factory
            elif strategy == "sweep":
                grid = _sweep_sweep_grid()
                builder = _build_sweep_factory
            else:
                print(f"WARN: unknown strategy '{strategy}'; skipping")
                continue
            cell_results: list[dict] = []
            for params in grid:
                factory = builder(asset, params)
                if factory is None:
                    cell_results.append({"params": params, "err": "no preset"})
                    continue
                try:
                    res = _run_wf(
                        asset, timeframe, factory,
                        window_days=window_days, step_days=step_days,
                    )
                    res["params"] = params
                    cell_results.append(res)
                except Exception as e:  # noqa: BLE001
                    cell_results.append({"params": params, "err": str(e)})
            _print_cell_table(asset, strategy, cell_results)
            all_results[cell_key] = cell_results

    # Best-per-cell summary
    print("\n\n=== BEST CONFIG PER CELL (by composite_score, gate=PASS only) ===")
    print(f"{'Cell':<20}  {'IS_Sh':>7}  {'OOS_Sh':>7}  {'OOS_tr':>6}"
          f"  {'comp':>6}  params")
    for cell_key, results in all_results.items():
        passing = [r for r in results if r.get("gate_pass")]
        if not passing:
            print(f"{cell_key:<20}  no PASS configs")
            continue
        best = max(passing, key=lambda r: r["composite_score"])
        print(
            f"{cell_key:<20}  {best['is_sharpe']:>7.3f}"
            f"  {best['oos_sharpe']:>7.3f}  {best['oos_trades']:>6}"
            f"  {best['composite_score']:>6.2f}  {best['params']}"
        )

    # Persist for follow-on registry promotion
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n[supercharge-sweep] results -> {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_research_grid
=========================================================
Sweep a matrix of (symbol, timeframe, scorer, threshold, gate) and
emit one comparison table so we can see at a glance which slice of
the configuration space the strategy holds up on.

Why this exists
---------------
We've been running walk-forward one config at a time, copying
numbers between research-log entries by hand. That doesn't scale
once we have 33 datasets × 2 scorers × multiple gate options. This
harness runs the matrix in one shot and writes a single dated
markdown report that's directly comparable across rows.

Default matrix
--------------
Picks the longest-history dataset per (symbol, timeframe) via the
data library. Tries the global scorer + the MNQ-tuned scorer.
Tries gated + ungated. Six configs total by default; override the
list at the top of ``main()`` for a custom run.

Output
------
* stdout — single comparison table
* ``docs/research_log/research_grid_<utc-stamp>.md`` — the same
  table in markdown, plus per-row details. Re-runnable; the
  filename contains a timestamp so previous runs are preserved.

This becomes the "did anything regress" smoke test for any change
to the engines, scorers, or feature pipeline.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class ResearchCell:
    """One row in the research matrix."""

    label: str
    symbol: str
    timeframe: str
    scorer_name: str  # "global" or "mnq"
    threshold: float
    block_regimes: frozenset[str] | None
    window_days: int
    step_days: int
    min_trades_per_window: int
    strategy_kind: str = "confluence"  # "confluence" or "orb"
    # Per-bot strategy knobs forwarded from per_bot_registry.extras.
    # Picked up by _build_crypto_strategy_factory; ignored by the
    # confluence/orb/drb branches that have no per-bot overrides yet.
    extras: dict[str, object] = field(default_factory=dict)


@dataclass
class CellResult:
    cell: ResearchCell
    n_windows: int
    n_positive_oos: int
    agg_is_sharpe: float
    agg_oos_sharpe: float
    avg_oos_degradation: float
    deflated_sharpe: float
    fold_dsr_median: float
    fold_dsr_pass_fraction: float
    pass_gate: bool
    note: str = ""


def _filter_extras(extras: dict[str, object], prefix: str) -> dict[str, object]:
    """Pick the subset of extras prefixed with ``<prefix>_`` and strip
    the prefix so they map directly to dataclass field names.

    Example: extras={"crypto_orb_range_minutes": 240, "deactivated": True}
             _filter_extras(extras, "crypto_orb") -> {"range_minutes": 240}
    """
    pre = f"{prefix}_"
    return {k[len(pre):]: v for k, v in extras.items() if k.startswith(pre)}


def _build_crypto_strategy_factory(  # type: ignore[no-untyped-def]  # noqa: ANN202
    kind: str, extras: dict[str, object] | None = None,
):
    """Return a zero-arg factory that builds a fresh crypto strategy
    instance per walk-forward window. Per-bot extras prefixed with the
    strategy_kind (e.g. ``crypto_orb_range_minutes``) get applied to the
    config dataclass; unknown keys are silently ignored so the registry
    can carry non-strategy fields too."""
    extras = extras or {}
    if kind == "crypto_orb":
        from eta_engine.strategies.crypto_orb_strategy import (
            CryptoORBConfig,
            crypto_orb_strategy,
        )
        cfg = CryptoORBConfig(**_filter_extras(extras, "crypto_orb"))
        return lambda: crypto_orb_strategy(cfg)
    if kind == "crypto_trend":
        from eta_engine.strategies.crypto_trend_strategy import (
            CryptoTrendConfig,
            CryptoTrendStrategy,
        )
        cfg = CryptoTrendConfig(**_filter_extras(extras, "crypto_trend"))
        return lambda: CryptoTrendStrategy(cfg)
    if kind == "crypto_meanrev":
        from eta_engine.strategies.crypto_meanrev_strategy import (
            CryptoMeanRevConfig,
            CryptoMeanRevStrategy,
        )
        cfg = CryptoMeanRevConfig(**_filter_extras(extras, "crypto_meanrev"))
        return lambda: CryptoMeanRevStrategy(cfg)
    if kind == "crypto_scalp":
        from eta_engine.strategies.crypto_scalp_strategy import (
            CryptoScalpConfig,
            CryptoScalpStrategy,
        )
        cfg = CryptoScalpConfig(**_filter_extras(extras, "crypto_scalp"))
        return lambda: CryptoScalpStrategy(cfg)
    if kind == "grid":
        from eta_engine.strategies.grid_trading_strategy import (
            GridConfig,
            GridTradingStrategy,
        )
        cfg = GridConfig(**_filter_extras(extras, "grid"))
        return lambda: GridTradingStrategy(cfg)
    msg = f"unknown crypto strategy_kind: {kind!r}"
    raise ValueError(msg)


def _resolve_scorer(name: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    from eta_engine.core.confluence_scorer import (
        score_confluence,
        score_confluence_btc,
        score_confluence_mnq,
    )

    return {
        "global": score_confluence,
        "mnq": score_confluence_mnq,
        "btc": score_confluence_btc,
    }[name]


def run_cell(cell: ResearchCell) -> CellResult:
    """Run one walk-forward sweep and return the headline stats."""
    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.scripts.run_walk_forward_mnq_real import _ctx

    ds = default_library().get(symbol=cell.symbol, timeframe=cell.timeframe)
    if ds is None:
        return CellResult(
            cell=cell, n_windows=0, n_positive_oos=0,
            agg_is_sharpe=0.0, agg_oos_sharpe=0.0,
            avg_oos_degradation=0.0, deflated_sharpe=0.0,
            fold_dsr_median=0.0, fold_dsr_pass_fraction=0.0,
            pass_gate=False, note=f"NO_DATA: {cell.symbol}/{cell.timeframe}",
        )

    bars = default_library().load_bars(ds)
    if not bars:
        return CellResult(
            cell=cell, n_windows=0, n_positive_oos=0,
            agg_is_sharpe=0.0, agg_oos_sharpe=0.0,
            avg_oos_degradation=0.0, deflated_sharpe=0.0,
            fold_dsr_median=0.0, fold_dsr_pass_fraction=0.0,
            pass_gate=False, note="EMPTY_BARS",
        )

    base_cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=ds.symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=cell.threshold,
        max_trades_per_day=10,
    )
    wf = WalkForwardConfig(
        window_days=cell.window_days,
        step_days=cell.step_days,
        anchored=True,
        oos_fraction=0.3,
        min_trades_per_window=cell.min_trades_per_window,
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )
    # ORB strategy bypasses the scorer/regime/ctx path entirely.
    if cell.strategy_kind == "orb":
        from eta_engine.strategies.orb_strategy import ORBConfig, ORBStrategy
        orb_cfg = ORBConfig()  # defaults; per-bot overrides via extras later
        res = WalkForwardEngine().run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=wf,
            base_backtest_config=base_cfg,
            ctx_builder=lambda b, h: {},
            strategy_factory=lambda: ORBStrategy(orb_cfg),
        )
    elif cell.strategy_kind == "drb":
        # DRB is the daily-timeframe sibling of ORB. It also bypasses
        # the confluence-scorer path — without this branch the DRB bots
        # silently fell through to confluence on daily bars and produced
        # nonsense (OOS Sharpe 1e+14). 2026-04-27.
        from eta_engine.strategies.drb_strategy import DRBConfig, DRBStrategy
        drb_cfg = DRBConfig()
        res = WalkForwardEngine().run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=wf,
            base_backtest_config=base_cfg,
            ctx_builder=lambda b, h: {},
            strategy_factory=lambda: DRBStrategy(drb_cfg),
        )
    elif cell.strategy_kind in (
        "crypto_orb", "crypto_trend", "crypto_meanrev", "crypto_scalp", "grid",
    ):
        # Crypto-specific strategy variants. All share the same
        # maybe_enter(bar, hist, equity, config) -> _Open|None contract
        # as ORB/DRB, so they bypass the confluence-scorer path. The
        # registry wires per-bot defaults; per-bot extras can override
        # individual knobs once we start sweeping params per bot.
        factory = _build_crypto_strategy_factory(cell.strategy_kind, cell.extras)
        res = WalkForwardEngine().run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=wf,
            base_backtest_config=base_cfg,
            ctx_builder=lambda b, h: {},
            strategy_factory=factory,
        )
    else:
        res = WalkForwardEngine().run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=wf,
            base_backtest_config=base_cfg,
            ctx_builder=_ctx,
            scorer=_resolve_scorer(cell.scorer_name),
            block_regimes=cell.block_regimes,
        )
    n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0.0) > 0)
    return CellResult(
        cell=cell,
        n_windows=len(res.windows),
        n_positive_oos=n_pos,
        agg_is_sharpe=res.aggregate_is_sharpe,
        agg_oos_sharpe=res.aggregate_oos_sharpe,
        avg_oos_degradation=res.oos_degradation_avg,
        deflated_sharpe=res.deflated_sharpe,
        fold_dsr_median=res.fold_dsr_median,
        fold_dsr_pass_fraction=res.fold_dsr_pass_fraction,
        pass_gate=res.pass_gate,
        note=f"{ds.row_count} bars / {ds.days_span():.0f}d",
    )


def render_table(results: Sequence[CellResult]) -> str:
    header = (
        "| Config | Sym/TF | Scorer | Thr | Gate | W | +OOS | IS Sh | "
        "OOS Sh | Deg% | DSR med | DSR pass% | Verdict | Note |"
    )
    lines = [
        header,
        "|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in results:
        gate_str = (
            ("/".join(sorted(r.cell.block_regimes)) if r.cell.block_regimes else "—")
            if r.cell.block_regimes else "—"
        )
        verdict = "PASS" if r.pass_gate else "FAIL"
        lines.append(
            f"| {r.cell.label} | {r.cell.symbol}/{r.cell.timeframe} | {r.cell.scorer_name} | "
            f"{r.cell.threshold:.1f} | {gate_str} | {r.n_windows} | {r.n_positive_oos} | "
            f"{r.agg_is_sharpe:.3f} | {r.agg_oos_sharpe:.3f} | "
            f"{r.avg_oos_degradation * 100:.1f} | {r.fold_dsr_median:.3f} | "
            f"{r.fold_dsr_pass_fraction * 100:.1f} | {verdict} | {r.note} |"
        )
    return "\n".join(lines)


def _matrix_from_registry() -> list[ResearchCell]:
    """Pull one ResearchCell per bot from strategies.per_bot_registry.

    This is the canonical entry point for the per-bot baseline sweep.
    Hand-rolled cells (the ad-hoc matrix below) stay around for quick
    one-off questions, but the registry-driven sweep is what the
    "is anything regressing across the bot fleet" smoke test reads.
    """
    from eta_engine.strategies.per_bot_registry import all_assignments

    cells: list[ResearchCell] = []
    for a in all_assignments():
        cells.append(
            ResearchCell(
                label=a.bot_id,
                symbol=a.symbol,
                timeframe=a.timeframe,
                scorer_name=a.scorer_name,
                threshold=a.confluence_threshold,
                block_regimes=a.block_regimes if a.block_regimes else None,
                window_days=a.window_days,
                step_days=a.step_days,
                min_trades_per_window=a.min_trades_per_window,
                strategy_kind=a.strategy_kind,
                extras=dict(a.extras),
            )
        )
    return cells


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="run_research_grid")
    p.add_argument(
        "--source",
        choices=("registry", "ad_hoc"),
        default="registry",
        help="registry = run every bot's assigned strategy (default); "
        "ad_hoc = the static research-question matrix below",
    )
    args = p.parse_args()

    base_block = frozenset({"trending_up", "trending_down"})
    if args.source == "registry":
        matrix = _matrix_from_registry()
    else:
        # Ad-hoc cells preserved for one-off research questions.
        matrix = [
            ResearchCell("5m_ungated", "MNQ1", "5m", "global", 7.0, None, 30, 15, 5),
            ResearchCell("5m_gated_mnq", "MNQ1", "5m", "mnq", 5.0, base_block, 30, 15, 5),
            ResearchCell("1h_gated", "MNQ1", "1h", "mnq", 5.0, base_block, 90, 30, 10),
            ResearchCell("4h_gated", "MNQ1", "4h", "mnq", 5.0, base_block, 180, 60, 10),
            ResearchCell("D_NQ1_gated", "NQ1", "D", "mnq", 5.0, base_block, 365, 180, 10),
        ]
    print(f"[research_grid] running {len(matrix)} cells\n")
    results: list[CellResult] = []
    for cell in matrix:
        print(f"  - {cell.label}: {cell.symbol}/{cell.timeframe} ...")
        try:
            r = run_cell(cell)
            results.append(r)
            print(
                f"      -> windows={r.n_windows} "
                f"agg_OOS={r.agg_oos_sharpe:+.3f} pass_frac={r.fold_dsr_pass_fraction*100:.1f}% "
                f"verdict={'PASS' if r.pass_gate else 'FAIL'}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"      -> ERROR: {exc!r}")
            results.append(CellResult(
                cell=cell, n_windows=0, n_positive_oos=0,
                agg_is_sharpe=0.0, agg_oos_sharpe=0.0,
                avg_oos_degradation=0.0, deflated_sharpe=0.0,
                fold_dsr_median=0.0, fold_dsr_pass_fraction=0.0,
                pass_gate=False, note=f"ERROR: {type(exc).__name__}",
            ))

    table = render_table(results)
    print("\n" + table)

    log_dir = ROOT / "docs" / "research_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"research_grid_{stamp}.md"
    log_path.write_text(
        f"# Research Grid — {datetime.now(UTC).isoformat()}\n\n"
        f"Cells: {len(matrix)}\n\n"
        + table + "\n",
        encoding="utf-8",
    )
    print(f"\n[saved to {log_path}]")
    return 0 if any(r.pass_gate for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

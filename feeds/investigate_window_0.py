"""
EVOLUTIONARY TRADING ALGO  //  scripts.investigate_window_0
============================================================
Pull window 0 of the real MNQ walk-forward into a single backtest
and dump the full tearsheet so we can see WHY the OOS Sharpe was
positive when the surrounding 5 windows were negative.

Usage::

    python -m eta_engine.scripts.investigate_window_0

This is a research-only script — no production codepaths run it.
The output goes both to stdout and to
``docs/research_log/window_0_tearsheet_<datestamp>.md`` so the
artifact survives the next /clear.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import MNQ_DATA_ROOT  # noqa: E402


def main() -> int:
    from eta_engine.backtest import (
        BacktestConfig,
        BacktestEngine,
        WalkForwardConfig,
    )
    from eta_engine.backtest.tearsheet import TearsheetBuilder
    from eta_engine.core.confluence_scorer import score_confluence_mnq
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.scripts.run_walk_forward_mnq_real import _ctx, _load_csv

    data_path = Path(os.environ.get("MNQ_DATA_PATH", str(MNQ_DATA_ROOT / "mnq_5m.csv")))
    bars = _load_csv(data_path)
    if not bars:
        print(f"ABORT: zero bars at {data_path}")
        return 1

    # Recompute window 0 boundaries with the same config the
    # walk-forward uses: window_days=30, step_days=15, anchored=True,
    # oos_fraction=0.3.
    wf = WalkForwardConfig(window_days=30, step_days=15, anchored=True, oos_fraction=0.3)
    start = bars[0].timestamp
    is_end = start + timedelta(days=int(wf.window_days * (1 - wf.oos_fraction)))  # ~21 days
    oos_end = start + timedelta(days=wf.window_days)  # ~30 days

    is_bars = [b for b in bars if start <= b.timestamp < is_end]
    oos_bars = [b for b in bars if is_end <= b.timestamp < oos_end]

    cfg_template = BacktestConfig(
        start_date=start,
        end_date=oos_end,
        symbol=bars[0].symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=5.0,
        max_trades_per_day=10,
    )
    is_cfg = cfg_template.model_copy(update={"start_date": is_bars[0].timestamp, "end_date": is_bars[-1].timestamp})
    oos_cfg = cfg_template.model_copy(update={"start_date": oos_bars[0].timestamp, "end_date": oos_bars[-1].timestamp})
    pipeline = FeaturePipeline.default()
    is_res = BacktestEngine(
        pipeline,
        is_cfg,
        ctx_builder=_ctx,
        strategy_id="w0-IS",
        scorer=score_confluence_mnq,
    ).run(is_bars)
    oos_res = BacktestEngine(
        pipeline,
        oos_cfg,
        ctx_builder=_ctx,
        strategy_id="w0-OOS",
        scorer=score_confluence_mnq,
    ).run(oos_bars)

    sections: list[str] = []
    sections.append(f"# Window 0 deep-dive — generated {datetime.now(UTC).isoformat()}")
    sections.append("")
    sections.append(f"IS bars: {len(is_bars)}  range {is_bars[0].timestamp.date()} -> {is_bars[-1].timestamp.date()}")
    sections.append(
        f"OOS bars: {len(oos_bars)}  range {oos_bars[0].timestamp.date()} -> {oos_bars[-1].timestamp.date()}"
    )
    sections.append("")
    sections.append("## In-sample (training) tearsheet")
    sections.append(TearsheetBuilder.from_result(is_res))
    sections.append("## Out-of-sample (verification) tearsheet")
    sections.append(TearsheetBuilder.from_result(oos_res))

    out = "\n".join(sections)
    log_dir = ROOT / "docs" / "research_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"window_0_tearsheet_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.md"
    log_path.write_text(out, encoding="utf-8")

    print(out)
    print(f"\n[saved to {log_path}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())

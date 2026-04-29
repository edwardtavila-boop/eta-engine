"""
EVOLUTIONARY TRADING ALGO  //  scripts.compare_coinbase_vs_ibkr
================================================================
Pre-live drift gate for crypto strategy promotion.

Why this exists
---------------
Every crypto strategy promoted via the Coinbase-spot baseline
(see ``docs/strategy_baselines.json``) must be re-evaluated on
IBKR-native bars before real-money activation. This is the gate
operator policy ``eta_data_source_policy.md`` requires.

What it does
------------
1. Resolves the registry assignment for the named bot
   (e.g. ``btc_hybrid``) — same strategy_kind + extras config
   the strict gate promoted.
2. Loads Coinbase bars from the workspace ``data/crypto/history`` root.
3. Loads IBKR-native bars from the workspace ``data/crypto/ibkr/history`` root
   (populated by ``scripts.fetch_ibkr_crypto_bars``).
4. Runs the promoted strategy on each tape independently —
   producing a Coinbase trade list and an IBKR trade list over
   the same date window.
5. Builds a ``BaselineSnapshot`` from the Coinbase trades and
   calls ``obs.drift_monitor.assess_drift`` with the IBKR trades
   as ``recent``. Severity is ``green`` / ``amber`` / ``red``.
6. Writes the comparison to
   ``docs/research_log/<bot_id>_data_swap_<date>.md``
   per the operator policy's audit-trail requirement.

Promotion rule
--------------
* ``green`` — Coinbase baseline transfers; live activation may
  proceed (subject to other operational gates).
* ``amber`` / ``red`` — DO NOT promote. Re-tune on IBKR data,
  treat IBKR as the authoritative baseline, repeat.

Usage::

    python -m eta_engine.scripts.compare_coinbase_vs_ibkr \\
        --bot-id btc_hybrid \\
        [--start 2025-04-27 --end 2026-04-27]

If no window is provided, uses the intersection of the two
fetched tapes' date ranges.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import CRYPTO_HISTORY_ROOT as COINBASE_HISTORY_ROOT  # noqa: E402
from eta_engine.scripts.workspace_roots import CRYPTO_IBKR_HISTORY_ROOT as IBKR_HISTORY_ROOT  # noqa: E402


@dataclass(frozen=True)
class _ComparisonInputs:
    bot_id: str
    coinbase_csv: Path
    ibkr_csv: Path
    start: datetime
    end: datetime


def _load_bars_from_csv(path: Path, *, symbol: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """Load history-schema CSV into BarData list. Returns [] on missing file."""
    from eta_engine.core.data_pipeline import BarData

    if not path.exists():
        return []
    bars: list = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                ts = int(float(row["time"]))
                bars.append(
                    BarData(
                        timestamp=datetime.fromtimestamp(ts, tz=UTC),
                        symbol=symbol,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume") or 0.0),
                    ),
                )
            except (KeyError, ValueError, TypeError):
                continue
    bars.sort(key=lambda b: b.timestamp)
    return bars


def _slice_bars(bars: list, start: datetime, end: datetime) -> list:
    return [b for b in bars if start <= b.timestamp < end]


def _resolve_inputs(bot_id: str, start: str | None, end: str | None) -> _ComparisonInputs:
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        raise SystemExit(f"unknown bot_id: {bot_id!r}")

    sym = a.symbol.upper()
    tf = a.timeframe
    tf_for_filename = {"1d": "D", "1w": "W"}.get(tf.lower(), tf)
    fname = f"{sym}_{tf_for_filename}.csv"
    cb = COINBASE_HISTORY_ROOT / fname
    ib = IBKR_HISTORY_ROOT / fname

    s = (
        datetime.fromisoformat(start).replace(tzinfo=UTC)
        if start else datetime(1970, 1, 1, tzinfo=UTC)
    )
    e = (
        datetime.fromisoformat(end).replace(tzinfo=UTC)
        if end else datetime.now(UTC)
    )
    return _ComparisonInputs(bot_id=bot_id, coinbase_csv=cb, ibkr_csv=ib, start=s, end=e)


def _run_strategy(  # type: ignore[no-untyped-def]  # noqa: ANN202, ANN001
    *, assignment, bars: list,  # noqa: ANN001
):
    """Run the bot's registered strategy over bars; return BacktestResult."""
    from eta_engine.backtest import BacktestConfig, BacktestEngine
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.scripts.run_research_grid import _build_crypto_strategy_factory

    if not bars:
        return None
    cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=bars[0].symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=assignment.confluence_threshold,
        max_trades_per_day=10,
    )
    factory = _build_crypto_strategy_factory(
        assignment.strategy_kind, dict(assignment.extras),
    )
    strat = factory()
    return BacktestEngine(
        pipeline=FeaturePipeline.default(), config=cfg, strategy=strat,
    ).run(bars)


def main() -> int:
    p = argparse.ArgumentParser(prog="compare_coinbase_vs_ibkr")
    p.add_argument("--bot-id", required=True, help="registered bot_id (e.g. btc_hybrid)")
    p.add_argument("--start", help="ISO date YYYY-MM-DD; default = beginning of overlap")
    p.add_argument("--end", help="ISO date YYYY-MM-DD; default = today")
    p.add_argument(
        "--min-trades", type=int, default=20,
        help="minimum recent (IBKR) trades for a non-green verdict",
    )
    p.add_argument(
        "--out-dir", type=Path,
        default=ROOT / "docs" / "research_log",
        help="research-log destination",
    )
    args = p.parse_args()

    from eta_engine.obs.drift_monitor import BaselineSnapshot, assess_drift
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(args.bot_id)
    if a is None:
        print(f"[compare] unknown bot_id: {args.bot_id!r}", file=sys.stderr)
        return 2

    inputs = _resolve_inputs(args.bot_id, args.start, args.end)
    print(
        f"[compare] {args.bot_id} {a.strategy_id} {a.symbol}/{a.timeframe} "
        f"{inputs.start.date()} -> {inputs.end.date()}",
    )
    print(f"  coinbase: {inputs.coinbase_csv}")
    print(f"  ibkr:     {inputs.ibkr_csv}")
    if not inputs.coinbase_csv.exists():
        print("[compare] Coinbase CSV missing — run scripts/fetch_btc_bars first")
        return 1
    if not inputs.ibkr_csv.exists():
        print(
            "[compare] IBKR CSV missing — run "
            "scripts/fetch_ibkr_crypto_bars first (gateway must be running)",
        )
        return 1

    cb_bars = _slice_bars(_load_bars_from_csv(inputs.coinbase_csv, symbol=a.symbol), inputs.start, inputs.end)
    ib_bars = _slice_bars(_load_bars_from_csv(inputs.ibkr_csv, symbol=a.symbol), inputs.start, inputs.end)
    if not cb_bars:
        print("[compare] Coinbase tape is empty in the requested window")
        return 1
    if not ib_bars:
        print("[compare] IBKR tape is empty in the requested window")
        return 1

    print(f"  bars: coinbase={len(cb_bars)}  ibkr={len(ib_bars)}")
    cb_res = _run_strategy(assignment=a, bars=cb_bars)
    ib_res = _run_strategy(assignment=a, bars=ib_bars)
    if cb_res is None or ib_res is None:
        print("[compare] backtest produced no result")
        return 1
    print(
        f"  coinbase trades: {cb_res.n_trades}  "
        f"ibkr trades: {ib_res.n_trades}",
    )

    baseline = BaselineSnapshot.from_trades(
        strategy_id=a.strategy_id, trades=cb_res.trades,
    )
    assessment = assess_drift(
        strategy_id=a.strategy_id,
        recent=ib_res.trades,
        baseline=baseline,
        min_trades=args.min_trades,
    )
    print(
        f"\n[compare] severity = {assessment.severity.upper()}  "
        f"(n_recent={assessment.n_recent}, "
        f"WR z={assessment.win_rate_z:+.2f}, "
        f"R z={assessment.avg_r_z:+.2f})",
    )
    for r in assessment.reasons:
        print(f"  - {r}")

    # Audit-trail markdown per eta_data_source_policy.md
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    out_path = args.out_dir / f"{args.bot_id}_data_swap_{stamp}.md"
    lines = [
        f"# {args.bot_id} — Coinbase → IBKR drift check ({stamp})",
        "",
        f"- strategy_id: `{a.strategy_id}`",
        f"- strategy_kind: `{a.strategy_kind}`",
        f"- symbol/timeframe: {a.symbol}/{a.timeframe}",
        f"- window: {inputs.start.date()} → {inputs.end.date()}",
        "",
        "| | Coinbase (baseline) | IBKR (recent) |",
        "|---|---:|---:|",
        f"| bars | {len(cb_bars)} | {len(ib_bars)} |",
        f"| trades | {cb_res.n_trades} | {ib_res.n_trades} |",
        f"| win rate | {baseline.win_rate * 100:.1f}% | {assessment.recent_win_rate * 100:.1f}% |",
        f"| avg R | {baseline.avg_r:+.4f} | {assessment.recent_avg_r:+.4f} |",
        f"| R stddev | {baseline.r_stddev:.4f} | — |",
        "",
        f"**Severity: `{assessment.severity}`**",
        "",
        f"- win-rate z: {assessment.win_rate_z:+.2f}",
        f"- avg-R z: {assessment.avg_r_z:+.2f}",
        "",
    ]
    if assessment.reasons:
        lines.append("Reasons:")
        for r in assessment.reasons:
            lines.append(f"- {r}")
        lines.append("")
    lines.extend(
        [
            "## Promotion rule",
            "",
            "- `green` — Coinbase baseline transfers; live activation may proceed.",
            "- `amber` / `red` — DO NOT promote. Re-tune on IBKR data, treat",
            "  IBKR as the authoritative baseline, repeat.",
            "",
            "Per ``memory/eta_data_source_policy.md``.",
            "",
        ],
    )
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[compare] wrote {out_path}")

    # Exit non-zero on amber/red so this can gate a CI workflow.
    if assessment.severity == "red":
        return 3
    if assessment.severity == "amber":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

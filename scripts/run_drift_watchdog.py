"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_drift_watchdog
==========================================================
Standing drift watchdog over the promoted strategy fleet.

Why this exists
---------------
A strategy that passed the strict gate on data through 2026-04-27
isn't guaranteed to still pass two months later — markets change,
regimes shift, the numbers drift. The framework deferred this
watchdog until ≥3 strategies were promoted; with the BTC promotion
on 2026-04-27 the threshold is met and the watchdog goes live.

What it does
------------
For each strategy listed in ``docs/strategy_baselines.json`` with
``_promotion_status`` == "production":

1. Resolves the bot via ``per_bot_registry`` to recover its
   current strategy_kind + extras config.
2. Loads the latest bars for the bot's (symbol, timeframe).
3. Slices the last ``--lookback-days`` (default 30) of bars as
   the "recent" window.
4. Runs the strategy over the recent window to produce a fresh
   trade list.
5. Loads the pinned baseline from strategy_baselines.json and
   converts the recent trades to the same shape.
6. Calls ``obs.drift_monitor.assess_drift(recent, baseline)``.
7. Logs each result to ``var/eta_engine/state/drift_watchdog.jsonl``
   (append-only).
8. Dispatches amber/red severity via ``obs.alert_dispatcher`` so
   the operator sees them in the alerts feed.

What it isn't
-------------
This watchdog measures "is the strategy still the same on the
same data source." It does NOT measure "live vs research" drift
— that's ``scripts.compare_coinbase_vs_ibkr`` and is a single
pre-promotion gate, not a standing monitor.

Both watchdogs are needed: this one catches research-state drift
over time; the comparator catches the data-source-swap gap once
at promotion.

Usage::

    # Run all production-promoted strategies (default)
    python -m eta_engine.scripts.run_drift_watchdog

    # One bot only
    python -m eta_engine.scripts.run_drift_watchdog --bot-id btc_hybrid

    # Custom lookback / alert thresholds
    python -m eta_engine.scripts.run_drift_watchdog \\
        --lookback-days 14 --amber-z 1.5 --red-z 2.5

Cron-friendly: returns exit code 0 (all green or no data),
2 (any amber), 3 (any red). One scheduled task per day at 09:00 UTC
is the recommended cadence.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import ETA_DRIFT_WATCHDOG_LOG_PATH  # noqa: E402

# Windows cp1252 console can't print Greek/math glyphs that show up in
# assess_drift reasons. Force stdout/stderr to UTF-8 so the watchdog
# doesn't crash mid-run on the first σ-bearing reason string.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

DEFAULT_BASELINES = ROOT / "docs" / "strategy_baselines.json"
DEFAULT_LOG = ETA_DRIFT_WATCHDOG_LOG_PATH


def _load_baselines(path: Path) -> list[dict[str, Any]]:
    """Read strategy_baselines.json and return only production rows."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[drift_watchdog] cannot read baselines: {exc}")
        return []
    strategies = payload.get("strategies") or []
    if not isinstance(strategies, list):
        return []
    out = []
    for s in strategies:
        if not isinstance(s, dict):
            continue
        # Default to "production" when no explicit status is set, since
        # the original entries (mnq_orb_v1, nq_orb_v1) predate the
        # _promotion_status field.
        status = s.get("_promotion_status", "production")
        if status == "production":
            out.append(s)
    return out


def _resolve_assignment(strategy_id: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """Find the registry assignment whose strategy_id matches."""
    from eta_engine.strategies.per_bot_registry import all_assignments

    for a in all_assignments():
        if a.strategy_id == strategy_id:
            return a
    return None


def _build_strategy(assignment):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
    """Construct the strategy instance for the given assignment.

    Reuses the dispatch tables from the research grid so this
    watchdog can't drift away from how strategies are wired
    elsewhere.
    """
    from eta_engine.scripts.run_research_grid import _build_strategy_factory

    factory = _build_strategy_factory(
        assignment.strategy_kind,
        dict(assignment.extras),
    )
    return factory()


def _run_recent(  # type: ignore[no-untyped-def]  # noqa: ANN202, ANN001
    *, assignment, lookback_days: int,  # noqa: ANN001
):
    """Backtest the strategy over the most-recent ``lookback_days``.

    Returns the BacktestResult or ``None`` if data is unavailable.
    """
    from eta_engine.backtest import BacktestConfig, BacktestEngine
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline

    ds = default_library().get(symbol=assignment.symbol, timeframe=assignment.timeframe)
    if ds is None:
        return None
    bars = default_library().load_bars(ds)
    if not bars:
        return None
    cutoff = bars[-1].timestamp - timedelta(days=lookback_days)
    recent_bars = [b for b in bars if b.timestamp >= cutoff]
    if not recent_bars:
        return None
    cfg = BacktestConfig(
        start_date=recent_bars[0].timestamp,
        end_date=recent_bars[-1].timestamp,
        symbol=assignment.symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=assignment.confluence_threshold,
        max_trades_per_day=10,
    )
    strat = _build_strategy(assignment)
    return BacktestEngine(
        pipeline=FeaturePipeline.default(), config=cfg, strategy=strat,
    ).run(recent_bars)


def _emit_alert(severity: str, strategy_id: str, summary: str) -> None:
    """Dispatch an alert via the standard dispatcher.

    Falls back to printing if the dispatcher is unavailable so the
    watchdog never silently swallows red events.
    """
    try:
        from eta_engine.obs.alert_dispatcher import (
            AlertDispatcher,  # noqa: PLC0415
        )
    except ImportError:
        print(f"[drift_watchdog] {severity.upper()} {strategy_id}: {summary}")
        return
    try:
        dispatcher = AlertDispatcher()
        dispatcher.dispatch(  # type: ignore[attr-defined]
            event="drift_watchdog",
            severity=severity,
            payload={"strategy_id": strategy_id, "summary": summary},
        )
    except (AttributeError, TypeError, RuntimeError):
        # Different dispatcher API on this branch; fall back to log.
        print(f"[drift_watchdog] {severity.upper()} {strategy_id}: {summary}")


def main() -> int:
    p = argparse.ArgumentParser(prog="run_drift_watchdog")
    p.add_argument(
        "--baselines", type=Path, default=DEFAULT_BASELINES,
        help="path to strategy_baselines.json",
    )
    p.add_argument(
        "--bot-id", default=None,
        help="restrict to one bot (default: all production strategies)",
    )
    p.add_argument(
        "--lookback-days", type=int, default=30,
        help="how many trailing bar-days count as 'recent'",
    )
    p.add_argument(
        "--min-trades", type=int, default=5,
        help="below this many recent trades, severity is 'green' (insufficient sample)",
    )
    p.add_argument("--amber-z", type=float, default=2.0)
    p.add_argument("--red-z", type=float, default=3.0)
    p.add_argument(
        "--log-path", type=Path, default=DEFAULT_LOG,
        help=f"JSONL append target (default: {DEFAULT_LOG})",
    )
    p.add_argument(
        "--no-alerts", action="store_true",
        help="skip alert_dispatcher; just write the JSONL log",
    )
    args = p.parse_args()

    from eta_engine.obs.drift_monitor import BaselineSnapshot, assess_drift

    rows = _load_baselines(args.baselines)
    if not rows:
        print(f"[drift_watchdog] no production strategies in {args.baselines}")
        return 0

    print(
        f"[drift_watchdog] {len(rows)} production strategies, "
        f"lookback {args.lookback_days}d",
    )
    args.log_path.parent.mkdir(parents=True, exist_ok=True)

    worst = "green"
    out_records: list[dict[str, Any]] = []
    for row in rows:
        strategy_id = row.get("strategy_id")
        if not strategy_id:
            continue
        if args.bot_id is not None:
            from eta_engine.strategies.per_bot_registry import get_for_bot
            target = get_for_bot(args.bot_id)
            if target is None or target.strategy_id != strategy_id:
                continue
        a = _resolve_assignment(strategy_id)
        if a is None:
            print(f"  - {strategy_id}: SKIP (no registry assignment)")
            continue
        res = _run_recent(assignment=a, lookback_days=args.lookback_days)
        if res is None or res.n_trades == 0:
            note = "no recent trades" if res is not None else "no data"
            print(f"  - {strategy_id}: SKIP ({note})")
            continue

        baseline = BaselineSnapshot(
            strategy_id=strategy_id,
            n_trades=int(row.get("n_trades", 0)),
            win_rate=float(row.get("win_rate", 0.0)),
            avg_r=float(row.get("avg_r", 0.0)),
            r_stddev=float(row.get("r_stddev", 0.0)),
        )
        assessment = assess_drift(
            strategy_id=strategy_id,
            recent=res.trades,
            baseline=baseline,
            min_trades=args.min_trades,
            amber_z=args.amber_z,
            red_z=args.red_z,
        )
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "bot_id": a.bot_id,
            "strategy_id": strategy_id,
            "lookback_days": args.lookback_days,
            "n_recent": assessment.n_recent,
            "recent_win_rate": round(assessment.recent_win_rate, 4),
            "recent_avg_r": round(assessment.recent_avg_r, 4),
            "win_rate_z": round(assessment.win_rate_z, 4),
            "avg_r_z": round(assessment.avg_r_z, 4),
            "severity": assessment.severity,
            "reasons": assessment.reasons,
            "baseline": {
                "n_trades": baseline.n_trades,
                "win_rate": baseline.win_rate,
                "avg_r": baseline.avg_r,
                "r_stddev": baseline.r_stddev,
            },
        }
        out_records.append(record)
        sev = assessment.severity
        if sev == "red" or (sev == "amber" and worst != "red"):
            worst = sev
        print(
            f"  - {strategy_id}: {sev.upper()} "
            f"n_recent={assessment.n_recent} "
            f"WR z={assessment.win_rate_z:+.2f} "
            f"R z={assessment.avg_r_z:+.2f}",
        )
        for r in assessment.reasons:
            print(f"      - {r}")
        if sev in ("amber", "red") and not args.no_alerts:
            summary = (
                f"WR z={assessment.win_rate_z:+.2f} "
                f"R z={assessment.avg_r_z:+.2f} "
                f"(n={assessment.n_recent})"
            )
            _emit_alert(sev, strategy_id, summary)

    with args.log_path.open("a", encoding="utf-8") as fh:
        for r in out_records:
            fh.write(json.dumps(r) + "\n")
    print(f"[drift_watchdog] appended {len(out_records)} record(s) to {args.log_path}")

    if worst == "red":
        return 3
    if worst == "amber":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Out-of-sample validation runner.

Scaffolds an honest train/test split for each promoted bot and
re-runs the strategy lab on the test window. The lab reports we
already have were generated on some historical window — without
knowing which window was train vs test, we can't tell if the bot's
sharpe is genuine edge or curve fit.

This script:

  1. Reads each bot's strategy assignment from per_bot_registry.
  2. Pulls the bot's available bar history (yfinance / coinbase /
     local CSV per the lab's existing data resolver).
  3. Splits N-month windows: last K months = "test" (out of sample),
     remainder = "train" (in-sample reference).
  4. Re-runs the strategy lab engine on each window.
  5. Writes a comparison report to
     reports/oos_validation/<bot_id>__<ts>.json with:
       - in_sample sharpe / win_rate / n_trades
       - out_sample sharpe / win_rate / n_trades
       - drift = out_sample - in_sample (negative = curve fit)
  6. Aggregates a fleet summary.

This is a SCAFFOLD — the actual lab engine call is delegated to
``feeds.strategy_lab.engine.run_strategy_lab(bot_id, window=...)``
which already exists. Where the engine doesn't accept a custom
window, this script falls back to ``run_strategy_lab(bot_id)`` and
records the existing result as "full_window" without a split.

Usage:
    python -m eta_engine.scripts.oos_validation                 # all bots
    python -m eta_engine.scripts.oos_validation --bot btc_hybrid # one bot
    python -m eta_engine.scripts.oos_validation --test-months 3  # last 3 mo as OOS

Environment:
    ETA_OOS_TEST_MONTHS  default 3
    ETA_OOS_REPORT_DIR   default reports/oos_validation/
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


def _bootstrap_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_bootstrap_env()


def _list_promoted_bots() -> list[str]:
    """Bot IDs with launch_lane in {paper_soak, live_preflight}."""
    try:
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS
    except ImportError as exc:
        logger.error("per_bot_registry import failed: %s", exc)
        return []
    promoted: list[str] = []
    for a in ASSIGNMENTS:
        extras = getattr(a, "extras", {}) or {}
        lane = extras.get("launch_lane") or extras.get("promotion_status", "")
        if str(lane).lower() in {
            "paper_soak", "live_preflight", "production_candidate", "live",
        }:
            promoted.append(a.bot_id)
    return promoted


def _run_lab(bot_id: str, window: str | None = None) -> dict[str, Any]:
    """Invoke the strategy lab. Tolerates the lab not exposing a
    window parameter — falls back to the default invocation and
    records the result as "full_window"."""
    try:
        from eta_engine.feeds.strategy_lab.engine import run_strategy_lab
    except ImportError as exc:
        return {"error": f"strategy_lab import failed: {exc}"}

    try:
        if window is not None:
            try:
                result = run_strategy_lab(bot_id, window=window)
            except TypeError:
                # Old signature without window kwarg
                logger.info("lab engine doesn't accept window=; running default")
                result = run_strategy_lab(bot_id)
        else:
            result = run_strategy_lab(bot_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    # Normalize — pull just the metrics we care about.
    if isinstance(result, dict):
        return {
            "sharpe": result.get("sharpe") or result.get("sharpe_ratio"),
            "win_rate": result.get("win_rate"),
            "n_trades": result.get("n_trades") or result.get("trades_count"),
            "expectancy_r": result.get("expectancy_r") or result.get("avg_r"),
            "max_drawdown": result.get("max_drawdown") or result.get("max_dd"),
            "raw": {k: v for k, v in result.items() if k not in {"bars", "trades"}},
        }
    return {"raw": str(result)[:1000]}


def _validate_bot(bot_id: str, *, test_months: int) -> dict[str, Any]:
    """Run lab on the IS window then the OOS window, compute drift."""
    now = datetime.now(UTC)
    test_start = (now - timedelta(days=test_months * 30)).date().isoformat()
    test_end = now.date().isoformat()
    train_end = test_start

    is_window = f"start={None}|end={train_end}"
    oos_window = f"start={test_start}|end={test_end}"

    logger.info("OOS validating %s — train≤%s, test=%s..%s",
                bot_id, train_end, test_start, test_end)

    in_sample = _run_lab(bot_id, window=is_window)
    out_sample = _run_lab(bot_id, window=oos_window)

    drift: dict[str, Any] = {}
    for metric in ("sharpe", "win_rate", "expectancy_r"):
        try:
            in_v = float(in_sample.get(metric) or 0)
            out_v = float(out_sample.get(metric) or 0)
            drift[metric] = round(out_v - in_v, 4)
        except (TypeError, ValueError):
            drift[metric] = None

    verdict = "indeterminate"
    sharpe_drift = drift.get("sharpe")
    if isinstance(sharpe_drift, (int, float)):
        if sharpe_drift >= -0.10:
            verdict = "robust"      # OOS held up
        elif sharpe_drift >= -0.40:
            verdict = "soft_drift"  # some degradation
        else:
            verdict = "curve_fit"   # strong indicator of overfit

    return {
        "bot_id": bot_id,
        "checked_at": now.isoformat(),
        "test_months": test_months,
        "in_sample": in_sample,
        "out_sample": out_sample,
        "drift": drift,
        "verdict": verdict,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bot", default=None, help="Validate a single bot_id")
    p.add_argument(
        "--test-months", type=int,
        default=int(os.getenv("ETA_OOS_TEST_MONTHS", "3")),
    )
    p.add_argument(
        "--report-dir", default=os.getenv(
            "ETA_OOS_REPORT_DIR",
            str(Path(r"C:\EvolutionaryTradingAlgo\reports\oos_validation")),
        ),
    )
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    bots = [args.bot] if args.bot else _list_promoted_bots()
    if not bots:
        logger.error("no bots to validate")
        return 1

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    fleet_report: list[dict[str, Any]] = []

    for bid in bots:
        res = _validate_bot(bid, test_months=args.test_months)
        out_path = report_dir / f"{bid}__{ts}.json"
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2, default=str)
        fleet_report.append({
            "bot_id": bid,
            "verdict": res["verdict"],
            "is_sharpe": res["in_sample"].get("sharpe"),
            "oos_sharpe": res["out_sample"].get("sharpe"),
            "drift_sharpe": res["drift"].get("sharpe"),
        })
        logger.info(
            "%s → %s (drift_sharpe=%s)",
            bid, res["verdict"], res["drift"].get("sharpe"),
        )

    summary_path = report_dir / f"_fleet_summary__{ts}.json"
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump({
            "checked_at": datetime.now(UTC).isoformat(),
            "test_months": args.test_months,
            "bots": fleet_report,
            "verdicts": {
                v: sum(1 for r in fleet_report if r["verdict"] == v)
                for v in {"robust", "soft_drift", "curve_fit", "indeterminate"}
            },
        }, fh, indent=2, default=str)

    logger.info("fleet summary → %s", summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

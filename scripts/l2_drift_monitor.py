"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_drift_monitor
=======================================================
Rolling-window performance drift monitor — fires WARN BEFORE
falsification criteria trigger.

Why this exists
---------------
The promotion_evaluator's falsification triggers are designed to
catch a strategy that's already broken (sharpe < 0 over 60d).  But
a strategy can degrade slowly without crossing the hard threshold:
sharpe ratchets from +0.8 down to +0.1 over 90 days, never going
negative, never triggering retirement, but the edge is dying.

This monitor catches that earlier by comparing the strategy's
recent rolling sharpe to its initial / baseline sharpe.  When the
ratio falls below a threshold (default 0.7 = "lost 30% of edge"),
it fires a YELLOW warning so the operator can investigate before
the falsification trigger.

Metrics
-------
- Initial sharpe   : first valid sharpe across last 7-day window
                     in the strategy's history (when it first hit
                     min_n_for_sharpe=30)
- Current sharpe   : latest 14-day rolling sharpe
- Ratio            : current / initial
- Drift verdict    : OK (ratio >= 1.0)
                   | DEGRADING (0.7 <= ratio < 1.0)
                   | DRIFTING (0.4 <= ratio < 0.7)
                   | CRITICAL (ratio < 0.4)

Other signals
-------------
- Sharpe stability: stddev of rolling 7d sharpes; high stddev = regime fragility
- Win-rate stability: same idea on win_rate
- Trade-rate drift: are signals firing at a stable cadence, or has volume collapsed?

Run
---
::

    python -m eta_engine.scripts.l2_drift_monitor
    python -m eta_engine.scripts.l2_drift_monitor --strategy book_imbalance
    python -m eta_engine.scripts.l2_drift_monitor --json
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
L2_BACKTEST_LOG = LOG_DIR / "l2_backtest_runs.jsonl"
DRIFT_LOG = LOG_DIR / "l2_drift_monitor.jsonl"


@dataclass
class DriftReport:
    strategy: str
    symbol: str
    initial_sharpe: float | None
    current_rolling_sharpe: float | None
    sharpe_ratio: float | None  # current / initial
    sharpe_stddev: float | None  # across rolling windows
    win_rate_stddev: float | None
    trade_rate_per_day_recent: float | None
    trade_rate_per_day_baseline: float | None
    trade_rate_ratio: float | None
    drift_verdict: str  # OK | DEGRADING | DRIFTING | CRITICAL | INSUFFICIENT
    notes: list[str] = field(default_factory=list)


def _read_backtest_records(
    path: Path, *, strategy: str | None = None, symbol: str | None = None, since_days: int = 90
) -> list[dict]:
    if not path.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt < cutoff:
                    continue
                if strategy and rec.get("strategy") != strategy:
                    continue
                if symbol and rec.get("symbol") != symbol:
                    continue
                rec["_parsed_ts"] = dt
                out.append(rec)
    except OSError:
        return []
    out.sort(key=lambda r: r["_parsed_ts"])
    return out


def _drift_verdict(ratio: float | None) -> str:
    if ratio is None:
        return "INSUFFICIENT"
    if ratio >= 1.0:
        return "OK"
    if ratio >= 0.7:
        return "DEGRADING"
    if ratio >= 0.4:
        return "DRIFTING"
    return "CRITICAL"


def compute_drift(
    strategy: str,
    symbol: str,
    *,
    _path: Path | None = None,
    recent_window_days: int = 14,
    baseline_window_days: int = 14,
) -> DriftReport:
    """Compute drift between current rolling window and the strategy's
    earliest valid (n_trades >= min) window."""
    records = _read_backtest_records(
        _path if _path is not None else L2_BACKTEST_LOG, strategy=strategy, symbol=symbol, since_days=180
    )
    notes: list[str] = []
    if not records:
        return DriftReport(
            strategy=strategy,
            symbol=symbol,
            initial_sharpe=None,
            current_rolling_sharpe=None,
            sharpe_ratio=None,
            sharpe_stddev=None,
            win_rate_stddev=None,
            trade_rate_per_day_recent=None,
            trade_rate_per_day_baseline=None,
            trade_rate_ratio=None,
            drift_verdict="INSUFFICIENT",
            notes=["no records in backtest log"],
        )

    # Initial: first record with sharpe_proxy_valid=True
    initial_record = next(
        (r for r in records if r.get("sharpe_proxy_valid", False)),
        None,
    )
    if initial_record is None:
        notes.append("no record yet has sharpe_proxy_valid=True (need n_trades >= min_n_for_sharpe)")
    initial_sharpe = initial_record.get("sharpe_proxy") if initial_record else None

    # Current: latest record OR average of recent N
    now = datetime.now(UTC)
    recent_cutoff = now - timedelta(days=recent_window_days)
    recent = [r for r in records if r["_parsed_ts"] >= recent_cutoff and r.get("sharpe_proxy_valid", False)]
    if not recent:
        notes.append(f"no valid sharpe records in last {recent_window_days}d")
        current_sharpe = None
    else:
        current_sharpe = statistics.mean(r.get("sharpe_proxy", 0.0) for r in recent)

    # Ratio
    if initial_sharpe is not None and current_sharpe is not None and initial_sharpe != 0:
        # Be defensive about sign: if initial < 0, ratio doesn't have meaning
        if initial_sharpe <= 0:
            ratio = None
            notes.append("initial_sharpe <= 0; drift ratio not meaningful")
        else:
            ratio = current_sharpe / initial_sharpe
    else:
        ratio = None

    # Stability: stddev of all valid sharpes over the lookback
    valid_sharpes = [
        r.get("sharpe_proxy")
        for r in records
        if r.get("sharpe_proxy_valid", False) and r.get("sharpe_proxy") is not None
    ]
    sharpe_stddev = statistics.stdev(valid_sharpes) if len(valid_sharpes) >= 2 else None
    valid_wr = [r.get("win_rate") for r in records if r.get("win_rate") is not None]
    win_rate_stddev = statistics.stdev(valid_wr) if len(valid_wr) >= 2 else None

    # Trade rate
    baseline_cutoff_start = now - timedelta(days=baseline_window_days * 2)
    baseline_cutoff_end = now - timedelta(days=baseline_window_days)
    baseline = [r for r in records if baseline_cutoff_start <= r["_parsed_ts"] < baseline_cutoff_end]
    baseline_trades = sum(r.get("n_trades", 0) for r in baseline)
    recent_trades = sum(r.get("n_trades", 0) for r in recent)
    baseline_rate = baseline_trades / baseline_window_days if baseline else None
    recent_rate = recent_trades / recent_window_days if recent else None
    trade_rate_ratio = recent_rate / baseline_rate if baseline_rate and baseline_rate > 0 else None
    if trade_rate_ratio is not None and trade_rate_ratio < 0.3:
        notes.append(
            f"trade_rate dropped {(1 - trade_rate_ratio) * 100:.0f}% vs baseline — strategy may have lost signal source"
        )

    verdict = _drift_verdict(ratio)
    if verdict == "DEGRADING":
        notes.append(
            f"Edge degraded {(1 - (ratio or 0)) * 100:.0f}% vs initial; investigate before retirement triggers fire."
        )
    elif verdict in ("DRIFTING", "CRITICAL"):
        notes.append(f"Edge fell {(1 - (ratio or 0)) * 100:.0f}%; consider manual retirement before falsification.")
    return DriftReport(
        strategy=strategy,
        symbol=symbol,
        initial_sharpe=round(initial_sharpe, 3) if initial_sharpe else None,
        current_rolling_sharpe=round(current_sharpe, 3) if current_sharpe else None,
        sharpe_ratio=round(ratio, 3) if ratio else None,
        sharpe_stddev=round(sharpe_stddev, 3) if sharpe_stddev else None,
        win_rate_stddev=round(win_rate_stddev, 3) if win_rate_stddev else None,
        trade_rate_per_day_recent=round(recent_rate, 2) if recent_rate else None,
        trade_rate_per_day_baseline=round(baseline_rate, 2) if baseline_rate else None,
        trade_rate_ratio=round(trade_rate_ratio, 2) if trade_rate_ratio else None,
        drift_verdict=verdict,
        notes=notes,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default="book_imbalance")
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--recent-days", type=int, default=14)
    ap.add_argument("--baseline-days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = compute_drift(
        args.strategy,
        args.symbol,
        recent_window_days=args.recent_days,
        baseline_window_days=args.baseline_days,
    )

    try:
        with DRIFT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(), **asdict(report)}, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: drift log write failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return 1 if report.drift_verdict in ("DRIFTING", "CRITICAL") else 0

    print()
    print("=" * 78)
    print(f"L2 DRIFT MONITOR  ({report.strategy} on {report.symbol})")
    print("=" * 78)
    print(f"  verdict                  : {report.drift_verdict}")
    print(f"  initial sharpe           : {report.initial_sharpe}")
    print(f"  current rolling sharpe   : {report.current_rolling_sharpe}")
    print(f"  ratio (current/initial)  : {report.sharpe_ratio}")
    print(f"  sharpe stddev (90d)      : {report.sharpe_stddev}")
    print(f"  win_rate stddev (90d)    : {report.win_rate_stddev}")
    print(f"  trade rate recent (/day) : {report.trade_rate_per_day_recent}")
    print(f"  trade rate baseline      : {report.trade_rate_per_day_baseline}")
    print(f"  trade rate ratio         : {report.trade_rate_ratio}")
    if report.notes:
        print()
        print("  Notes:")
        for n in report.notes:
            print(f"    - {n}")
    print()
    return 1 if report.drift_verdict in ("DRIFTING", "CRITICAL") else 0


if __name__ == "__main__":
    raise SystemExit(main())

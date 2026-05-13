"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_performance_attribution
=================================================================
P&L attribution: decompose each trade's net pnl into components
so the operator understands WHERE the edge (or loss) came from.

Why this exists
---------------
"book_imbalance made $1200 last week" tells you nothing about WHY.
Did it nail entries with great timing?  Did it grab tighter spreads?
Did it just happen to trade in a friendly regime?

This script decomposes per-trade pnl into 5 components:

  Total net pnl = alpha + entry_timing + exit_slip + regime + commission

Where:
  - alpha          : the per-strategy expected pnl in the trade's regime
                       (estimated from rolling history)
  - entry_timing   : (intended_entry - actual_entry) * point_value
                       (positive = bought cheaper than intended)
  - exit_slip      : (actual_exit - intended_exit) * point_value
                       (negative = slipped past target / past stop)
  - regime         : pnl - baseline_pnl_in_this_regime
                       (positive = trade outperformed regime baseline)
  - commission     : -commission_per_rt_usd

A strategy with sharpe = 1.0 might have:
  alpha = +$5    (real edge)
  entry_timing = +$2 (good fills)
  exit_slip = -$1 (predicted)
  regime = +$0   (regime-neutral)
  commission = -$1
  -----------------
  total = +$5

vs a strategy with sharpe = 1.0 driven entirely by:
  alpha = +$0    (no real edge)
  entry_timing = +$8 (insanely lucky fills)
  exit_slip = -$1
  regime = -$1   (slightly bad regime)
  commission = -$1
  -----------------
  total = +$5

The second strategy is fragile.  Attribution exposes it.

Run
---
::

    python -m eta_engine.scripts.l2_performance_attribution \\
        --strategy book_imbalance --symbol MNQ --days 30
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
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SIGNAL_LOG = LOG_DIR / "l2_signal_log.jsonl"
BROKER_FILL_LOG = LOG_DIR / "broker_fills.jsonl"
ATTRIBUTION_LOG = LOG_DIR / "l2_attribution.jsonl"


@dataclass
class TradeAttribution:
    signal_id: str
    pnl_total: float
    alpha: float
    entry_timing: float
    exit_slip: float
    regime: float
    commission: float


@dataclass
class AttributionReport:
    strategy_id: str | None
    n_trades: int
    total_pnl: float
    alpha_pct: float | None
    entry_timing_pct: float | None
    exit_slip_pct: float | None
    regime_pct: float | None
    commission_pct: float | None
    sharpe_attributed: dict[str, float] = field(default_factory=dict)
    trades: list[TradeAttribution] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _read_jsonl(path: Path, *, since_days: int = 30, strategy_id: str | None = None) -> list[dict]:
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
                if strategy_id and rec.get("strategy_id") != strategy_id:
                    continue
                out.append(rec)
    except OSError:
        return []
    return out


def attribute_trade(
    *,
    signal: dict,
    entry_fill: dict,
    exit_fill: dict,
    baseline_alpha: float = 0.0,
    regime_baseline: float = 0.0,
    point_value: float = 2.0,
    commission_per_rt: float = 0.85,
) -> TradeAttribution:
    """Decompose a single trade's pnl."""
    intended_entry = float(signal.get("entry_price", 0))
    intended_target = float(signal.get("intended_target_price", 0))
    intended_stop = float(signal.get("intended_stop_price", 0))
    side = str(signal.get("side", "LONG")).upper()
    is_long = side in ("LONG", "BUY")

    actual_entry = float(entry_fill.get("actual_fill_price", intended_entry))
    actual_exit = float(exit_fill.get("actual_fill_price", 0))
    exit_reason = str(exit_fill.get("exit_reason", "TIMEOUT")).upper()
    intended_exit = intended_target if exit_reason == "TARGET" else intended_stop

    # Total pnl in dollars
    if is_long:
        pnl_points = actual_exit - actual_entry
        entry_timing_pts = intended_entry - actual_entry  # positive = bought cheaper
        exit_slip_pts = actual_exit - intended_exit  # positive = sold higher than target
    else:
        pnl_points = actual_entry - actual_exit
        entry_timing_pts = actual_entry - intended_entry  # positive = sold higher than intended
        exit_slip_pts = intended_exit - actual_exit  # positive = bought back lower

    pnl_total = pnl_points * point_value - commission_per_rt
    entry_timing_usd = entry_timing_pts * point_value
    exit_slip_usd = exit_slip_pts * point_value
    commission_usd = -commission_per_rt
    # Regime contribution = (this trade's gross pnl) - (regime baseline gross pnl)
    gross = pnl_points * point_value
    regime_contrib = gross - regime_baseline
    # Alpha = pnl_total - (entry_timing + exit_slip + regime + commission)
    # Equivalently: alpha = (gross - regime) - commission - entry_timing - exit_slip
    alpha = pnl_total - (entry_timing_usd + exit_slip_usd + regime_contrib + commission_usd)

    return TradeAttribution(
        signal_id=signal.get("signal_id", ""),
        pnl_total=round(pnl_total, 4),
        alpha=round(alpha, 4),
        entry_timing=round(entry_timing_usd, 4),
        exit_slip=round(exit_slip_usd, 4),
        regime=round(regime_contrib, 4),
        commission=round(commission_usd, 4),
    )


def run_attribution(
    strategy_id: str | None = None,
    *,
    since_days: int = 30,
    point_value: float = 2.0,
    commission_per_rt: float = 0.85,
    _signal_path: Path | None = None,
    _fill_path: Path | None = None,
) -> AttributionReport:
    signals = _read_jsonl(
        _signal_path if _signal_path is not None else SIGNAL_LOG, since_days=since_days, strategy_id=strategy_id
    )
    fills = _read_jsonl(_fill_path if _fill_path is not None else BROKER_FILL_LOG, since_days=since_days)

    # Group fills by signal_id
    fills_by_sig: dict[str, list[dict]] = {}
    for f in fills:
        sid = f.get("signal_id")
        if sid:
            fills_by_sig.setdefault(sid, []).append(f)

    trade_attributions: list[TradeAttribution] = []
    for sig in signals:
        sid = sig.get("signal_id")
        if not sid or sid not in fills_by_sig:
            continue
        sig_fills = fills_by_sig[sid]
        entry_fill = next((f for f in sig_fills if str(f.get("exit_reason", "")).upper() == "ENTRY"), None)
        exit_fill = next(
            (f for f in sig_fills if str(f.get("exit_reason", "")).upper() in ("TARGET", "STOP", "TIMEOUT")), None
        )
        if not entry_fill or not exit_fill:
            continue
        # Naive regime baseline: trailing mean pnl across all signals
        # (could be enriched with actual regime classification)
        attr = attribute_trade(
            signal=sig,
            entry_fill=entry_fill,
            exit_fill=exit_fill,
            baseline_alpha=0.0,
            regime_baseline=0.0,
            point_value=point_value,
            commission_per_rt=commission_per_rt,
        )
        trade_attributions.append(attr)

    if not trade_attributions:
        return AttributionReport(
            strategy_id=strategy_id,
            n_trades=0,
            total_pnl=0.0,
            alpha_pct=None,
            entry_timing_pct=None,
            exit_slip_pct=None,
            regime_pct=None,
            commission_pct=None,
            notes=["no matched trade lifecycles (signal + ENTRY + exit)"],
        )

    total = sum(t.pnl_total for t in trade_attributions)
    alpha_total = sum(t.alpha for t in trade_attributions)
    timing_total = sum(t.entry_timing for t in trade_attributions)
    slip_total = sum(t.exit_slip for t in trade_attributions)
    regime_total = sum(t.regime for t in trade_attributions)
    commission_total = sum(t.commission for t in trade_attributions)
    # Express each as % of |total| (avoid div by 0)
    denom = abs(total) if abs(total) > 1e-6 else 1.0

    # Sharpe contribution: per-component sharpe = mean/std of that
    # component across trades
    def _sharpe(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        m = statistics.mean(values)
        var = sum((x - m) ** 2 for x in values) / max(len(values) - 1, 1)
        std = var**0.5
        return round(m / std, 4) if std > 0 else 0.0

    sharpe_attributed = {
        "alpha": _sharpe([t.alpha for t in trade_attributions]),
        "entry_timing": _sharpe([t.entry_timing for t in trade_attributions]),
        "exit_slip": _sharpe([t.exit_slip for t in trade_attributions]),
        "regime": _sharpe([t.regime for t in trade_attributions]),
    }

    return AttributionReport(
        strategy_id=strategy_id,
        n_trades=len(trade_attributions),
        total_pnl=round(total, 2),
        alpha_pct=round(alpha_total / denom * 100, 1),
        entry_timing_pct=round(timing_total / denom * 100, 1),
        exit_slip_pct=round(slip_total / denom * 100, 1),
        regime_pct=round(regime_total / denom * 100, 1),
        commission_pct=round(commission_total / denom * 100, 1),
        sharpe_attributed=sharpe_attributed,
        trades=trade_attributions,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default=None, help="strategy_id filter (default: all)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--point-value", type=float, default=2.0, help="USD per point (default 2.0 for MNQ)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = run_attribution(
        strategy_id=args.strategy,
        since_days=args.days,
        point_value=args.point_value,
    )
    try:
        with ATTRIBUTION_LOG.open("a", encoding="utf-8") as f:
            d = asdict(report)
            d.pop("trades", None)  # trim per-trade detail from log
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(), **d}, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: attribution log write failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return 0

    print()
    print("=" * 78)
    print(f"L2 PERFORMANCE ATTRIBUTION  (strategy={report.strategy_id or 'all'})")
    print("=" * 78)
    print(f"  n_trades         : {report.n_trades}")
    print(f"  total pnl (USD)  : ${report.total_pnl}")
    print()
    if report.alpha_pct is not None:
        print("  Decomposition (% of |total|):")
        print(f"    alpha          : {report.alpha_pct:+.1f}%")
        print(f"    entry_timing   : {report.entry_timing_pct:+.1f}%")
        print(f"    exit_slip      : {report.exit_slip_pct:+.1f}%")
        print(f"    regime         : {report.regime_pct:+.1f}%")
        print(f"    commission     : {report.commission_pct:+.1f}%")
        print()
        print("  Per-component sharpe across trades:")
        for k, v in report.sharpe_attributed.items():
            print(f"    {k:<15s} : {v:+.3f}")
    if report.notes:
        print()
        print("  Notes:")
        for n in report.notes:
            print(f"    - {n}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

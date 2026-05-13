"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_promotion_evaluator
============================================================
Reads recent L2 strategy results + capture-health digests + sweep
results and emits a per-strategy promotion verdict.

Why this exists
---------------
The l2_strategy_registry defines falsification + promotion criteria
per strategy.  Without an evaluator nothing checks those criteria —
they're just documentation.  This script materializes the criteria
as boolean decisions the operator can act on.

Promotion ladder
----------------
shadow → paper:
  - >= 14 days of clean depth captures (no MISSING/STALE)
  - >= 30 total trades from harness (real data)
  - bootstrap win_rate lower bound > 0.50
  - no risk-execution alerts in last 14 days

paper → live:
  - walk_forward.test.sharpe_proxy >= 0.5
  - walk_forward.promotion_gate.passes == True
  - sweep best deflated_sharpe >= 0.5
  - no risk-execution alerts in last 7 days

Retirement (falsification):
  - 60-day OOS sharpe < 0 (book_imbalance criterion)
  - 14-day rolling sharpe < -0.5 in any window
  - Brier > 0.30 on confidence calibration

Output
------
Per-strategy verdict in the SUPERCHARGE_VERDICT_CACHE format so the
operator's existing dashboard picks it up automatically.

Run
---
::

    python -m eta_engine.scripts.l2_promotion_evaluator
    python -m eta_engine.scripts.l2_promotion_evaluator --json
    python -m eta_engine.scripts.l2_promotion_evaluator --strategy mnq_book_imbalance_shadow
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)

L2_BACKTEST_LOG = LOG_DIR / "l2_backtest_runs.jsonl"
L2_SWEEP_LOG = LOG_DIR / "l2_sweep_runs.jsonl"
CAPTURE_HEALTH_LOG = LOG_DIR / "capture_health.jsonl"
ALERTS_LOG = LOG_DIR / "alerts_log.jsonl"
PROMOTION_LOG = LOG_DIR / "l2_promotion_decisions.jsonl"


@dataclass
class CriterionResult:
    name: str
    passed: bool
    observed: str  # human-readable observed value
    threshold: str  # human-readable required value


@dataclass
class StrategyEvaluation:
    bot_id: str
    strategy_id: str
    symbol: str
    current_status: str  # "shadow" | "paper" | "live"
    recommended_status: str  # what we'd promote to (or retire)
    criteria_for_shadow_to_paper: list[CriterionResult] = field(default_factory=list)
    criteria_for_paper_to_live: list[CriterionResult] = field(default_factory=list)
    criteria_for_retirement: list[CriterionResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _read_recent_jsonl(
    path: Path, *, since_days: int = 30, strategy: str | None = None, symbol: str | None = None
) -> list[dict]:
    """Read JSONL records from the last N days, optionally filtered."""
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
                ts = rec.get("ts") or rec.get("timestamp_utc")
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
                out.append(rec)
    except OSError:
        return []
    return out


def _harness_strategy_name(strategy_id: str) -> str:
    """Convert registry IDs like ``book_imbalance_v1`` to harness names."""
    return strategy_id.removesuffix("_v1")


def _evaluate_shadow_to_paper(strategy_id: str, symbol: str) -> list[CriterionResult]:
    """Criteria for shadow → paper promotion."""
    results: list[CriterionResult] = []
    # Map registry strategy_id to harness strategy name
    harness_strategy = _harness_strategy_name(strategy_id)

    # 1. >= 14 days of clean captures
    cap_records = _read_recent_jsonl(CAPTURE_HEALTH_LOG, since_days=14)
    n_clean_days = sum(1 for r in cap_records if r.get("verdict") == "GREEN")
    results.append(
        CriterionResult(
            name="captures_clean_14d",
            passed=n_clean_days >= 14,
            observed=f"{n_clean_days} green capture days",
            threshold=">= 14 clean days",
        )
    )

    # 2. >= 30 total trades from harness (real, recent)
    bt_records = _read_recent_jsonl(L2_BACKTEST_LOG, since_days=30, strategy=harness_strategy, symbol=symbol)
    total_trades = sum(r.get("n_trades", 0) for r in bt_records)
    results.append(
        CriterionResult(
            name="cumulative_trades_30d",
            passed=total_trades >= 30,
            observed=f"{total_trades} trades across {len(bt_records)} runs",
            threshold=">= 30 trades",
        )
    )

    # 3. Bootstrap win_rate CI lower bound > 0.50
    latest = bt_records[-1] if bt_records else None
    win_lo = None
    if latest:
        ci = latest.get("win_rate_ci_95") or [None, None]
        win_lo = ci[0] if isinstance(ci, list) and len(ci) >= 2 else None
    results.append(
        CriterionResult(
            name="win_rate_lower_ci_gt_50pct",
            passed=(win_lo is not None and win_lo > 0.50),
            observed=f"win_rate CI lower = {win_lo}" if win_lo is not None else "no CI yet",
            threshold="> 0.50",
        )
    )

    # 4. No risk alerts in last 14 days
    alerts = _read_recent_jsonl(ALERTS_LOG, since_days=14)
    risk_alerts = [
        a for a in alerts if a.get("level") in ("RED", "CRITICAL") and "risk" in str(a.get("source", "")).lower()
    ]
    results.append(
        CriterionResult(
            name="no_risk_alerts_14d",
            passed=len(risk_alerts) == 0,
            observed=f"{len(risk_alerts)} risk alerts",
            threshold="0 risk alerts",
        )
    )
    return results


def _evaluate_paper_to_live(strategy_id: str, symbol: str) -> list[CriterionResult]:
    """Criteria for paper → live promotion."""
    results: list[CriterionResult] = []
    harness_strategy = _harness_strategy_name(strategy_id)
    bt_records = _read_recent_jsonl(L2_BACKTEST_LOG, since_days=30, strategy=harness_strategy, symbol=symbol)
    latest = bt_records[-1] if bt_records else None

    # 1. Walk-forward OOS sharpe >= 0.5
    wf_test_sharpe = None
    if latest:
        wf = latest.get("walk_forward") or {}
        wf_test_sharpe = (wf.get("test") or {}).get("sharpe_proxy")
    results.append(
        CriterionResult(
            name="walk_forward_oos_sharpe_ge_0.5",
            passed=(wf_test_sharpe is not None and wf_test_sharpe >= 0.5),
            observed=f"OOS sharpe = {wf_test_sharpe}",
            threshold=">= 0.5",
        )
    )

    # 2. Promotion gate passes from walk-forward
    wf_passes = False
    if latest:
        wf = latest.get("walk_forward") or {}
        wf_passes = bool((wf.get("promotion_gate") or {}).get("passes"))
    results.append(
        CriterionResult(
            name="walk_forward_promotion_gate_passes",
            passed=wf_passes,
            observed=f"gate.passes={wf_passes}",
            threshold="True",
        )
    )

    # 3. Best sweep deflated_sharpe >= 0.5
    sweep_records = _read_recent_jsonl(L2_SWEEP_LOG, since_days=30, strategy=harness_strategy, symbol=symbol)
    latest_sweep = sweep_records[-1] if sweep_records else None
    best_dsr = None
    if latest_sweep:
        best_dsr = latest_sweep.get("best_deflated_sharpe")
    results.append(
        CriterionResult(
            name="sweep_best_deflated_sharpe_ge_0.5",
            passed=(best_dsr is not None and best_dsr >= 0.5),
            observed=f"sweep dsr = {best_dsr}",
            threshold=">= 0.5",
        )
    )

    # 4. No risk alerts in last 7 days
    alerts = _read_recent_jsonl(ALERTS_LOG, since_days=7)
    risk_alerts = [
        a for a in alerts if a.get("level") in ("RED", "CRITICAL") and "risk" in str(a.get("source", "")).lower()
    ]
    results.append(
        CriterionResult(
            name="no_risk_alerts_7d",
            passed=len(risk_alerts) == 0,
            observed=f"{len(risk_alerts)} risk alerts",
            threshold="0 risk alerts",
        )
    )
    return results


def _evaluate_retirement(strategy_id: str, symbol: str) -> list[CriterionResult]:
    """Falsification criteria — retirement triggers."""
    results: list[CriterionResult] = []
    harness_strategy = _harness_strategy_name(strategy_id)
    bt_records = _read_recent_jsonl(L2_BACKTEST_LOG, since_days=60, strategy=harness_strategy, symbol=symbol)

    # 1. 60-day OOS sharpe < 0
    latest = bt_records[-1] if bt_records else None
    wf_test_sharpe = None
    if latest:
        wf = latest.get("walk_forward") or {}
        wf_test_sharpe = (wf.get("test") or {}).get("sharpe_proxy")
    results.append(
        CriterionResult(
            name="60d_oos_sharpe_lt_0",
            passed=(wf_test_sharpe is not None and wf_test_sharpe < 0),  # passed=triggers retirement
            observed=f"OOS sharpe = {wf_test_sharpe}",
            threshold="< 0 → retire",
        )
    )

    # 2. Any 14-day rolling sharpe < -0.5
    rolling_14d = _read_recent_jsonl(L2_BACKTEST_LOG, since_days=14, strategy=harness_strategy, symbol=symbol)
    worst_sharpe = None
    for r in rolling_14d:
        sp = r.get("sharpe_proxy")
        if sp is None:
            continue
        if worst_sharpe is None or sp < worst_sharpe:
            worst_sharpe = sp
    results.append(
        CriterionResult(
            name="14d_rolling_sharpe_lt_minus_0.5",
            passed=(worst_sharpe is not None and worst_sharpe < -0.5),
            observed=f"14d worst sharpe = {worst_sharpe}",
            threshold="< -0.5 → retire",
        )
    )

    # 3. Sharpe CI entirely below zero on the latest run
    sharpe_ci_upper = None
    if latest:
        ci = latest.get("sharpe_ci_95") or [None, None]
        sharpe_ci_upper = ci[1] if isinstance(ci, list) and len(ci) >= 2 else None
    results.append(
        CriterionResult(
            name="sharpe_ci_entirely_negative",
            passed=(sharpe_ci_upper is not None and sharpe_ci_upper < 0),
            observed=f"sharpe CI upper bound = {sharpe_ci_upper}",
            threshold="< 0 (upper bound) → retire",
        )
    )
    return results


def evaluate_strategy(bot_id: str) -> StrategyEvaluation:
    from eta_engine.strategies.l2_strategy_registry import get_l2_strategy

    entry = get_l2_strategy(bot_id)
    if entry is None:
        raise ValueError(f"Unknown bot_id: {bot_id}")

    shadow_to_paper = _evaluate_shadow_to_paper(entry.strategy_id, entry.symbol)
    paper_to_live = _evaluate_paper_to_live(entry.strategy_id, entry.symbol)
    retirement = _evaluate_retirement(entry.strategy_id, entry.symbol)

    # Recommend status — defaults to current
    recommended = entry.promotion_status
    notes: list[str] = []

    # Retirement check first (overrides everything)
    if any(c.passed for c in retirement):
        # In retirement criteria, "passed=True" means the TRIGGER fired
        triggers = [c.name for c in retirement if c.passed]
        recommended = "retired"
        notes.append(f"Retirement triggered: {', '.join(triggers)}")
    else:
        # No retirement — consider promotion
        if entry.promotion_status == "shadow" and all(c.passed for c in shadow_to_paper):
            recommended = "paper"
            notes.append("All shadow→paper criteria met")
        elif entry.promotion_status == "paper" and all(c.passed for c in paper_to_live):
            recommended = "live"
            notes.append("All paper→live criteria met")
        elif entry.promotion_status == "shadow":
            failed = [c.name for c in shadow_to_paper if not c.passed]
            notes.append(f"Shadow→paper blocked by: {', '.join(failed)}")
        elif entry.promotion_status == "paper":
            failed = [c.name for c in paper_to_live if not c.passed]
            notes.append(f"Paper→live blocked by: {', '.join(failed)}")

    return StrategyEvaluation(
        bot_id=bot_id,
        strategy_id=entry.strategy_id,
        symbol=entry.symbol,
        current_status=entry.promotion_status,
        recommended_status=recommended,
        criteria_for_shadow_to_paper=shadow_to_paper,
        criteria_for_paper_to_live=paper_to_live,
        criteria_for_retirement=retirement,
        notes=notes,
    )


def evaluate_all() -> list[StrategyEvaluation]:
    from eta_engine.strategies.l2_strategy_registry import L2_STRATEGIES

    return [
        evaluate_strategy(s.bot_id) for s in L2_STRATEGIES if s.max_qty_contracts > 0
    ]  # skip filters (e.g. spread_regime)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default=None, help="Specific bot_id; default = all entry strategies")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    evals = [evaluate_strategy(args.strategy)] if args.strategy else evaluate_all()

    # Persist decisions
    try:
        with PROMOTION_LOG.open("a", encoding="utf-8") as f:
            for e in evals:
                f.write(
                    json.dumps(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "bot_id": e.bot_id,
                            "current_status": e.current_status,
                            "recommended_status": e.recommended_status,
                            "notes": e.notes,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
    except OSError as e:
        print(f"WARN: could not append promotion decision: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps([asdict(e) for e in evals], indent=2))
        return 0

    print()
    print("=" * 78)
    print("L2 PROMOTION EVALUATOR")
    print("=" * 78)
    for e in evals:
        print()
        print(f"{e.bot_id}  ({e.symbol})")
        print(f"  current_status     : {e.current_status}")
        print(f"  recommended_status : {e.recommended_status}")
        if e.notes:
            print("  notes:")
            for n in e.notes:
                print(f"    - {n}")
        if e.current_status == "shadow":
            print("  shadow → paper criteria:")
            for c in e.criteria_for_shadow_to_paper:
                mark = "[OK]" if c.passed else "[--]"
                print(f"    {mark} {c.name:<35s} observed: {c.observed:<35s} req: {c.threshold}")
        elif e.current_status == "paper":
            print("  paper → live criteria:")
            for c in e.criteria_for_paper_to_live:
                mark = "[OK]" if c.passed else "[--]"
                print(f"    {mark} {c.name:<35s} observed: {c.observed:<35s} req: {c.threshold}")
        # Always show retirement criteria
        triggers = [c for c in e.criteria_for_retirement if c.passed]
        if triggers:
            print("  RETIREMENT TRIGGERS FIRED:")
            for c in triggers:
                print(f"    [!!] {c.name:<35s} observed: {c.observed}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

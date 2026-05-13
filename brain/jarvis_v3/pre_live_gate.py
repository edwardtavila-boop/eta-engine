"""Pre-live promotion gate (Wave-16, 2026-04-27).

Hard gate every config has to pass before going to real money.
Combines:

  * Walk-forward harness output (Sharpe, max DD, PSR, Bonferroni)
  * Replay engine output (counterfactual lift over recent decisions)
  * Regression test set (must not break known-good cases)
  * Configurable hard floors

The operator runs:

    from eta_engine.brain.jarvis_v3.pre_live_gate import evaluate_for_live

    decision = evaluate_for_live(
        candidate_id="v23_sage_tighter",
        recent_r_multiples=journal_r,
        replay_lift=0.15,        # from replay_engine
        regression_pass_rate=1.0, # from regression_test_set
    )
    if decision.passed:
        print(f"PROMOTED: {decision.summary}")
    else:
        print(f"BLOCKED: {decision.failed_gates}")

Pure stdlib.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DECISIONS_LOG = ROOT / "state" / "jarvis_intel" / "promotion_decisions.jsonl"


@dataclass
class PreLiveGateConfig:
    """Operator-tunable knobs for the pre-live gate."""

    # Walk-forward
    min_sharpe: float = 1.0
    max_drawdown_r: float = 6.0
    min_psr: float = 0.95
    min_trades: int = 100

    # Replay
    min_replay_lift: float = 0.0  # 0.0 = at least no regression

    # Regression
    min_regression_pass_rate: float = 1.0  # default: no broken cases

    # Other
    min_is_oos_ratio: float = 0.6  # OOS sharpe / IS sharpe


@dataclass
class PromotionDecision:
    """Output of evaluate_for_live."""

    candidate_id: str
    ts: str
    passed: bool
    sharpe: float
    psr: float
    max_dd_r: float
    n_trades: int
    replay_lift: float
    regression_pass_rate: float
    is_oos_ratio: float
    gates: dict[str, bool] = field(default_factory=dict)
    failed_gates: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def evaluate_for_live(
    *,
    candidate_id: str,
    recent_r_multiples: list[float],
    replay_lift: float = 0.0,
    regression_pass_rate: float = 1.0,
    cfg: PreLiveGateConfig | None = None,
    decisions_log_path: Path = DEFAULT_DECISIONS_LOG,
    auto_persist: bool = True,
) -> PromotionDecision:
    """Run the gate. Returns a PromotionDecision with PASS/FAIL.

    The walk-forward harness gets called internally; replay_lift
    and regression_pass_rate are caller-supplied because their
    upstream components live elsewhere.
    """
    cfg = cfg or PreLiveGateConfig()

    from eta_engine.brain.jarvis_v3.walk_forward_harness import (
        WalkForwardConfig,
        run_walk_forward,
    )

    wf_cfg = WalkForwardConfig(
        target_sharpe=cfg.min_sharpe,
        max_dd_r=cfg.max_drawdown_r,
        psr_threshold=cfg.min_psr,
        min_aggregate_trades=cfg.min_trades,
    )
    wf = run_walk_forward(sample_r=recent_r_multiples, cfg=wf_cfg)

    is_oos_ratio = 0.0
    if wf.aggregate_sharpe > 0:
        train_sharpe = wf.aggregate_sharpe + wf.is_oos_gap
        is_oos_ratio = wf.aggregate_sharpe / train_sharpe if train_sharpe > 0 else 0.0

    gates: dict[str, bool] = {
        "walk_forward_sharpe": wf.aggregate_sharpe >= cfg.min_sharpe,
        "walk_forward_psr": wf.aggregate_psr >= cfg.min_psr,
        "walk_forward_max_dd": wf.aggregate_max_dd_r <= cfg.max_drawdown_r,
        "walk_forward_n_trades": wf.n_total_trades >= cfg.min_trades,
        "is_oos_ratio": is_oos_ratio >= cfg.min_is_oos_ratio,
        "replay_lift": replay_lift >= cfg.min_replay_lift,
        "regression_pass_rate": regression_pass_rate >= cfg.min_regression_pass_rate,
    }
    failed = [name for name, ok in gates.items() if not ok]
    passed = not failed

    summary = (
        f"{candidate_id}: {'PASS' if passed else 'FAIL'}; "
        f"sharpe={wf.aggregate_sharpe:.2f}, PSR={wf.aggregate_psr:.2f}, "
        f"DD={wf.aggregate_max_dd_r:.2f}R, "
        f"n={wf.n_total_trades}, "
        f"replay_lift={replay_lift:+.3f}, "
        f"regression={regression_pass_rate:.0%}, "
        f"IS/OOS={is_oos_ratio:.2f}"
    )
    if failed:
        summary += f"; failed: {', '.join(failed)}"

    decision = PromotionDecision(
        candidate_id=candidate_id,
        ts=datetime.now(UTC).isoformat(),
        passed=passed,
        sharpe=round(wf.aggregate_sharpe, 4),
        psr=round(wf.aggregate_psr, 4),
        max_dd_r=round(wf.aggregate_max_dd_r, 4),
        n_trades=wf.n_total_trades,
        replay_lift=round(replay_lift, 4),
        regression_pass_rate=round(regression_pass_rate, 4),
        is_oos_ratio=round(is_oos_ratio, 4),
        gates=gates,
        failed_gates=failed,
        summary=summary,
    )

    if auto_persist:
        try:
            decisions_log_path.parent.mkdir(parents=True, exist_ok=True)
            with decisions_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(decision.to_dict()) + "\n")
        except OSError as exc:
            logger.warning("pre_live_gate: persist failed (%s)", exc)

    return decision

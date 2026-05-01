"""Closed-loop trade feedback (Wave-12, 2026-04-27).

When a trade closes, multiple JARVIS subsystems need to learn from
the outcome:

  * HierarchicalMemory  -- record the episode (regime, action,
                            realized_r, narrative) so future
                            similar setups retrieve this analog
  * FilterBandit        -- credit/debit the filter arm that allowed
                            the trade
  * Calibrator          -- label the verdict with the realized
                            outcome so the calibrator can refit
  * MetaLearnerFull     -- record the outcome on any active shadow
                            trial whose hyperparameters mirrored
                            the deciding configuration
  * DecisionJournal     -- append the post-trade reconciliation
                            entry tying the original verdict to
                            the realized R

Without a unified handler, callers have to remember each of these
and inevitably miss one. This module is the SINGLE method:

    from eta_engine.brain.jarvis_v3.feedback_loop import close_trade

    close_trade(
        signal_id="cascade_hunter_2026-04-27T15:32",
        realized_r=2.4,
        regime="bullish_low_vol", session="rth", stress=0.3,
        direction="long",
        narrative="EMA stack aligned, sage approved, sentiment +0.4",
        action_taken="approve_full",
        memory=hierarchical_memory,
        filter_bandit=fb,
        meta_learner=ml,
        bot_id="cascade_hunter",
    )

Per-subsystem failures are caught individually so a calibrator
write error never blocks a memory write.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.filter_bandit import FilterBandit
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.meta_learner_full import MetaLearnerFull

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRADE_LOG = ROOT / "state" / "jarvis_intel" / "trade_closes.jsonl"


@dataclass
class TradeCloseRecord:
    """Audit record for one trade-close cycle."""

    ts: str
    signal_id: str
    bot_id: str
    realized_r: float
    regime: str
    session: str
    direction: str
    action_taken: str
    layers_updated: list[str] = field(default_factory=list)
    layer_errors: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def close_trade(
    *,
    signal_id: str,
    realized_r: float,
    regime: str,
    session: str,
    stress: float,
    direction: str,
    narrative: str = "",
    action_taken: str = "approve_full",
    bot_id: str = "",
    memory: HierarchicalMemory | None = None,
    filter_bandit: FilterBandit | None = None,
    filter_arm_used: str | None = None,
    meta_learner: MetaLearnerFull | None = None,
    meta_challenger_id: str | None = None,
    extra: dict | None = None,
    trade_log_path: Path = DEFAULT_TRADE_LOG,
) -> TradeCloseRecord:
    """Propagate a closed trade's realized R to every learning subsystem.

    Returns a TradeCloseRecord summarizing what got updated.
    Any subsystem failure is caught and recorded -- never raised.
    """
    layers_updated: list[str] = []
    layer_errors: list[str] = []

    # 1. Hierarchical memory
    if memory is not None:
        try:
            memory.record_episode(
                signal_id=signal_id,
                regime=regime, session=session, stress=stress,
                direction=direction, realized_r=realized_r,
                narrative=narrative,
                extra={"action": action_taken, "bot_id": bot_id, **(extra or {})},
            )
            layers_updated.append("memory")
        except Exception as exc:  # noqa: BLE001
            layer_errors.append(f"memory: {exc}")

    # 2. Filter bandit
    if filter_bandit is not None and filter_arm_used:
        try:
            filter_bandit.observe_outcome(filter_arm_used, realized_r)
            layers_updated.append("filter_bandit")
        except Exception as exc:  # noqa: BLE001
            layer_errors.append(f"filter_bandit: {exc}")

    # 3. Meta-learner shadow trial
    if meta_learner is not None and meta_challenger_id:
        try:
            meta_learner.record_outcome(meta_challenger_id, realized_r)
            layers_updated.append("meta_learner")
        except Exception as exc:  # noqa: BLE001
            layer_errors.append(f"meta_learner: {exc}")

    # 4. Decision journal -- best-effort append
    try:
        from eta_engine.obs.decision_journal import DecisionJournal
        journal = DecisionJournal.default()
        journal.append_post_trade(
            signal_id=signal_id, realized_r=realized_r,
            metadata={
                "bot_id": bot_id, "action_taken": action_taken,
                "regime": regime, "session": session,
            },
        )
        layers_updated.append("decision_journal")
    except Exception as exc:  # noqa: BLE001
        # DecisionJournal API may differ in production; keep this
        # opt-in / best-effort so we never block on the journal
        layer_errors.append(f"decision_journal: {exc}")

    # 5. Calibrator -- label the verdict for refit
    try:
        from eta_engine.brain.jarvis_v3.calibration import (
            CalibratorRecorder,
        )
        rec = CalibratorRecorder.default()
        rec.record_label(
            signal_id=signal_id, realized_r=realized_r,
            regime=regime, action=action_taken,
        )
        layers_updated.append("calibrator")
    except Exception as exc:  # noqa: BLE001
        layer_errors.append(f"calibrator: {exc}")

    # 6. Per-school edge tracker -- attribute realized R to each school
    #    that was consulted during the trade's entry signal.
    try:
        from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker
        from eta_engine.brain.jarvis_v3.sage.last_report_cache import pop_last_any
        tracker = default_tracker()
        report = pop_last_any()
        if report is not None:
            for school_name, verdict in report.per_school.items():
                tracker.observe(
                    school=school_name,
                    school_bias=verdict.bias.value,
                    entry_side=direction,
                    realized_r=realized_r,
                )
            layers_updated.append("sage_edge_tracker")
    except Exception as exc:  # noqa: BLE001
        layer_errors.append(f"sage_edge_tracker: {exc}")

    # 7. Persist trade-close audit record
    record = TradeCloseRecord(
        ts=datetime.now(UTC).isoformat(),
        signal_id=signal_id,
        bot_id=bot_id,
        realized_r=float(realized_r),
        regime=regime,
        session=session,
        direction=direction,
        action_taken=action_taken,
        layers_updated=layers_updated,
        layer_errors=layer_errors,
        extra=extra or {},
    )
    try:
        trade_log_path.parent.mkdir(parents=True, exist_ok=True)
        with trade_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record)) + "\n")
    except OSError as exc:
        logger.warning("feedback_loop: trade-log append failed (%s)", exc)

    return record


def replay_trade_closes(
    *,
    memory: HierarchicalMemory,
    trade_log_path: Path = DEFAULT_TRADE_LOG,
    skip_existing: bool = True,
) -> int:
    """Rebuild memory state from the trade-close log.

    Useful for warm-starting a fresh memory instance from journal
    history. Returns the number of episodes loaded.
    """
    if not trade_log_path.exists():
        return 0
    n_loaded = 0
    seen_signal_ids: set[str] = set()
    if skip_existing:
        seen_signal_ids = {
            ep.signal_id for ep in memory._episodes
        }
    try:
        for line in trade_log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            sig = str(d.get("signal_id", ""))
            if sig in seen_signal_ids:
                continue
            try:
                memory.record_episode(
                    signal_id=sig,
                    regime=str(d.get("regime", "neutral")),
                    session=str(d.get("session", "rth")),
                    stress=float(d.get("extra", {}).get("stress", 0.5)),
                    direction=str(d.get("direction", "long")),
                    realized_r=float(d.get("realized_r", 0.0)),
                    narrative="(replayed from trade_closes.jsonl)",
                    extra=d.get("extra", {}),
                )
                seen_signal_ids.add(sig)
                n_loaded += 1
            except (KeyError, ValueError):
                continue
    except OSError as exc:
        logger.warning("feedback_loop: replay failed (%s)", exc)
    return n_loaded

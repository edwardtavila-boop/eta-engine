"""Counterfactual replay engine (Wave-13, 2026-04-27).

JARVIS keeps a journal of every consultation he's done. The replay
engine asks: "if I'd been running TODAY's policy stack for the
entire history of that journal, where would my decisions have
DIFFERED, and would they have been better?"

Use case (operator's pre-promotion check):

    from eta_engine.brain.jarvis_v3.replay_engine import (
        replay_decisions,
    )

    report = replay_decisions(
        verdict_log_path=Path("state/jarvis_intel/verdicts.jsonl"),
        n_days_back=90,
        new_policy_fn=lambda packet: my_new_policy(packet),
    )
    print(report.summary)
    # "Replay over 90 days, 412 consultations:
    #   - 38 verdicts would change (9.2%)
    #   - 24 changes are improvements (avg +0.4R)
    #   - 11 are regressions (avg -0.3R)
    #   - 3 ambiguous (no realized R yet)
    #   Net counterfactual lift: +0.21R per trade"

The new policy is a callable that takes a verdict record dict and
returns the verdict it WOULD produce. Pure -- no I/O, no live calls.
The engine joins replayed verdicts against the trade-close log so
the realized R is available for measuring lift.

Pure stdlib. No NumPy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts import workspace_roots

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_VERDICT_LOG = workspace_roots.ETA_JARVIS_VERDICTS_PATH
DEFAULT_TRADE_LOG = workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH


@dataclass
class ReplayDelta:
    """One verdict that changed under the new policy."""

    signal_id: str
    ts: str
    original_verdict: str
    new_verdict: str
    realized_r: float | None
    change_kind: str  # "improvement" / "regression" / "ambiguous"
    delta_r: float | None  # signed: positive = new policy did better


@dataclass
class ReplayReport:
    """Aggregated counterfactual replay summary."""

    n_consultations: int
    n_changed: int
    n_improvements: int
    n_regressions: int
    n_ambiguous: int
    avg_improvement_r: float
    avg_regression_r: float
    net_counterfactual_lift: float
    deltas: list[ReplayDelta] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "n_consultations": self.n_consultations,
            "n_changed": self.n_changed,
            "n_improvements": self.n_improvements,
            "n_regressions": self.n_regressions,
            "n_ambiguous": self.n_ambiguous,
            "avg_improvement_r": self.avg_improvement_r,
            "avg_regression_r": self.avg_regression_r,
            "net_counterfactual_lift": self.net_counterfactual_lift,
            "summary": self.summary,
        }


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("replay_engine: %s read failed (%s)", p, exc)
    return out


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        return None


def replay_decisions(
    *,
    verdict_log_path: Path = DEFAULT_VERDICT_LOG,
    trade_log_path: Path = DEFAULT_TRADE_LOG,
    n_days_back: float = 90,
    new_policy_fn: Callable[[dict], str],
    improvement_kinds: dict[tuple[str, str], float] | None = None,
) -> ReplayReport:
    """Replay every consultation in the verdict log under ``new_policy_fn``.

    ``new_policy_fn(verdict_dict) -> str`` takes the historical
    verdict record and returns the new policy's verdict label
    (APPROVED / DEFERRED / DENIED / etc.).

    To measure lift, we join verdicts against trade closes by
    signal_id and read the realized R. For each changed verdict:
      * "improvement" if the new policy's choice would have produced
        a BETTER outcome than the original (e.g. original APPROVED
        a -2R loser, new policy DENIED -> +2R improvement)
      * "regression" if the new policy's choice would have been
        WORSE (DENIED a winner)
      * "ambiguous" if no realized R is available yet

    The improvement_kinds mapping defines the signed payoff per
    (original, new) verdict pair -- defaults are conservative.
    """
    cutoff = datetime.now(UTC) - timedelta(days=n_days_back)
    verdicts = _read_jsonl(verdict_log_path)
    trades = _read_jsonl(trade_log_path)

    # Index trades by signal_id -> realized_r
    trade_by_sig: dict[str, float] = {}
    for t in trades:
        sig = str(t.get("signal_id", ""))
        if sig:
            trade_by_sig[sig] = float(t.get("realized_r", 0.0))

    # Filter to recent verdicts
    recent = [v for v in verdicts if (dt := _parse_ts(v.get("ts"))) is not None and dt >= cutoff]

    deltas: list[ReplayDelta] = []
    n_consultations = len(recent)

    for v in recent:
        original = str(v.get("final_verdict", "UNKNOWN"))
        try:
            new = str(new_policy_fn(v))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "replay_engine: new_policy raised on %s (%s)",
                v.get("signal_id"),
                exc,
            )
            continue
        if new == original:
            continue
        sig = str(v.get("signal_id", ""))
        realized = trade_by_sig.get(sig)
        change_kind, delta_r = _classify(
            original,
            new,
            realized,
            improvement_kinds,
        )
        deltas.append(
            ReplayDelta(
                signal_id=sig,
                ts=str(v.get("ts", "")),
                original_verdict=original,
                new_verdict=new,
                realized_r=realized,
                change_kind=change_kind,
                delta_r=delta_r,
            )
        )

    n_changed = len(deltas)
    improvements = [d for d in deltas if d.change_kind == "improvement"]
    regressions = [d for d in deltas if d.change_kind == "regression"]
    ambiguous = [d for d in deltas if d.change_kind == "ambiguous"]
    avg_imp = (
        (sum(d.delta_r for d in improvements if d.delta_r is not None) / len(improvements)) if improvements else 0.0
    )
    avg_reg = (sum(d.delta_r for d in regressions if d.delta_r is not None) / len(regressions)) if regressions else 0.0
    net_lift = sum(d.delta_r for d in deltas if d.delta_r is not None) / max(n_consultations, 1)

    summary = (
        f"Replay over {n_days_back:.0f} days, {n_consultations} consultations: "
        f"{n_changed} would change ({100 * n_changed / max(n_consultations, 1):.1f}%); "
        f"{len(improvements)} improvements (avg {avg_imp:+.3f}R), "
        f"{len(regressions)} regressions (avg {avg_reg:+.3f}R), "
        f"{len(ambiguous)} ambiguous. "
        f"Net counterfactual lift: {net_lift:+.3f}R per consultation."
    )

    return ReplayReport(
        n_consultations=n_consultations,
        n_changed=n_changed,
        n_improvements=len(improvements),
        n_regressions=len(regressions),
        n_ambiguous=len(ambiguous),
        avg_improvement_r=round(avg_imp, 4),
        avg_regression_r=round(avg_reg, 4),
        net_counterfactual_lift=round(net_lift, 4),
        deltas=deltas,
        summary=summary,
    )


def _classify(
    original: str,
    new: str,
    realized_r: float | None,
    improvement_kinds: dict[tuple[str, str], float] | None,
) -> tuple[str, float | None]:
    """Determine if (original -> new) is improvement / regression /
    ambiguous given the realized R."""
    if realized_r is None:
        return "ambiguous", None
    # The "right" call after the fact:
    #   * realized_r > 0 -> the trade was a WINNER, APPROVED was right
    #   * realized_r < 0 -> the trade was a LOSER, DENIED/DEFERRED was right
    #   * realized_r == 0 -> tie
    original_was_approve = original.upper() in {"APPROVED", "CONDITIONAL"}
    new_is_approve = new.upper() in {"APPROVED", "CONDITIONAL"}

    # Same direction -> not actually a meaningful change
    if original_was_approve == new_is_approve:
        return "ambiguous", 0.0

    if realized_r > 0:
        # Trade won -> approving was correct
        if new_is_approve:
            # Original missed it; new would catch it
            return "improvement", abs(realized_r)
        # Original caught the win; new would have skipped
        return "regression", -abs(realized_r)
    if realized_r < 0:
        # Trade lost -> declining was correct
        if not new_is_approve:
            # Original took the loser; new would have skipped
            return "improvement", abs(realized_r)
        # Original avoided the loser; new would have taken
        return "regression", -abs(realized_r)
    return "ambiguous", 0.0

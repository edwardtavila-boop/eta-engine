"""
JARVIS v3 // next_level.strategy_synthesis
==========================================
Automated strategy synthesis from precedent gaps.

JARVIS observes thousands of decisions over time. Each is tagged with
``(regime, session, event_category, binding_constraint)`` plus an
outcome. This module mines the precedent graph for buckets where:

  1. sample count is growing (the regime+session occurs often)
  2. current policy behavior (mostly STAND_ASIDE / DENIED / REDUCE)
  3. shadow-portfolio regret is positive (we'd have won if we traded)

These are candidate strategy ideas -- the operator hands the spec to
``strategy-generator`` for backtesting. JARVIS becomes a source of
hypotheses, not just a gate.

Pure / deterministic. Hands off to external strategy-generator via
a structured ``StrategySpec`` pydantic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.precedent import (
        PrecedentGraph,
        PrecedentKey,
    )


class StrategySpec(BaseModel):
    """Candidate strategy the operator should consider building."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    regime: str
    session_phase: str
    event_category: str = ""
    binding_constraint: str = ""
    sample_support: int = Field(ge=0)
    historical_mean_r: float | None = None
    historical_win_rate: float | None = None
    priority: str = Field(pattern="^(low|medium|high)$")
    rationale: str
    proposed_at: datetime


class SynthesisReport(BaseModel):
    """Output of one mining pass."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    buckets_scanned: int = Field(ge=0)
    candidates_found: int = Field(ge=0)
    specs: list[StrategySpec]
    note: str


# Thresholds for "this bucket is interesting"
MIN_SAMPLE_SUPPORT = 20  # need at least N observations
MIN_POSITIVE_R = 0.40  # historical mean R worth pursuing
MIN_WIN_RATE = 0.50


def mine(
    graph: PrecedentGraph,
    *,
    min_support: int = MIN_SAMPLE_SUPPORT,
    min_mean_r: float = MIN_POSITIVE_R,
    min_win_rate: float = MIN_WIN_RATE,
    now: datetime | None = None,
) -> SynthesisReport:
    """Scan every bucket and emit specs for high-regret / high-alpha ones."""
    now = now or datetime.now(UTC)
    specs: list[StrategySpec] = []
    buckets = graph.keys()
    for k in buckets:
        q = graph.query(k)
        if q.n < min_support:
            continue
        mean_r = q.mean_r or 0.0
        wr = q.win_rate or 0.0
        if mean_r < min_mean_r or wr < min_win_rate:
            continue
        priority = "high" if (mean_r >= 1.0 and wr >= 0.60) else ("medium" if mean_r >= 0.7 else "low")
        hypothesis = _build_hypothesis(k, mean_r, wr, q.n)
        spec_id = f"S-{k.regime}-{k.session_phase}-{_short_hash(hypothesis)}"
        specs.append(
            StrategySpec(
                id=spec_id,
                hypothesis=hypothesis,
                regime=k.regime,
                session_phase=k.session_phase,
                event_category=k.event_category,
                binding_constraint=k.binding_constraint,
                sample_support=q.n,
                historical_mean_r=round(mean_r, 3),
                historical_win_rate=round(wr, 3),
                priority=priority,
                rationale=(
                    f"precedent bucket ({k.regime}, {k.session_phase}) has "
                    f"mean_r={mean_r:+.2f} over {q.n} samples with "
                    f"win_rate={wr:.0%} -- historical edge"
                ),
                proposed_at=now,
            )
        )
    return SynthesisReport(
        ts=now,
        buckets_scanned=len(buckets),
        candidates_found=len(specs),
        specs=specs,
        note=(
            f"mined {len(buckets)} buckets; {len(specs)} passed "
            f"min_support={min_support}, min_mean_r={min_mean_r}, "
            f"min_win_rate={min_win_rate}"
        ),
    )


def export_specs(report: SynthesisReport, out_path: Path | str) -> None:
    """Serialize specs to disk so strategy-generator can pick them up."""
    data = {
        "ts": report.ts.isoformat(),
        "buckets_scanned": report.buckets_scanned,
        "candidates_found": report.candidates_found,
        "specs": [s.model_dump(mode="json") for s in report.specs],
    }
    Path(out_path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _build_hypothesis(
    key: PrecedentKey,
    mean_r: float,
    wr: float,
    n: int,
) -> str:
    parts = [
        f"When regime={key.regime}",
        f"session={key.session_phase}",
    ]
    if key.event_category and key.event_category != "none":
        parts.append(f"event={key.event_category}")
    if key.binding_constraint and key.binding_constraint != "none":
        parts.append(f"and binding={key.binding_constraint}")
    preamble = ", ".join(parts)
    return (
        f"{preamble}: historically produced {mean_r:+.2f} mean R "
        f"({wr:.0%} win rate, n={n}). Hypothesize a dedicated setup that "
        f"preferentially takes action in this regime/session combination."
    )


def _short_hash(s: str) -> str:
    h = 0
    for ch in s:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return f"{h:08x}"

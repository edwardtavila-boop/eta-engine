"""
EVOLUTIONARY TRADING ALGO  //  brain.jarvis_daily_report
=============================================
End-of-day JARVIS report — markdown summary of session state, recommendations,
demotion savings, anomalies. Auto-generated for daily ops review.

Why this exists
---------------
Every 24h the operator should know:
  - How is JARVIS feeling? (health verdict)
  - What's the current state? (phase, slow-bleed, regime, gates)
  - What did JARVIS recommend today and was it acted on?
  - What anomalies fired?
  - How much did phase-aware routing save?

This module produces a single markdown blob covering all of it.

Public API
----------
  * ``DailyReport``          — pydantic with all sections
  * ``generate_daily_report``— builder
  * ``render_markdown``      — turns DailyReport into operator-readable md
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from eta_engine.brain.jarvis_anomaly import detect_transition_anomalies
from eta_engine.brain.jarvis_health import run_self_test
from eta_engine.brain.jarvis_journals import (
    AnomalyJournal,
    RecommendationJournal,
    StateJournal,
)
from eta_engine.brain.jarvis_recommender import recommend
from eta_engine.brain.jarvis_session_state import snapshot

if TYPE_CHECKING:
    from pathlib import Path


class DailyReport(BaseModel):
    generated_at: datetime
    health_verdict: str
    health_failures: list[str] = Field(default_factory=list)
    current_state: dict[str, Any] = Field(default_factory=dict)
    n_state_journal_entries_today: int = 0
    n_recommendations_logged_today: int = 0
    n_anomalies_logged_today: int = 0
    open_recommendations: list[dict] = Field(default_factory=list)
    open_anomalies: list[dict] = Field(default_factory=list)
    demotion_savings_summary: dict | None = None


def _entries_in_last(j_entries: list[dict], hours: float) -> list[dict]:
    if not j_entries:
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    cutoff_iso = cutoff.isoformat()
    return [e for e in j_entries if e.get("ts", "") >= cutoff_iso]


def generate_daily_report(
    *,
    state_journal_path: Path | None = None,
    recs_journal_path: Path | None = None,
    anomaly_journal_path: Path | None = None,
    cost_ledger_path: Path | None = None,
    hours: float = 24.0,
) -> DailyReport:
    """Build a daily report for the last `hours` of JARVIS activity."""
    # Health check — fresh
    health_results, verdict = run_self_test()
    failures = [r.detail for r in health_results if not r.passed]

    # Current state — fresh
    snap = snapshot()
    open_recs = recommend(snap)

    # Detect any anomalies between latest two state journal entries
    state_j = StateJournal(state_journal_path) if state_journal_path else StateJournal()
    state_entries = state_j.read_all()
    state_today = _entries_in_last(state_entries, hours)
    open_anom: list[dict] = []
    if len(state_entries) >= 2:
        try:
            from eta_engine.brain.jarvis_session_state import (
                SessionStateSnapshot,
            )

            prev = SessionStateSnapshot.model_validate(state_entries[-2]["snapshot"])
            curr = SessionStateSnapshot.model_validate(state_entries[-1]["snapshot"])
            anomalies = detect_transition_anomalies(prev, curr)
            open_anom = [a.model_dump(mode="json") for a in anomalies]
        except Exception:  # noqa: BLE001
            pass

    # Recs / anomaly journal counts
    recs_j = RecommendationJournal(recs_journal_path) if recs_journal_path else RecommendationJournal()
    anom_j = AnomalyJournal(anomaly_journal_path) if anomaly_journal_path else AnomalyJournal()
    recs_today = _entries_in_last(recs_j.read_all(), hours)
    anom_today = _entries_in_last(anom_j.read_all(), hours)

    # Cost demotion savings (best effort — load if persistence file given)
    savings_summary: dict | None = None
    if cost_ledger_path is not None:
        try:
            from eta_engine.brain.jarvis_cost_attribution import CostLedger

            ledger = CostLedger.load_from_jsonl(cost_ledger_path)
            cutoff = datetime.now(UTC) - timedelta(hours=hours)
            savings_summary = ledger.demotion_savings(window_start=cutoff)
        except Exception:  # noqa: BLE001
            pass

    return DailyReport(
        generated_at=datetime.now(UTC),
        health_verdict=verdict.value,
        health_failures=failures,
        current_state={
            "phase": snap.iteration_phase.value,
            "freeze_label": snap.freeze_label,
            "cumulative_trials": snap.cumulative_trials,
            "trial_budget_alert": snap.trial_budget_alert,
            "slow_bleed_level": snap.slow_bleed_level.value,
            "regime_label": snap.regime_label,
            "gate_report_label": snap.gate_report_label,
            "gate_auto_fail": snap.gate_auto_fail,
            "applicable_lesson_ids": snap.applicable_lesson_ids,
        },
        n_state_journal_entries_today=len(state_today),
        n_recommendations_logged_today=len(recs_today),
        n_anomalies_logged_today=len(anom_today),
        open_recommendations=[r.model_dump(mode="json") for r in open_recs],
        open_anomalies=open_anom,
        demotion_savings_summary=savings_summary,
    )


def render_markdown(report: DailyReport) -> str:
    """Operator-readable markdown rendering."""
    lines = [
        "# JARVIS Daily Report",
        f"**Generated:** {report.generated_at.isoformat()}",
        f"**Health:** `{report.health_verdict}`",
        "",
    ]
    if report.health_failures:
        lines.append("**Health failures:**")
        for f in report.health_failures:
            lines.append(f"- {f}")
        lines.append("")

    lines.append("## Current state")
    for k, v in report.current_state.items():
        lines.append(f"- **{k}:** `{v}`")
    lines.append("")

    lines.append("## Activity (last 24h)")
    lines.append(f"- State journal entries: {report.n_state_journal_entries_today}")
    lines.append(f"- Recommendations logged: {report.n_recommendations_logged_today}")
    lines.append(f"- Anomalies logged: {report.n_anomalies_logged_today}")
    lines.append("")

    if report.open_recommendations:
        lines.append("## Open recommendations")
        for r in report.open_recommendations:
            lines.append(f"### `{r['level']}` — {r['title']}")
            lines.append(f"_{r['code']}_")
            lines.append("")
            lines.append(r["rationale"])
            if r.get("action"):
                lines.append("")
                lines.append(f"**Action:** {r['action']}")
            if r.get("lesson_refs"):
                lines.append("")
                lines.append(f"**Lessons:** {', '.join(f'#{n}' for n in r['lesson_refs'])}")
            lines.append("")

    if report.open_anomalies:
        lines.append("## Open anomalies")
        for a in report.open_anomalies:
            lines.append(f"- **{a['severity']}** `{a['code']}` — {a['message']}")
        lines.append("")

    if report.demotion_savings_summary is not None:
        s = report.demotion_savings_summary
        lines.append("## Phase-aware routing savings (24h)")
        lines.append(f"- Demoted events: {s.get('n_demoted_events', 0)}")
        lines.append(f"- Tokens demoted: {s.get('tokens_demoted', 0)}")
        lines.append(f"- Sonnet-equivalent units saved: {s.get('sonnet_equiv_saved', 0):.1f}")
        lines.append("")

    return "\n".join(lines)

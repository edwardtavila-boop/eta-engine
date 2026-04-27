"""JARVIS-owned aggregator: trial_log + slow_bleed + regime + gate_report."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS = _REPO_ROOT / "eta_engine" / "docs"
_TRIAL_LOG = _DOCS / "trial_log.json"


class IterationPhase(StrEnum):
    SEARCH = "search"
    DEPLOYMENT = "deployment"


class SlowBleedLevel(StrEnum):
    GREEN = "green"
    WARNING = "warning"
    TRIPPED = "tripped"


class SessionStateSnapshot(BaseModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    iteration_phase: IterationPhase = IterationPhase.SEARCH
    cumulative_trials: int = 0
    freeze_label: str | None = None
    n_trials_log_entries: int = 0
    slow_bleed_level: SlowBleedLevel = SlowBleedLevel.GREEN
    rolling_expectancy_r: float | None = None
    slow_bleed_window_n: int = 0
    slow_bleed_threshold_r: float = -0.10
    regime_composite: float | None = None
    regime_label: str | None = None
    regime_signal_set: str = "v2x"
    gate_report_label: str | None = None
    gate_report_status: str | None = None
    gate_auto_pass: int = 0
    gate_auto_fail: int = 0
    gate_auto_insufficient: int = 0
    trial_budget_remaining: int | None = None
    sharpe_for_budget_calc: float | None = None
    sources_present: list[str] = Field(default_factory=list)
    sources_missing: list[str] = Field(default_factory=list)
    trial_budget_alert: str = "GREEN"
    gate_report_age_hours: float | None = None
    gate_report_stale: bool = False
    gate_report_stale_threshold_hours: float = 168.0
    applicable_lesson_ids: list[int] = Field(default_factory=list)


def _load_trial_log_state() -> tuple[IterationPhase, int, str | None, int, list[str], list[str]]:
    present: list[str] = []
    missing: list[str] = []
    if not _TRIAL_LOG.exists():
        missing.append(str(_TRIAL_LOG))
        return IterationPhase.SEARCH, 0, None, 0, present, missing
    present.append(str(_TRIAL_LOG))
    try:
        data = json.loads(_TRIAL_LOG.read_text())
    except (json.JSONDecodeError, OSError):
        missing.append(f"{_TRIAL_LOG} (corrupt)")
        return IterationPhase.SEARCH, 0, None, 0, present, missing
    rows = data.get("trials", []) if isinstance(data, dict) else []
    n_entries = len(rows)
    if not rows:
        return IterationPhase.SEARCH, 0, None, 0, present, missing
    last_freeze_idx: int | None = None
    for idx in range(len(rows) - 1, -1, -1):
        if rows[idx].get("trial_kind") == "freeze":
            last_freeze_idx = idx
            break
    if last_freeze_idx is None:
        cumulative = sum(int(r.get("n_variants_tested", 1)) for r in rows)
        return IterationPhase.SEARCH, cumulative, None, n_entries, present, missing
    if last_freeze_idx == len(rows) - 1:
        prev_freeze_idx: int | None = None
        for idx in range(last_freeze_idx - 1, -1, -1):
            if rows[idx].get("trial_kind") == "freeze":
                prev_freeze_idx = idx
                break
        start = (prev_freeze_idx + 1) if prev_freeze_idx is not None else 0
        cumulative = sum(int(r.get("n_variants_tested", 1)) for r in rows[start:last_freeze_idx])
        return (
            IterationPhase.DEPLOYMENT,
            cumulative,
            rows[last_freeze_idx].get("label", ""),
            n_entries,
            present,
            missing,
        )
    cumulative = sum(int(r.get("n_variants_tested", 1)) for r in rows[last_freeze_idx + 1 :])
    return IterationPhase.SEARCH, cumulative, None, n_entries, present, missing


def _classify_slow_bleed(
    rs: list[float] | None, *, window_n: int = 20, min_n: int = 10, threshold_r: float = -0.10, warn_ratio: float = 0.5
) -> tuple[SlowBleedLevel, float | None, int]:
    if not rs:
        return SlowBleedLevel.GREEN, None, 0
    window = rs[-window_n:]
    n = len(window)
    if n == 0:
        return SlowBleedLevel.GREEN, None, 0
    rolling = sum(window) / n
    warn_line = threshold_r * warn_ratio
    if n >= min_n and rolling <= threshold_r:
        return SlowBleedLevel.TRIPPED, rolling, n
    if rolling <= warn_line:
        return SlowBleedLevel.WARNING, rolling, n
    return SlowBleedLevel.GREEN, rolling, n


def _load_latest_gate_report() -> tuple[str | None, str | None, int, int, int, list[str], float | None]:
    sources: list[str] = []
    if not _DOCS.exists():
        return None, None, 0, 0, 0, sources, None
    candidates = sorted(_DOCS.glob("gate_report_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None, None, 0, 0, 0, sources, None
    latest = candidates[0]
    sources.append(str(latest))
    age_hours: float | None = None
    with contextlib.suppress(OSError):
        age_hours = max(0.0, (datetime.now(UTC).timestamp() - latest.stat().st_mtime) / 3600.0)
    try:
        data = json.loads(latest.read_text())
    except (json.JSONDecodeError, OSError):
        return None, None, 0, 0, 0, sources, age_hours
    label = str(data.get("label", "")) or None
    summary = data.get("summary", {}) if isinstance(data, dict) else {}
    return (
        label,
        summary.get("live_promotion_status"),
        int(summary.get("auto_pass", 0)),
        int(summary.get("auto_fail", 0)),
        int(summary.get("auto_insufficient", 0)),
        sources,
        age_hours,
    )


def _trial_budget_at_sharpe(sharpe: float | None) -> int | None:
    if sharpe is None or sharpe <= 0.0:
        return 0
    try:
        from eta_engine.backtest.deflated_sharpe import compute_dsr
    except ImportError:
        return None
    sk, kt, n = 0.0, 3.0, 51
    last_pass = 0
    for n_trials in (1, 2, 3, 5, 8, 10, 15, 20, 30, 50, 100):
        if compute_dsr(sharpe, n, sk, kt, n_trials=n_trials) > 0.95:
            last_pass = n_trials
        else:
            break
    return last_pass


def _classify_trial_budget_alert(remaining: int | None) -> str:
    if remaining is None:
        return "UNKNOWN"
    if remaining == 0:
        return "RED"
    if remaining <= 5:
        return "YELLOW"
    return "GREEN"


def _applicable_lessons(p: dict) -> list[int]:
    out: set[int] = set()
    if p.get("slow_bleed_level") in ("warning", "tripped"):
        out.update([14, 19])
    if p.get("regime_label") == "choppy":
        out.update([28, 29])
    if p.get("iteration_phase") == "search" and p.get("trial_budget_alert") in ("RED", "YELLOW"):
        out.update([18, 27])
    if p.get("gate_report_stale"):
        out.add(22)
    if int(p.get("gate_auto_fail", 0)) > 0:
        out.add(16)
    return sorted(out)


def snapshot(
    *,
    recent_trade_rs: list[float] | None = None,
    regime_composite: float | None = None,
    regime_label: str | None = None,
    regime_signal_set: str = "v2x",
    sharpe_for_budget: float | None = None,
    slow_bleed_window_n: int = 20,
    slow_bleed_min_n: int = 10,
    slow_bleed_threshold_r: float = -0.10,
) -> SessionStateSnapshot:
    sources_present: list[str] = []
    sources_missing: list[str] = []
    phase, cumulative, freeze_label, n_entries, t_present, t_missing = _load_trial_log_state()
    sources_present.extend(t_present)
    sources_missing.extend(t_missing)
    bleed, rolling, win_n = _classify_slow_bleed(
        recent_trade_rs, window_n=slow_bleed_window_n, min_n=slow_bleed_min_n, threshold_r=slow_bleed_threshold_r
    )
    label, status, ap, af, ai, gr_sources, age_h = _load_latest_gate_report()
    sources_present.extend(gr_sources)
    stale_threshold = 168.0
    stale = age_h is not None and age_h > stale_threshold
    budget = _trial_budget_at_sharpe(sharpe_for_budget)
    budget_remaining = max(0, budget - cumulative) if budget is not None and budget > 0 else 0
    budget_alert = _classify_trial_budget_alert(budget_remaining)
    lessons = _applicable_lessons(
        {
            "slow_bleed_level": bleed.value,
            "regime_label": regime_label,
            "iteration_phase": phase.value,
            "trial_budget_alert": budget_alert,
            "gate_report_stale": stale,
            "gate_auto_fail": af,
        }
    )
    return SessionStateSnapshot(
        iteration_phase=phase,
        cumulative_trials=cumulative,
        freeze_label=freeze_label,
        n_trials_log_entries=n_entries,
        slow_bleed_level=bleed,
        rolling_expectancy_r=rolling,
        slow_bleed_window_n=win_n,
        slow_bleed_threshold_r=slow_bleed_threshold_r,
        regime_composite=regime_composite,
        regime_label=regime_label,
        regime_signal_set=regime_signal_set,
        gate_report_label=label,
        gate_report_status=status,
        gate_auto_pass=ap,
        gate_auto_fail=af,
        gate_auto_insufficient=ai,
        trial_budget_remaining=budget_remaining,
        sharpe_for_budget_calc=sharpe_for_budget,
        sources_present=sources_present,
        sources_missing=sources_missing,
        trial_budget_alert=budget_alert,
        gate_report_age_hours=age_h,
        gate_report_stale=stale,
        gate_report_stale_threshold_hours=stale_threshold,
        applicable_lesson_ids=lessons,
    )


def render_summary(snap: SessionStateSnapshot) -> dict[str, Any]:
    return {
        "phase": snap.iteration_phase.value,
        "cumulative_trials": snap.cumulative_trials,
        "freeze": snap.freeze_label or "(none)",
        "slow_bleed": snap.slow_bleed_level.value,
        "rolling_exp_R": f"{snap.rolling_expectancy_r:+.4f}" if snap.rolling_expectancy_r is not None else "n/a",
        "regime": snap.regime_label or "n/a",
        "regime_composite": f"{snap.regime_composite:+.3f}" if snap.regime_composite is not None else "n/a",
        "gate_report": snap.gate_report_label or "n/a",
        "auto_gates": f"{snap.gate_auto_pass}P / {snap.gate_auto_fail}F / {snap.gate_auto_insufficient}I",
        "trial_budget_remaining": snap.trial_budget_remaining if snap.trial_budget_remaining is not None else "n/a",
        "trial_budget_alert": snap.trial_budget_alert,
        "gate_report_stale": snap.gate_report_stale,
        "applicable_lessons": snap.applicable_lesson_ids,
    }

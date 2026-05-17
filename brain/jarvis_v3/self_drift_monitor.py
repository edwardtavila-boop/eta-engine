"""Self-drift monitor (Wave-13, 2026-04-27).

JARVIS watches the market for drift. The self-drift monitor watches
JARVIS HIMSELF for drift.

What can go wrong without this layer:
  * Calibrator silently aged out -> high-confidence calls start
    losing more than they should
  * Bandit converged on an arm that was right LAST month, wrong
    this month
  * A code change to a policy module subtly shifted verdict
    distribution, but no one noticed until P&L tanked

The monitor compares JARVIS's recent verdict distribution to a
ROLLING BASELINE and flags significant shifts. Three primary
metrics:

  1. APPROVED-rate: fraction of verdicts that were APPROVED in the
     last N hours vs. the prior 7 days. >2σ shift = flag.
  2. Mean confidence: weekly avg confidence vs. baseline. Big
     drops mean the firm-board has lost consensus; big rises mean
     the calibrator might be overconfident.
  3. Per-subsystem verdict mix: if MNQ_BOT used to get APPROVED
     60% and now 90%, something changed for MNQ specifically.

When drift is flagged, the monitor surfaces it through the operator
channel (alert_dispatcher) so the operator can investigate before
P&L damage compounds.

Pure stdlib + math.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import workspace_roots

logger = logging.getLogger(__name__)

DEFAULT_VERDICT_LOG = workspace_roots.ETA_JARVIS_VERDICTS_PATH


# ─── Output schema ───────────────────────────────────────────────


@dataclass
class DriftSignal:
    """One detected drift event."""

    metric: str  # e.g. "approved_rate"
    subsystem: str | None  # None = fleet-wide
    recent_value: float
    baseline_value: float
    z_score: float
    severity: str  # "info" / "warning" / "critical"
    note: str = ""


@dataclass
class SelfDriftReport:
    """Aggregated self-drift summary."""

    ts: str
    n_recent_consultations: int
    n_baseline_consultations: int
    signals: list[DriftSignal] = field(default_factory=list)
    overall_status: str = "OK"
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "n_recent_consultations": self.n_recent_consultations,
            "n_baseline_consultations": self.n_baseline_consultations,
            "overall_status": self.overall_status,
            "summary": self.summary,
            "signals": [
                {
                    "metric": s.metric,
                    "subsystem": s.subsystem,
                    "recent_value": s.recent_value,
                    "baseline_value": s.baseline_value,
                    "z_score": s.z_score,
                    "severity": s.severity,
                    "note": s.note,
                }
                for s in self.signals
            ],
        }


# ─── Helpers ─────────────────────────────────────────────────────


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
        logger.warning("self_drift: %s read failed (%s)", p, exc)
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


def _proportion_z(p_recent: float, p_baseline: float, n_recent: int) -> float:
    """One-proportion z-test under H0: p_recent == p_baseline."""
    if n_recent < 5 or p_baseline <= 0 or p_baseline >= 1:
        return 0.0
    se = math.sqrt(p_baseline * (1.0 - p_baseline) / n_recent)
    if se == 0:
        return 0.0
    return (p_recent - p_baseline) / se


def _severity_from_z(z: float) -> str:
    abs_z = abs(z)
    if abs_z >= 3.0:
        return "critical"
    if abs_z >= 2.0:
        return "warning"
    return "info"


# ─── Detection ───────────────────────────────────────────────────


def detect_self_drift(
    *,
    recent_window_hours: float = 24,
    baseline_window_hours: float = 168,  # 7 days
    z_threshold: float = 2.0,
    log_path: Path = DEFAULT_VERDICT_LOG,
) -> SelfDriftReport:
    """Compare JARVIS's recent decision distribution to baseline.

    Returns a report listing every metric whose |z-score| exceeds
    ``z_threshold``. Empty signals list = JARVIS in steady state.
    """
    now = datetime.now(UTC)
    recent_cutoff = now - timedelta(hours=recent_window_hours)
    baseline_cutoff = now - timedelta(hours=baseline_window_hours)

    all_records = _read_jsonl(log_path)
    recent: list[dict] = []
    baseline: list[dict] = []
    for r in all_records:
        dt = _parse_ts(r.get("ts"))
        if dt is None:
            continue
        if dt >= recent_cutoff:
            recent.append(r)
        elif dt >= baseline_cutoff:
            baseline.append(r)

    n_recent = len(recent)
    n_baseline = len(baseline)
    if n_recent < 5 or n_baseline < 30:
        return SelfDriftReport(
            ts=now.isoformat(),
            n_recent_consultations=n_recent,
            n_baseline_consultations=n_baseline,
            overall_status="OK",
            summary=(f"insufficient data for drift detection (recent={n_recent}, baseline={n_baseline})"),
        )

    signals: list[DriftSignal] = []

    # 1. APPROVED-rate
    recent_approved = sum(1 for r in recent if str(r.get("final_verdict", "")).upper() == "APPROVED") / n_recent
    baseline_approved = sum(1 for r in baseline if str(r.get("final_verdict", "")).upper() == "APPROVED") / n_baseline
    z_app = _proportion_z(recent_approved, baseline_approved, n_recent)
    if abs(z_app) >= z_threshold:
        signals.append(
            DriftSignal(
                metric="approved_rate",
                subsystem=None,
                recent_value=round(recent_approved, 3),
                baseline_value=round(baseline_approved, 3),
                z_score=round(z_app, 2),
                severity=_severity_from_z(z_app),
                note=(f"approved-rate shifted {recent_approved:.0%} -> {baseline_approved:.0%}"),
            )
        )

    # 2. Mean confidence
    confs_recent = [float(r.get("confidence", 0.0)) for r in recent if r.get("confidence") is not None]
    confs_baseline = [float(r.get("confidence", 0.0)) for r in baseline if r.get("confidence") is not None]
    if confs_recent and confs_baseline:
        m_r = sum(confs_recent) / len(confs_recent)
        m_b = sum(confs_baseline) / len(confs_baseline)
        # Approximate z via baseline std
        if len(confs_baseline) > 1:
            var_b = sum((c - m_b) ** 2 for c in confs_baseline) / (len(confs_baseline) - 1)
            sd_b = math.sqrt(var_b)
            se = sd_b / math.sqrt(len(confs_recent))
            z_conf = (m_r - m_b) / se if se > 0 else 0.0
            if abs(z_conf) >= z_threshold:
                signals.append(
                    DriftSignal(
                        metric="mean_confidence",
                        subsystem=None,
                        recent_value=round(m_r, 3),
                        baseline_value=round(m_b, 3),
                        z_score=round(z_conf, 2),
                        severity=_severity_from_z(z_conf),
                        note=(f"avg confidence shifted {m_r:.2f} <- {m_b:.2f}"),
                    )
                )

    # 3. Per-subsystem APPROVED-rate
    subsystems: set[str] = set()
    for r in recent + baseline:
        sub = str(r.get("subsystem", ""))
        if sub:
            subsystems.add(sub)
    for sub in subsystems:
        rec_for_sub = [r for r in recent if str(r.get("subsystem", "")) == sub]
        base_for_sub = [r for r in baseline if str(r.get("subsystem", "")) == sub]
        if len(rec_for_sub) < 3 or len(base_for_sub) < 10:
            continue
        rec_approved = sum(1 for r in rec_for_sub if str(r.get("final_verdict", "")).upper() == "APPROVED") / len(
            rec_for_sub
        )
        base_approved = sum(1 for r in base_for_sub if str(r.get("final_verdict", "")).upper() == "APPROVED") / len(
            base_for_sub
        )
        z = _proportion_z(rec_approved, base_approved, len(rec_for_sub))
        if abs(z) >= z_threshold:
            signals.append(
                DriftSignal(
                    metric="approved_rate_per_subsystem",
                    subsystem=sub,
                    recent_value=round(rec_approved, 3),
                    baseline_value=round(base_approved, 3),
                    z_score=round(z, 2),
                    severity=_severity_from_z(z),
                    note=(f"{sub} approved-rate shifted {base_approved:.0%} -> {rec_approved:.0%}"),
                )
            )

    # Overall status = worst severity in signals
    if any(s.severity == "critical" for s in signals):
        overall = "CRITICAL"
    elif any(s.severity == "warning" for s in signals):
        overall = "WARNING"
    else:
        overall = "OK"

    summary = (
        f"self-drift: {overall} ({len(signals)} signals over {n_recent} recent / {n_baseline} baseline consultations)"
    )

    return SelfDriftReport(
        ts=now.isoformat(),
        n_recent_consultations=n_recent,
        n_baseline_consultations=n_baseline,
        signals=signals,
        overall_status=overall,
        summary=summary,
    )

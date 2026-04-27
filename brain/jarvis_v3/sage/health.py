"""Sage health watchdog (Wave-5 #26, 2026-04-27).

Detects silently-broken schools: a school that returns NEUTRAL on
>95% of consultations for >24h is probably broken (missing dep, raised
exception caught by consult_sage's safety net, etc.). This module:

  * keeps a rolling counter of (school, NEUTRAL/non-NEUTRAL) per consultation
  * persists to ``state/sage/health.json``
  * provides ``check_health() -> list[Issue]`` for the watchdog task

Run via ``Eta-Sage-Health-Daily`` scheduled task; alerts via Resend
when any school's neutral_rate exceeds the threshold.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = (
    Path(__file__).resolve().parents[3] / "state" / "sage" / "health.json"
)


@dataclass
class _SchoolHealth:
    school: str
    n_consultations: int = 0
    n_neutral: int = 0
    last_observed: str = ""

    @property
    def neutral_rate(self) -> float:
        return self.n_neutral / self.n_consultations if self.n_consultations else 0.0


@dataclass(frozen=True)
class HealthIssue:
    school: str
    neutral_rate: float
    n_consultations: int
    severity: str  # "warn" | "critical"
    detail: str


class SageHealthMonitor:
    NEUTRAL_RATE_WARN: float = 0.85
    NEUTRAL_RATE_CRITICAL: float = 0.95
    MIN_OBSERVATIONS: int = 30

    def __init__(self, state_path: Path = DEFAULT_STATE_PATH) -> None:
        self.state_path = state_path
        self._lock = threading.Lock()
        self._health: dict[str, _SchoolHealth] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            for name, snap in data.get("schools", {}).items():
                self._health[name] = _SchoolHealth(
                    school=name,
                    n_consultations=int(snap.get("n_consultations", 0)),
                    n_neutral=int(snap.get("n_neutral", 0)),
                    last_observed=snap.get("last_observed", ""),
                )
        except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
            logger.warning("sage health load failed: %s", exc)

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.state_path.write_text(json.dumps({
                "saved_at": datetime.now(UTC).isoformat(),
                "schools": {
                    name: {
                        "n_consultations": h.n_consultations,
                        "n_neutral": h.n_neutral,
                        "neutral_rate": round(h.neutral_rate, 4),
                        "last_observed": h.last_observed,
                    }
                    for name, h in self._health.items()
                },
            }, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("sage health save failed: %s", exc)

    def observe_consultation(self, *, school: str, was_neutral: bool) -> None:
        with self._lock:
            h = self._health.setdefault(school, _SchoolHealth(school=school))
            h.n_consultations += 1
            if was_neutral:
                h.n_neutral += 1
            h.last_observed = datetime.now(UTC).isoformat()
            # Save every 10 observations so we don't thrash the disk
            if h.n_consultations % 10 == 0:
                self._save()

    def observe(self, report: Any) -> None:  # noqa: ANN401 -- duck-typed SageReport
        """Convenience: feed a full SageReport. Iterates per_school
        verdicts and calls ``observe_consultation`` for each.

        Used by ``consult_sage`` so every live consultation auto-feeds
        the health monitor without the consultation layer needing to
        understand per-school iteration.
        """
        per_school = getattr(report, "per_school", None) or {}
        for name, verdict in per_school.items():
            try:
                bias_value = verdict.bias.value
            except AttributeError:
                # Duck-type: if bias is already a string-ish, compare directly
                bias_value = str(getattr(verdict, "bias", ""))
            self.observe_consultation(
                school=name,
                was_neutral=(bias_value == "neutral"),
            )

    def check_health(self) -> list[HealthIssue]:
        """Surface every school whose neutral_rate breaches threshold."""
        issues: list[HealthIssue] = []
        with self._lock:
            for name, h in self._health.items():
                if h.n_consultations < self.MIN_OBSERVATIONS:
                    continue
                if h.neutral_rate >= self.NEUTRAL_RATE_CRITICAL:
                    severity = "critical"
                elif h.neutral_rate >= self.NEUTRAL_RATE_WARN:
                    severity = "warn"
                else:
                    continue
                issues.append(HealthIssue(
                    school=name,
                    neutral_rate=h.neutral_rate,
                    n_consultations=h.n_consultations,
                    severity=severity,
                    detail=(
                        f"school '{name}' returned NEUTRAL on "
                        f"{h.n_neutral}/{h.n_consultations} consults "
                        f"({h.neutral_rate*100:.1f}%); likely broken "
                        f"(missing dep, exception, stale data)."
                    ),
                ))
        return issues

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {
                name: {
                    "n_consultations": h.n_consultations,
                    "n_neutral": h.n_neutral,
                    "neutral_rate": round(h.neutral_rate, 4),
                    "last_observed": h.last_observed,
                }
                for name, h in self._health.items()
            }


_default: SageHealthMonitor | None = None


def default_monitor() -> SageHealthMonitor:
    global _default
    if _default is None:
        _default = SageHealthMonitor()
    return _default

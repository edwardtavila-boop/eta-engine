"""Sage health watchdog (Wave-5 #26, 2026-04-27).

The original monitor treated "NEUTRAL most of the time" as synonymous
with "broken". That proved too blunt once Sage added regime-oriented
schools and telemetry-gated schools:

* ``volatility_regime`` is directionally neutral by design.
* ``risk_management`` and ``cross_asset_correlation`` can return healthy
  neutral regime reads with real conviction/signals.
* ``order_flow`` / ``funding_basis`` / ``options_greeks`` often return
  neutral only because the runtime did not supply the needed telemetry.

This module now distinguishes three cases:

* ``silent_neutral``      -> suspicious, likely a truly broken/no-op school
* ``missing_telemetry``   -> wiring gap; surface it honestly but don't
                             confuse it with a broken implementation
* ``informative_neutral`` -> healthy regime/risk/read-only output

Run via ``Eta-Sage-Health-Daily`` scheduled task; alerts fire when a
school becomes truly silent or when a wiring gap persists long enough to
matter operationally.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path(__file__).resolve().parents[3] / "state" / "sage" / "health.json"


@dataclass
class _SchoolHealth:
    school: str
    n_consultations: int = 0
    n_neutral: int = 0
    n_directional: int = 0
    n_silent_neutral: int = 0
    n_informative_neutral: int = 0
    n_structural_neutral: int = 0
    n_missing_telemetry: int = 0
    n_warmup: int = 0
    last_observed: str = ""

    @property
    def neutral_rate(self) -> float:
        return self.n_neutral / self.n_consultations if self.n_consultations else 0.0

    @property
    def silent_neutral_rate(self) -> float:
        return self.n_silent_neutral / self.n_consultations if self.n_consultations else 0.0

    @property
    def missing_telemetry_rate(self) -> float:
        return self.n_missing_telemetry / self.n_consultations if self.n_consultations else 0.0


@dataclass(frozen=True)
class HealthIssue:
    school: str
    neutral_rate: float
    n_consultations: int
    severity: str  # "warn" | "critical"
    detail: str
    issue_type: str = "silent_neutral"
    observed_rate: float = 0.0


_STRUCTURAL_NEUTRAL_SCHOOLS = frozenset(
    {
        "cross_asset_correlation",
        "risk_management",
        "volatility_regime",
    }
)
_WARMUP_RATIONALE_FRAGMENTS = (
    "insufficient bars",
    "zero baseline vol",
    "zero path",
)
_MISSING_TELEMETRY_RATIONALE_FRAGMENTS = (
    "no order-flow telemetry",
    "no funding/basis telemetry",
    "no options telemetry",
    "no peer_returns",
    "school skipped",
)


class SageHealthMonitor:
    NEUTRAL_RATE_WARN: float = 0.85
    NEUTRAL_RATE_CRITICAL: float = 0.95
    MIN_OBSERVATIONS: int = 30
    SAVE_INTERVAL_S: float = 5.0
    SAVE_OBSERVATION_BATCH: int = 50

    def __init__(self, state_path: Path = DEFAULT_STATE_PATH) -> None:
        self.state_path = state_path
        self._lock = threading.Lock()
        self._health: dict[str, _SchoolHealth] = {}
        self._dirty = False
        self._dirty_observations = 0
        self._last_save_monotonic = 0.0
        self._loaded_mtime_ns: int | None = None
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            self._loaded_mtime_ns = None
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            loaded: dict[str, _SchoolHealth] = {}
            for name, snap in data.get("schools", {}).items():
                loaded[name] = _SchoolHealth(
                    school=name,
                    n_consultations=int(snap.get("n_consultations", 0)),
                    n_neutral=int(snap.get("n_neutral", 0)),
                    n_directional=int(snap.get("n_directional", 0)),
                    n_silent_neutral=int(snap.get("n_silent_neutral", 0)),
                    n_informative_neutral=int(snap.get("n_informative_neutral", 0)),
                    n_structural_neutral=int(snap.get("n_structural_neutral", 0)),
                    n_missing_telemetry=int(snap.get("n_missing_telemetry", 0)),
                    n_warmup=int(snap.get("n_warmup", 0)),
                    last_observed=snap.get("last_observed", ""),
                )
            self._health = loaded
            self._dirty = False
            self._dirty_observations = 0
            self._loaded_mtime_ns = self.state_path.stat().st_mtime_ns
        except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
            logger.warning("sage health load failed: %s", exc)

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.state_path.write_text(
                json.dumps(
                    {
                        "saved_at": datetime.now(UTC).isoformat(),
                        "schools": {
                            name: {
                                "n_consultations": h.n_consultations,
                                "n_neutral": h.n_neutral,
                                "n_directional": h.n_directional,
                                "n_silent_neutral": h.n_silent_neutral,
                                "n_informative_neutral": h.n_informative_neutral,
                                "n_structural_neutral": h.n_structural_neutral,
                                "n_missing_telemetry": h.n_missing_telemetry,
                                "n_warmup": h.n_warmup,
                                "neutral_rate": round(h.neutral_rate, 4),
                                "silent_neutral_rate": round(h.silent_neutral_rate, 4),
                                "missing_telemetry_rate": round(h.missing_telemetry_rate, 4),
                                "last_observed": h.last_observed,
                            }
                            for name, h in self._health.items()
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            self._dirty = False
            self._dirty_observations = 0
            self._last_save_monotonic = time.monotonic()
            self._loaded_mtime_ns = self.state_path.stat().st_mtime_ns
        except OSError as exc:
            logger.warning("sage health save failed: %s", exc)

    def _save_if_due(self, *, force: bool = False) -> None:
        if not self._dirty:
            return
        if force:
            self._save()
            return
        now = time.monotonic()
        if self._dirty_observations >= self.SAVE_OBSERVATION_BATCH:
            self._save()
            return
        if (now - self._last_save_monotonic) >= self.SAVE_INTERVAL_S:
            self._save()

    def _reload_if_changed(self) -> None:
        if self._dirty:
            return
        try:
            mtime_ns = self.state_path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime_ns = None
        except OSError as exc:
            logger.debug("sage health stat failed: %s", exc)
            return
        if mtime_ns == self._loaded_mtime_ns:
            return
        self._load()

    def observe_consultation(
        self,
        *,
        school: str,
        was_neutral: bool,
        observation_kind: str | None = None,
    ) -> None:
        with self._lock:
            h = self._health.setdefault(school, _SchoolHealth(school=school))
            h.n_consultations += 1
            if was_neutral:
                h.n_neutral += 1
                kind = observation_kind or "silent_neutral"
                if kind == "silent_neutral":
                    h.n_silent_neutral += 1
                elif kind == "informative_neutral":
                    h.n_informative_neutral += 1
                elif kind == "structural_neutral":
                    h.n_structural_neutral += 1
                elif kind == "missing_telemetry":
                    h.n_missing_telemetry += 1
                elif kind == "warmup":
                    h.n_warmup += 1
                else:
                    h.n_silent_neutral += 1
            else:
                h.n_directional += 1
            h.last_observed = datetime.now(UTC).isoformat()
            self._dirty = True
            self._dirty_observations += 1

    def _classify_verdict(self, school: str, verdict: object) -> tuple[bool, str]:
        try:
            bias_value = verdict.bias.value
        except AttributeError:
            bias_value = str(getattr(verdict, "bias", ""))
        bias_value = str(bias_value).lower()
        if bias_value != "neutral":
            return False, "directional"

        signals = getattr(verdict, "signals", None)
        if not isinstance(signals, dict):
            signals = {}
        rationale = str(getattr(verdict, "rationale", "") or "").lower()
        conviction = float(getattr(verdict, "conviction", 0.0) or 0.0)
        aligned = bool(getattr(verdict, "aligned_with_entry", False))

        missing = signals.get("missing")
        if isinstance(missing, list) and missing:
            return True, "missing_telemetry"
        if any(fragment in rationale for fragment in _MISSING_TELEMETRY_RATIONALE_FRAGMENTS):
            return True, "missing_telemetry"
        if any(fragment in rationale for fragment in _WARMUP_RATIONALE_FRAGMENTS):
            return True, "warmup"
        if school in _STRUCTURAL_NEUTRAL_SCHOOLS:
            return True, "structural_neutral"
        if conviction > 0.0 or aligned or signals:
            return True, "informative_neutral"
        return True, "silent_neutral"

    def observe(self, report: Any) -> None:  # noqa: ANN401 -- duck-typed SageReport
        """Convenience: feed a full SageReport. Iterates per_school
        verdicts and calls ``observe_consultation`` for each.

        Used by ``consult_sage`` so every live consultation auto-feeds
        the health monitor without the consultation layer needing to
        understand per-school iteration.
        """
        per_school = getattr(report, "per_school", None) or {}
        observed_any = False
        for name, verdict in per_school.items():
            was_neutral, observation_kind = self._classify_verdict(name, verdict)
            self.observe_consultation(
                school=name,
                was_neutral=was_neutral,
                observation_kind=observation_kind,
            )
            observed_any = True
        if observed_any:
            with self._lock:
                self._save_if_due(force=not self.state_path.exists())

    def check_health(self) -> list[HealthIssue]:
        """Surface schools that are silently failing or permanently unwired."""
        issues: list[HealthIssue] = []
        with self._lock:
            self._reload_if_changed()
            for name, h in self._health.items():
                if h.n_consultations < self.MIN_OBSERVATIONS:
                    continue
                if h.silent_neutral_rate >= self.NEUTRAL_RATE_CRITICAL:
                    issues.append(
                        HealthIssue(
                            school=name,
                            neutral_rate=h.neutral_rate,
                            n_consultations=h.n_consultations,
                            severity="critical",
                            issue_type="silent_neutral",
                            observed_rate=h.silent_neutral_rate,
                            detail=(
                                f"school '{name}' produced silent neutral verdicts on "
                                f"{h.n_silent_neutral}/{h.n_consultations} consults "
                                f"({h.silent_neutral_rate * 100:.1f}%) with no conviction/signals; "
                                "likely a broken or inert implementation."
                            ),
                        )
                    )
                    continue
                if h.silent_neutral_rate >= self.NEUTRAL_RATE_WARN:
                    issues.append(
                        HealthIssue(
                            school=name,
                            neutral_rate=h.neutral_rate,
                            n_consultations=h.n_consultations,
                            severity="warn",
                            issue_type="silent_neutral",
                            observed_rate=h.silent_neutral_rate,
                            detail=(
                                f"school '{name}' is drifting toward silence: "
                                f"{h.n_silent_neutral}/{h.n_consultations} consults "
                                f"({h.silent_neutral_rate * 100:.1f}%) returned neutral "
                                "without usable conviction or signals."
                            ),
                        )
                    )
                    continue
                if h.missing_telemetry_rate >= self.NEUTRAL_RATE_WARN:
                    issues.append(
                        HealthIssue(
                            school=name,
                            neutral_rate=h.neutral_rate,
                            n_consultations=h.n_consultations,
                            severity="warn",
                            issue_type="missing_telemetry",
                            observed_rate=h.missing_telemetry_rate,
                            detail=(
                                f"school '{name}' is mostly skipped because runtime telemetry is missing on "
                                f"{h.n_missing_telemetry}/{h.n_consultations} consults "
                                f"({h.missing_telemetry_rate * 100:.1f}%); wire the required feed before "
                                "treating this school as live."
                            ),
                        )
                    )
        return issues

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            self._reload_if_changed()
            return {
                name: {
                    "n_consultations": h.n_consultations,
                    "n_neutral": h.n_neutral,
                    "n_directional": h.n_directional,
                    "n_silent_neutral": h.n_silent_neutral,
                    "n_informative_neutral": h.n_informative_neutral,
                    "n_structural_neutral": h.n_structural_neutral,
                    "n_missing_telemetry": h.n_missing_telemetry,
                    "n_warmup": h.n_warmup,
                    "neutral_rate": round(h.neutral_rate, 4),
                    "silent_neutral_rate": round(h.silent_neutral_rate, 4),
                    "missing_telemetry_rate": round(h.missing_telemetry_rate, 4),
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

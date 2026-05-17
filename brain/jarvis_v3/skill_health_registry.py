"""Skill health registry (Wave-14, 2026-04-27).

Every external dependency JARVIS uses (TradingView, IBKR, Bybit,
OKX, Coinbase, lunarcrush, blockscout, sentiment feed, etc.) gets
a live health score so JARVIS can route around degraded sources.

Tracked per skill:
  * latency p50 / p95 (rolling 100 calls)
  * error rate (rolling)
  * consecutive failures (kill-switch trigger)
  * last_success_ts / last_failure_ts

Status thresholds:
  * HEALTHY    -- error_rate < 5%, p95 latency within budget
  * DEGRADED   -- error_rate 5-25% OR p95 latency > 2x budget
  * UNAVAILABLE -- error_rate > 25% OR 5+ consecutive failures

Persisted to ``state/jarvis_intel/skill_health.json``.

Integration pattern (every external call wraps in record_call):

    from eta_engine.brain.jarvis_v3.skill_health_registry import (
        SkillRegistry,
    )

    reg = SkillRegistry.default()
    reg.register_skill("ibkr_data", kind="market_data",
                        target_latency_ms=200)

    t0 = time.perf_counter()
    try:
        bars = ibkr.get_bars(...)
        reg.record_call("ibkr_data", success=True,
                        latency_ms=(time.perf_counter() - t0) * 1000)
    except Exception as exc:
        reg.record_call("ibkr_data", success=False,
                        error_msg=str(exc))
        # caller decides what to do
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from eta_engine.scripts import workspace_roots

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = workspace_roots.ETA_JARVIS_INTEL_STATE_DIR / "skill_health.json"

ROLLING_WINDOW = 100


class SkillStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class SkillHealth:
    """One skill's current health snapshot."""

    name: str
    kind: str
    status: SkillStatus
    target_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    error_rate: float
    consecutive_failures: int
    n_calls: int
    last_success_ts: str = ""
    last_failure_ts: str = ""
    last_error_msg: str = ""


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = max(0, min(len(s) - 1, int(p * len(s))))
    return s[idx]


# ─── Internal record ──────────────────────────────────────────────


@dataclass
class _SkillRecord:
    name: str
    kind: str
    target_latency_ms: float
    latencies: deque = field(default_factory=lambda: deque(maxlen=ROLLING_WINDOW))
    successes: deque = field(default_factory=lambda: deque(maxlen=ROLLING_WINDOW))
    consecutive_failures: int = 0
    n_calls: int = 0
    last_success_ts: str = ""
    last_failure_ts: str = ""
    last_error_msg: str = ""


# ─── Registry ────────────────────────────────────────────────────


class SkillRegistry:
    """Persistent health-tracking registry for every external dep."""

    def __init__(self, *, state_path: Path = DEFAULT_STATE_PATH) -> None:
        self.state_path = state_path
        self._skills: dict[str, _SkillRecord] = {}
        self._load()

    @classmethod
    def default(cls) -> SkillRegistry:
        return cls()

    def register_skill(
        self,
        name: str,
        *,
        kind: str = "external",
        target_latency_ms: float = 1000.0,
    ) -> None:
        if name in self._skills:
            return
        self._skills[name] = _SkillRecord(
            name=name,
            kind=kind,
            target_latency_ms=target_latency_ms,
        )

    def record_call(
        self,
        name: str,
        *,
        success: bool,
        latency_ms: float = 0.0,
        error_msg: str = "",
    ) -> None:
        rec = self._skills.get(name)
        if rec is None:
            self.register_skill(name)
            rec = self._skills[name]
        rec.n_calls += 1
        rec.successes.append(1 if success else 0)
        if latency_ms > 0:
            rec.latencies.append(float(latency_ms))
        ts = datetime.now(UTC).isoformat()
        if success:
            rec.consecutive_failures = 0
            rec.last_success_ts = ts
        else:
            rec.consecutive_failures += 1
            rec.last_failure_ts = ts
            if error_msg:
                rec.last_error_msg = error_msg[:200]
        # Periodic save (every 10 calls) so we don't thrash disk
        if rec.n_calls % 10 == 0:
            self._save()

    def health(self, name: str) -> SkillHealth | None:
        rec = self._skills.get(name)
        if rec is None:
            return None
        return self._compute_health(rec)

    def health_report(self) -> list[SkillHealth]:
        return [self._compute_health(r) for r in self._skills.values()]

    def degraded_or_unavailable(self) -> list[SkillHealth]:
        return [h for h in self.health_report() if h.status != SkillStatus.HEALTHY]

    def is_available(self, name: str) -> bool:
        h = self.health(name)
        if h is None:
            return False  # unknown skill -> unavailable
        return h.status != SkillStatus.UNAVAILABLE

    def force_save(self) -> None:
        self._save()

    # ── Internals ──────────────────────────────────────────

    def _compute_health(self, rec: _SkillRecord) -> SkillHealth:
        latencies = list(rec.latencies)
        successes = list(rec.successes)
        n = len(successes)
        error_rate = 1.0 - sum(successes) / n if n > 0 else 0.0
        p50 = _percentile(latencies, 0.50) if latencies else 0.0
        p95 = _percentile(latencies, 0.95) if latencies else 0.0

        status: SkillStatus
        if rec.consecutive_failures >= 5 or error_rate > 0.25:
            status = SkillStatus.UNAVAILABLE
        elif error_rate > 0.05 or (rec.target_latency_ms > 0 and p95 > 2 * rec.target_latency_ms):
            status = SkillStatus.DEGRADED
        else:
            status = SkillStatus.HEALTHY

        return SkillHealth(
            name=rec.name,
            kind=rec.kind,
            status=status,
            target_latency_ms=rec.target_latency_ms,
            p50_latency_ms=round(p50, 1),
            p95_latency_ms=round(p95, 1),
            error_rate=round(error_rate, 3),
            consecutive_failures=rec.consecutive_failures,
            n_calls=rec.n_calls,
            last_success_ts=rec.last_success_ts,
            last_failure_ts=rec.last_failure_ts,
            last_error_msg=rec.last_error_msg,
        )

    # ── Persistence ────────────────────────────────────────

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            for name, raw in data.items():
                rec = _SkillRecord(
                    name=name,
                    kind=raw.get("kind", "external"),
                    target_latency_ms=float(raw.get("target_latency_ms", 1000.0)),
                    consecutive_failures=int(raw.get("consecutive_failures", 0)),
                    n_calls=int(raw.get("n_calls", 0)),
                    last_success_ts=raw.get("last_success_ts", ""),
                    last_failure_ts=raw.get("last_failure_ts", ""),
                    last_error_msg=raw.get("last_error_msg", ""),
                )
                rec.latencies.extend(raw.get("latencies", []))
                rec.successes.extend(raw.get("successes", []))
                self._skills[name] = rec
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("skill_health: load failed (%s)", exc)

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                name: {
                    "kind": rec.kind,
                    "target_latency_ms": rec.target_latency_ms,
                    "consecutive_failures": rec.consecutive_failures,
                    "n_calls": rec.n_calls,
                    "last_success_ts": rec.last_success_ts,
                    "last_failure_ts": rec.last_failure_ts,
                    "last_error_msg": rec.last_error_msg,
                    "latencies": list(rec.latencies),
                    "successes": list(rec.successes),
                }
                for name, rec in self._skills.items()
            }
            self.state_path.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("skill_health: save failed (%s)", exc)

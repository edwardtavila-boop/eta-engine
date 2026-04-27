"""
JARVIS v3 // claude_layer.usage_tracker
=======================================
Per-call cost tracker + hourly/daily quota enforcement for the
claude_layer. Separate from the higher-level ``brain.jarvis_v3.budget``
module because THIS tracker sees actual dollar cost + cache statistics,
not just cost-units.

Exposes:
  * ``record_call()``       -- log one ClaudeCallResult
  * ``hourly_spend_usd()`` / ``daily_spend_usd()``
  * ``cache_hit_rate()``    -- for observability
  * ``quota_state()``       -- OK / DOWNSHIFT / FREEZE
  * persistence to JSON
"""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.jarvis_v3.claude_layer.prompt_cache import (
    ClaudeCallResult,
)

HOURLY_WINDOW = timedelta(hours=1)
DAILY_WINDOW = timedelta(days=1)


class QuotaState(StrEnum):
    OK = "OK"
    WARN = "WARN"  # approaching threshold
    DOWNSHIFT = "DOWNSHIFT"  # demote all tiers by one step
    FREEZE = "FREEZE"  # skip Claude entirely; JARVIS-only mode


class QuotaStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: datetime
    hourly_spend: float = Field(ge=0.0)
    daily_spend: float = Field(ge=0.0)
    hourly_budget: float = Field(ge=0.0)
    daily_budget: float = Field(ge=0.0)
    hourly_pct: float = Field(ge=0.0)
    daily_pct: float = Field(ge=0.0)
    cache_hit_rate: float = Field(ge=0.0, le=1.0)
    calls_1h: int = Field(ge=0)
    state: QuotaState
    note: str


class UsageTracker:
    """Rolling USD tracker with quota logic keyed to real Anthropic pricing."""

    def __init__(
        self,
        *,
        hourly_usd_budget: float = 1.00,
        daily_usd_budget: float = 10.00,
        warn_pct: float = 0.60,
        downshift_pct: float = 0.80,
        freeze_pct: float = 0.95,
    ) -> None:
        self.hourly_usd_budget = hourly_usd_budget
        self.daily_usd_budget = daily_usd_budget
        self.warn_pct = warn_pct
        self.downshift_pct = downshift_pct
        self.freeze_pct = freeze_pct
        self._calls: deque[ClaudeCallResult] = deque()

    def record_call(self, result: ClaudeCallResult) -> None:
        self._calls.append(result)
        self._prune()

    def hourly_spend_usd(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        cutoff = now - HOURLY_WINDOW
        return sum(r.cost_usd for r in self._calls if r.ts >= cutoff)

    def daily_spend_usd(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        cutoff = now - DAILY_WINDOW
        return sum(r.cost_usd for r in self._calls if r.ts >= cutoff)

    def cache_hit_rate(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        cutoff = now - HOURLY_WINDOW
        recent = [r for r in self._calls if r.ts >= cutoff]
        if not recent:
            return 0.0
        hits = sum(1 for r in recent if r.cache_hit)
        return round(hits / len(recent), 4)

    def calls_last_hour(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        cutoff = now - HOURLY_WINDOW
        return sum(1 for r in self._calls if r.ts >= cutoff)

    def quota_state(self, now: datetime | None = None) -> QuotaStatus:
        now = now or datetime.now(UTC)
        self._prune(now)
        hs = self.hourly_spend_usd(now)
        ds = self.daily_spend_usd(now)
        hpct = hs / self.hourly_usd_budget if self.hourly_usd_budget else 0.0
        dpct = ds / self.daily_usd_budget if self.daily_usd_budget else 0.0
        worst = max(hpct, dpct)
        if worst >= self.freeze_pct:
            state = QuotaState.FREEZE
            note = f"burn {worst:.0%} >= {self.freeze_pct:.0%} -- JARVIS-only mode"
        elif worst >= self.downshift_pct:
            state = QuotaState.DOWNSHIFT
            note = f"burn {worst:.0%} >= {self.downshift_pct:.0%} -- downshift all tiers"
        elif worst >= self.warn_pct:
            state = QuotaState.WARN
            note = f"burn {worst:.0%} >= {self.warn_pct:.0%} -- approaching throttle"
        else:
            state = QuotaState.OK
            note = f"burn hourly={hpct:.0%} daily={dpct:.0%} -- nominal"
        return QuotaStatus(
            ts=now,
            hourly_spend=round(hs, 4),
            daily_spend=round(ds, 4),
            hourly_budget=self.hourly_usd_budget,
            daily_budget=self.daily_usd_budget,
            hourly_pct=round(hpct, 4),
            daily_pct=round(dpct, 4),
            cache_hit_rate=self.cache_hit_rate(now),
            calls_1h=self.calls_last_hour(now),
            state=state,
            note=note,
        )

    def _prune(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        cutoff = now - DAILY_WINDOW
        while self._calls and self._calls[0].ts < cutoff:
            self._calls.popleft()

    # Persistence -------------------------------------------------------
    def save(self, path: Path | str) -> None:
        data = {
            "hourly_usd_budget": self.hourly_usd_budget,
            "daily_usd_budget": self.daily_usd_budget,
            "warn_pct": self.warn_pct,
            "downshift_pct": self.downshift_pct,
            "freeze_pct": self.freeze_pct,
            "calls": [r.model_dump(mode="json") for r in self._calls],
        }
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> UsageTracker:
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        inst = cls(
            hourly_usd_budget=float(data.get("hourly_usd_budget", 1.00)),
            daily_usd_budget=float(data.get("daily_usd_budget", 10.00)),
            warn_pct=float(data.get("warn_pct", 0.60)),
            downshift_pct=float(data.get("downshift_pct", 0.80)),
            freeze_pct=float(data.get("freeze_pct", 0.95)),
        )
        for r in data.get("calls", []):
            inst._calls.append(ClaudeCallResult.model_validate(r))
        return inst

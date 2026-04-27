"""
JARVIS v3 // budget
===================
Budget-aware LLM routing.

The Max plan has finite quota. Opus at 5x the cost of Sonnet burns
through that quota fast if used indiscriminately. This module tracks:

  * per-invocation cost (cost_multiplier from model_policy)
  * rolling hourly / daily spend
  * downshift policy: when hourly >= hourly_cap_pct or daily >= daily_cap_pct,
    downgrade OPUS -> SONNET for non-pinned categories

The tracker is a ring buffer keyed by (ts, tier, cost). Every invocation
appends and the tracker prunes expired entries. State persists to JSON
so it survives daemon restarts.

Pure stdlib + pydantic.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.jarvis_v3.bandit import PINNED_CATEGORIES
from eta_engine.brain.model_policy import COST_RATIO, ModelTier, TaskCategory

HOURLY_WINDOW = timedelta(hours=1)
DAILY_WINDOW = timedelta(days=1)

# Default: budget is expressed as "cost units" where 1 unit = 1 Sonnet call.
# Operator can override at construction time.
DEFAULT_HOURLY_BUDGET = 150.0  # 150 Sonnet-equivalents/hour
DEFAULT_DAILY_BUDGET = 2000.0


class InvocationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: datetime
    tier: ModelTier
    cost: float = Field(ge=0.0)
    category: TaskCategory | None = None


class BudgetStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    hourly_spend: float = Field(ge=0.0)
    daily_spend: float = Field(ge=0.0)
    hourly_budget: float = Field(ge=0.0)
    daily_budget: float = Field(ge=0.0)
    hourly_burn_pct: float = Field(ge=0.0)
    daily_burn_pct: float = Field(ge=0.0)
    tier_state: str = Field(min_length=1)
    downgrade_active: bool = False
    note: str


class BudgetTracker:
    """Rolling-window spend tracker with downshift logic."""

    def __init__(
        self,
        *,
        hourly_budget: float = DEFAULT_HOURLY_BUDGET,
        daily_budget: float = DEFAULT_DAILY_BUDGET,
        hourly_downshift_pct: float = 0.80,
        daily_downshift_pct: float = 0.80,
        hourly_critical_pct: float = 0.95,
    ) -> None:
        self.hourly_budget = hourly_budget
        self.daily_budget = daily_budget
        self.hourly_downshift_pct = hourly_downshift_pct
        self.daily_downshift_pct = daily_downshift_pct
        self.hourly_critical_pct = hourly_critical_pct
        self._records: deque[InvocationRecord] = deque()

    def record(
        self,
        tier: ModelTier,
        category: TaskCategory | None = None,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        self._records.append(
            InvocationRecord(
                ts=now,
                tier=tier,
                cost=COST_RATIO[tier],
                category=category,
            )
        )
        self._prune(now)

    def spend(
        self,
        window: timedelta,
        now: datetime | None = None,
    ) -> float:
        now = now or datetime.now(UTC)
        cutoff = now - window
        return sum(r.cost for r in self._records if r.ts >= cutoff)

    def status(self, now: datetime | None = None) -> BudgetStatus:
        now = now or datetime.now(UTC)
        self._prune(now)
        hourly = self.spend(HOURLY_WINDOW, now=now)
        daily = self.spend(DAILY_WINDOW, now=now)
        hpct = hourly / self.hourly_budget if self.hourly_budget else 0.0
        dpct = daily / self.daily_budget if self.daily_budget else 0.0
        if hpct >= self.hourly_critical_pct:
            state = "CRITICAL"
            note = f"hourly burn {hpct:.0%} >= {self.hourly_critical_pct:.0%} -- emergency"
            downgrade = True
        elif hpct >= self.hourly_downshift_pct or dpct >= self.daily_downshift_pct:
            state = "DOWNSHIFT"
            note = f"hourly {hpct:.0%} / daily {dpct:.0%} >= downshift threshold -- Opus demoted to Sonnet"
            downgrade = True
        else:
            state = "OK"
            note = f"hourly {hpct:.0%} / daily {dpct:.0%} -- normal routing"
            downgrade = False
        return BudgetStatus(
            hourly_spend=round(hourly, 2),
            daily_spend=round(daily, 2),
            hourly_budget=self.hourly_budget,
            daily_budget=self.daily_budget,
            hourly_burn_pct=round(hpct, 4),
            daily_burn_pct=round(dpct, 4),
            tier_state=state,
            downgrade_active=downgrade,
            note=note,
        )

    def routed_tier(
        self,
        proposed_tier: ModelTier,
        category: TaskCategory | None = None,
        now: datetime | None = None,
    ) -> tuple[ModelTier, str]:
        """Apply budget-aware downshift.

        Pinned architectural categories are never downgraded -- the
        operator pinned them for a reason.
        """
        if category is not None and category in PINNED_CATEGORIES:
            return proposed_tier, f"{category.value} pinned -- no downshift"
        st = self.status(now=now)
        if st.downgrade_active and proposed_tier == ModelTier.OPUS:
            return ModelTier.SONNET, f"budget downshift: {st.note}"
        if st.tier_state == "CRITICAL" and proposed_tier == ModelTier.SONNET:
            return ModelTier.HAIKU, f"critical burn: demoting SONNET to HAIKU ({st.note})"
        return proposed_tier, "no downshift"

    # Housekeeping ------------------------------------------------------
    def _prune(self, now: datetime) -> None:
        cutoff = now - DAILY_WINDOW
        while self._records and self._records[0].ts < cutoff:
            self._records.popleft()

    # Persistence -------------------------------------------------------
    def save(self, path: Path | str) -> None:
        data = {
            "hourly_budget": self.hourly_budget,
            "daily_budget": self.daily_budget,
            "hourly_downshift_pct": self.hourly_downshift_pct,
            "daily_downshift_pct": self.daily_downshift_pct,
            "hourly_critical_pct": self.hourly_critical_pct,
            "records": [
                {
                    "ts": r.ts.isoformat(),
                    "tier": r.tier.value,
                    "cost": r.cost,
                    "category": r.category.value if r.category else None,
                }
                for r in self._records
            ],
        }
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> BudgetTracker:
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        inst = cls(
            hourly_budget=float(data.get("hourly_budget", DEFAULT_HOURLY_BUDGET)),
            daily_budget=float(data.get("daily_budget", DEFAULT_DAILY_BUDGET)),
            hourly_downshift_pct=float(data.get("hourly_downshift_pct", 0.80)),
            daily_downshift_pct=float(data.get("daily_downshift_pct", 0.80)),
            hourly_critical_pct=float(data.get("hourly_critical_pct", 0.95)),
        )
        for r in data.get("records", []):
            cat = TaskCategory(r["category"]) if r.get("category") else None
            inst._records.append(
                InvocationRecord(
                    ts=datetime.fromisoformat(r["ts"]),
                    tier=ModelTier(r["tier"]),
                    cost=float(r["cost"]),
                    category=cat,
                )
            )
        return inst

"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.cost_forecast
===============================================
Rolling burn-rate estimator with monthly-cap projection.

Why this exists
---------------
The Max plan has generous limits but not infinite ones, and the operator
cares about dollar-equivalent spend whether or not we're inside those
limits. A misconfigured Opus loop can quietly tally up hundreds of
dispatches before anyone looks. This module reads the Avengers journal,
sums ``cost_multiplier`` over sliding windows, and projects to a monthly
burn rate.

Output
------
A ``BurnReport`` with three windows (1h, 6h, 24h), each projected to a
monthly-equivalent rate plus a traffic-light severity:

  * GREEN   -- projected monthly <= 50 %% of cap.
  * YELLOW  -- 50 %% - 100 %% of cap.
  * RED     -- projected monthly > 100 %% of cap.

Cost convention
---------------
``TaskResult.cost_multiplier`` is the ratio vs Sonnet baseline:
``Sonnet = 1.0, Opus = 5.0, Haiku = 0.2``. We multiply by
``sonnet_usd_per_call`` (rough blended estimate) to reach dollars.
Default is $0.06 -- coarse but directionally correct.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.avengers.base import AVENGERS_JOURNAL

if TYPE_CHECKING:
    from pathlib import Path

Severity = Literal["GREEN", "YELLOW", "RED"]


class BurnWindow(BaseModel):
    """Dispatch + cost totals over one time window."""

    model_config = ConfigDict(frozen=True)

    window_hours: float = Field(gt=0.0)
    dispatches: int = Field(ge=0, default=0)
    total_cost_mult: float = Field(ge=0.0, default=0.0)
    total_usd: float = Field(ge=0.0, default=0.0)
    projected_monthly: float = Field(ge=0.0, default=0.0)
    cost_by_persona: dict[str, float] = Field(default_factory=dict)
    cost_by_category: dict[str, float] = Field(default_factory=dict)


class BurnReport(BaseModel):
    """Full snapshot. Persist / render as-is."""

    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    monthly_cap_usd: float = Field(ge=0.0)
    projected_monthly: float = Field(ge=0.0)
    headroom_usd: float  # may go negative if over cap
    severity: Severity
    last_hour: BurnWindow
    last_six_hours: BurnWindow
    last_day: BurnWindow
    top_callers: list[tuple[str, float]]


class CostForecast:
    """Loads the journal and produces burn snapshots on demand.

    Parameters
    ----------
    journal_path
        JSONL file to tail. Defaults to the Avengers journal.
    monthly_cap_usd
        Operator-set monthly spend cap. Used to classify severity.
    sonnet_usd_per_call
        Blended $/call at Sonnet baseline. Multiplied by
        ``cost_multiplier`` to reach dollars.
    """

    # Hours -> month. 730 = 30.4 days average.
    _MONTH_HOURS = 730.0

    def __init__(
        self,
        *,
        journal_path: Path | None = None,
        monthly_cap_usd: float = 200.0,
        sonnet_usd_per_call: float = 0.06,
        clock: callable | None = None,
    ) -> None:
        self.journal_path = journal_path or AVENGERS_JOURNAL
        self.monthly_cap_usd = monthly_cap_usd
        self.sonnet_usd_per_call = sonnet_usd_per_call
        self._clock = clock or (lambda: datetime.now(UTC))

    def _load(self, *, hours: float) -> list[dict]:
        if not self.journal_path.exists():
            return []
        cutoff = self._clock() - timedelta(hours=hours)
        out: list[dict] = []
        try:
            for raw in self.journal_path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") == "heartbeat":
                    continue
                env = rec.get("envelope") or {}
                res = rec.get("result") or {}
                if not env or not res:
                    continue
                ts_raw = rec.get("ts") or env.get("ts")
                try:
                    ts = datetime.fromisoformat(
                        str(ts_raw).replace("Z", "+00:00"),
                    )
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    continue
                if ts < cutoff:
                    continue
                out.append(
                    {
                        "ts": ts,
                        "persona": env.get("persona", res.get("persona_id", "")),
                        "caller": env.get("caller", ""),
                        "category": env.get("category", res.get("category", "")),
                        "cost_multiplier": float(res.get("cost_multiplier", 0.0) or 0.0),
                    }
                )
        except OSError:
            return []
        return out

    def _window(self, *, hours: float) -> BurnWindow:
        rows = self._load(hours=hours)
        total_mult = sum(r["cost_multiplier"] for r in rows)
        total_usd = total_mult * self.sonnet_usd_per_call
        per_hour = total_usd / hours
        projected = per_hour * self._MONTH_HOURS

        by_persona: Counter[str] = Counter()
        by_category: Counter[str] = Counter()
        for r in rows:
            by_persona[r["persona"] or "unknown"] += r["cost_multiplier"]
            by_category[r["category"] or "unknown"] += r["cost_multiplier"]

        return BurnWindow(
            window_hours=hours,
            dispatches=len(rows),
            total_cost_mult=total_mult,
            total_usd=total_usd,
            projected_monthly=projected,
            cost_by_persona=dict(by_persona),
            cost_by_category=dict(by_category),
        )

    def snapshot(self) -> BurnReport:
        """Produce a full burn report. Main entry point."""
        w1 = self._window(hours=1.0)
        w6 = self._window(hours=6.0)
        w24 = self._window(hours=24.0)

        # Use 24h as the canonical projection; it's smoother than 1h.
        projected_monthly = w24.projected_monthly
        headroom = self.monthly_cap_usd - projected_monthly

        severity: Severity
        if self.monthly_cap_usd <= 0:
            severity = "GREEN"
        elif projected_monthly > self.monthly_cap_usd:
            severity = "RED"
        elif projected_monthly > 0.5 * self.monthly_cap_usd:
            severity = "YELLOW"
        else:
            severity = "GREEN"

        # Top callers over 24h.
        caller_cost: Counter[str] = Counter()
        for r in self._load(hours=24.0):
            caller_cost[r["caller"] or "unknown"] += r["cost_multiplier"]
        top = caller_cost.most_common(5)
        top_dollars: list[tuple[str, float]] = [(caller, mult * self.sonnet_usd_per_call) for caller, mult in top]

        return BurnReport(
            generated_at=self._clock(),
            monthly_cap_usd=self.monthly_cap_usd,
            projected_monthly=projected_monthly,
            headroom_usd=headroom,
            severity=severity,
            last_hour=w1,
            last_six_hours=w6,
            last_day=w24,
            top_callers=top_dollars,
        )

    def render_plaintext(self, report: BurnReport | None = None) -> str:
        """Human-readable one-liner + breakdown. For CLI / Pushover body."""
        r = report or self.snapshot()
        lines: list[str] = []
        lines.append(
            f"[{r.severity}] proj/mo=${r.projected_monthly:.2f} "
            f"cap=${r.monthly_cap_usd:.2f} "
            f"headroom=${r.headroom_usd:.2f}",
        )
        lines.append(
            f"  1h:  {r.last_hour.dispatches}d  "
            f"${r.last_hour.total_usd:.2f}  "
            f"(proj ${r.last_hour.projected_monthly:.2f}/mo)",
        )
        lines.append(
            f"  6h:  {r.last_six_hours.dispatches}d  "
            f"${r.last_six_hours.total_usd:.2f}  "
            f"(proj ${r.last_six_hours.projected_monthly:.2f}/mo)",
        )
        lines.append(
            f"  24h: {r.last_day.dispatches}d  "
            f"${r.last_day.total_usd:.2f}  "
            f"(proj ${r.last_day.projected_monthly:.2f}/mo)",
        )
        if r.top_callers:
            lines.append("  top callers 24h:")
            for caller, usd in r.top_callers:
                lines.append(f"    {caller:<32s} ${usd:.2f}")
        return "\n".join(lines)


__all__ = [
    "BurnReport",
    "BurnWindow",
    "CostForecast",
    "Severity",
]

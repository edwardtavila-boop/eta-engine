"""EVOLUTIONARY TRADING ALGO  //  brain.jarvis_cost_attribution.

JARVIS cost telemetry per task category.

Why this module exists
----------------------
:mod:`brain.model_policy` routes every LLM call to the cheapest adequate
tier (Opus 4.7 / Sonnet 4.6 / Haiku 4.5). The operator directive set a
$50/month budget cap; on the Max plan a stray OPUS-default agent can
burn the month in a single session.

The policy router alone cannot tell the operator *which task category*
is eating the burn. That's what this module is for: every subsystem
that calls ``select_model()`` also records a :class:`CostEvent` against
the shared :class:`CostLedger`. A weekly rollup shows spend by bucket
(ARCHITECTURAL / ROUTINE / GRUNT) and by category -- the operator
sees exactly where the budget went.

Design
------
* **Pure accumulation.** ``CostLedger`` is an in-memory ring of
  :class:`CostEvent` instances. No I/O except when ``write_report``
  is called.
* **Cost ratios inherited from model_policy.** We never re-derive the
  $ price; we store tokens + tier and compute a *SONNET-equivalent
  cost unit* via the :data:`COST_RATIO` table. That way the $ price
  per token can change without invalidating historical events.
* **Bucket-sorted Markdown report.** ARCHITECTURAL first (highest
  per-call cost), ROUTINE second, GRUNT last. Operator scans top-down
  and immediately sees the Opus hotspots.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from eta_engine.brain.model_policy import (
    COST_RATIO,
    ModelTier,
    TaskBucket,
    TaskCategory,
    bucket_for,
    tier_for,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "CostEvent",
    "CategoryTotal",
    "BucketTotal",
    "CostReport",
    "CostLedger",
    "weekly_report",
    "render_markdown",
]


# ---------------------------------------------------------------------------
# Event + rollup types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostEvent:
    """One LLM invocation attributed to a task category."""

    task_category: TaskCategory
    tier: ModelTier
    input_tokens: int
    output_tokens: int
    ts_utc: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def sonnet_equiv_units(self) -> float:
        """Tokens * cost multiplier -- the universal cost unit."""
        return float(self.total_tokens) * COST_RATIO[self.tier]


@dataclass(frozen=True)
class CategoryTotal:
    """Per-category aggregation slice."""

    category: TaskCategory
    tier: ModelTier
    bucket: TaskBucket
    n_events: int
    total_tokens: int
    sonnet_equiv_units: float


@dataclass(frozen=True)
class BucketTotal:
    """Per-bucket aggregation slice."""

    bucket: TaskBucket
    n_events: int
    total_tokens: int
    sonnet_equiv_units: float
    pct_of_grand_total: float


@dataclass(frozen=True)
class CostReport:
    """Full cost report for a window."""

    window_start: datetime
    window_end: datetime
    n_events: int
    total_tokens: int
    total_sonnet_equiv_units: float
    by_category: tuple[CategoryTotal, ...]
    by_bucket: tuple[BucketTotal, ...]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class CostLedger:
    """Accumulates :class:`CostEvent` instances and rolls them up on demand."""

    def __init__(self, events: list[CostEvent] | None = None) -> None:
        self._events: list[CostEvent] = list(events) if events else []

    @property
    def events(self) -> tuple[CostEvent, ...]:
        return tuple(self._events)

    def record(
        self,
        task_category: TaskCategory,
        *,
        input_tokens: int,
        output_tokens: int,
        tier: ModelTier | None = None,
        ts_utc: datetime | None = None,
    ) -> CostEvent:
        """Record a new call. If ``tier`` is omitted, look it up via policy."""
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be non-negative")
        resolved_tier = tier if tier is not None else tier_for(task_category)
        event = CostEvent(
            task_category=task_category,
            tier=resolved_tier,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            ts_utc=ts_utc or datetime.now(UTC),
        )
        self._events.append(event)
        return event

    def events_in_window(self, start: datetime, end: datetime) -> list[CostEvent]:
        return [e for e in self._events if start <= e.ts_utc < end]

    # ---- v3.5 upgrade #16 (2026-04-26) — persistence -------------------

    def save_to_jsonl(self, path: Path) -> None:
        """Persist every CostEvent to a JSONL file (one event per line).

        Atomic via tmp + replace so a crash mid-write can't corrupt
        the audit trail.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for e in self._events:
                fh.write(
                    json.dumps(
                        {
                            "task_category": e.task_category.value,
                            "tier": e.tier.value,
                            "input_tokens": e.input_tokens,
                            "output_tokens": e.output_tokens,
                            "ts_utc": e.ts_utc.isoformat(),
                        }
                    )
                    + "\n"
                )
        tmp.replace(path)

    @classmethod
    def load_from_jsonl(cls, path: Path) -> CostLedger:
        """Reconstruct a CostLedger from a JSONL file.

        Skips lines that fail to parse (best-effort). Returns an empty
        ledger if the file doesn't exist.
        """
        if not path.exists():
            return cls()
        events: list[CostEvent] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    events.append(
                        CostEvent(
                            task_category=TaskCategory(raw["task_category"]),
                            tier=ModelTier(raw["tier"]),
                            input_tokens=int(raw["input_tokens"]),
                            output_tokens=int(raw["output_tokens"]),
                            ts_utc=datetime.fromisoformat(raw["ts_utc"]),
                        )
                    )
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    continue
        return cls(events=events)

    def demotion_savings(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> dict:
        """Sum the cost savings from deployment-phase tier demotions.

        Compares each event's actual tier against the static (search-phase)
        mapping. If actual is CHEAPER, the delta counts as savings.
        """
        scoped = list(self._events)
        if (window_start is not None or window_end is not None) and scoped:
            ws = window_start or min(e.ts_utc for e in scoped)
            we = window_end or (max(e.ts_utc for e in scoped) + timedelta(seconds=1))
            scoped = [e for e in scoped if ws <= e.ts_utc < we]

        n_demoted = 0
        tokens_demoted = 0
        units_saved = 0.0
        by_cat: dict[TaskCategory, dict[str, float]] = {}
        for e in scoped:
            nominal_tier = tier_for(e.task_category)
            if nominal_tier == e.tier:
                continue
            nominal_units = float(e.total_tokens) * COST_RATIO[nominal_tier]
            actual_units = e.sonnet_equiv_units
            if nominal_units <= actual_units:
                continue
            saved = nominal_units - actual_units
            n_demoted += 1
            tokens_demoted += e.total_tokens
            units_saved += saved
            slot = by_cat.setdefault(
                e.task_category,
                {"n": 0.0, "tokens": 0.0, "units_saved": 0.0},
            )
            slot["n"] += 1
            slot["tokens"] += e.total_tokens
            slot["units_saved"] += saved
        return {
            "n_demoted_events": n_demoted,
            "tokens_demoted": tokens_demoted,
            "sonnet_equiv_saved": units_saved,
            "by_category": {cat.value: stats for cat, stats in by_cat.items()},
        }

    def rollup(self, *, window_start: datetime | None = None, window_end: datetime | None = None) -> CostReport:
        """Aggregate events in ``[window_start, window_end)`` into a report."""
        if not self._events:
            ws = window_start or datetime.now(UTC)
            we = window_end or ws
            return CostReport(
                window_start=ws,
                window_end=we,
                n_events=0,
                total_tokens=0,
                total_sonnet_equiv_units=0.0,
                by_category=(),
                by_bucket=(),
            )

        ws = window_start or min(e.ts_utc for e in self._events)
        we = window_end or (max(e.ts_utc for e in self._events) + timedelta(seconds=1))
        scoped = self.events_in_window(ws, we)

        by_cat: dict[TaskCategory, list[CostEvent]] = defaultdict(list)
        for e in scoped:
            by_cat[e.task_category].append(e)

        category_totals: list[CategoryTotal] = []
        for cat, group in by_cat.items():
            total_tok = sum(e.total_tokens for e in group)
            units = sum(e.sonnet_equiv_units for e in group)
            category_totals.append(
                CategoryTotal(
                    category=cat,
                    tier=tier_for(cat),
                    bucket=bucket_for(cat),
                    n_events=len(group),
                    total_tokens=total_tok,
                    sonnet_equiv_units=units,
                )
            )
        # Sort: by bucket (ARCH first), then by units desc within bucket.
        bucket_order = {
            TaskBucket.ARCHITECTURAL: 0,
            TaskBucket.ROUTINE: 1,
            TaskBucket.GRUNT: 2,
        }
        category_totals.sort(key=lambda c: (bucket_order[c.bucket], -c.sonnet_equiv_units))

        grand_units = sum(c.sonnet_equiv_units for c in category_totals) or 1.0
        by_bucket: dict[TaskBucket, list[CategoryTotal]] = defaultdict(list)
        for c in category_totals:
            by_bucket[c.bucket].append(c)

        bucket_totals: list[BucketTotal] = []
        for bucket in (
            TaskBucket.ARCHITECTURAL,
            TaskBucket.ROUTINE,
            TaskBucket.GRUNT,
        ):
            bucket_cats = by_bucket.get(bucket, [])
            n = sum(c.n_events for c in bucket_cats)
            tok = sum(c.total_tokens for c in bucket_cats)
            units = sum(c.sonnet_equiv_units for c in bucket_cats)
            bucket_totals.append(
                BucketTotal(
                    bucket=bucket,
                    n_events=n,
                    total_tokens=tok,
                    sonnet_equiv_units=units,
                    pct_of_grand_total=round(100.0 * units / grand_units, 1),
                )
            )

        return CostReport(
            window_start=ws,
            window_end=we,
            n_events=len(scoped),
            total_tokens=sum(e.total_tokens for e in scoped),
            total_sonnet_equiv_units=sum(e.sonnet_equiv_units for e in scoped),
            by_category=tuple(category_totals),
            by_bucket=tuple(bucket_totals),
        )


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


def weekly_report(ledger: CostLedger, *, week_start: datetime | None = None) -> CostReport:
    """Convenience wrapper: rollup events in the week starting ``week_start``."""
    ws = week_start or (datetime.now(UTC) - timedelta(days=7))
    we = ws + timedelta(days=7)
    return ledger.rollup(window_start=ws, window_end=we)


def render_markdown(report: CostReport) -> str:
    """Render a :class:`CostReport` as a bucket-sorted Markdown table."""
    lines: list[str] = []
    lines.append("# EVOLUTIONARY TRADING ALGO // JARVIS Cost Telemetry")
    lines.append("")
    lines.append(f"**Window:** `{report.window_start.isoformat()}` -> `{report.window_end.isoformat()}`")
    lines.append(
        f"**Events:** {report.n_events}  "
        f"**Tokens:** {report.total_tokens:,}  "
        f"**Units (Sonnet-equiv):** {report.total_sonnet_equiv_units:,.0f}"
    )
    lines.append("")

    # Bucket summary first -- operator scans this for the Opus hotspots.
    lines.append("## Bucket summary")
    lines.append("")
    lines.append("| Bucket | Events | Tokens | Units | % of total |")
    lines.append("|---|---:|---:|---:|---:|")
    for b in report.by_bucket:
        lines.append(
            f"| {b.bucket.value} | {b.n_events} | {b.total_tokens:,} | "
            f"{b.sonnet_equiv_units:,.0f} | {b.pct_of_grand_total:.1f}% |"
        )
    lines.append("")

    lines.append("## Per-category breakdown")
    lines.append("")
    lines.append("| Category | Tier | Bucket | Events | Tokens | Units |")
    lines.append("|---|---|---|---:|---:|---:|")
    for c in report.by_category:
        lines.append(
            f"| {c.category.value} | {c.tier.value} | {c.bucket.value} | "
            f"{c.n_events} | {c.total_tokens:,} | "
            f"{c.sonnet_equiv_units:,.0f} |"
        )
    if not report.by_category:
        lines.append("| _(no events)_ | - | - | 0 | 0 | 0 |")
    return "\n".join(lines)


def write_report(
    report: CostReport,
    output: Path,
    *,
    also_json: bool = False,
) -> Path:
    """Write the report to ``output`` (Markdown). Optionally emit a JSON sidecar."""
    output.parent.mkdir(parents=True, exist_ok=True)
    md = render_markdown(report)
    output.write_text(md, encoding="utf-8")
    if also_json:
        payload: dict[str, Any] = {
            "window_start": report.window_start.isoformat(),
            "window_end": report.window_end.isoformat(),
            "n_events": report.n_events,
            "total_tokens": report.total_tokens,
            "total_sonnet_equiv_units": report.total_sonnet_equiv_units,
            "by_bucket": [
                {
                    "bucket": b.bucket.value,
                    "n_events": b.n_events,
                    "total_tokens": b.total_tokens,
                    "sonnet_equiv_units": b.sonnet_equiv_units,
                    "pct_of_grand_total": b.pct_of_grand_total,
                }
                for b in report.by_bucket
            ],
            "by_category": [
                {
                    "category": c.category.value,
                    "tier": c.tier.value,
                    "bucket": c.bucket.value,
                    "n_events": c.n_events,
                    "total_tokens": c.total_tokens,
                    "sonnet_equiv_units": c.sonnet_equiv_units,
                }
                for c in report.by_category
            ],
        }
        output.with_suffix(".json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output

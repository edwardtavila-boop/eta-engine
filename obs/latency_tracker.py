"""End-to-end latency tracker (Tier-2 #9, 2026-04-27).

Measures the time between Signal emission, JARVIS verdict, order
submit, and venue ACK. Without this, slow degradation goes unnoticed.

Usage at each pipeline stage::

    from eta_engine.obs.latency_tracker import LatencyTimer

    timer = LatencyTimer(signal_id="sig-123")
    timer.mark("signal_emitted")
    ...
    timer.mark("jarvis_verdict")
    ...
    timer.mark("order_submitted")
    ...
    timer.mark("venue_ack")
    timer.finalize()    # writes one row to state/latency/events.jsonl
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
EVENTS_PATH = ROOT / "state" / "latency" / "events.jsonl"


@dataclass
class LatencyEvent:
    signal_id: str
    stages: dict[str, float] = field(default_factory=dict)  # stage -> epoch
    deltas_ms: dict[str, float] = field(default_factory=dict)
    total_ms: float = 0.0
    finalized_at: float = 0.0


class LatencyTimer:
    """Per-signal latency timer. Attach by passing a unique ``signal_id``;
    call ``mark(stage)`` at each pipeline checkpoint; ``finalize()`` to
    persist the event."""

    KNOWN_STAGES = (
        "signal_emitted",
        "jarvis_verdict",
        "preflight_decision",
        "order_submitted",
        "venue_ack",
        "fill_received",
    )

    def __init__(self, signal_id: str) -> None:
        self.event = LatencyEvent(signal_id=signal_id)
        self._first_ts: float | None = None

    def mark(self, stage: str) -> None:
        now = time.time()
        if self._first_ts is None:
            self._first_ts = now
        self.event.stages[stage] = now

    def finalize(self) -> Path:
        """Compute deltas + persist."""
        ordered = list(self.event.stages.items())
        ordered.sort(key=lambda kv: kv[1])
        for i in range(1, len(ordered)):
            prev_stage, prev_ts = ordered[i - 1]
            stage, ts = ordered[i]
            self.event.deltas_ms[f"{prev_stage}_to_{stage}"] = round((ts - prev_ts) * 1000.0, 2)
        if ordered:
            self.event.total_ms = round((ordered[-1][1] - ordered[0][1]) * 1000.0, 2)
        self.event.finalized_at = time.time()

        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(self.event), default=str) + "\n")
        return EVENTS_PATH


def daily_summary(*, since_hours: float = 24.0) -> dict:
    """Aggregate latency events from the last N hours."""
    if not EVENTS_PATH.exists():
        return {"n": 0, "since_hours": since_hours}
    cutoff = (datetime.now(UTC) - timedelta(hours=since_hours)).timestamp()
    totals: list[float] = []
    by_pair: dict[str, list[float]] = {}
    try:
        for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("finalized_at", 0) < cutoff:
                continue
            totals.append(rec.get("total_ms", 0.0))
            for pair, ms in (rec.get("deltas_ms") or {}).items():
                by_pair.setdefault(pair, []).append(float(ms))
    except OSError:
        pass

    if not totals:
        return {"n": 0, "since_hours": since_hours}

    totals_sorted = sorted(totals)
    p95 = totals_sorted[int(0.95 * (len(totals) - 1))]
    return {
        "n": len(totals),
        "since_hours": since_hours,
        "mean_total_ms": round(sum(totals) / len(totals), 1),
        "p95_total_ms": round(p95, 1),
        "max_total_ms": round(max(totals), 1),
        "by_pair": {
            pair: {
                "n": len(samples),
                "mean_ms": round(sum(samples) / len(samples), 1),
                "p95_ms": round(sorted(samples)[int(0.95 * (len(samples) - 1))], 1),
            }
            for pair, samples in by_pair.items()
        },
    }

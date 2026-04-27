"""
EVOLUTIONARY TRADING ALGO  //  obs.metrics
==============================
Minimal, allocation-friendly metrics registry.

Emits Prometheus exposition format for scrape endpoints and a dict snapshot
for in-process dashboards. Histograms keep a bounded ring buffer per series
and compute p50 / p95 / p99 on demand.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Canonical metric names (keep as constants so call sites never typo)
# ---------------------------------------------------------------------------

TRADES_OPENED = "apex_trades_opened_total"
TRADES_CLOSED = "apex_trades_closed_total"
PNL_REALIZED_USD = "apex_pnl_realized_usd"
LATENCY_ORDER_MS = "apex_order_latency_ms"
CONFLUENCE_SCORE = "apex_confluence_score"
EQUITY_USD = "apex_equity_usd"
DRAWDOWN_PCT = "apex_drawdown_pct"
KILL_SWITCH_TRIGGERED = "apex_kill_switch_triggered_total"
FIRM_VERDICT = "apex_firm_verdict"
VENUE_FAILOVER = "apex_venue_failover_total"

# Gate-override telemetry -- "never on autopilot" discipline metric.
# Every time an operator or agent overrides a protective gate, record why.
GATE_OVERRIDES_TOTAL = "apex_gate_overrides_total"
GATE_BLOCKS_TOTAL = "apex_gate_blocks_total"
GATE_OVERRIDE_RATE = "apex_gate_override_rate"  # gauge 0..1

HISTOGRAM_WINDOW = 1024  # ring-buffer size per histogram series


class MetricType(StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY = "summary"


class Metric(BaseModel):
    """Single metric observation."""

    name: str
    value: float
    labels: dict[str, str] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metric_type: MetricType = MetricType.GAUGE


def _label_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted(labels.items()))


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((p / 100.0) * (len(ordered) - 1)))
    return ordered[idx]


class MetricsRegistry:
    """Thread-safe in-memory registry for counters, gauges, and histograms."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], deque[float]] = defaultdict(
            lambda: deque(maxlen=HISTOGRAM_WINDOW)
        )

    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        with self._lock:
            self._counters[(name, _label_key(labels))] += value

    def gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._gauges[(name, _label_key(labels))] = float(value)

    def histogram(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._histograms[(name, _label_key(labels))].append(float(value))

    def get_counter(self, name: str, labels: dict[str, str] | None = None) -> float:
        with self._lock:
            return self._counters.get((name, _label_key(labels)), 0.0)

    def get_gauge(self, name: str, labels: dict[str, str] | None = None) -> float | None:
        with self._lock:
            return self._gauges.get((name, _label_key(labels)))

    def histogram_stats(self, name: str, labels: dict[str, str] | None = None) -> dict[str, float]:
        with self._lock:
            series = list(self._histograms.get((name, _label_key(labels)), deque()))
        if not series:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0}
        return {
            "count": len(series),
            "p50": _percentile(series, 50),
            "p95": _percentile(series, 95),
            "p99": _percentile(series, 99),
            "avg": sum(series) / len(series),
        }

    def snapshot(self) -> dict[str, list[dict]]:
        """Render all metrics as a nested dict."""
        with self._lock:
            counters = [
                {"name": n, "labels": dict(lk), "value": v, "type": "counter"} for (n, lk), v in self._counters.items()
            ]
            gauges = [
                {"name": n, "labels": dict(lk), "value": v, "type": "gauge"} for (n, lk), v in self._gauges.items()
            ]
            histograms = [
                {"name": n, "labels": dict(lk), "type": "histogram", **self.histogram_stats(n, dict(lk))}
                for (n, lk) in list(self._histograms.keys())
            ]
        return {"counters": counters, "gauges": gauges, "histograms": histograms}

    def to_prometheus(self) -> str:
        """Render Prometheus exposition format."""
        lines: list[str] = []
        snap = self.snapshot()
        for row in snap["counters"]:
            lines.append(f"# TYPE {row['name']} counter")
            lines.append(self._render_line(row["name"], row["labels"], row["value"]))
        for row in snap["gauges"]:
            lines.append(f"# TYPE {row['name']} gauge")
            lines.append(self._render_line(row["name"], row["labels"], row["value"]))
        for row in snap["histograms"]:
            base = row["name"]
            labels = row["labels"]
            lines.append(f"# TYPE {base} summary")
            for q in ("p50", "p95", "p99"):
                lines.append(self._render_line(f"{base}_{q}", labels, row[q]))
            lines.append(self._render_line(f"{base}_count", labels, row["count"]))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_line(name: str, labels: dict[str, str], value: float) -> str:
        if not labels:
            return f"{name} {value}"
        rendered = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{rendered}}} {value}"

    def reset(self) -> None:
        """Wipe all metrics. Mainly for tests."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


# Module-level singleton -- import this for 99% of use.
REGISTRY = MetricsRegistry()

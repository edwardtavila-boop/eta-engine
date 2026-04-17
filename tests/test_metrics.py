"""Tests for obs.metrics registry + Prometheus rendering."""

from __future__ import annotations

from eta_engine.obs.metrics import (
    CONFLUENCE_SCORE,
    LATENCY_ORDER_MS,
    TRADES_OPENED,
    MetricsRegistry,
    MetricType,
)


def test_counter_inc_defaults_to_one() -> None:
    reg = MetricsRegistry()
    reg.inc(TRADES_OPENED)
    reg.inc(TRADES_OPENED)
    reg.inc(TRADES_OPENED)
    assert reg.get_counter(TRADES_OPENED) == 3.0


def test_counter_with_labels_tracks_series_separately() -> None:
    reg = MetricsRegistry()
    reg.inc(TRADES_OPENED, labels={"bot": "mnq"})
    reg.inc(TRADES_OPENED, labels={"bot": "mnq"})
    reg.inc(TRADES_OPENED, labels={"bot": "eth_perp"})
    assert reg.get_counter(TRADES_OPENED, labels={"bot": "mnq"}) == 2.0
    assert reg.get_counter(TRADES_OPENED, labels={"bot": "eth_perp"}) == 1.0


def test_gauge_overwrites() -> None:
    reg = MetricsRegistry()
    reg.gauge(CONFLUENCE_SCORE, 7.5)
    reg.gauge(CONFLUENCE_SCORE, 9.1)
    assert reg.get_gauge(CONFLUENCE_SCORE) == 9.1


def test_histogram_percentiles() -> None:
    reg = MetricsRegistry()
    for v in range(1, 101):
        reg.histogram(LATENCY_ORDER_MS, float(v))
    stats = reg.histogram_stats(LATENCY_ORDER_MS)
    assert stats["count"] == 100
    assert 49 <= stats["p50"] <= 51
    assert 94 <= stats["p95"] <= 96
    assert 98 <= stats["p99"] <= 100


def test_snapshot_shape() -> None:
    reg = MetricsRegistry()
    reg.inc(TRADES_OPENED)
    reg.gauge(CONFLUENCE_SCORE, 6.4)
    reg.histogram(LATENCY_ORDER_MS, 42.0)
    snap = reg.snapshot()
    assert {"counters", "gauges", "histograms"} <= snap.keys()
    assert any(r["name"] == TRADES_OPENED for r in snap["counters"])
    assert any(r["name"] == CONFLUENCE_SCORE for r in snap["gauges"])
    assert any(r["name"] == LATENCY_ORDER_MS for r in snap["histograms"])


def test_prometheus_rendering_includes_types() -> None:
    reg = MetricsRegistry()
    reg.inc(TRADES_OPENED, labels={"bot": "mnq"})
    reg.gauge(CONFLUENCE_SCORE, 8.0, labels={"bot": "mnq"})
    reg.histogram(LATENCY_ORDER_MS, 15.0)
    out = reg.to_prometheus()
    assert "# TYPE apex_trades_opened_total counter" in out
    assert "# TYPE apex_confluence_score gauge" in out
    assert "# TYPE apex_order_latency_ms summary" in out
    assert 'apex_trades_opened_total{bot="mnq"} 1.0' in out


def test_metric_type_enum_values() -> None:
    assert MetricType.COUNTER.value == "counter"
    assert MetricType.GAUGE.value == "gauge"
    assert MetricType.HISTOGRAM.value == "histogram"
    assert MetricType.SUMMARY.value == "summary"

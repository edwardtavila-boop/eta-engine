"""Grafana dashboard JSON generator — P7_OPS observability.

Emits a Grafana 10.x compatible dashboard JSON that renders the canonical
Evolutionary Trading Algo metric names defined in :mod:`eta_engine.obs.metrics`:

* ``apex_equity_usd`` — portfolio equity gauge
* ``apex_drawdown_pct`` — drawdown line
* ``apex_trades_opened_total`` / ``apex_trades_closed_total`` — counters
* ``apex_pnl_realized_usd`` — realized PnL bar
* ``apex_order_latency_ms`` — order-latency histogram (p50/p95/p99)
* ``apex_confluence_score`` — live confluence gauge
* ``apex_kill_switch_triggered_total`` — incident counter
* ``apex_firm_verdict`` — latest Firm decision (numeric encoding)
* ``apex_venue_failover_total`` — venue-failover counter

The generated JSON is importable via Grafana's "Import Dashboard" UI.
Datasource is parameterised as a template variable (``$datasource``) so the
user selects which Prometheus instance to query at import time.

No Grafana HTTP client dependency — this just writes a JSON blob.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from eta_engine.obs.metrics import (
    CONFLUENCE_SCORE,
    DRAWDOWN_PCT,
    EQUITY_USD,
    FIRM_VERDICT,
    KILL_SWITCH_TRIGGERED,
    LATENCY_ORDER_MS,
    PNL_REALIZED_USD,
    TRADES_CLOSED,
    TRADES_OPENED,
    VENUE_FAILOVER,
)

logger = logging.getLogger(__name__)

DASHBOARD_TITLE = "EVOLUTIONARY TRADING ALGO — Live Trading"
DASHBOARD_UID = "eta-engine-live"
SCHEMA_VERSION = 38  # Grafana 10.x compatible


def _panel_gauge(panel_id: int, title: str, expr: str, *, unit: str = "currencyUSD") -> dict[str, Any]:
    return {
        "id": panel_id,
        "type": "stat",
        "title": title,
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "targets": [{"expr": expr, "refId": "A"}],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                        {"color": "yellow", "value": 0.9},
                        {"color": "red", "value": 0.95},
                    ],
                },
            },
        },
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value"},
    }


def _panel_timeseries(
    panel_id: int,
    title: str,
    expr: str,
    *,
    unit: str = "short",
) -> dict[str, Any]:
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "targets": [{"expr": expr, "refId": "A"}],
        "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 1}}},
    }


def _panel_histogram(
    panel_id: int,
    title: str,
    base_name: str,
) -> dict[str, Any]:
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "datasource": {"type": "prometheus", "uid": "${datasource}"},
        "targets": [
            {"expr": f"{base_name}_p50", "refId": "A", "legendFormat": "p50"},
            {"expr": f"{base_name}_p95", "refId": "B", "legendFormat": "p95"},
            {"expr": f"{base_name}_p99", "refId": "C", "legendFormat": "p99"},
        ],
        "fieldConfig": {"defaults": {"unit": "ms", "custom": {"lineWidth": 1}}},
    }


def _position(x: int, y: int, w: int = 6, h: int = 6) -> dict[str, int]:
    return {"x": x, "y": y, "w": w, "h": h}


def build_dashboard() -> dict[str, Any]:
    """Compose the full dashboard JSON."""
    panels: list[dict[str, Any]] = []

    # Row 1: core equity / drawdown gauges
    panels.append({**_panel_gauge(1, "Portfolio Equity", EQUITY_USD, unit="currencyUSD"), "gridPos": _position(0, 0)})
    panels.append({**_panel_gauge(2, "Drawdown", DRAWDOWN_PCT, unit="percent"), "gridPos": _position(6, 0)})
    panels.append({**_panel_gauge(3, "Confluence Score", CONFLUENCE_SCORE, unit="none"), "gridPos": _position(12, 0)})
    panels.append({**_panel_gauge(4, "Firm Verdict", FIRM_VERDICT, unit="none"), "gridPos": _position(18, 0)})

    # Row 2: counters (rate over 5m)
    panels.append(
        {**_panel_timeseries(5, "Trades Opened (5m rate)", f"rate({TRADES_OPENED}[5m])"), "gridPos": _position(0, 6)}
    )
    panels.append(
        {**_panel_timeseries(6, "Trades Closed (5m rate)", f"rate({TRADES_CLOSED}[5m])"), "gridPos": _position(6, 6)}
    )
    panels.append(
        {**_panel_timeseries(7, "Realized PnL", PNL_REALIZED_USD, unit="currencyUSD"), "gridPos": _position(12, 6)}
    )
    panels.append(
        {**_panel_timeseries(8, "Kill-Switch Triggers", f"{KILL_SWITCH_TRIGGERED}"), "gridPos": _position(18, 6)}
    )

    # Row 3: latency histogram + failover
    panels.append(
        {**_panel_histogram(9, "Order Latency (ms)", LATENCY_ORDER_MS), "gridPos": _position(0, 12, w=12, h=8)}
    )
    panels.append({**_panel_timeseries(10, "Venue Failovers", VENUE_FAILOVER), "gridPos": _position(12, 12, w=12, h=8)})

    return {
        "title": DASHBOARD_TITLE,
        "uid": DASHBOARD_UID,
        "schemaVersion": SCHEMA_VERSION,
        "version": 1,
        "refresh": "10s",
        "tags": ["eta-engine", "trading"],
        "time": {"from": "now-6h", "to": "now"},
        "timezone": "browser",
        "templating": {
            "list": [
                {
                    "name": "datasource",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {"text": "prometheus", "value": "prometheus"},
                },
            ],
        },
        "panels": panels,
    }


def write_dashboard(path: Path | str) -> Path:
    """Serialize the dashboard to ``path`` as JSON. Creates parent dir."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build_dashboard(), indent=2, sort_keys=False), encoding="utf-8")
    logger.info("grafana dashboard written to %s", out)
    return out

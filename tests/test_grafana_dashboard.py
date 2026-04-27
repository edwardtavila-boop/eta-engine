"""Grafana dashboard JSON generator tests — P7_OPS observability."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from eta_engine.obs.grafana_dashboard import (
    DASHBOARD_TITLE,
    DASHBOARD_UID,
    SCHEMA_VERSION,
    build_dashboard,
    write_dashboard,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_dashboard_has_required_top_level_fields() -> None:
    db = build_dashboard()
    assert db["title"] == DASHBOARD_TITLE
    assert db["uid"] == DASHBOARD_UID
    assert db["schemaVersion"] == SCHEMA_VERSION
    assert "panels" in db
    assert "templating" in db


def test_all_core_metric_names_referenced() -> None:
    db = build_dashboard()
    serialized = json.dumps(db)
    for canonical in (
        "apex_equity_usd",
        "apex_drawdown_pct",
        "apex_confluence_score",
        "apex_trades_opened_total",
        "apex_trades_closed_total",
        "apex_pnl_realized_usd",
        "apex_order_latency_ms",
        "apex_kill_switch_triggered_total",
        "apex_firm_verdict",
        "apex_venue_failover_total",
    ):
        assert canonical in serialized, f"{canonical} missing from dashboard JSON"


def test_every_panel_has_unique_id_and_gridpos() -> None:
    db = build_dashboard()
    ids = [p["id"] for p in db["panels"]]
    assert len(set(ids)) == len(ids)
    for panel in db["panels"]:
        assert "gridPos" in panel
        for k in ("x", "y", "w", "h"):
            assert k in panel["gridPos"]


def test_latency_histogram_panel_has_three_targets() -> None:
    db = build_dashboard()
    latency_panel = next(p for p in db["panels"] if "Latency" in p.get("title", ""))
    assert len(latency_panel["targets"]) == 3
    legends = {t["legendFormat"] for t in latency_panel["targets"]}
    assert legends == {"p50", "p95", "p99"}


def test_datasource_is_parameterized_via_template_var() -> None:
    db = build_dashboard()
    # Every panel's datasource UID should reference the template variable
    for panel in db["panels"]:
        assert panel["datasource"]["uid"] == "${datasource}"
    # And the template var itself should be present
    template_names = [v["name"] for v in db["templating"]["list"]]
    assert "datasource" in template_names


def test_write_dashboard_round_trip(tmp_path: Path) -> None:
    path = write_dashboard(tmp_path / "eta_dashboard.json")
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["title"] == DASHBOARD_TITLE
    assert len(payload["panels"]) >= 10

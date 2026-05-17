"""
Tests for deploy.scripts.dashboard_api -- FastAPI backend for the Apex
Predator dashboard.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Point dashboard_api at a temp state dir + return a TestClient."""
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ETA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ETA_DASHBOARD_DISABLE_BROKER_PROBES", "1")
    monkeypatch.setenv("ETA_IBKR_GATEWAY_AUTHORITY", "1")
    monkeypatch.setenv(
        "ETA_COMMAND_CENTER_DOCTOR_RECEIPT_PATH",
        str(tmp_path / "state" / "command_center_doctor_latest.json"),
    )
    monkeypatch.setenv(
        "ETA_COMMAND_CENTER_WATCHDOG_STATUS_PATH",
        str(tmp_path / "state" / "command_center_watchdog_status_latest.json"),
    )
    monkeypatch.setenv(
        "ETA_READINESS_SNAPSHOT_STATUS_PATH",
        str(tmp_path / "state" / "eta_readiness_snapshot_latest.json"),
    )
    # Pin the BTC fleet dir so the dashboard doesn't accidentally see a
    # real fleet directory sitting in the dev package tree.
    monkeypatch.setenv(
        "ETA_BTC_FLEET_DIR",
        str(tmp_path / "state" / "broker_fleet"),
    )
    (tmp_path / "state").mkdir()
    (tmp_path / "logs").mkdir()
    # Seed a couple of state files
    (tmp_path / "state" / "avengers_heartbeat.json").write_text(
        json.dumps(
            {
                "ts": "2026-04-24T00:00:00+00:00",
                "quota_state": "OK",
                "hourly_pct": 0.0,
                "daily_pct": 0.0,
                "cache_hit_rate": 0.0,
                "distiller_version": 0,
                "distiller_trained": False,
            }
        )
    )
    (tmp_path / "state" / "dashboard_payload.json").write_text(
        json.dumps(
            {
                "ts": "2026-04-24T00:00:00+00:00",
                "health": "GREEN",
                "regime": "NEUTRAL",
                "session_phase": "MORNING",
                "suggestion": "TRADE",
                "stress": {"composite": 0.2, "binding": "equity_dd", "components": []},
                "horizons": {"now": 0.2, "next_15m": 0.2, "next_1h": 0.2, "overnight": 0.2},
                "projection": {"level": 0.2, "trend": 0.0, "forecast_5": 0.2},
            }
        )
    )
    (tmp_path / "state" / "kaizen_ledger.json").write_text(
        json.dumps(
            {
                "retrospectives": [{"ts": "2026-04-24T00:00:00+00:00"}],
                "tickets": [
                    {
                        "id": "KZN-1",
                        "title": "Fix x",
                        "status": "OPEN",
                        "rationale": "r",
                        "parent_retrospective_ts": "2026-04-24T00:00:00+00:00",
                        "opened_at": "2026-04-24T00:00:00+00:00",
                        "impact": "small",
                        "owner": "op",
                        "shipped_at": None,
                        "drop_reason": "",
                    },
                ],
            }
        )
    )
    # Force reimport so env vars take effect
    import importlib

    import eta_engine.deploy.scripts.dashboard_api as mod

    importlib.reload(mod)
    for cache_name in (
        "_ALPACA_PER_BOT_CACHE",
        "_DASHBOARD_DIAGNOSTICS_CACHE",
        "_IBKR_PROBE_CACHE",
        "_PUBLIC_OPERATOR_BROKER_STATE_CACHE",
        "_PUBLIC_OPERATOR_RETUNE_STATUS_CACHE",
        "_WORKSPACE_CHECKOUT_CACHE",
    ):
        cache = getattr(mod, cache_name, None)
        if isinstance(cache, dict):
            cache.clear()
    from eta_engine.scripts import jarvis_status

    monkeypatch.setattr(
        jarvis_status,
        "build_second_brain_summary",
        lambda **_kwargs: {
            "source": "jarvis_status.second_brain",
            "status": "cold_start",
            "n_episodes": 0,
            "win_rate": 0.0,
            "avg_r": 0.0,
            "semantic_patterns": 0,
            "procedural_versions": 0,
            "top_patterns": [],
            "playbook": {"eligible_patterns": 0, "favor_patterns": [], "avoid_patterns": []},
            "paths": {},
            "sources": {},
            "legacy_sources_active": False,
        },
    )
    return TestClient(mod.app)


class TestDashboardAPI:
    def test_kaizen_latest_prefers_active_loop_json(self, app_client, tmp_path):
        state = tmp_path / "state"
        (state / "kaizen_latest.json").write_text(
            json.dumps(
                {
                    "started_at": "2026-05-08T15:01:44+00:00",
                    "applied": False,
                    "n_bots": 3,
                    "applied_count": 0,
                    "held_count": 1,
                    "action_counts": {"RETIRE": 1},
                },
            ),
            encoding="utf-8",
        )
        legacy = state / "kaizen" / "tickets"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "old.md").write_text("# stale ticket", encoding="utf-8")

        r = app_client.get("/api/jarvis/kaizen_latest")

        assert r.status_code == 200
        payload = r.json()
        assert payload["source"] == "kaizen_latest_json"

    def test_api_data_status_prefers_canonical_inventory_snapshot(self, app_client, tmp_path):
        state = tmp_path / "state"
        (state / "data_inventory_latest.json").write_text(
            json.dumps(
                {
                    "ts": "2026-05-15T12:00:00+00:00",
                    "schema_version": 3,
                    "summary": {"datasets": 1, "ok": 1},
                    "by_dataset": {
                        "bars:MNQ1/5m": {
                            "status": "OK",
                            "end": "2026-05-15T11:55:00+00:00",
                            "rows": 42,
                            "path": "var/eta_engine/state/data/bars/MNQ1_5m.parquet",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/data/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["catalog"]["ts"] == "2026-05-15T12:00:00+00:00"
        assert payload["catalog"]["summary"]["datasets"] == 1
        assert payload["catalog"]["active_symbol_rows"][0]["symbol"] == "MNQ1"

    def test_public_dashboard_uses_local_bot_fleet_truth(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_live_broker_state_payload",
            lambda: {
                "ready": True,
                "today_actual_fills": 0,
                "today_realized_pnl": 0.0,
                "total_unrealized_pnl": 0.0,
                "open_position_count": 0,
                "win_rate_30d": None,
                "alpaca": {"ready": True, "open_positions": [], "open_position_count": 0},
                "ibkr": {"ready": True, "open_positions": [], "open_position_count": 0},
            },
        )

        state = tmp_path / "state"
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": "2026-04-28T12:00:00+00:00",
                    "mode": "paper_live",
                    "bots": [
                        {
                            "bot_id": "mnq_futures_sage",
                            "symbol": "MNQ1",
                            "strategy_kind": "orb",
                            "n_entries": 1,
                            "n_exits": 1,
                            "realized_pnl": 0.0,
                            "last_signal_at": "2026-04-28T11:59:00+00:00",
                        },
                    ],
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/public/dashboard")

        assert r.status_code == 200
        payload = r.json()
        assert payload["truth_status"] == "stale"
        assert payload["confirmed_bots"] == 1
        assert payload["bots"][0]["name"] == "mnq_futures_sage"

    def test_target_exit_summary_splits_broker_flat_from_paper_watch(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        summary = mod._target_exit_summary(
            [
                {
                    "name": "mnq_futures_sage",
                    "symbol": "MNQ1",
                    "open_positions": 1,
                    "position_state": {
                        "state": "open",
                        "bracket_stop": 28297.25,
                        "bracket_target": 29302.75,
                        "target_exit_visibility": {
                            "status": "watching",
                            "owner": "supervisor",
                            "target_distance_points": 540.25,
                            "target_distance_pct": 1.8783,
                        },
                    },
                },
            ],
            broker_open_position_count=0,
        )

        assert summary["status"] == "paper_watching"
        assert summary["open_position_count"] == 1
        assert summary["broker_open_position_count"] == 0
        assert summary["supervisor_local_position_count"] == 1
        assert "0 broker open" in summary["summary_line"]
        assert "1 supervisor paper-local open" in summary["summary_line"]

    def test_target_exit_summary_counts_unbracketed_broker_exposure(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        summary = mod._target_exit_summary(
            [
                {
                    "name": "mnq_futures_sage",
                    "symbol": "MNQ1",
                    "open_positions": 1,
                    "position_state": {
                        "state": "open",
                        "bracket_stop": 29323.75,
                        "bracket_target": 29362.75,
                        "target_exit_visibility": {
                            "status": "watching",
                            "owner": "supervisor",
                            "target_distance_points": 24.0,
                            "stop_distance_points": 8.5,
                        },
                    },
                },
            ],
            broker_open_position_count=2,
        )

        assert summary["status"] == "missing_brackets"
        assert summary["broker_open_position_count"] == 2
        assert summary["broker_bracket_count"] == 0
        assert summary["broker_unbracketed_count"] == 2
        assert summary["missing_bracket_count"] == 2
        assert "2 broker open" in summary["summary_line"]
        assert "2 missing bracket(s)" in summary["summary_line"]

    def test_broker_bracket_audit_endpoint_reports_missing_oco(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.scripts import broker_bracket_audit

        monkeypatch.setattr(
            broker_bracket_audit,
            "_adapter_support",
            lambda: {
                "ibkr_futures_server_oco": True,
                "tradovate_order_payload_brackets": True,
            },
        )
        monkeypatch.setattr(mod, "_supervisor_roster_rows", lambda _now_ts: [])
        monkeypatch.setattr(
            mod,
            "_live_broker_state_payload",
            lambda: {
                "ready": True,
                "open_position_count": 1,
                "ibkr": {
                    "ready": True,
                    "open_position_count": 1,
                    "open_positions": [
                        {
                            "symbol": "MNQM6",
                            "secType": "FUT",
                            "position": 3,
                            "avg_cost": 58662.59,
                            "market_price": 29399.0,
                            "market_value": 176394.0,
                            "unrealized_pnl": -250.0,
                        },
                    ],
                    "open_orders": [],
                },
            },
        )

        r = app_client.get("/api/jarvis/broker_bracket_audit")

        assert r.status_code == 200
        payload = r.json()
        assert payload["source"] == "dashboard_api_direct_broker_bracket_audit"
        assert payload["summary"] == "BLOCKED_UNBRACKETED_EXPOSURE"
        assert payload["ready_for_prop_dry_run"] is False
        assert payload["operator_action_required"] is True
        assert payload["position_summary"]["missing_bracket_count"] == 1
        assert payload["primary_unprotected_position"]["symbol"] == "MNQM6"
        assert payload["operator_actions"][0]["label"] == "Verify broker OCO coverage"
        assert payload["target_exit_summary"]["missing_bracket_count"] == 1

    def test_broker_bracket_audit_endpoint_enables_live_stale_order_validation(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.scripts import broker_bracket_audit

        calls: dict[str, object] = {}

        monkeypatch.setattr(mod, "_supervisor_roster_rows", lambda _now_ts: [])
        monkeypatch.setattr(
            mod,
            "_live_broker_state_payload",
            lambda: {
                "ready": True,
                "open_position_count": 0,
                "ibkr": {
                    "ready": True,
                    "open_position_count": 0,
                    "open_positions": [],
                    "open_orders": [],
                },
            },
        )

        def _fake_build_bracket_audit(*, fleet, validate_live_stale_orders=False):
            calls["fleet"] = fleet
            calls["validate_live_stale_orders"] = validate_live_stale_orders
            return {
                "summary": "READY_NO_OPEN_EXPOSURE",
                "ready_for_prop_dry_run": True,
                "operator_action_required": False,
                "operator_actions": [],
                "position_summary": {"broker_open_position_count": 0},
                "stale_flat_open_orders": [],
            }

        monkeypatch.setattr(broker_bracket_audit, "build_bracket_audit", _fake_build_bracket_audit)

        r = app_client.get("/api/jarvis/broker_bracket_audit")

        assert r.status_code == 200
        assert calls["validate_live_stale_orders"] is True

    def test_broker_bracket_audit_payload_skips_live_stale_validation_by_default(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.scripts import broker_bracket_audit

        calls: dict[str, object] = {}

        def _fake_build_bracket_audit(*, fleet, validate_live_stale_orders=False):
            calls["fleet"] = fleet
            calls["validate_live_stale_orders"] = validate_live_stale_orders
            return {
                "summary": "READY_NO_OPEN_EXPOSURE",
                "ready_for_prop_dry_run": True,
                "operator_action_required": False,
                "operator_actions": [],
                "position_summary": {"broker_open_position_count": 0},
                "stale_flat_open_orders": [],
            }

        monkeypatch.setattr(broker_bracket_audit, "build_bracket_audit", _fake_build_bracket_audit)

        payload = mod._broker_bracket_audit_payload(target_exit_summary={}, live_broker_state={})

        assert payload["summary"] == "READY_NO_OPEN_EXPOSURE"
        assert calls["validate_live_stale_orders"] is False

    def test_live_broker_summary_is_compact_and_data_only(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "source": "cached_live_broker_state_for_diagnostics",
                "reporting_timezone": "America/New_York",
                "today_start_utc": "2026-05-15T04:00:00Z",
                "today_actual_fills": 4,
                "today_realized_pnl": -123.45,
                "total_unrealized_pnl": 12.0,
                "open_position_count": 1,
                "broker_mtd_pnl": 20521.0,
                "broker_snapshot_state": "warm",
                "focus_policy": {
                    "active_venues": ["ibkr"],
                    "standby_venues": ["tastytrade"],
                    "dormant_venues": ["tradovate"],
                    "paused_venues": ["alpaca"],
                },
                "close_history": {
                    "source": "trade_close_ledger",
                    "default_window": "mtd",
                    "timezone": "America/New_York",
                    "day_boundary": "local_midnight",
                    "windows": {
                        "today": {
                            "window": "today",
                            "label": "Today",
                            "closed_outcome_count": 2,
                            "evaluated_outcome_count": 2,
                            "winning_outcomes": 1,
                            "losing_outcomes": 1,
                            "win_rate": 0.5,
                            "realized_pnl": -20.0,
                            "recent_outcomes": [{"bot_id": "too_verbose"}],
                            "pnl_map": {
                                "limit": 5,
                                "top_winners": [{"bot_id": "mbt", "realized_pnl": 10.0}],
                                "top_losers": [{"bot_id": "mnq", "realized_pnl": -30.0}],
                            },
                        },
                        "mtd": {
                            "window": "mtd",
                            "label": "MTD",
                            "closed_outcome_count": 10,
                            "evaluated_outcome_count": 9,
                            "win_rate": 0.4444,
                            "realized_pnl": -100.0,
                        },
                    },
                },
            },
        )
        monkeypatch.setattr(
            mod,
            "_daily_loss_killswitch_snapshot",
            lambda: {
                "source": "daily_loss_killswitch",
                "status": "tripped",
                "tripped": True,
                "disabled": False,
                "today_pnl_usd": -925.50,
                "limit_usd": -900.0,
                "reason": "day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
                "timezone": "America/New_York",
                "reset_at": "2026-05-16T00:00:00-04:00",
                "reset_display": "2026-05-16 00:00 EDT",
            },
        )

        response = app_client.get("/api/live/broker_summary")

        assert response.status_code == 200
        payload = response.json()
        assert payload["ready"] is True
        assert payload["order_action_allowed"] is False
        assert payload["order_action_status"] == "held_by_daily_loss_stop"
        assert "day_pnl=$-925.50" in payload["order_action_reason"]
        assert "resets automatically at 2026-05-16 00:00 EDT" in payload["order_action_reason"]
        assert payload["order_action_reset_display"] == "2026-05-16 00:00 EDT"
        assert payload["daily_loss_killswitch"]["timezone"] == "America/New_York"
        assert payload["broker"]["broker_mtd_pnl"] == 20521.0
        assert payload["broker"]["broker_today_realized_pnl"] == -123.45
        assert payload["pnl_reconciliation"]["status"] == "different_scopes"
        assert payload["pnl_reconciliation"]["broker_mtd_pnl"] == 20521.0
        assert payload["pnl_reconciliation"]["close_history_mtd_realized_pnl"] == -100.0
        assert payload["pnl_reconciliation"]["difference"] == 20621.0
        assert payload["pnl_reconciliation"]["display_rule"].startswith("Broker MTD is account-level")
        assert payload["close_history"]["today"]["closed_outcome_count"] == 2
        assert payload["close_history"]["today"]["pnl_map"]["top_losers"][0]["bot_id"] == "mnq"
        assert "recent_outcomes" not in payload["close_history"]["today"]
        assert payload["focus_policy"]["active_venues"] == ["ibkr"]

    def test_target_exit_summary_counts_only_bracket_required_broker_exposure(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        summary = mod._target_exit_summary(
            [
                {
                    "name": "eth_sage_daily",
                    "symbol": "ETH",
                    "open_positions": 1,
                    "position_state": {
                        "state": "open",
                        "bracket_stop": 2280.0,
                        "bracket_target": 2350.0,
                        "target_exit_visibility": {
                            "status": "watching",
                            "owner": "supervisor",
                            "target_distance_points": 25.0,
                            "stop_distance_points": 10.0,
                        },
                    },
                },
                {
                    "name": "mnq_futures_sage",
                    "symbol": "MNQ1",
                    "open_positions": 1,
                    "position_state": {
                        "state": "open",
                        "bracket_stop": 29323.75,
                        "bracket_target": 29362.75,
                        "target_exit_visibility": {
                            "status": "watching",
                            "owner": "supervisor",
                            "target_distance_points": 24.0,
                            "stop_distance_points": 8.5,
                        },
                    },
                },
            ],
            broker_open_position_count=2,
            broker_bracket_required_position_count=1,
        )

        assert summary["status"] == "missing_brackets"
        assert summary["broker_open_position_count"] == 2
        assert summary["broker_bracket_required_position_count"] == 1
        assert summary["broker_supervisor_managed_position_count"] == 1
        assert summary["broker_unbracketed_count"] == 1
        assert summary["missing_bracket_count"] == 1
        assert "2 broker open" in summary["summary_line"]
        assert "1 broker bracket-required" in summary["summary_line"]
        assert "1 missing bracket(s)" in summary["summary_line"]

    def test_target_exit_summary_accepts_broker_open_order_verified_brackets(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        summary = mod._target_exit_summary(
            [],
            broker_open_position_count=2,
            broker_bracket_required_position_count=2,
            broker_open_order_verified_bracket_count=2,
        )

        assert summary["status"] == "watching"
        assert summary["broker_open_position_count"] == 2
        assert summary["broker_bracket_required_position_count"] == 2
        assert summary["broker_open_order_verified_bracket_count"] == 2
        assert summary["broker_bracket_count"] == 2
        assert summary["broker_unbracketed_count"] == 0
        assert summary["missing_bracket_count"] == 0
        assert "2 broker bracket(s)" in summary["summary_line"]
        assert "0 missing bracket(s)" in summary["summary_line"]

    def test_target_exit_summary_does_not_mark_broker_only_exposure_flat(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        summary = mod._target_exit_summary(
            [],
            broker_open_position_count=2,
            broker_bracket_required_position_count=1,
        )

        assert summary["status"] == "missing_brackets"
        assert summary["open_position_count"] == 0
        assert summary["broker_open_position_count"] == 2
        assert summary["broker_bracket_required_position_count"] == 1
        assert summary["missing_bracket_count"] == 1
        assert "2 broker open" in summary["summary_line"]
        assert "1 missing bracket(s)" in summary["summary_line"]
        assert "flat" not in summary["summary_line"]

    def test_target_exit_summary_surfaces_stale_position_sla(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        server_dt = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
        summary = mod._target_exit_summary(
            [
                {
                    "name": "ng_sweep_reclaim",
                    "symbol": "NG1",
                    "open_positions": 1,
                    "position_state": {
                        "state": "open",
                        "opened_at": "2026-04-28T09:50:00+00:00",
                        "bracket_stop": 2.72,
                        "bracket_target": 2.94,
                        "target_exit_visibility": {
                            "status": "watching",
                            "owner": "supervisor",
                            "target_distance_points": 0.08,
                            "stop_distance_points": 0.015,
                        },
                    },
                },
            ],
            broker_open_position_count=0,
            server_ts=server_dt.timestamp(),
        )

        stale = summary["position_staleness"]
        assert stale["status"] == "force_flatten_due"
        assert stale["force_flatten_due_count"] == 1
        assert stale["oldest_position"]["bot"] == "ng_sweep_reclaim"
        assert stale["oldest_position"]["age_s"] == 7800
        assert stale["oldest_position"]["level"] == "FORCE_FLATTEN"
        assert summary["stale_position_status"] == "force_flatten_due"

    def test_target_exit_summary_skips_force_flatten_for_broker_managed_bracket_watch(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        server_dt = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
        summary = mod._target_exit_summary(
            [
                {
                    "name": "ng_sweep_reclaim",
                    "symbol": "NG1",
                    "open_positions": 1,
                    "position_state": {
                        "state": "open",
                        "opened_at": "2026-04-28T09:50:00+00:00",
                        "bracket_stop": 2.72,
                        "bracket_target": 2.94,
                        "broker_bracket": True,
                        "target_exit_visibility": {
                            "status": "broker_bracket_watch",
                            "owner": "broker",
                            "target_distance_points": 0.08,
                            "stop_distance_points": 0.015,
                        },
                    },
                },
            ],
            broker_open_position_count=1,
            broker_bracket_required_position_count=1,
            server_ts=server_dt.timestamp(),
        )

        stale = summary["position_staleness"]
        assert summary["status"] == "watching"
        assert summary["missing_bracket_count"] == 0
        assert stale["status"] == "broker_managed_watch"
        assert stale["broker_managed_open_count"] == 1
        assert stale["force_flatten_due_count"] == 0

    def test_target_exit_card_status_marks_bracketed_broker_watch_yellow(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        status = mod._target_exit_card_status(
            {
                "status": "watching",
                "missing_bracket_count": 0,
                "broker_open_position_count": 4,
                "broker_bracket_count": 4,
                "supervisor_local_position_count": 0,
                "position_staleness": {
                    "status": "broker_managed_watch",
                    "force_flatten_due_count": 0,
                },
                "stale_position_status": "broker_managed_watch",
            }
        )

        assert status == "YELLOW"

    def test_target_exit_summary_marks_already_tightened_positions(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        server_dt = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
        summary = mod._target_exit_summary(
            [
                {
                    "name": "volume_profile_mnq",
                    "symbol": "MNQ1",
                    "open_positions": 1,
                    "open_position": {
                        "entry_ts": "2026-04-28T10:50:00+00:00",
                        "stale_tighten_applied_at": "2026-04-28T11:50:00+00:00",
                    },
                    "position_state": {
                        "state": "open",
                        "opened_at": "2026-04-28T10:50:00+00:00",
                        "bracket_stop": 29327.0,
                        "bracket_target": 29359.5,
                        "target_exit_visibility": {
                            "status": "watching",
                            "owner": "supervisor",
                            "target_distance_points": 24.0,
                            "stop_distance_points": 8.5,
                        },
                    },
                },
            ],
            broker_open_position_count=0,
            server_ts=server_dt.timestamp(),
        )

        stale = summary["position_staleness"]
        assert stale["status"] == "tightened_watch"
        assert stale["tighten_stop_due_count"] == 0
        assert stale["tightened_watch_count"] == 1
        assert stale["oldest_position"]["level"] == "TIGHTEN_STOP_APPLIED"
        assert stale["oldest_position"]["next_action"] == "continue_watch_until_force_flatten"
        assert summary["stale_position_status"] == "tightened_watch"

    def test_normalize_trade_close_preserves_zero_values_from_extra(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        close = mod._normalize_trade_close(
            {
                "ts": "2026-05-07T21:14:22.210169+00:00",
                "bot_id": "volume_profile_btc",
                "realized_r": 0.0,
                "action_taken": "approve_full",
                "layers_updated": ["memory"],
                "layer_errors": [],
                "extra": {
                    "realized_pnl": 0.0,
                    "fill_price": 79775.0,
                    "qty": 0.0,
                    "symbol": "BTC",
                    "side": "BUY",
                    "close_ts": "2026-05-07T21:14:21.990957+00:00",
                },
            },
        )

        assert close is not None
        assert close["realized_pnl"] == 0.0
        assert close["qty"] == 0.0
        assert close["fill_price"] == 79775.0

    def test_health(self, app_client):
        r = app_client.get("/health")
        assert r.status_code == 200
        j = r.json()
        assert j["status"] == "ok"
        assert j["state_dir_exists"]
        assert j["dashboard_version"] == "v1"
        assert j["release_stage"] == "pre_beta"
        assert j["beta_launched"] is False
        assert set(j["required_data"]) == {
            "bot_fleet",
            "fleet_equity",
            "auth_session",
            "source_freshness",
        }

    def test_public_ops_origin_can_read_fleet_api(self, app_client):
        r = app_client.options(
            "/api/bot-fleet",
            headers={
                "Origin": "https://ops.evolutionarytradingalgo.com",
                "Access-Control-Request-Method": "GET",
            },
        )

        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "https://ops.evolutionarytradingalgo.com"

    def test_force_multiplier_status_is_public_ops_compatible(self, app_client, tmp_path):
        state = tmp_path / "state"
        (state / "fm_health.json").write_text(
            json.dumps(
                {
                    "all_ready": False,
                    "pass_count": 2,
                    "total_count": 3,
                    "providers": [],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/fm/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["mode"] == "force_multiplier"
        assert payload["status"] == "ok"
        assert "providers" in payload
        assert payload["health_snapshot"]["payload"]["pass_count"] == 2
        assert "no-store" in r.headers["Cache-Control"]

    def test_force_multiplier_status_endpoint_uses_path_only_probe(self, app_client, monkeypatch):
        from eta_engine.brain import multi_model

        calls: list[bool] = []

        def fake_status(*, probe: bool = True):
            calls.append(probe)
            return {"mode": "force_multiplier", "providers": {}, "routing_table": {}}

        monkeypatch.setattr(multi_model, "force_multiplier_status", fake_status)

        r = app_client.get("/api/fm/status")

        assert r.status_code == 200
        assert calls == [False]

    def test_heartbeat(self, app_client):
        r = app_client.get("/api/heartbeat")
        assert r.status_code == 200
        assert r.json()["quota_state"] == "OK"

    def test_heartbeat_prefers_canonical_supervisor_when_present(self, app_client):
        state = os.environ["ETA_STATE_DIR"]
        supervisor_dir = os.path.join(state, "jarvis_intel", "supervisor")
        os.makedirs(supervisor_dir, exist_ok=True)
        now = datetime.now(UTC)
        with open(os.path.join(supervisor_dir, "heartbeat.json"), "w", encoding="utf-8") as fh:
            json.dump({"ts": now.isoformat(), "mode": "paper_live", "tick_count": 42}, fh)
        with open(os.path.join(supervisor_dir, "heartbeat_keepalive.json"), "w", encoding="utf-8") as fh:
            json.dump({"keepalive_ts": now.isoformat()}, fh)

        r = app_client.get("/api/heartbeat")

        assert r.status_code == 200
        payload = r.json()
        assert payload["source"] == "jarvis_strategy_supervisor"
        assert payload["canonical"] is True
        assert payload["status"] == "RUNNING"
        assert payload["mode"] == "paper_live"
        assert payload["tick_count"] == 42
        assert payload["quota_state"] == "OK"
        assert payload["supervisor_liveness"]["main_heartbeat_fresh"] is True

    def test_dashboard(self, app_client):
        r = app_client.get("/api/dashboard")
        assert r.status_code == 200
        assert r.json()["regime"] == "NEUTRAL"
        assert "operator_queue" in r.json()
        assert "paper_live_transition" in r.json()
        assert "symbol_intelligence" in r.json()
        assert "strategy_supercharge_manifest" not in r.json()
        assert "strategy_supercharge_results" not in r.json()
        assert "no-store" in r.headers["Cache-Control"]

    def test_dashboard_first_paint_does_not_build_heavy_supercharge_artifacts(
        self,
        app_client,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        def fail_manifest() -> dict:
            raise AssertionError("dashboard bootstrap must not build heavy manifest")

        def fail_results() -> dict:
            raise AssertionError("dashboard bootstrap must not build heavy results")

        monkeypatch.setattr(mod, "_strategy_supercharge_manifest_payload", fail_manifest)
        monkeypatch.setattr(mod, "_strategy_supercharge_results_payload", fail_results)

        r = app_client.get("/api/dashboard")

        assert r.status_code == 200
        assert "strategy_supercharge_manifest" not in r.json()
        assert "strategy_supercharge_results" not in r.json()

    def test_dashboard_includes_symbol_intelligence_snapshot(self, app_client, tmp_path):
        state = tmp_path / "state"
        (state / "symbol_intelligence_latest.json").write_text(
            json.dumps(
                {
                    "schema": "eta.symbol_intelligence.audit.v1",
                    "kind": "eta_symbol_intelligence_audit",
                    "status": "AMBER",
                    "overall_status": "amber",
                    "average_score_pct": 83,
                    "symbols": [
                        {
                            "symbol": "MNQ1",
                            "status": "green",
                            "missing_required": [],
                            "missing_optional": ["news"],
                            "optional_components": {"news": False, "book": True},
                        },
                        {
                            "symbol": "ES1",
                            "status": "amber",
                            "missing_required": ["decisions"],
                            "missing_optional": ["book"],
                            "optional_components": {"news": True, "book": False},
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "symbol_intelligence_collector_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_symbol_intelligence_collector",
                    "status": "ok",
                    "bootstrap_counts": {"news": 7, "book": 2, "sentiment_snapshots": 4},
                    "audit": {"overall_status": "amber"},
                }
            ),
            encoding="utf-8",
        )
        (state / "sentiment_snapshot_collector_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_sentiment_snapshot_collector",
                    "status": "ok",
                    "requested_assets": ["BTC", "ETH", "SOL", "macro"],
                    "ok_count": 4,
                    "results": {
                        "BTC": {
                            "ok": True,
                            "raw_source": "google_news_proxy",
                            "fear_greed": 0.43,
                            "social_volume_z": 0.6,
                            "topic_flags": {"regulation": True},
                            "headline_count": 2,
                            "headlines": [
                                {
                                    "headline": "Bitcoin ETF flows stay positive",
                                    "publisher": "Reuters",
                                    "published_at_utc": "2026-05-14T15:00:00+00:00",
                                    "url": "https://example.com/btc",
                                }
                            ],
                        },
                        "ETH": {
                            "ok": True,
                            "raw_source": "google_news_proxy",
                            "fear_greed": 0.41,
                            "social_volume_z": -0.5,
                            "topic_flags": {"regulation": True},
                            "headline_count": 1,
                            "headlines": [],
                        },
                        "SOL": {
                            "ok": True,
                            "raw_source": "google_news_proxy",
                            "fear_greed": 0.72,
                            "social_volume_z": 2.5,
                            "topic_flags": {"fomo": True},
                            "headline_count": 1,
                            "headlines": [
                                {
                                    "headline": "Solana breakout lifts alt sentiment",
                                    "publisher": "Bloomberg",
                                    "published_at_utc": "2026-05-14T15:30:00+00:00",
                                    "url": "https://example.com/sol",
                                }
                            ],
                        },
                        "macro": {
                            "ok": True,
                            "raw_source": "google_news_proxy",
                            "fear_greed": 0.36,
                            "social_volume_z": 0.8,
                            "topic_flags": {"fomc": True, "inflation": True},
                            "headline_count": 2,
                            "headlines": [
                                {
                                    "headline": "Fed officials keep inflation in focus",
                                    "publisher": "WSJ",
                                    "published_at_utc": "2026-05-14T16:00:00+00:00",
                                    "url": "https://example.com/fed",
                                }
                            ],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        dashboard = app_client.get("/api/dashboard")
        symbol_intelligence = dashboard.json()["symbol_intelligence"]
        assert dashboard.json()["symbol_intelligence_status"] == "AMBER"
        assert dashboard.json()["symbol_intelligence_required_gap_count"] == 1
        assert dashboard.json()["symbol_intelligence_optional_gap_count"] == 2
        assert dashboard.json()["news_ready_symbols"] == 1
        assert dashboard.json()["book_ready_symbols"] == 1
        assert dashboard.json()["symbol_intelligence_news_flowing"] is True
        assert dashboard.json()["symbol_intelligence_book_flowing"] is True
        assert dashboard.json()["sentiment_asset_count"] == 4
        assert dashboard.json()["sentiment_ok_count"] == 4
        assert dashboard.json()["sentiment_lead_asset"] == "SOL"
        assert "fomc" in dashboard.json()["sentiment_active_topics"]
        assert dashboard.json()["sentiment_assets"][0]["asset"] == "BTC"
        assert dashboard.json()["sentiment_assets"][0]["headline_count"] == 2
        assert dashboard.json()["sentiment_macro_headlines"][0]["publisher"] == "WSJ"
        assert dashboard.json()["sentiment_lead_headlines"][0]["headline"] == "Solana breakout lifts alt sentiment"
        assert dashboard.json()["sentiment_pressure_status"] == "risk_on"
        assert dashboard.json()["sentiment_pressure"]["lead_positive_asset"] == "SOL"
        assert dashboard.json()["sentiment_pressure"]["lead_negative_asset"] == "ETH"
        assert "inflation" in dashboard.json()["sentiment_pressure_summary"]
        assert symbol_intelligence["status"] == "AMBER"
        assert symbol_intelligence["average_score_pct"] == 83
        assert symbol_intelligence["symbol_count"] == 2
        assert symbol_intelligence["status_counts"]["green"] == 1
        assert symbol_intelligence["required_gap_count"] == 1
        assert symbol_intelligence["optional_gap_count"] == 2
        assert symbol_intelligence["news_ready_symbols"] == 1
        assert symbol_intelligence["book_ready_symbols"] == 1
        assert symbol_intelligence["collector"]["status"] == "ok"
        assert symbol_intelligence["collector"]["news_records_added_last_run"] == 7
        assert symbol_intelligence["collector"]["book_records_added_last_run"] == 2
        assert symbol_intelligence["collector"]["news_ready_symbols"] == 1
        assert symbol_intelligence["collector"]["book_ready_symbols"] == 1
        assert symbol_intelligence["collector"]["news_flowing"] is True
        assert symbol_intelligence["collector"]["book_flowing"] is True
        assert symbol_intelligence["collector"]["sentiment_snapshot_count"] == 4
        assert symbol_intelligence["sentiment"]["ok_count"] == 4
        assert "fomc" in symbol_intelligence["sentiment"]["active_topics"]
        assert symbol_intelligence["sentiment"]["asset_summaries"][2]["asset"] == "SOL"
        assert symbol_intelligence["sentiment"]["lead_headlines"][0]["publisher"] == "Bloomberg"
        assert symbol_intelligence["sentiment"]["pressure"]["status"] == "risk_on"
        assert symbol_intelligence["sentiment"]["pressure"]["lead_positive_asset"] == "SOL"

        direct = app_client.get("/api/data/symbol-intelligence")
        assert direct.status_code == 200
        assert direct.json()["symbol_count"] == 2
        assert direct.json()["optional_component_symbol_counts"]["news"] == 1
        assert direct.json()["collector"]["news_records"] == 7
        assert direct.json()["sentiment"]["lead_asset"] == "SOL"
        assert direct.json()["sentiment"]["macro_headlines"][0]["headline"] == "Fed officials keep inflation in focus"
        assert direct.json()["sentiment"]["pressure"]["lead_negative_asset"] == "ETH"
        assert "no-store" in direct.headers["Cache-Control"]

    def test_dashboard_includes_diamond_retune_status(self, app_client, tmp_path):
        state = tmp_path / "state"
        (state / "diamond_retune_status_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_diamond_retune_status",
                    "generated_at_utc": "2026-05-14T21:05:43+00:00",
                    "summary": {
                        "n_targets": 2,
                        "n_attempted_bots": 1,
                        "n_unattempted_targets": 1,
                        "n_research_backlog_targets": 1,
                        "n_low_sample_keep_collecting": 1,
                        "n_near_miss_keep_tuning": 1,
                        "n_unstable_positive_keep_tuning": 1,
                        "n_research_passed_broker_proof_required": 1,
                        "n_stuck_research_failing": 0,
                        "n_timeout_retry": 0,
                        "broker_proof_required_closes": 100,
                        "n_broker_sample_ready": 0,
                        "n_broker_edge_ready": 0,
                        "n_broker_proof_ready": 0,
                        "n_broker_sample_ready_negative_edge": 0,
                        "n_broker_proof_shortfall": 1,
                        "largest_broker_proof_gap": 64,
                        "total_broker_proof_gap": 64,
                        "safe_to_mutate_live": False,
                    },
                    "bots": [
                        {
                            "bot_id": "nq_futures_sage",
                            "retune_state": "PASS_AWAITING_BROKER_PROOF",
                            "next_action": "review research artifact, then collect 64 more paper/broker closes",
                            "broker_close_evidence": {
                                "closed_trade_count": 36,
                                "required_closed_trade_count": 100,
                                "remaining_closed_trade_count": 64,
                                "sample_progress_pct": 36.0,
                            },
                        }
                    ],
                    "research_backlog": [
                        {
                            "bot_id": "mes_sweep_reclaim_v2",
                            "retune_state": "RESEARCH_GATE_FAILED",
                            "next_action": "rerun runtime-only research grid, then launch-check; no live changes",
                            "safe_to_mutate_live": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        dashboard = app_client.get("/api/dashboard")
        retune_status = dashboard.json()["diamond_retune_status"]
        assert retune_status["status"] == "ready"
        assert retune_status["contract_ok"] is True
        assert retune_status["summary"]["n_attempted_bots"] == 1
        assert retune_status["summary"]["n_research_backlog_targets"] == 1
        assert retune_status["summary"]["n_low_sample_keep_collecting"] == 1
        assert retune_status["summary"]["n_near_miss_keep_tuning"] == 1
        assert retune_status["summary"]["n_unstable_positive_keep_tuning"] == 1
        assert retune_status["summary"]["n_broker_sample_ready"] == 0
        assert retune_status["summary"]["n_broker_edge_ready"] == 0
        assert retune_status["summary"]["n_broker_proof_shortfall"] == 1
        assert retune_status["summary"]["largest_broker_proof_gap"] == 64
        assert retune_status["summary"]["safe_to_mutate_live"] is False
        assert retune_status["bots"][0]["broker_close_evidence"]["remaining_closed_trade_count"] == 64
        assert retune_status["research_backlog"][0]["bot_id"] == "mes_sweep_reclaim_v2"

        direct = app_client.get("/api/jarvis/diamond_retune_status")
        assert direct.status_code == 200
        assert direct.json()["summary"]["n_research_backlog_targets"] == 1
        assert direct.json()["research_backlog"][0]["retune_state"] == "RESEARCH_GATE_FAILED"
        assert direct.json()["summary"]["n_low_sample_keep_collecting"] == 1
        assert direct.json()["summary"]["n_near_miss_keep_tuning"] == 1
        assert direct.json()["summary"]["n_unstable_positive_keep_tuning"] == 1
        assert direct.json()["summary"]["n_research_passed_broker_proof_required"] == 1
        assert direct.json()["summary"]["total_broker_proof_gap"] == 64
        assert "no-store" in direct.headers["Cache-Control"]

    def test_dashboard_diagnostics_summarizes_symbol_intelligence(self, app_client, tmp_path):
        state = tmp_path / "state"
        (state / "symbol_intelligence_latest.json").write_text(
            json.dumps(
                {
                    "schema": "eta.symbol_intelligence.audit.v1",
                    "status": "GREEN",
                    "average_score_pct": 100,
                    "symbols": [
                        {
                            "symbol": "MNQ1",
                            "status": "green",
                            "optional_components": {"news": True, "book": True},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "symbol_intelligence_collector_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_symbol_intelligence_collector",
                    "status": "ok",
                    "bootstrap_counts": {"news": 4, "book": 1, "sentiment_snapshots": 4},
                    "audit": {"overall_status": "green"},
                }
            ),
            encoding="utf-8",
        )
        (state / "sentiment_snapshot_collector_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_sentiment_snapshot_collector",
                    "status": "ok",
                    "requested_assets": ["BTC", "ETH", "SOL", "macro"],
                    "ok_count": 4,
                    "results": {
                        "macro": {
                            "ok": True,
                            "raw_source": "google_news_proxy",
                            "fear_greed": 0.38,
                            "social_volume_z": 1.0,
                            "topic_flags": {"fomc": True, "inflation": True},
                            "headline_count": 1,
                            "headlines": [
                                {
                                    "headline": "Inflation stays sticky ahead of the Fed",
                                    "publisher": "CNBC",
                                    "published_at_utc": "2026-05-14T14:00:00+00:00",
                                    "url": "https://example.com/macro",
                                }
                            ],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics?refresh=true")

        assert r.status_code == 200
        data = r.json()["symbol_intelligence"]
        assert data["status"] == "GREEN"
        assert data["ready"] is True
        assert data["contract_ok"] is True
        assert data["symbol_count"] == 1
        assert data["news_ready_symbols"] == 1
        assert data["book_ready_symbols"] == 1
        assert data["collector"]["status"] == "ok"
        assert data["collector"]["book_records"] == 1
        assert data["collector"]["book_records_added_last_run"] == 1
        assert data["collector"]["news_flowing"] is True
        assert data["collector"]["book_flowing"] is True
        assert data["sentiment"]["status"] == "ok"
        assert data["sentiment"]["ok_count"] == 4
        assert "fomc" in data["sentiment"]["active_topics"]
        assert data["sentiment"]["macro_headlines"][0]["publisher"] == "CNBC"
        assert data["sentiment"]["pressure"]["status"] == "neutral"
        assert "macro" in data["sentiment"]["pressure"]["lead_positive_asset"].lower()

    def test_dashboard_diagnostics_summarizes_diamond_retune_status(self, app_client, tmp_path):
        state = tmp_path / "state"
        (state / "diamond_retune_status_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_diamond_retune_status",
                    "focus_bot": "mnq_futures_sage",
                    "focus_state": "STUCK_RESEARCH_FAILING",
                    "focus_issue": "broker_pnl_negative",
                    "focus_next_action": "pause repeated attempts",
                    "focus_active_experiment": {
                        "experiment_id": "partial_profit_disabled",
                        "started_at": "2026-05-16T01:44:06+00:00",
                        "partial_profit_enabled": False,
                        "post_change_closed_trade_count": 2,
                        "post_change_cumulative_r": 0.8192,
                        "post_change_total_realized_pnl": 40.0,
                        "post_change_profit_factor": 1.5,
                    },
                    "focus_active_experiment_outcome_line": (
                        "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
                    ),
                    "summary": {
                        "n_targets": 3,
                        "n_attempted_bots": 2,
                        "n_unattempted_targets": 1,
                        "n_low_sample_keep_collecting": 1,
                        "n_near_miss_keep_tuning": 1,
                        "n_unstable_positive_keep_tuning": 1,
                        "n_research_passed_broker_proof_required": 1,
                        "n_stuck_research_failing": 1,
                        "n_timeout_retry": 0,
                        "broker_proof_required_closes": 100,
                        "n_broker_sample_ready": 0,
                        "n_broker_edge_ready": 0,
                        "n_broker_proof_ready": 0,
                        "n_broker_sample_ready_negative_edge": 0,
                        "n_broker_proof_shortfall": 1,
                        "largest_broker_proof_gap": 91,
                        "total_broker_proof_gap": 91,
                        "broker_truth_focus_bot_id": "mnq_futures_sage",
                        "broker_truth_focus_edge_status": "sample_met_negative_edge",
                        "broker_truth_focus_closed_trade_count": 126,
                        "broker_truth_focus_required_closed_trade_count": 100,
                        "broker_truth_focus_remaining_closed_trade_count": 0,
                        "broker_truth_focus_total_realized_pnl": -1939.75,
                        "broker_truth_focus_profit_factor": 0.3951,
                        "broker_truth_focus_issue_code": "broker_pnl_negative",
                        "broker_truth_focus_priority_score": 1061.81,
                        "broker_truth_focus_strategy_kind": "orb_sage_gated",
                        "broker_truth_focus_best_session": "close",
                        "broker_truth_focus_worst_session": "overnight",
                        "broker_truth_focus_parameter_focus": ["session predicate", "rr_target"],
                        "broker_truth_focus_primary_experiment": (
                            "Bias fresh sample toward close and block overnight."
                        ),
                        "broker_truth_focus_next_command": (
                            "python -m eta_engine.scripts.run_research_grid "
                            "--source registry --bots mnq_futures_sage --report-policy runtime"
                        ),
                        "broker_truth_focus_next_action": "pause repeated attempts",
                        "broker_truth_focus_active_experiment": {
                            "experiment_id": "partial_profit_disabled",
                            "started_at": "2026-05-16T01:44:06+00:00",
                            "partial_profit_enabled": False,
                            "post_change_closed_trade_count": 2,
                            "post_change_cumulative_r": 0.8192,
                            "post_change_total_realized_pnl": 40.0,
                            "post_change_profit_factor": 1.5,
                        },
                        "broker_truth_focus_active_experiment_summary_line": (
                            "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
                        ),
                        "broker_truth_summary_line": (
                            "mnq_futures_sage: sample met (126/100) but broker edge is negative"
                        ),
                        "safe_to_mutate_live": False,
                    },
                    "bots": [
                        {
                            "bot_id": "mnq_futures_sage",
                            "retune_state": "STUCK_RESEARCH_FAILING",
                            "next_action": "pause repeated attempts",
                            "broker_close_evidence": {
                                "closed_trade_count": 9,
                                "required_closed_trade_count": 100,
                                "remaining_closed_trade_count": 91,
                                "sample_progress_pct": 9.0,
                                "edge_status": "needs_more_broker_closes",
                                "has_positive_edge": False,
                                "total_realized_pnl": -125.25,
                                "profit_factor": 0.72,
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics?refresh=true")

        assert r.status_code == 200
        payload = r.json()
        data = payload["diamond_retune_status"]
        assert data["status"] == "ready"
        assert data["ready"] is True
        assert data["contract_ok"] is True
        assert data["n_targets"] == 3
        assert data["n_low_sample_keep_collecting"] == 1
        assert data["n_near_miss_keep_tuning"] == 1
        assert data["n_unstable_positive_keep_tuning"] == 1
        assert data["n_stuck_research_failing"] == 1
        assert data["n_broker_sample_ready"] == 0
        assert data["n_broker_edge_ready"] == 0
        assert data["n_broker_proof_shortfall"] == 1
        assert data["largest_broker_proof_gap"] == 91
        assert data["total_broker_proof_gap"] == 91
        assert data["top_bot_id"] == "mnq_futures_sage"
        assert data["top_remaining_closed_trade_count"] == 91
        assert data["top_sample_progress_pct"] == 9.0
        assert data["top_broker_edge_status"] == "needs_more_broker_closes"
        assert data["top_broker_has_positive_edge"] is False
        assert data["top_broker_total_realized_pnl"] == -125.25
        assert data["top_broker_profit_factor"] == 0.72
        assert data["broker_truth_focus_bot_id"] == "mnq_futures_sage"
        assert data["broker_truth_focus_edge_status"] == "sample_met_negative_edge"
        assert data["broker_truth_focus_closed_trade_count"] == 126
        assert data["broker_truth_focus_remaining_closed_trade_count"] == 0
        assert data["broker_truth_focus_total_realized_pnl"] == -1939.75
        assert data["broker_truth_focus_profit_factor"] == 0.3951
        assert data["broker_truth_focus_issue_code"] == "broker_pnl_negative"
        assert data["broker_truth_focus_priority_score"] == 1061.81
        assert data["broker_truth_focus_strategy_kind"] == "orb_sage_gated"
        assert data["broker_truth_focus_best_session"] == "close"
        assert data["broker_truth_focus_worst_session"] == "overnight"
        assert data["broker_truth_focus_parameter_focus"] == ["session predicate", "rr_target"]
        assert data["broker_truth_focus_primary_experiment"] == "Bias fresh sample toward close and block overnight."
        assert data["broker_truth_focus_next_command"].endswith(
            "--bots mnq_futures_sage --report-policy runtime"
        )
        assert data["broker_truth_focus_active_experiment"]["experiment_id"] == "partial_profit_disabled"
        assert data["broker_truth_focus_active_experiment_summary_line"] == (
            "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
        )
        assert data["broker_truth_focus_active_experiment_outcome_line"] == (
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        )
        assert data["broker_truth_summary_line"].startswith("mnq_futures_sage: sample met")
        assert payload["retune_focus_bot_id"] == "mnq_futures_sage"
        assert payload["retune_focus_state"] == "STUCK_RESEARCH_FAILING"
        assert payload["retune_focus_issue"] == "broker_pnl_negative"
        assert payload["retune_focus_next_action"] == "pause repeated attempts"
        assert payload["retune_focus_active_experiment"]["experiment_id"] == "partial_profit_disabled"
        assert payload["retune_focus_active_experiment_summary_line"] == (
            "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
        )
        assert payload["retune_focus_active_experiment_outcome_line"] == (
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        )

    def test_dashboard_cold_start_still_exposes_operator_queue(self, tmp_path, app_client):
        state = tmp_path / "state"
        (state / "dashboard_payload.json").unlink()

        r = app_client.get("/api/dashboard")

        assert r.status_code == 200
        assert r.json()["_warning"] == "no_data"
        assert "operator_queue" in r.json()
        assert r.json()["symbol_intelligence"]["status"] == "UNKNOWN"
        assert r.json()["diamond_retune_status"]["status"] == "missing"

    def test_dashboard_uses_operator_queue_summary(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_operator_queue_summary",
            lambda **_kwargs: {
                "source": "operator_action_queue",
                "error": None,
                "summary": {"BLOCKED": 1},
                "top_blockers": [{"op_id": "OP-18", "title": "Resolve DR blockers"}],
            },
        )

        r = app_client.get("/api/dashboard")

        assert r.status_code == 200
        queue = r.json()["operator_queue"]
        assert queue["summary"]["BLOCKED"] == 1
        assert queue["top_blockers"][0]["op_id"] == "OP-18"

    def test_jarvis_operator_queue_endpoint(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_operator_queue_summary",
            lambda **_kwargs: {
                "source": "operator_action_queue",
                "error": None,
                "summary": {"BLOCKED": 0},
                "top_blockers": [],
            },
        )

        r = app_client.get("/api/jarvis/operator_queue")

        assert r.status_code == 200
        assert r.json()["summary"]["BLOCKED"] == 0
        assert "no-store" in r.headers["Cache-Control"]

    def test_jarvis_operator_queue_endpoint_derives_non_launch_advisory_fields(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_operator_queue_summary",
            lambda **_kwargs: {
                "source": "operator_action_queue",
                "error": None,
                "summary": {"BLOCKED": 2, "OBSERVED": 0, "UNKNOWN": 0},
                "launch_blocked_count": 1,
                "top_blockers": [
                    {
                        "op_id": "OP-19",
                        "title": "Recover IBKR Gateway API 4002",
                        "detail": "Seed the gateway credentials and reauth on the VPS.",
                        "next_actions": ["run tws_watchdog"],
                    },
                    {
                        "op_id": "OP-16",
                        "title": "Research backlog still needs promotion proof",
                        "detail": "4 research candidate bot(s) still below promotion gate.",
                        "evidence": {
                            "launch_blocker": False,
                            "launch_role": "strategy_optimization_backlog",
                        },
                        "next_actions": ["continue research soak"],
                    },
                ],
                "top_launch_blockers": [
                    {
                        "op_id": "OP-19",
                        "detail": "Seed the gateway credentials and reauth on the VPS.",
                    }
                ],
            },
        )

        r = app_client.get("/api/jarvis/operator_queue")

        assert r.status_code == 200
        payload = r.json()
        assert payload["launch_blocked_count"] == 1
        assert payload["non_launch_blocked_count"] == 1
        assert payload["advisory_count"] == 1
        assert payload["advisory_only"] is False
        assert payload["first_non_launch_blocker_op_id"] == "OP-16"
        assert payload["first_non_launch_next_action"] == "continue research soak"
        assert payload["top_non_launch_blockers"][0]["op_id"] == "OP-16"
        assert payload["non_launch_next_actions"] == ["continue research soak"]

    def test_jarvis_operator_queue_endpoint_fails_soft(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        def boom(**_kwargs):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(jarvis_status, "build_operator_queue_summary", boom)

        r = app_client.get("/api/jarvis/operator_queue")

        assert r.status_code == 200
        assert r.json()["error"] == "probe exploded"
        assert r.json()["top_blockers"] == []

    def test_dashboard_uses_second_brain_summary(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_second_brain_summary",
            lambda **_kwargs: {
                "source": "jarvis_status.second_brain",
                "status": "warm",
                "n_episodes": 42,
                "win_rate": 0.571,
                "avg_r": 0.2346,
                "semantic_patterns": 3,
                "procedural_versions": 1,
                "top_patterns": [{"pattern": "neutral+rth+long"}],
                "playbook": {
                    "eligible_patterns": 2,
                    "favor_patterns": [{"pattern": "neutral+rth+long"}],
                    "avoid_patterns": [],
                },
                "paths": {},
                "sources": {},
                "legacy_sources_active": False,
            },
        )

        r = app_client.get("/api/dashboard")

        assert r.status_code == 200
        second_brain = r.json()["second_brain"]
        assert second_brain["status"] == "warm"
        assert second_brain["n_episodes"] == 42
        assert second_brain["playbook"]["eligible_patterns"] == 2

    def test_jarvis_second_brain_endpoint(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_second_brain_summary",
            lambda **_kwargs: {
                "source": "jarvis_status.second_brain",
                "status": "warm",
                "n_episodes": 42,
                "win_rate": 0.571,
                "avg_r": 0.2346,
                "semantic_patterns": 3,
                "procedural_versions": 1,
                "top_patterns": [],
                "playbook": {"eligible_patterns": 2, "favor_patterns": [], "avoid_patterns": []},
                "paths": {},
                "sources": {},
                "legacy_sources_active": False,
            },
        )

        r = app_client.get("/api/jarvis/second_brain")

        assert r.status_code == 200
        assert r.json()["status"] == "warm"
        assert r.json()["n_episodes"] == 42
        assert "no-store" in r.headers["Cache-Control"]

    def test_jarvis_second_brain_endpoint_fails_soft(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        def boom(**_kwargs):
            raise RuntimeError("memory probe exploded")

        monkeypatch.setattr(jarvis_status, "build_second_brain_summary", boom)

        r = app_client.get("/api/jarvis/second_brain")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "unavailable"
        assert data["error"] == "memory probe exploded"
        assert data["n_episodes"] == 0
        assert data["playbook"]["eligible_patterns"] == 0

    def test_dashboard_uses_dirty_worktree_reconciliation_summary(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_dirty_worktree_reconciliation_summary",
            lambda **_kwargs: {
                "source": "jarvis_status.dirty_worktree_reconciliation",
                "status": "review_required",
                "ready": False,
                "action": "review_child_dirty_groups_before_gitlink_wiring",
                "dirty_modules": ["eta_engine", "mnq_backtest"],
                "blocking_modules": ["eta_engine", "mnq_backtest"],
                "next_actions": ["eta_engine: start with scripts=130"],
                "module_summaries": [{"module": "eta_engine", "entry_count": 444}],
                "review_batches": [{"batch_id": "eta_engine:scripts", "count": 130}],
                "safety": {"no_git_mutation": True},
            },
        )

        r = app_client.get("/api/dashboard")

        assert r.status_code == 200
        payload = r.json()["dirty_worktree_reconciliation"]
        assert payload["status"] == "review_required"
        assert payload["dirty_modules"] == ["eta_engine", "mnq_backtest"]
        assert payload["next_actions"] == ["eta_engine: start with scripts=130"]
        assert payload["review_batches"] == [{"batch_id": "eta_engine:scripts", "count": 130}]

    def test_jarvis_dirty_worktree_reconciliation_endpoint(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_dirty_worktree_reconciliation_summary",
            lambda **_kwargs: {
                "source": "jarvis_status.dirty_worktree_reconciliation",
                "status": "review_required",
                "ready": False,
                "action": "review_child_dirty_groups_before_gitlink_wiring",
                "dirty_modules": ["eta_engine"],
                "blocking_modules": ["eta_engine"],
                "next_actions": [],
                "module_summaries": [],
                "review_batches": [{"batch_id": "eta_engine:scripts"}],
                "safety": {"no_git_mutation": True},
            },
        )

        r = app_client.get("/api/jarvis/dirty_worktree_reconciliation")

        assert r.status_code == 200
        assert r.json()["status"] == "review_required"
        assert r.json()["dirty_modules"] == ["eta_engine"]
        assert r.json()["review_batches"] == [{"batch_id": "eta_engine:scripts"}]
        assert "no-store" in r.headers["Cache-Control"]

    def test_jarvis_dirty_worktree_reconciliation_endpoint_fails_soft(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        def boom(**_kwargs):
            raise RuntimeError("reconciliation probe exploded")

        monkeypatch.setattr(jarvis_status, "build_dirty_worktree_reconciliation_summary", boom)

        r = app_client.get("/api/jarvis/dirty_worktree_reconciliation")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "unavailable"
        assert data["error"] == "reconciliation probe exploded"
        assert data["dirty_modules"] == []
        assert data["review_batches"] == []

    def test_jarvis_paper_live_transition_endpoint_uses_cached_snapshot(
        self,
        app_client,
        monkeypatch,
        tmp_path,
    ):
        from eta_engine.scripts import paper_live_transition_check

        def boom(**_kwargs):
            raise RuntimeError("live probe should not run")

        monkeypatch.setattr(paper_live_transition_check, "build_transition_check", boom)
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-05-06T14:00:00+00:00",
                    "status": "blocked",
                    "critical_ready": False,
                    "operator_queue_first_blocker_op_id": "OP-19",
                    "operator_queue_first_next_action": "install IB Gateway 10.46",
                    "paper_ready_bots": 10,
                    "gates": [{"name": "tws_api_4002", "passed": False}],
                }
            )
        )

        r = app_client.get("/api/jarvis/paper_live_transition")

        assert r.status_code == 200
        data = r.json()
        assert data["source"] == "paper_live_transition_check_cache"
        assert data["cache_status"] == "hit"
        assert data["status"] == "blocked"
        assert data["critical_ready"] is False
        assert data["operator_queue_first_blocker_op_id"] == "OP-19"
        assert data["paper_ready_bots"] == 10
        assert data["source_age_s"] >= 0
        assert "no-store" in r.headers["Cache-Control"]

    def test_jarvis_paper_live_transition_endpoint_marks_stale_cache_detail(
        self,
        app_client,
        monkeypatch,
        tmp_path,
    ):
        from eta_engine.scripts import paper_live_transition_check

        def boom(**_kwargs):
            raise RuntimeError("live probe should not run")

        monkeypatch.setattr(paper_live_transition_check, "build_transition_check", boom)
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": (datetime.now(UTC) - timedelta(hours=18)).isoformat(),
                    "status": "blocked",
                    "critical_ready": False,
                    "paper_ready_bots": 11,
                    "gates": [{"name": "tws_api_4002", "passed": False}],
                }
            )
        )

        r = app_client.get("/api/jarvis/paper_live_transition")

        assert r.status_code == 200
        data = r.json()
        assert data["cache_stale"] is True
        assert data["stale_receipt"] is True
        assert data["effective_status"] == "stale_receipt"
        assert "stale" in data["effective_detail"].lower()
        assert "stale" in data["stale_detail"].lower()

    def test_jarvis_paper_live_transition_endpoint_surfaces_daily_loss_shadow_advisory(
        self,
        app_client,
        monkeypatch,
        tmp_path,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.scripts import paper_live_transition_check

        def boom(**_kwargs):
            raise RuntimeError("live probe should not run")

        monkeypatch.setattr(paper_live_transition_check, "build_transition_check", boom)
        monkeypatch.setattr(
            mod,
            "_daily_loss_killswitch_snapshot",
            lambda: {
                "source": "daily_loss_killswitch",
                "status": "tripped",
                "tripped": True,
                "disabled": False,
                "today_pnl_usd": -925.50,
                "limit_usd": -900.0,
                "reason": "day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
                "timezone": "America/New_York",
                "reset_display": "2026-05-16 00:00 EDT",
            },
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "operator_queue_launch_blocked_count": 0,
                    "paper_ready_bots": 9,
                    "gates": [],
                },
            ),
            encoding="utf-8",
        )
        r = app_client.get("/api/jarvis/paper_live_transition")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready_to_launch_paper_live"
        assert data["raw_status"] == "ready_to_launch_paper_live"
        assert data["effective_status"] == "shadow_paper_active"
        assert data["held_by_daily_loss_stop"] is False
        assert data["daily_loss_gate_mode"] == "advisory"
        assert data["daily_loss_advisory_active"] is True
        assert data["capital_lanes_held_by_daily_loss_stop"] is True
        assert "Shadow paper remains live" in data["effective_detail"]
        assert "day_pnl=$-925.50" in data["effective_detail"]
        assert "resets automatically at 2026-05-16 00:00 EDT" in data["effective_detail"]
        assert data["daily_loss_killswitch"]["status"] == "tripped"
        assert data["daily_loss_killswitch"]["timezone"] == "America/New_York"

    def test_jarvis_paper_live_transition_endpoint_enforces_daily_loss_when_operator_overrides(
        self,
        app_client,
        monkeypatch,
        tmp_path,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.scripts import paper_live_transition_check

        monkeypatch.setenv("ETA_PAPER_LIVE_KILLSWITCH_MODE", "enforce")
        monkeypatch.setattr(paper_live_transition_check, "build_transition_check", lambda **_kwargs: {})
        monkeypatch.setattr(
            mod,
            "_daily_loss_killswitch_snapshot",
            lambda: {
                "source": "daily_loss_killswitch",
                "status": "tripped",
                "tripped": True,
                "disabled": False,
                "today_pnl_usd": -925.50,
                "limit_usd": -900.0,
                "reason": "day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
            },
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "operator_queue_launch_blocked_count": 0,
                    "paper_ready_bots": 9,
                    "gates": [],
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/jarvis/paper_live_transition")

        assert r.status_code == 200
        data = r.json()
        assert data["effective_status"] == "held_by_daily_loss_stop"
        assert data["held_by_daily_loss_stop"] is True
        assert data["daily_loss_gate_mode"] == "enforce"
        assert data["daily_loss_advisory_active"] is False
        assert data["capital_lanes_held_by_daily_loss_stop"] is True

    def test_jarvis_paper_live_transition_endpoint_ignores_local_daily_loss_on_non_authoritative_host(
        self,
        app_client,
        monkeypatch,
        tmp_path,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_daily_loss_killswitch_snapshot",
            lambda: {
                "source": "daily_loss_killswitch",
                "status": "tripped",
                "tripped": True,
                "disabled": False,
                "today_pnl_usd": -925.50,
                "limit_usd": -900.0,
                "reason": "day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
            },
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "blocked",
                    "critical_ready": False,
                    "non_authoritative_gateway_host": True,
                    "operator_queue_launch_blocked_count": 1,
                    "operator_queue_first_launch_next_action": (
                        "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass "
                        "-File .\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 "
                        "-Apply -Role vps"
                    ),
                    "paper_ready_bots": 9,
                    "gates": [],
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/jarvis/paper_live_transition")

        assert r.status_code == 200
        data = r.json()
        assert data["effective_status"] == "blocked"
        assert data["held_by_daily_loss_stop"] is False
        assert data["daily_loss_advisory_active"] is False
        assert data["capital_lanes_held_by_daily_loss_stop"] is False
        assert data["daily_loss_suppressed_non_authoritative_gateway_host"] is True
        assert (
            data["effective_detail"]
            == "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass "
            "-File .\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 -Apply -Role vps"
        )
        assert (
            data["detail"]
            == "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass "
            "-File .\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 -Apply -Role vps"
        )
        assert (
            data["first_launch_next_action"]
            == "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass "
            "-File .\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 -Apply -Role vps"
        )
        assert data["first_failed_gate"] == {}
        assert data["daily_loss_killswitch"]["status"] == "tripped"

    def test_jarvis_paper_live_transition_endpoint_keeps_advisory_queue_out_of_launch_action(
        self,
        app_client,
        tmp_path,
    ):
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "operator_queue_blocked_count": 1,
                    "operator_queue_launch_blocked_count": 0,
                    "operator_queue_first_blocker_op_id": "OP-16",
                    "operator_queue_first_next_action": "continue research soak",
                    "operator_queue_first_launch_blocker_op_id": None,
                    "operator_queue_first_launch_next_action": None,
                    "paper_ready_bots": 12,
                    "gates": [],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/jarvis/paper_live_transition")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready_to_launch_paper_live"
        assert data["effective_status"] == "ready_to_launch_paper_live"
        assert data["effective_detail"] == ""
        assert data["detail"] == ""
        assert data["first_launch_next_action"] == ""
        assert data["first_failed_gate"] == {}

    def test_jarvis_paper_live_transition_endpoint_marks_live_shadow_runtime_active(
        self,
        app_client,
        tmp_path,
    ):
        now_iso = datetime.now(UTC).isoformat()
        state = tmp_path / "state"
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": now_iso,
                    "mode": "paper_live",
                    "feed": "composite",
                    "bots": [
                        {
                            "bot_id": "volume_profile_mnq",
                            "symbol": "MNQ1",
                            "strategy_kind": "confluence_scorecard",
                            "execution_lane": "shadow_paper",
                            "last_bar_ts": now_iso,
                            "open_position": {},
                            "n_entries": 0,
                            "n_exits": 0,
                            "realized_pnl": 0.0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": now_iso,
                    "status": "blocked",
                    "critical_ready": False,
                    "paper_ready_bots": 11,
                    "operator_queue_blocked_count": 5,
                    "operator_queue_launch_blocked_count": 1,
                    "operator_queue_first_launch_blocker_op_id": "OP-19",
                    "gates": [],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/jarvis/paper_live_transition")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "blocked"
        assert data["raw_status"] == "blocked"
        assert data["effective_status"] == "shadow_paper_active"
        assert data["effective_detail"] == "live shadow paper lane active on 1 attached bot(s)"

    def test_jarvis_paper_live_transition_endpoint_refreshes_on_demand(self, app_client, monkeypatch):
        from eta_engine.scripts import paper_live_transition_check

        monkeypatch.setattr(
            paper_live_transition_check,
            "build_transition_check",
            lambda **_kwargs: {
                "status": "blocked",
                "critical_ready": False,
                "operator_queue_first_blocker_op_id": "OP-19",
                "operator_queue_first_next_action": "install IB Gateway 10.46",
                "paper_ready_bots": 10,
                "gates": [{"name": "tws_api_4002", "passed": False}],
            },
        )

        r = app_client.get("/api/jarvis/paper_live_transition?refresh=1")

        assert r.status_code == 200
        data = r.json()
        assert data["source"] == "paper_live_transition_check"
        assert data["status"] == "blocked"
        assert data["operator_queue_first_blocker_op_id"] == "OP-19"

    def test_jarvis_paper_live_transition_endpoint_fails_soft(self, app_client, monkeypatch):
        from eta_engine.scripts import paper_live_transition_check

        def boom(**_kwargs):
            raise RuntimeError("transition probe exploded")

        monkeypatch.setattr(paper_live_transition_check, "build_transition_check", boom)

        r = app_client.get("/api/jarvis/paper_live_transition?refresh=1")

        assert r.status_code == 200
        data = r.json()
        assert data["source"] == "paper_live_transition_check"
        assert data["status"] == "unreadable"
        assert data["critical_ready"] is False
        assert data["error"] == "transition probe exploded"
        assert data["gates"] == []

    def test_dashboard_uses_bot_strategy_readiness_summary(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_bot_strategy_readiness_summary",
            lambda **_kwargs: {
                "source": "bot_strategy_readiness",
                "status": "ready",
                "summary": {"blocked_data": 0, "launch_lanes": {"live_preflight": 6}},
                "top_actions": [],
            },
        )

        r = app_client.get("/api/dashboard")

        assert r.status_code == 200
        readiness = r.json()["bot_strategy_readiness"]
        assert readiness["status"] == "ready"
        assert readiness["summary"]["launch_lanes"]["live_preflight"] == 6

    def test_jarvis_bot_strategy_readiness_endpoint(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_bot_strategy_readiness_summary",
            lambda **_kwargs: {
                "source": "bot_strategy_readiness",
                "status": "ready",
                "summary": {"blocked_data": 0},
                "top_actions": [{"bot_id": "mnq_futures_sage"}],
            },
        )

        r = app_client.get("/api/jarvis/bot_strategy_readiness")

        assert r.status_code == 200
        assert r.json()["summary"]["blocked_data"] == 0
        assert r.json()["top_actions"][0]["bot_id"] == "mnq_futures_sage"
        assert "no-store" in r.headers["Cache-Control"]

    def test_jarvis_bot_strategy_readiness_bot_endpoint(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_bot_strategy_readiness_summary",
            lambda **_kwargs: {
                "source": "bot_strategy_readiness",
                "status": "ready",
                "summary": {"total_bots": 2},
                "row_count": 2,
                "rows": [],
                "rows_by_bot": {
                    "nq_daily_drb": {
                        "bot_id": "nq_daily_drb",
                        "strategy_id": "nq_daily_drb_v1",
                        "launch_lane": "live_preflight",
                        "can_paper_trade": True,
                        "can_live_trade": False,
                        "next_action": "Run per-bot promotion preflight.",
                    }
                },
                "top_actions": [],
            },
        )

        r = app_client.get("/api/jarvis/bot_strategy_readiness/nq_daily_drb")

        assert r.status_code == 200
        data = r.json()
        assert data["found"] is True
        assert data["bot_id"] == "nq_daily_drb"
        assert data["row"]["strategy_id"] == "nq_daily_drb_v1"
        assert data["launch_lane"] == "live_preflight"
        assert data["can_paper_trade"] is True
        assert data["can_live_trade"] is False
        assert data["readiness_next_action"].startswith("Run per-bot promotion")
        assert "no-store" in r.headers["Cache-Control"]

    def test_jarvis_bot_strategy_readiness_bot_endpoint_fails_soft_when_missing(
        self,
        app_client,
        monkeypatch,
    ):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_bot_strategy_readiness_summary",
            lambda **_kwargs: {
                "source": "bot_strategy_readiness",
                "status": "ready",
                "summary": {"total_bots": 1},
                "row_count": 1,
                "rows": [],
                "rows_by_bot": {"mnq_futures_sage": {"bot_id": "mnq_futures_sage"}},
                "top_actions": [],
            },
        )

        r = app_client.get("/api/jarvis/bot_strategy_readiness/nq_daily_drb")

        assert r.status_code == 200
        data = r.json()
        assert data["found"] is False
        assert data["bot_id"] == "nq_daily_drb"
        assert data["row"] == {}
        assert data["available_bots"] == ["mnq_futures_sage"]

    def test_jarvis_bot_strategy_readiness_endpoint_fails_soft(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        def boom(**_kwargs):
            raise RuntimeError("snapshot probe exploded")

        monkeypatch.setattr(jarvis_status, "build_bot_strategy_readiness_summary", boom)

        r = app_client.get("/api/jarvis/bot_strategy_readiness")

        assert r.status_code == 200
        assert r.json()["error"] == "snapshot probe exploded"
        assert r.json()["summary"] == {}
        assert r.json()["row_count"] == 0
        assert r.json()["rows"] == []
        assert r.json()["rows_by_bot"] == {}
        assert r.json()["top_actions"] == []

    def test_jarvis_strategy_supercharge_scorecard_endpoint(self, app_client, monkeypatch):
        from eta_engine.scripts import strategy_supercharge_scorecard

        monkeypatch.setattr(
            strategy_supercharge_scorecard,
            "build_scorecard",
            lambda: {
                "source": "strategy_supercharge_scorecard",
                "status": "ready",
                "summary": {"next_best_bot": "eth_compression", "a_c_targets": 4},
                "rows": [{"bot_id": "eth_compression", "supercharge_phase": "A_C_PAPER_SOAK"}],
                "rows_by_bot": {"eth_compression": {"bot_id": "eth_compression"}},
            },
        )

        r = app_client.get("/api/jarvis/strategy_supercharge_scorecard")

        assert r.status_code == 200
        data = r.json()
        assert data["summary"]["next_best_bot"] == "eth_compression"
        assert data["rows"][0]["supercharge_phase"] == "A_C_PAPER_SOAK"
        assert "no-store" in r.headers["Cache-Control"]

    def test_jarvis_strategy_supercharge_scorecard_endpoint_fails_soft(self, app_client, monkeypatch):
        from eta_engine.scripts import strategy_supercharge_scorecard

        def boom() -> dict[str, object]:
            raise RuntimeError("scorecard exploded")

        monkeypatch.setattr(strategy_supercharge_scorecard, "build_scorecard", boom)

        r = app_client.get("/api/jarvis/strategy_supercharge_scorecard")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "unreadable"
        assert data["error"] == "scorecard exploded"
        assert data["rows"] == []
        assert data["rows_by_bot"] == {}

    def test_jarvis_strategy_supercharge_manifest_endpoint(self, app_client, monkeypatch):
        from eta_engine.scripts import strategy_supercharge_manifest

        monkeypatch.setattr(
            strategy_supercharge_manifest,
            "build_manifest",
            lambda: {
                "source": "strategy_supercharge_manifest",
                "status": "ready",
                "summary": {"next_bot": "btc_ensemble_2of3", "a_c_now": 11},
                "rows": [{"bot_id": "btc_ensemble_2of3", "action_type": "research_grid_retest"}],
                "rows_by_bot": {"btc_ensemble_2of3": {"bot_id": "btc_ensemble_2of3"}},
                "next_batch": [{"bot_id": "btc_ensemble_2of3"}],
                "b_later": [],
                "hold": [],
            },
        )

        r = app_client.get("/api/jarvis/strategy_supercharge_manifest")

        assert r.status_code == 200
        data = r.json()
        assert data["summary"]["next_bot"] == "btc_ensemble_2of3"
        assert data["next_batch"][0]["bot_id"] == "btc_ensemble_2of3"
        assert "no-store" in r.headers["Cache-Control"]

    def test_jarvis_strategy_supercharge_manifest_endpoint_fails_soft(self, app_client, monkeypatch):
        from eta_engine.scripts import strategy_supercharge_manifest

        def boom() -> dict[str, object]:
            raise RuntimeError("manifest exploded")

        monkeypatch.setattr(strategy_supercharge_manifest, "build_manifest", boom)

        r = app_client.get("/api/jarvis/strategy_supercharge_manifest")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "unreadable"
        assert data["error"] == "manifest exploded"
        assert data["rows"] == []
        assert data["rows_by_bot"] == {}
        assert data["next_batch"] == []

    def test_jarvis_strategy_supercharge_results_endpoint(self, app_client, monkeypatch):
        from eta_engine.scripts import strategy_supercharge_results

        monkeypatch.setattr(
            strategy_supercharge_results,
            "build_results",
            lambda: {
                "source": "strategy_supercharge_results",
                "status": "ready",
                "summary": {"tested": 3, "failed": 3, "pending": 8},
                "rows": [{"bot_id": "btc_ensemble_2of3", "result_status": "fail"}],
                "rows_by_bot": {"btc_ensemble_2of3": {"bot_id": "btc_ensemble_2of3"}},
                "tested": [{"bot_id": "btc_ensemble_2of3"}],
                "pending": [],
            },
        )

        r = app_client.get("/api/jarvis/strategy_supercharge_results")

        assert r.status_code == 200
        data = r.json()
        assert data["summary"]["tested"] == 3
        assert data["rows"][0]["result_status"] == "fail"
        assert "no-store" in r.headers["Cache-Control"]

    def test_jarvis_strategy_supercharge_results_endpoint_fails_soft(self, app_client, monkeypatch):
        from eta_engine.scripts import strategy_supercharge_results

        def boom() -> dict[str, object]:
            raise RuntimeError("results exploded")

        monkeypatch.setattr(strategy_supercharge_results, "build_results", boom)

        r = app_client.get("/api/jarvis/strategy_supercharge_results")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "unreadable"
        assert data["error"] == "results exploded"
        assert data["rows"] == []
        assert data["rows_by_bot"] == {}
        assert data["near_misses"] == []
        assert data["retune_queue"] == []

    def test_jarvis_diamond_retune_status_endpoint_reads_latest_snapshot(self, app_client, tmp_path):
        state = tmp_path / "state"
        (state / "diamond_retune_status_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_diamond_retune_status",
                    "status": "ready",
                    "generated_at": "2026-05-14T20:00:00+00:00",
                    "summary": {
                        "n_targets": 5,
                        "n_attempted_bots": 2,
                        "n_unattempted_targets": 3,
                        "n_low_sample_keep_collecting": 1,
                        "n_near_miss_keep_tuning": 1,
                        "n_unstable_positive_keep_tuning": 1,
                        "n_stuck_research_failing": 1,
                        "n_research_passed_broker_proof_required": 1,
                        "broker_truth_focus_issue_code": "broker_pnl_negative",
                        "broker_truth_focus_strategy_kind": "orb_sage_gated",
                        "broker_truth_focus_worst_session": "overnight",
                        "broker_truth_focus_parameter_focus": ["session predicate", "rr_target"],
                        "broker_truth_focus_next_command": (
                            "python -m eta_engine.scripts.run_research_grid "
                            "--source registry --bots mnq_futures_sage --report-policy runtime"
                        ),
                        "broker_truth_focus_active_experiment": {
                            "experiment_id": "partial_profit_disabled",
                            "started_at": "2026-05-16T01:44:06+00:00",
                            "partial_profit_enabled": False,
                            "post_change_closed_trade_count": 2,
                            "post_change_cumulative_r": 0.8192,
                            "post_change_total_realized_pnl": 40.0,
                            "post_change_profit_factor": 1.5,
                        },
                        "broker_truth_focus_active_experiment_summary_line": (
                            "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
                        ),
                        "safe_to_mutate_live": False,
                    },
                    "bots": [
                        {"bot_id": "mnq_futures_sage", "stage": "stuck_research_failing"},
                        {"bot_id": "mcl_sweep_reclaim", "stage": "research_passed_broker_proof_required"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/jarvis/diamond_retune_status")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["summary"]["n_targets"] == 5
        assert data["summary"]["n_low_sample_keep_collecting"] == 1
        assert data["summary"]["n_near_miss_keep_tuning"] == 1
        assert data["summary"]["n_unstable_positive_keep_tuning"] == 1
        assert data["summary"]["n_stuck_research_failing"] == 1
        assert data["summary"]["broker_truth_focus_issue_code"] == "broker_pnl_negative"
        assert data["summary"]["broker_truth_focus_strategy_kind"] == "orb_sage_gated"
        assert data["summary"]["broker_truth_focus_worst_session"] == "overnight"
        assert data["summary"]["broker_truth_focus_parameter_focus"] == ["session predicate", "rr_target"]
        assert data["summary"]["broker_truth_focus_next_command"].endswith(
            "--bots mnq_futures_sage --report-policy runtime"
        )
        assert data["focus_bot"] == "mnq_futures_sage"
        assert data["focus_issue"] == "broker_pnl_negative"
        assert data["focus_state"] == "stuck_research_failing"
        assert data["focus_strategy_kind"] == "orb_sage_gated"
        assert data["focus_worst_session"] == "overnight"
        assert data["focus_parameter_focus"] == ["session predicate", "rr_target"]
        assert data["focus_command"].endswith("--bots mnq_futures_sage --report-policy runtime")
        assert data["summary"]["broker_truth_focus_active_experiment"]["experiment_id"] == "partial_profit_disabled"
        assert data["summary"]["broker_truth_focus_active_experiment_summary_line"] == (
            "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
        )
        assert data["summary"]["broker_truth_focus_active_experiment_outcome_line"] == (
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        )
        assert data["focus_active_experiment"]["partial_profit_enabled"] is False
        assert data["focus_active_experiment_outcome_line"] == (
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        )
        assert data["summary"]["safe_to_mutate_live"] is False
        assert data["safe_to_mutate_live"] is False
        assert data["bots"][0]["bot_id"] == "mnq_futures_sage"
        assert data["source_path"].endswith("diamond_retune_status_latest.json")
        assert "no-store" in r.headers["Cache-Control"]

    def test_dashboard_includes_diamond_retune_status_snapshot(self, app_client, tmp_path):
        state = tmp_path / "state"
        (state / "diamond_retune_status_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_diamond_retune_status",
                    "status": "ready",
                    "summary": {
                        "n_targets": 4,
                        "n_attempted_bots": 4,
                        "n_unattempted_targets": 0,
                        "n_low_sample_keep_collecting": 2,
                        "n_near_miss_keep_tuning": 1,
                        "n_unstable_positive_keep_tuning": 1,
                        "n_stuck_research_failing": 0,
                        "safe_to_mutate_live": False,
                    },
                    "bots": [{"bot_id": "mes_orb", "stage": "complete"}],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard")

        assert r.status_code == 200
        status = r.json()["diamond_retune_status"]
        assert status["summary"]["n_targets"] == 4
        assert status["summary"]["n_attempted_bots"] == 4
        assert status["summary"]["n_low_sample_keep_collecting"] == 2
        assert status["summary"]["n_near_miss_keep_tuning"] == 1
        assert status["summary"]["n_unstable_positive_keep_tuning"] == 1
        assert status["safe_to_mutate_live"] is False
        assert status["bots"][0]["bot_id"] == "mes_orb"

    def test_dashboard_bootstrap_uses_cached_broker_state_without_live_probe(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        def fail_live_probe() -> dict:
            raise AssertionError("dashboard bootstrap must not block on fresh broker probe")

        monkeypatch.setattr(mod, "_live_broker_state_payload", fail_live_probe)
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "source": "cached_live_broker_state_for_diagnostics",
                "probe_skipped": True,
                "broker_snapshot_source": "ibkr_probe_cache",
                "broker_snapshot_age_s": 8.5,
                "today_actual_fills": 2,
                "today_realized_pnl": 42.0,
                "total_unrealized_pnl": 0.0,
                "open_position_count": 0,
            },
        )

        r = app_client.get("/api/dashboard")

        assert r.status_code == 200
        live = r.json()["live_broker_state"]
        assert live["probe_skipped"] is True
        assert live["broker_snapshot_source"] == "ibkr_probe_cache"
        assert live["broker_snapshot_age_s"] == 8.5

    def test_dashboard_ibkr_probe_defaults_are_fast_first_paint(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.delenv("ETA_DASHBOARD_IBKR_CLIENT_ID", raising=False)
        monkeypatch.delenv("ETA_DASHBOARD_IBKR_CLIENT_ID_BASE", raising=False)
        monkeypatch.delenv("ETA_DASHBOARD_IBKR_CLIENT_ID_SPAN", raising=False)
        monkeypatch.delenv("ETA_DASHBOARD_IBKR_TIMEOUT_S", raising=False)

        assert mod._dashboard_ibkr_client_id_candidates() == [1842]
        assert mod._dashboard_ibkr_connect_timeout_s() == 4.0

        monkeypatch.setenv("ETA_DASHBOARD_IBKR_TIMEOUT_S", "0.25")
        assert mod._dashboard_ibkr_connect_timeout_s() == 1.0

        monkeypatch.setenv("ETA_DASHBOARD_IBKR_TIMEOUT_S", "99")
        assert mod._dashboard_ibkr_connect_timeout_s() == 12.0

    def test_ibkr_open_order_snapshot_preserves_owner_client_id(self):
        from types import SimpleNamespace

        import eta_engine.deploy.scripts.dashboard_api as mod

        trade = SimpleNamespace(
            order=SimpleNamespace(
                action="BUY",
                orderType="LMT",
                totalQuantity=1,
                parentId=0,
                ocaGroup="",
                orderId=1404,
                permId=581506499,
                clientId=188,
            ),
            contract=SimpleNamespace(
                symbol="NG",
                localSymbol="NGM26",
                secType="FUT",
                exchange="NYMEX",
            ),
            orderStatus=SimpleNamespace(
                status="Submitted",
                remaining=1,
                permId=581506499,
            ),
        )

        row = mod._ibkr_open_order_snapshot(trade)

        assert row["symbol"] == "NGM26"
        assert row["owner_client_id"] == 188
        assert row["client_id"] == 188

    def test_live_broker_state_endpoint_defaults_to_cached_state(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        def fail_live_probe() -> dict:
            raise AssertionError("default broker endpoint must not open a fresh probe")

        monkeypatch.setattr(mod, "_live_broker_state_payload", fail_live_probe)
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "source": "cached_live_broker_state_for_diagnostics",
                "probe_skipped": True,
                "broker_snapshot_source": "ibkr_probe_cache",
                "broker_snapshot_age_s": 3.0,
            },
        )

        r = app_client.get("/api/live/broker_state")

        assert r.status_code == 200
        assert r.json()["probe_skipped"] is True
        assert r.json()["broker_snapshot_source"] == "ibkr_probe_cache"
        assert "no-store" in r.headers["Cache-Control"]

    def test_live_broker_state_endpoint_refresh_runs_live_probe(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_live_broker_state_payload",
            lambda: {
                "ready": True,
                "source": "live_broker_rest",
                "probe_skipped": False,
                "broker_snapshot_source": "live_broker_rest",
            },
        )
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {"ready": False, "source": "cached_live_broker_state_for_diagnostics"},
        )

        r = app_client.get("/api/live/broker_state?refresh=1")

        assert r.status_code == 200
        assert r.json()["source"] == "live_broker_rest"
        assert r.json()["probe_skipped"] is False

    def test_live_broker_state_refresh_falls_back_to_last_good_after_ibkr_timeout(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_live_broker_state_payload",
            lambda: {
                "source": "live_broker_rest",
                "broker_snapshot_source": "live_broker_rest",
                "broker_snapshot_state": "fresh",
                "today_actual_fills": 0,
                "today_realized_pnl": 0.0,
                "open_position_count": 0,
                "ibkr": {
                    "ready": False,
                    "error": "ibkr_probe_failed:TimeoutError: TimeoutError()",
                },
            },
        )
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "source": "cached_live_broker_state_for_diagnostics",
                "probe_skipped": True,
                "broker_snapshot_source": "ibkr_probe_cache",
                "broker_snapshot_state": "persisted",
                "today_actual_fills": 17,
                "today_realized_pnl": -321.25,
                "open_position_count": 3,
                "ibkr": {"ready": True},
            },
        )

        r = app_client.get("/api/live/broker_state?refresh=1")

        assert r.status_code == 200
        payload = r.json()
        assert payload["broker_snapshot_state"] == "persisted"
        assert payload["today_actual_fills"] == 17
        assert payload["open_position_count"] == 3
        assert payload["refresh_probe_failed"] is True
        assert "TimeoutError" in payload["refresh_probe_error"]

    def test_dashboard_card_health_contract_has_no_dead_or_stale_cards(self, app_client):
        r = app_client.get("/api/dashboard/card-health")
        assert r.status_code == 200
        data = r.json()
        assert data["dashboard_version"] == "v1"
        assert data["release_stage"] == "pre_beta"
        assert data["summary"]["dead"] == 0
        assert data["summary"]["stale"] == 0
        assert data["dead_cards"] == []
        assert data["stale_cards"] == []

        cards = {card["id"]: card for card in data["cards"]}
        assert "cc-verdict-stream" in cards
        assert "cc-paper-live-transition" in cards
        assert "fl-roster" in cards
        assert "fl-controls" in cards
        assert "fl-equity-curve" in cards
        assert cards["cc-verdict-stream"]["source"] == "sse"
        assert cards["cc-paper-live-transition"]["endpoint"] == "/api/jarvis/paper_live_transition"
        assert cards["cc-second-brain"]["endpoint"] == "/api/jarvis/second_brain"
        assert (
            cards["cc-dirty-worktree-reconciliation"]["endpoint"]
            == "/api/jarvis/dirty_worktree_reconciliation"
        )
        assert "cc-strategy-supercharge-results" not in cards
        assert cards["cc-diamond-retune-status"]["endpoint"] == "/api/jarvis/diamond_retune_status"
        assert cards["fl-controls"]["source"] == "client"
        assert cards["fl-roster"]["endpoint"] == "/api/bot-fleet?since_days=1&live_broker_probe=false"
        assert cards["fl-equity-curve"]["endpoint"].startswith("/api/fleet-equity?")
        assert all(card["status"] not in {"dead", "stale"} for card in data["cards"])

    def test_dashboard_diagnostics_rollup_explains_live_sources(self, app_client):
        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        data = r.json()
        assert data["dashboard_version"] == "v1"
        assert data["release_stage"] == "pre_beta"
        assert data["source_of_truth"] == "dashboard_diagnostics"
        assert set(data["api_build"]["capabilities"]) >= {
            "command_center_watchdog",
            "daily_stop_reset_audit",
            "eta_readiness_snapshot",
            "ibkr_futures_avg_cost_normalized",
            "dirty_worktree_reconciliation",
            "jarvis_second_brain",
            "sage_sentiment_pressure",
        }
        assert data["service"]["status"] == "ok"
        assert data["service"]["uptime_s"] >= 0
        assert data["paths"]["state_dir"].endswith("state")
        assert data["cards"]["summary"]["dead"] == 0
        assert data["cards"]["summary"]["stale"] == 0
        assert data["bot_fleet"]["bot_total"] >= 0
        assert data["bot_fleet"]["confirmed_bots"] == 0
        assert data["bot_fleet"]["active_bots"] == 0
        assert data["bot_fleet"]["runtime_active_bots"] == 0
        assert data["bot_fleet"]["running_bots"] == 0
        assert data["bot_fleet"]["live_attached_bots"] == 0
        assert data["bot_fleet"]["live_in_trade_bots"] == 0
        assert data["bot_fleet"]["idle_live_bots"] == 0
        assert data["bot_fleet"]["inactive_runtime_bots"] == 0
        assert data["bot_fleet"]["staged_bots"] == 0
        assert data["bot_fleet"]["current_blocked_bots"] == 0
        assert data["bot_fleet"]["current_blocked_kinds"] == {}
        assert data["bot_fleet"]["current_blocked_summary_line"] == ""
        assert data["bot_fleet"]["current_blocked_preview"] == []
        assert data["bot_fleet"]["truth_status"] in {"empty", "runtime_stopped", "stale", "live", "working"}
        if data["bot_fleet"]["bot_total"]:
            assert data["bot_fleet"]["truth_summary_line"]
        assert data["equity"]["source"] in {
            "canonical_state_empty",
            "supervisor_heartbeat",
            "fills_intraday",
            "blotter_curve",
            "aggregated_bot_curves",
            "bot_curve",
        }
        assert data["checks"]["card_contract"] is True
        assert data["checks"]["auth_contract"] is True
        assert "generated_at" in data
        assert "operator_queue" in data
        assert "paper_live_transition" in data
        assert "second_brain" in data
        assert data["checks"]["operator_queue_contract"] is True
        assert data["checks"]["paper_live_transition_contract"] is True
        assert data["checks"]["second_brain_contract"] is True
        assert data["daily_loss_killswitch"]["status"] in {"clear", "tripped", "disabled", "unknown"}
        assert data["checks"]["daily_loss_killswitch_contract"] is True
        assert data["dashboard_proxy_watchdog"]["status"] in {
            "ok",
            "missing",
            "stale",
            "probe_ok_watchdog_stale",
            "failed",
            "degraded",
            "unknown",
        }
        assert data["checks"]["dashboard_proxy_watchdog_contract"] is True
        assert data["command_center_watchdog"]["status"] in {
            "healthy",
            "missing_receipt",
            "missing_watchdog",
            "stale_receipt",
            "stale_service",
            "service_unreachable",
            "public_operator_drift",
            "public_tunnel_service_drift",
            "public_tunnel_token_rejected",
            "contract_failure",
            "secret_surface",
            "unknown",
        }
        assert data["checks"]["command_center_watchdog_contract"] is True
        assert data["eta_readiness_snapshot"]["status"] in {
            "ready",
            "blocked",
            "missing_receipt",
            "stale_receipt",
            "unknown",
        }
        assert data["checks"]["eta_readiness_snapshot_contract"] is True
        assert data["daily_stop_reset_audit"]["status"] in {
            "held_until_reset",
            "reset_cleared_ready",
            "reset_cleared_blocked",
            "still_tripped_after_reset_window",
            "stale_receipt",
            "missing",
            "unreadable",
            "invalid",
            "unknown",
        }
        assert data["checks"]["daily_stop_reset_audit_contract"] is True
        assert "vps_ops_hardening" in data
        assert data["checks"]["vps_ops_hardening_contract"] is True

    def test_dashboard_diagnostics_includes_daily_stop_reset_audit(self, app_client, tmp_path):
        state = tmp_path / "state"
        (state / "daily_stop_reset_audit_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "daily_stop_reset_audit",
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "reset_cleared_ready",
                    "post_reset_ready": True,
                    "read_only": True,
                    "safe_to_trade_mutation": False,
                    "operator_next_action": "Watch the first supervisor tick after reset.",
                    "daily_loss_killswitch": {"status": "clear", "tripped": False},
                    "paper_live_transition": {"status": "ready_to_launch_paper_live", "critical_ready": True},
                    "first_failed_gate": {},
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        audit = r.json()["daily_stop_reset_audit"]
        assert audit["status"] == "reset_cleared_ready"
        assert audit["ready"] is True
        assert audit["read_only"] is True
        assert audit["safe_to_trade_mutation"] is False
        assert audit["operator_next_action"].startswith("Watch the first supervisor tick")
        assert audit["detail"] == "Watch the first supervisor tick after reset."
        assert audit["path"].endswith("daily_stop_reset_audit_latest.json")
        assert r.json()["checks"]["daily_stop_reset_audit_contract"] is True

    def test_dashboard_diagnostics_marks_stale_daily_stop_reset_audit(self, app_client, tmp_path):
        state = tmp_path / "state"
        (state / "daily_stop_reset_audit_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "daily_stop_reset_audit",
                    "generated_at": (datetime.now(UTC) - timedelta(hours=6)).isoformat(),
                    "status": "reset_cleared_blocked",
                    "post_reset_ready": False,
                    "read_only": True,
                    "safe_to_trade_mutation": False,
                    "operator_next_action": "Old blocker detail",
                    "daily_loss_killswitch": {"status": "clear", "tripped": False},
                    "paper_live_transition": {"status": "blocked", "critical_ready": False},
                    "first_failed_gate": {"name": "tws_api_4002", "passed": False},
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        audit = r.json()["daily_stop_reset_audit"]
        assert audit["status"] == "stale_receipt"
        assert audit["source_status"] == "reset_cleared_blocked"
        assert audit["fresh"] is False
        assert audit["ready"] is False
        assert "stale" in audit["stale_detail"].lower()
        assert audit["detail"] == audit["stale_detail"]
        assert audit["operator_next_action"] == (
            "Run python -m eta_engine.scripts.daily_stop_reset_audit --json on the VPS"
        )
        assert audit["source_operator_next_action"] == "Old blocker detail"
        assert r.json()["checks"]["daily_stop_reset_audit_contract"] is True

    def test_dashboard_diagnostics_reuses_short_ttl_cache(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        calls: list[int] = []

        def fake_payload() -> dict:
            calls.append(1)
            return {
                "dashboard_version": "v1",
                "source_of_truth": "dashboard_diagnostics",
                "generated_at": f"call-{len(calls)}",
            }

        with mod._DASHBOARD_DIAGNOSTICS_CACHE_LOCK:
            mod._DASHBOARD_DIAGNOSTICS_CACHE["payload"] = None
            mod._DASHBOARD_DIAGNOSTICS_CACHE["ts"] = 0.0
            mod._DASHBOARD_DIAGNOSTICS_CACHE["dependency_stamp"] = None
        monkeypatch.setattr(mod, "_DASHBOARD_DIAGNOSTICS_CACHE_TTL_S", 60)
        monkeypatch.setattr(mod, "_dashboard_diagnostics_payload", fake_payload)

        first = app_client.get("/api/dashboard/diagnostics")
        second = app_client.get("/api/dashboard/diagnostics")
        refreshed = app_client.get("/api/dashboard/diagnostics?refresh=true")

        assert first.status_code == 200
        assert second.status_code == 200
        assert refreshed.status_code == 200
        assert len(calls) == 2
        assert first.json()["generated_at"] == "call-1"
        assert second.json()["generated_at"] == "call-1"
        assert second.json()["diagnostics_cache"]["status"] == "hit"
        assert refreshed.json()["generated_at"] == "call-2"
        assert refreshed.json()["diagnostics_cache"]["status"] == "miss"

    def test_dashboard_diagnostics_cache_age_starts_after_slow_build(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        calls: list[int] = []
        clock = [100.0]

        def fake_time() -> float:
            return clock[0]

        def fake_payload() -> dict:
            calls.append(1)
            clock[0] += 15.0
            return {
                "dashboard_version": "v1",
                "source_of_truth": "dashboard_diagnostics",
                "generated_at": f"call-{len(calls)}",
            }

        with mod._DASHBOARD_DIAGNOSTICS_CACHE_LOCK:
            mod._DASHBOARD_DIAGNOSTICS_CACHE["payload"] = None
            mod._DASHBOARD_DIAGNOSTICS_CACHE["ts"] = 0.0
            mod._DASHBOARD_DIAGNOSTICS_CACHE["dependency_stamp"] = None
        monkeypatch.setattr(mod, "_DASHBOARD_DIAGNOSTICS_CACHE_TTL_S", 10)
        monkeypatch.setattr(mod, "_dashboard_diagnostics_payload", fake_payload)
        monkeypatch.setattr(mod.time, "time", fake_time)

        first = mod._dashboard_diagnostics_cached_payload()
        second = mod._dashboard_diagnostics_cached_payload()

        assert len(calls) == 1
        assert first["diagnostics_cache"]["status"] == "miss"
        assert second["generated_at"] == "call-1"
        assert second["diagnostics_cache"]["status"] == "hit"
        assert second["diagnostics_cache"]["age_s"] == 0.0

    def test_dashboard_diagnostics_cache_invalidates_when_dependency_stamp_changes(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        calls: list[int] = []
        stamps = [(1, 1), (1, 1), (2, 1)]

        def fake_payload() -> dict:
            calls.append(1)
            return {
                "dashboard_version": "v1",
                "source_of_truth": "dashboard_diagnostics",
                "generated_at": f"call-{len(calls)}",
            }

        def fake_dependency_stamp() -> tuple[int, int]:
            return stamps.pop(0) if stamps else (2, 1)

        with mod._DASHBOARD_DIAGNOSTICS_CACHE_LOCK:
            mod._DASHBOARD_DIAGNOSTICS_CACHE["payload"] = None
            mod._DASHBOARD_DIAGNOSTICS_CACHE["ts"] = 0.0
            mod._DASHBOARD_DIAGNOSTICS_CACHE["dependency_stamp"] = None
        monkeypatch.setattr(mod, "_DASHBOARD_DIAGNOSTICS_CACHE_TTL_S", 60)
        monkeypatch.setattr(mod, "_dashboard_diagnostics_payload", fake_payload)
        monkeypatch.setattr(mod, "_dashboard_diagnostics_dependency_stamp", fake_dependency_stamp)

        first = mod._dashboard_diagnostics_cached_payload()
        second = mod._dashboard_diagnostics_cached_payload()
        third = mod._dashboard_diagnostics_cached_payload()

        assert len(calls) == 2
        assert first["generated_at"] == "call-1"
        assert first["diagnostics_cache"]["status"] == "miss"
        assert second["generated_at"] == "call-1"
        assert second["diagnostics_cache"]["status"] == "hit"
        assert third["generated_at"] == "call-2"
        assert third["diagnostics_cache"]["status"] == "miss"

    def test_dashboard_diagnostics_includes_vps_ops_admin_ai(self, app_client, tmp_path):
        state = tmp_path / "state"
        generated_at = datetime.now(UTC).isoformat()
        (state / "vps_ops_hardening_latest.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": generated_at,
                    "summary": {
                        "status": "YELLOW_SAFETY_BLOCKED",
                        "runtime_ready": True,
                        "dashboard_durable": False,
                        "paper_live_gate_ready": True,
                        "paper_live_status": "READY_FOR_PAPER_SOAK",
                        "trading_gate_ready": False,
                        "prop_promotion_gate_ready": False,
                        "live_promotion_blocked": True,
                        "admin_ai_ready": False,
                        "admin_ai_status": "WARN",
                        "promotion_allowed": False,
                        "order_action_allowed": False,
                    },
                    "safety_gates": {
                        "jarvis_hermes_admin_ai": {
                            "status": "WARN",
                            "ready": False,
                            "warned": 1,
                            "blocked": 0,
                            "next_actions": ["Review bridge_plan_tasks: T17 wave pending"],
                        }
                    },
                    "next_actions": ["Keep paper soak blocked until gates pass"],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        data = r.json()
        hardening = data["vps_ops_hardening"]
        assert data["hardening"] == hardening
        assert hardening["status"] == "YELLOW_SAFETY_BLOCKED"
        assert hardening["ready"] is False
        assert hardening["summary"]["paper_live_gate_ready"] is True
        assert hardening["summary"]["paper_live_status"] == "READY_FOR_PAPER_SOAK"
        assert hardening["summary"]["prop_promotion_gate_ready"] is False
        assert hardening["summary"]["live_promotion_blocked"] is True
        assert hardening["summary"]["admin_ai_status"] == "WARN"
        assert hardening["summary"]["promotion_allowed"] is False
        assert hardening["summary"]["order_action_allowed"] is False
        assert hardening["jarvis_hermes_admin_ai"]["status"] == "WARN"
        assert hardening["jarvis_hermes_admin_ai"]["ready"] is False
        assert hardening["jarvis_hermes_admin_ai"]["next_actions"] == ["Review bridge_plan_tasks: T17 wave pending"]
        assert hardening["detail"] == "Keep paper soak blocked until gates pass"
        assert hardening["age_s"] is not None
        assert data["checks"]["vps_ops_hardening_contract"] is True
        assert data["checks"]["hardening_contract"] is True

    def test_dashboard_diagnostics_surfaces_force_multiplier_control_plane_repair(self, app_client, tmp_path):
        state = tmp_path / "state"
        generated_at = datetime.now(UTC).isoformat()
        repair_action = (
            "Repair supervised Windows service ownership for live endpoint: "
            "FmStatusServer (port 8422 owner=python, manual module runner); run "
            "eta_engine\\deploy\\scripts\\repair_force_multiplier_control_plane_admin.cmd /RestartService"
        )
        (state / "vps_ops_hardening_latest.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": generated_at,
                    "summary": {
                        "status": "YELLOW_RESTART_REQUIRED",
                        "runtime_ready": True,
                        "dashboard_durable": True,
                        "force_multiplier_durable": True,
                        "dashboard_schema_current": True,
                        "paper_live_gate_ready": False,
                        "paper_live_status": "BLOCKED_BROKER_BRACKETS",
                        "trading_gate_ready": False,
                        "prop_promotion_gate_ready": False,
                        "live_promotion_blocked": True,
                        "admin_ai_ready": True,
                        "admin_ai_status": "PASS",
                        "promotion_allowed": False,
                        "order_action_allowed": False,
                    },
                    "runtime": {
                        "services": {
                            "runtime_drift": ["FmStatusServer"],
                            "runtime_drift_detail": {
                                "FmStatusServer": {
                                    "port": 8422,
                                    "port_owner_runner": "manual_module_runner",
                                    "port_owner_runner_label": "manual module runner",
                                    "port_owner_details": [
                                        {
                                            "Pid": 30980,
                                            "Name": "python",
                                            "Path": "C:/Python314/python.exe",
                                            "CommandLine": (
                                                '"C:/Python314/python.exe" '
                                                "-m eta_engine.deploy.fm_status_server"
                                            ),
                                        }
                                    ],
                                }
                            },
                        },
                        "tasks": {
                            "observed_missing_force_multiplier_durable": ["ETA-ThreeAI-Sync"],
                            "missing_force_multiplier_durable": [],
                            "artifact_backed_missing_force_multiplier_durable": ["ETA-ThreeAI-Sync"],
                            "stale_artifact_backed_force_multiplier_durable": ["ETA-ThreeAI-Sync"],
                        },
                    },
                    "service_config": {
                        "fm_status_server": {
                            "matches_expected": False,
                            "installed_executable": "C:\\Python314\\python.exe",
                        }
                    },
                    "safety_gates": {
                        "jarvis_hermes_admin_ai": {
                            "status": "PASS",
                            "ready": True,
                            "warned": 0,
                            "blocked": 0,
                            "next_actions": [],
                        }
                    },
                    "next_actions": [repair_action],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        hardening = r.json()["vps_ops_hardening"]
        fm_control = hardening["force_multiplier_control_plane"]
        assert hardening["status"] == "YELLOW_RESTART_REQUIRED"
        assert hardening["summary"]["force_multiplier_durable"] is True
        assert hardening["summary"]["service_runtime_drift"] == ["FmStatusServer"]
        assert hardening["summary"]["service_config_drift"] == ["fm_status_server"]
        assert hardening["summary"]["missing_force_multiplier_durable"] == []
        assert hardening["summary"]["stale_artifact_backed_force_multiplier_durable"] == ["ETA-ThreeAI-Sync"]
        assert fm_control["status"] == "restart_required"
        assert fm_control["restart_required"] is True
        assert fm_control["repair_required"] is True
        assert fm_control["watch_stale"] is True
        assert fm_control["service_runtime_drift"] == ["FmStatusServer"]
        assert fm_control["service_config_drift"] == ["fm_status_server"]
        assert fm_control["service_config"]["matches_expected"] is False
        assert fm_control["observed_missing_tasks"] == ["ETA-ThreeAI-Sync"]
        assert fm_control["stale_artifact_backed_tasks"] == ["ETA-ThreeAI-Sync"]
        assert fm_control["repair_command"].endswith(
            "repair_force_multiplier_control_plane_admin.cmd /RestartService"
        )
        assert fm_control["next_command"] == fm_control["repair_command"]
        assert fm_control["detail"] == repair_action

        master = app_client.get("/api/master/status")
        assert master.status_code == 200
        hardening_system = master.json()["systems"]["vps_ops_hardening"]
        assert hardening_system["force_multiplier_status"] == "restart_required"
        assert hardening_system["force_multiplier_next_command"] == fm_control["repair_command"]
        assert hardening_system["service_runtime_drift"] == ["FmStatusServer"]
        assert hardening_system["service_config_drift"] == ["fm_status_server"]
        assert hardening_system["missing_force_multiplier_durable"] == []
        assert hardening_system["stale_artifact_backed_force_multiplier_durable"] == ["ETA-ThreeAI-Sync"]
        assert hardening_system["operator_next_action"] == repair_action

    def test_dashboard_diagnostics_fails_soft_when_vps_ops_hardening_raises(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        def boom(*, server_ts):
            raise NameError("cached_live_broker_state")

        monkeypatch.setattr(mod, "_vps_ops_hardening_payload", boom)

        r = app_client.get("/api/dashboard/diagnostics?refresh=true")

        assert r.status_code == 200
        data = r.json()
        assert data["vps_ops_hardening"]["status"] == "error"
        assert data["vps_ops_hardening"]["ready"] is False
        assert "cached_live_broker_state" in data["vps_ops_hardening"]["error"]
        assert data["checks"]["vps_ops_hardening_contract"] is True
        assert data["checks"]["hardening_contract"] is True
        assert data["checks"]["second_brain_contract"] is True

    def test_dashboard_diagnostics_marks_stale_vps_ops_hardening(self, app_client, tmp_path):
        state = tmp_path / "state"
        generated_at = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        (state / "vps_ops_hardening_latest.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": generated_at,
                    "summary": {
                        "status": "RED_RUNTIME_DEGRADED",
                        "runtime_ready": False,
                        "dashboard_durable": False,
                        "paper_live_gate_ready": False,
                        "paper_live_status": "BLOCKED_RUNTIME",
                        "trading_gate_ready": False,
                        "prop_promotion_gate_ready": False,
                        "live_promotion_blocked": True,
                        "admin_ai_ready": True,
                        "admin_ai_status": "PASS",
                        "promotion_allowed": False,
                        "order_action_allowed": False,
                    },
                    "safety_gates": {
                        "jarvis_hermes_admin_ai": {
                            "status": "PASS",
                            "ready": True,
                            "warned": 0,
                            "blocked": 0,
                            "next_actions": ["Old hardening detail"],
                        }
                    },
                    "next_actions": ["Old hardening action"],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        hardening = r.json()["vps_ops_hardening"]
        assert hardening["status"] == "stale_receipt"
        assert hardening["source_status"] == "RED_RUNTIME_DEGRADED"
        assert hardening["fresh"] is False
        assert hardening["ready"] is False
        assert "stale" in hardening["stale_detail"].lower()
        assert hardening["detail"] == hardening["stale_detail"]
        assert hardening["jarvis_hermes_admin_ai"]["status"] == "stale_receipt"
        assert hardening["jarvis_hermes_admin_ai"]["source_status"] == "PASS"
        assert hardening["jarvis_hermes_admin_ai"]["ready"] is False
        assert hardening["next_actions"] == ["Run eta_engine.scripts.vps_ops_hardening_audit --json-out on the VPS"]
        assert hardening["source_next_actions"] == ["Old hardening action"]
        assert r.json()["checks"]["vps_ops_hardening_contract"] is True
        assert r.json()["checks"]["hardening_contract"] is True

    def test_dashboard_diagnostics_includes_supervisor_reconcile_gate(self, app_client, tmp_path):
        state = tmp_path / "state"
        generated_at = datetime.now(UTC).isoformat()
        (state / "vps_ops_hardening_latest.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": generated_at,
                    "summary": {
                        "status": "YELLOW_SAFETY_BLOCKED",
                        "runtime_ready": True,
                        "dashboard_durable": True,
                        "trading_gate_ready": False,
                        "admin_ai_ready": True,
                        "admin_ai_status": "PASS",
                        "supervisor_reconcile_ready": False,
                        "promotion_allowed": False,
                        "order_action_allowed": False,
                    },
                    "safety_gates": {
                        "supervisor_reconcile": {
                            "status": "BLOCKED_BROKER_SUPERVISOR_RECONCILE",
                            "ready": False,
                            "source": "supervisor_heartbeat_and_live_broker_state",
                            "broker_only_symbols": ["MCL", "MYM"],
                            "divergent_symbols": ["MNQ"],
                            "age_s": 12,
                            "max_age_s": 900,
                        }
                    },
                    "next_actions": [
                        "Do not unlock new entries: reconcile broker/supervisor positions "
                        "(broker-only: MCL, MYM; divergent: MNQ)."
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        data = r.json()
        reconcile = data["vps_ops_hardening"]["supervisor_reconcile"]
        assert data["vps_ops_hardening"]["summary"]["supervisor_reconcile_ready"] is False
        assert reconcile["status"] == "BLOCKED_BROKER_SUPERVISOR_RECONCILE"
        assert reconcile["ready"] is False
        assert reconcile["source"] == "supervisor_heartbeat_and_live_broker_state"
        assert reconcile["broker_only_symbols"] == ["MCL", "MYM"]
        assert reconcile["divergent_symbols"] == ["MNQ"]
        assert data["checks"]["vps_ops_hardening_contract"] is True

    def test_dashboard_diagnostics_uses_fast_cached_truth(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        def fail_live_broker_probe() -> dict:
            raise AssertionError("diagnostics must not open a live broker probe")

        monkeypatch.setattr(mod, "_live_broker_state_payload", fail_live_broker_probe)
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "source": "cached_live_broker_state_for_diagnostics",
                "probe_skipped": True,
                "broker_snapshot_source": "ibkr_probe_cache",
                "broker_snapshot_age_s": 6.4,
                "broker_snapshot_state": "warm",
                "today_actual_fills": 12,
                "today_realized_pnl": 125.5,
                "total_unrealized_pnl": 42.25,
                "open_position_count": 3,
                "ibkr": {"ready": True},
            },
        )
        (tmp_path / "state" / "operator_queue_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "blocked",
                    "launch_blocked_count": 1,
                    "operator_queue": {
                        "source": "jarvis_status.operator_queue",
                        "summary": {"BLOCKED": 1, "OBSERVED": 0, "UNKNOWN": 0},
                        "top_blockers": [{"op_id": "OP-19", "title": "IBKR API blocked"}],
                        "top_launch_blockers": [{"op_id": "OP-19", "detail": "IBKR API 4002 down"}],
                    },
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        data = r.json()
        assert data["bot_fleet"]["live_broker_probe_mode"] == "cached_diagnostics"
        assert data["operator_queue"]["source"] == "operator_queue_snapshot_cache"
        assert data["operator_queue"]["blocked"] == 1
        assert data["operator_queue"]["launch_blocked"] == 1
        assert data["operator_queue"]["cache_stale"] is False
        assert data["checks"]["operator_queue_contract"] is True
        assert data["live_broker_state"]["ready"] is True
        assert data["live_broker_state"]["broker_snapshot_source"] == "ibkr_probe_cache"
        assert data["live_broker_state"]["broker_snapshot_state"] == "warm"
        assert data["live_broker_state"]["broker_snapshot_age_s"] == 6.4
        assert data["live_broker_state"]["broker_probe_skipped"] is True
        assert data["live_broker_state"]["broker_refresh_probe_failed"] is False
        assert data["live_broker_state"]["broker_today_actual_fills"] == 12
        assert data["checks"]["live_broker_state_contract"] is True

    def test_dashboard_diagnostics_falls_back_when_operator_queue_cache_is_stale(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        from eta_engine.scripts import jarvis_status

        (tmp_path / "state" / "operator_queue_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                    "status": "blocked",
                    "launch_blocked_count": 1,
                    "operator_queue": {
                        "source": "jarvis_status.operator_queue",
                        "summary": {"BLOCKED": 1, "OBSERVED": 0, "UNKNOWN": 0},
                        "launch_blocked_count": 1,
                        "top_blockers": [{"op_id": "OP-18", "title": "stale install blocker"}],
                        "top_launch_blockers": [{"op_id": "OP-18", "detail": "stale install blocker"}],
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            jarvis_status,
            "build_operator_queue_summary",
            lambda **_kwargs: {
                "source": "operator_action_queue",
                "summary": {"BLOCKED": 3, "OBSERVED": 11, "UNKNOWN": 0},
                "launch_blocked_count": 1,
                "top_blockers": [{"op_id": "OP-19", "title": "IB Gateway API blocked"}],
                "top_launch_blockers": [
                    {
                        "op_id": "OP-19",
                        "detail": "Seed IBC credentials and recover TWS API 4002.",
                    }
                ],
            },
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        queue = r.json()["operator_queue"]
        assert queue["source"] == "operator_action_queue"
        assert queue["cache_status"] == "stale_fallback"
        assert queue["cache_stale"] is False
        assert queue["stale_cache_age_s"] >= 3600
        assert queue["top_launch_blocker_op_id"] == "OP-19"

    def test_dashboard_cross_check_is_route_backed(self, app_client):
        r = app_client.get("/api/dashboard/cross-check")

        assert r.status_code == 200
        data = r.json()
        assert data["dashboard_version"] == "v1"
        assert data["release_stage"] == "pre_beta"
        assert data["source_of_truth"] == "dashboard_cross_check"
        assert data["status"] == "ok"
        assert data["findings"] == []
        assert data["checks"]["route_backed"] is True
        assert data["checks"]["card_summary_match"] is True
        assert data["card_health"]["summary"]["dead"] == 0
        assert "no-store" in r.headers["Cache-Control"]

    def test_dashboard_data_cross_check_is_route_backed(self, app_client):
        r = app_client.get("/api/dashboard/data-cross-check")

        assert r.status_code == 200
        data = r.json()
        assert data["dashboard_version"] == "v1"
        assert data["release_stage"] == "pre_beta"
        assert data["source_of_truth"] == "dashboard_data_cross_check"
        assert data["status"] == "ok"
        assert data["findings"] == []
        assert data["direct"]["bot_fleet"]["bot_total"] == data["diagnostics"]["bot_fleet"]["bot_total"]
        assert data["direct"]["bot_fleet"]["active_bots"] == data["diagnostics"]["bot_fleet"]["active_bots"]
        assert data["direct"]["equity"]["point_count"] == data["diagnostics"]["equity"]["point_count"]
        assert "no-store" in r.headers["Cache-Control"]

    def test_dashboard_diagnostics_includes_bot_strategy_readiness(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_bot_strategy_readiness_summary",
            lambda **_kwargs: {
                "source": "bot_strategy_readiness",
                "status": "ready",
                "summary": {
                    "blocked_data": 0,
                    "can_paper_trade": 10,
                    "can_live_any": False,
                    "launch_lanes": {"live_preflight": 6, "paper_soak": 4},
                },
                "top_actions": [],
            },
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        readiness = r.json()["bot_strategy_readiness"]
        assert readiness["status"] == "ready"
        assert readiness["blocked_data"] == 0
        assert readiness["paper_ready"] == 10
        assert readiness["can_live_any"] is False
        assert readiness["launch_lanes"]["live_preflight"] == 6

    def test_dashboard_diagnostics_carries_bot_blocker_rollup(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "bot_fleet_roster",
            lambda *_args, **_kwargs: {
                "bots": [],
                "confirmed_bots": 4,
                "summary": {
                    "bot_total": 6,
                    "confirmed_bots": 4,
                    "active_bots": 2,
                    "runtime_active_bots": 2,
                    "running_bots": 2,
                    "live_attached_bots": 2,
                    "live_in_trade_bots": 2,
                    "idle_live_bots": 0,
                    "inactive_runtime_bots": 0,
                    "staged_bots": 4,
                    "truth_status": "working",
                    "truth_summary_line": "Live ETA truth: 4/6 bot heartbeat(s) are fresh.",
                    "current_blocked_bots": 3,
                    "current_blocked_kinds": {"daily_kill_switch": 1, "session_gate": 2},
                    "current_blocked_summary_line": (
                        "Current blockers: 3 bot(s) held - 1 daily kill switch, 2 session gate. "
                        "Top: mnq_futures_sage, volume_profile_nq."
                    ),
                    "current_blocked_preview": [
                        {
                            "bot_id": "mnq_futures_sage",
                            "symbol": "MNQ1",
                            "kind": "daily_kill_switch",
                            "summary": "Entries halted by daily kill switch: day_pnl=$-925.50 <= limit=$-900.00",
                            "at": "2026-05-15T13:15:02+00:00",
                        },
                        {
                            "bot_id": "volume_profile_nq",
                            "symbol": "NQ1",
                            "kind": "session_gate",
                            "summary": "Entries paused by session gate: outside_rth",
                            "at": "2026-05-15T13:15:04+00:00",
                        },
                    ],
                    "current_actionable_blocked_bots": 1,
                    "current_actionable_blocked_kinds": {"daily_kill_switch": 1},
                    "current_actionable_blocked_summary_line": (
                        "Actionable blockers: 1 bot(s) - 1 daily kill switch. "
                        "Top: mnq_futures_sage. Awaiting session: 2 bot(s)."
                    ),
                    "current_actionable_blocked_preview": [
                        {
                            "bot_id": "mnq_futures_sage",
                            "symbol": "MNQ1",
                            "kind": "daily_kill_switch",
                            "summary": "Entries halted by daily kill switch: day_pnl=$-925.50 <= limit=$-900.00",
                            "at": "2026-05-15T13:15:02+00:00",
                        },
                    ],
                    "current_session_gated_bots": 2,
                    "current_session_gated_kinds": {"session_gate": 2},
                    "current_session_gated_summary_line": (
                        "Awaiting session: 2 bot(s) - 2 session gate. Top: volume_profile_nq."
                    ),
                    "current_session_gated_preview": [
                        {
                            "bot_id": "volume_profile_nq",
                            "symbol": "NQ1",
                            "kind": "session_gate",
                            "summary": "Entries paused by session gate: outside_rth",
                            "at": "2026-05-15T13:15:04+00:00",
                        },
                    ],
                    "live_broker_probe_mode": "cached_diagnostics",
                },
                "truth_status": "working",
                "source_of_truth": "canonical_state",
            },
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        data = r.json()
        assert data["bot_fleet"]["current_blocked_bots"] == 3
        assert data["bot_fleet"]["current_blocked_kinds"] == {"daily_kill_switch": 1, "session_gate": 2}
        assert data["bot_fleet"]["current_blocked_summary_line"].startswith("Current blockers: 3 bot(s) held")
        assert data["bot_fleet"]["current_blocked_preview"][0]["bot_id"] == "mnq_futures_sage"
        assert data["bot_fleet"]["current_blocked_preview"][1]["summary"] == (
            "Entries paused by session gate: outside_rth"
        )
        assert data["bot_fleet"]["current_actionable_blocked_bots"] == 1
        assert data["bot_fleet"]["current_actionable_blocked_kinds"] == {"daily_kill_switch": 1}
        assert data["bot_fleet"]["current_actionable_blocked_summary_line"].startswith(
            "Actionable blockers: 1 bot(s)"
        )
        assert data["bot_fleet"]["current_session_gated_bots"] == 2
        assert data["bot_fleet"]["current_session_gated_kinds"] == {"session_gate": 2}
        assert data["bot_fleet"]["current_session_gated_summary_line"].startswith("Awaiting session: 2 bot(s)")
        assert data["bot_fleet"]["blocked_summary"].startswith("Actionable blockers: 1 bot(s)")
        assert data["bot_fleet"]["session_gated_bots"] == 2
        assert data["bot_fleet"]["session_gated_kinds"] == {"session_gate": 2}
        assert data["bot_fleet"]["session_gated_summary_line"].startswith("Awaiting session: 2 bot(s)")
        assert data["bot_fleet"]["live_attached_bots"] == 2
        assert data["bot_fleet"]["live_in_trade_bots"] == 2
        assert data["bot_fleet"]["idle_live_bots"] == 0
        assert data["bot_fleet"]["inactive_runtime_bots"] == 0

    def test_dashboard_diagnostics_flags_session_gate_signal_violations(self, app_client, monkeypatch, tmp_path):
        import eta_engine.deploy.scripts.dashboard_api as mod

        class _FakeGate:
            def __init__(self, allowed: bool, reason: str) -> None:
                self.allowed = allowed
                self.reason = reason

            def entries_allowed(self, _now):
                return self.allowed, self.reason

        supervisor_dir = tmp_path / "state" / "jarvis_intel" / "supervisor"
        supervisor_dir.mkdir(parents=True, exist_ok=True)
        (supervisor_dir / "sent_signals.jsonl").write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "bot_id": "mnq_futures_sage",
                            "signal_id": "mnq_sig_1",
                            "sent_at_utc": "2026-05-15T12:28:24+00:00",
                        }
                    ),
                    json.dumps(
                        {
                            "bot_id": "met_sweep_reclaim",
                            "signal_id": "met_sig_1",
                            "sent_at_utc": "2026-05-15T12:29:24+00:00",
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            mod,
            "_session_gate_audit_registry",
            lambda: {
                "mnq_futures_sage": {
                    "symbol": "MNQ1",
                    "strategy_kind": "orb_sage_gated",
                    "timezone_name": "America/Chicago",
                    "rth_start_local": "08:30:00",
                    "rth_end_local": "16:00:00",
                    "eod_cutoff_local": "15:59:00",
                    "gate": _FakeGate(False, "outside_rth"),
                },
                "met_sweep_reclaim": {
                    "symbol": "MET1",
                    "strategy_kind": "confluence_scorecard",
                    "timezone_name": "America/Chicago",
                    "rth_start_local": "00:00:00",
                    "rth_end_local": "23:59:59",
                    "eod_cutoff_local": "23:59:59",
                    "gate": _FakeGate(True, "allowed"),
                },
            },
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        data = r.json()
        audit = data["session_gate_signal_audit"]
        assert audit["status"] == "warn"
        assert audit["ready"] is True
        assert audit["session_gated_records_scanned"] == 2
        assert audit["violations_count"] == 1
        assert audit["violated_bots"] == ["mnq_futures_sage"]
        assert audit["recent_violations"][0]["bot_id"] == "mnq_futures_sage"
        assert audit["recent_violations"][0]["reason"] == "outside_rth"
        assert audit["recent_violations"][0]["sent_at_local"] == "2026-05-15 08:28:24 EDT"
        assert data["checks"]["session_gate_signal_audit_contract"] is True

    def test_dashboard_diagnostics_session_gate_signal_audit_passes_clean_tape(
        self,
        app_client,
        monkeypatch,
        tmp_path,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        class _FakeGate:
            def entries_allowed(self, _now):
                return True, "allowed"

        supervisor_dir = tmp_path / "state" / "jarvis_intel" / "supervisor"
        supervisor_dir.mkdir(parents=True, exist_ok=True)
        (supervisor_dir / "sent_signals.jsonl").write_text(
            json.dumps(
                {
                    "bot_id": "mnq_futures_sage",
                    "signal_id": "mnq_sig_ok",
                    "sent_at_utc": "2026-05-15T14:35:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            mod,
            "_session_gate_audit_registry",
            lambda: {
                "mnq_futures_sage": {
                    "symbol": "MNQ1",
                    "strategy_kind": "orb_sage_gated",
                    "timezone_name": "America/Chicago",
                    "rth_start_local": "08:30:00",
                    "rth_end_local": "16:00:00",
                    "eod_cutoff_local": "15:59:00",
                    "gate": _FakeGate(),
                }
            },
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        audit = r.json()["session_gate_signal_audit"]
        assert audit["status"] == "ok"
        assert audit["ready"] is True
        assert audit["session_gated_records_scanned"] == 1
        assert audit["violations_count"] == 0
        assert audit["violated_bots"] == []
        assert audit["summary_line"].startswith("No recent session-gate violations found")

    def test_dashboard_diagnostics_includes_operator_and_paper_live_rollups(
        self,
        app_client,
        monkeypatch,
        tmp_path,
    ):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_operator_queue_summary",
            lambda **_kwargs: {
                "source": "operator_action_queue",
                "error": None,
                "summary": {"BLOCKED": 1, "OBSERVED": 0, "UNKNOWN": 0},
                "launch_blocked_count": 1,
                "top_blockers": [{"op_id": "OP-19", "title": "Fix unattended launch"}],
                "top_launch_blockers": [
                    {
                        "op_id": "OP-19",
                        "title": "Fix unattended launch",
                        "detail": "Gateway healthy, startup task still drifted.",
                    }
                ],
            },
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-05-06T14:00:00+00:00",
                    "status": "blocked",
                    "critical_ready": False,
                    "paper_ready_bots": 10,
                    "operator_queue_first_launch_blocker_op_id": "OP-19",
                    "operator_queue_first_launch_next_action": "Rewrite ETA-IBGateway task",
                    "gates": [
                        {
                            "name": "op19_gateway_runtime",
                            "passed": False,
                            "detail": "Gateway healthy, startup task still drifted.",
                            "next_action": "Rewrite ETA-IBGateway task",
                        }
                    ],
                }
            )
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        payload = r.json()
        assert payload["operator_queue"]["blocked"] == 1
        assert payload["operator_queue"]["launch_blocked"] == 1
        assert payload["operator_queue"]["top_launch_blocker_op_id"] == "OP-19"
        assert payload["paper_live_transition"]["status"] == "blocked"
        assert payload["paper_live_transition"]["stale_receipt"] is True
        assert payload["paper_live_transition"]["effective_status"] == "stale_receipt"
        assert "stale" in payload["paper_live_transition"]["stale_detail"].lower()
        assert payload["paper_live_transition"]["paper_ready_bots"] == 10
        assert payload["paper_live_transition"]["first_launch_blocker_op_id"] == "OP-19"
        assert payload["paper_live_transition"]["first_failed_gate"]["name"] == "op19_gateway_runtime"

    def test_dashboard_diagnostics_keeps_advisory_queue_separate_from_paper_launch(
        self,
        app_client,
        monkeypatch,
        tmp_path,
    ):
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_operator_queue_summary",
            lambda **_kwargs: {
                "source": "operator_action_queue",
                "error": None,
                "summary": {"BLOCKED": 1, "OBSERVED": 11, "UNKNOWN": 0},
                "launch_blocked_count": 0,
                "first_blocker_op_id": "OP-16",
                "first_next_action": "continue research soak",
                "top_blockers": [
                    {
                        "op_id": "OP-16",
                        "title": "Research candidates need promotion proof",
                        "detail": "4 research candidate bot(s) still below promotion gate.",
                        "evidence": {
                            "launch_blocker": False,
                            "launch_role": "strategy_optimization_backlog",
                            "blocked_bots": [
                                "mbt_overnight_gap",
                                "mbt_rth_orb",
                                "mgc_sweep_reclaim",
                                "mes_sweep_reclaim_v2",
                            ],
                        },
                        "next_actions": [
                            "python -m eta_engine.scripts.paper_live_launch_check --bots mbt_overnight_gap --json",
                            "python -m eta_engine.scripts.paper_live_launch_check --bots mgc_sweep_reclaim --json",
                        ],
                    }
                ],
                "top_launch_blockers": [],
            },
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-05-08T17:35:00+00:00",
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 12,
                    "operator_queue_blocked_count": 1,
                    "operator_queue_launch_blocked_count": 0,
                    "operator_queue_first_blocker_op_id": "OP-16",
                    "operator_queue_first_next_action": "continue research soak",
                    "operator_queue_first_launch_blocker_op_id": None,
                    "operator_queue_first_launch_next_action": None,
                    "gates": [],
                }
            )
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        payload = r.json()
        assert payload["operator_queue"]["blocked"] == 1
        assert payload["operator_queue"]["launch_blocked"] == 0
        assert payload["operator_queue"]["top_blocker_op_id"] == "OP-16"
        assert payload["operator_queue"]["top_blocker_detail"] == (
            "4 research candidate bot(s) still below promotion gate."
        )
        assert payload["operator_queue"]["top_blocker_launch_blocker"] is False
        assert payload["operator_queue"]["top_blocker_launch_role"] == "strategy_optimization_backlog"
        assert payload["operator_queue"]["top_blocker_blocked_bots"] == [
            "mbt_overnight_gap",
            "mbt_rth_orb",
            "mgc_sweep_reclaim",
            "mes_sweep_reclaim_v2",
        ]
        assert payload["operator_queue"]["top_blocker_next_actions"] == [
            "python -m eta_engine.scripts.paper_live_launch_check --bots mbt_overnight_gap --json",
            "python -m eta_engine.scripts.paper_live_launch_check --bots mgc_sweep_reclaim --json",
        ]
        assert payload["operator_queue"]["advisory_count"] == 1
        assert payload["operator_queue"]["advisory_only"] is True
        assert payload["operator_queue"]["top_advisory_op_id"] == "OP-16"
        assert payload["operator_queue"]["top_advisory_detail"] == (
            "4 research candidate bot(s) still below promotion gate."
        )
        assert payload["operator_queue"]["top_advisory_launch_role"] == "strategy_optimization_backlog"
        assert payload["operator_queue"]["top_advisory_blocked_bots"] == [
            "mbt_overnight_gap",
            "mbt_rth_orb",
            "mgc_sweep_reclaim",
            "mes_sweep_reclaim_v2",
        ]
        assert payload["operator_queue"]["top_advisory_next_actions"] == [
            "python -m eta_engine.scripts.paper_live_launch_check --bots mbt_overnight_gap --json",
            "python -m eta_engine.scripts.paper_live_launch_check --bots mgc_sweep_reclaim --json",
        ]
        assert payload["operator_queue"]["top_launch_blocker_op_id"] == ""
        assert payload["paper_live_transition"]["status"] == "ready_to_launch_paper_live"
        assert payload["paper_live_transition"]["critical_ready"] is True
        assert payload["paper_live_transition"]["first_launch_blocker_op_id"] == ""
        assert payload["paper_live_transition"]["first_launch_next_action"] == ""

    def test_dashboard_diagnostics_prefers_fresh_operator_queue_over_stale_paper_cache(
        self,
        app_client,
        tmp_path,
    ):
        (tmp_path / "state" / "operator_queue_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "blocked",
                    "launch_blocked_count": 1,
                    "operator_queue": {
                        "source": "jarvis_status.operator_queue",
                        "summary": {"BLOCKED": 3, "OBSERVED": 11, "UNKNOWN": 0},
                        "launch_blocked_count": 1,
                        "top_blockers": [{"op_id": "OP-19", "title": "IB Gateway API blocked"}],
                        "top_launch_blockers": [
                            {
                                "op_id": "OP-19",
                                "title": "IB Gateway API blocked",
                                "detail": "Seed IBC credentials and recover TWS API 4002.",
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                    "status": "blocked",
                    "critical_ready": False,
                    "paper_ready_bots": 11,
                    "operator_queue_launch_blocked_count": 1,
                    "operator_queue_first_launch_blocker_op_id": "OP-18",
                    "operator_queue_first_launch_next_action": "python -m eta_engine.scripts.runtime_log_smoke --json",
                    "gates": [
                        {
                            "name": "tws_api_4002",
                            "passed": False,
                            "detail": "TWS API 4002 is down.",
                            "next_action": "python -m eta_engine.scripts.tws_watchdog --host 127.0.0.1 --port 4002",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        payload = r.json()
        assert payload["operator_queue"]["top_launch_blocker_op_id"] == "OP-19"
        assert payload["operator_queue"]["cache_stale"] is False
        assert payload["paper_live_transition"]["cache_stale"] is True
        assert payload["paper_live_transition"]["first_launch_blocker_op_id"] == "OP-19"
        assert payload["paper_live_transition"]["first_launch_next_action"] == (
            "Seed IBC credentials and recover TWS API 4002."
        )
        assert payload["paper_live_transition"]["first_failed_gate"]["name"] == "tws_api_4002"

    def test_dashboard_diagnostics_preserves_shared_paper_live_detail_on_stale_non_authoritative_host(
        self,
        app_client,
        tmp_path,
    ):
        authority_action = (
            "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass "
            "-File .\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 -Apply -Role vps"
        )
        (tmp_path / "state" / "operator_queue_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "blocked",
                    "launch_blocked_count": 1,
                    "operator_queue": {
                        "source": "jarvis_status.operator_queue",
                        "summary": {"BLOCKED": 5, "OBSERVED": 12, "UNKNOWN": 0},
                        "launch_blocked_count": 1,
                        "top_blockers": [
                            {
                                "op_id": "OP-19",
                                "title": "Install/configure canonical IB Gateway and recover TWS API 4002",
                                "detail": (
                                    "This host is not the VPS Gateway authority; do not repair, start, "
                                    "or reauth IB Gateway from this desktop. Verify the VPS authority "
                                    "marker and recovery lane on the 24/7 server."
                                ),
                                "next_actions": [
                                    authority_action,
                                    (
                                        "On the VPS only: python -m "
                                        "eta_engine.scripts.ibgateway_reauth_controller --execute"
                                    ),
                                ],
                            }
                        ],
                        "top_launch_blockers": [
                            {
                                "op_id": "OP-19",
                                "title": "Install/configure canonical IB Gateway and recover TWS API 4002",
                                "detail": (
                                    "This host is not the VPS Gateway authority; do not repair, start, "
                                    "or reauth IB Gateway from this desktop. Verify the VPS authority "
                                    "marker and recovery lane on the 24/7 server."
                                ),
                                "next_actions": [
                                    authority_action,
                                    (
                                        "On the VPS only: python -m "
                                        "eta_engine.scripts.ibgateway_reauth_controller --execute"
                                    ),
                                ],
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": (datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
                    "status": "blocked",
                    "critical_ready": False,
                    "non_authoritative_gateway_host": True,
                    "paper_ready_bots": 9,
                    "operator_queue_launch_blocked_count": 1,
                    "operator_queue_first_launch_blocker_op_id": "OP-18",
                    "operator_queue_first_launch_next_action": authority_action,
                    "gates": [
                        {
                            "name": "tws_api_4002",
                            "passed": False,
                            "detail": (
                                "Keep supervisor in paper_sim until TWS/IB Gateway API 4002 is running "
                                "and the watchdog confirms an API handshake."
                            ),
                            "next_action": (
                                "On the VPS only: python -m eta_engine.scripts.tws_watchdog "
                                "--host 127.0.0.1 --port 4002"
                            ),
                            "critical": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        payload = r.json()
        assert payload["paper_live_transition"]["status"] == "blocked"
        assert payload["paper_live_transition"]["effective_status"] == "stale_receipt"
        assert payload["paper_live_transition"]["detail"] == authority_action
        assert payload["paper_live_transition"]["effective_detail"].startswith(
            "paper-live transition cache is stale"
        )
        assert payload["paper_live_transition"]["first_launch_next_action"] == authority_action
        assert payload["paper_live_transition"]["non_authoritative_gateway_host"] is True
        assert payload["paper_live_transition"]["first_failed_gate"]["name"] == "tws_api_4002"

    def test_dashboard_diagnostics_lets_fresh_operator_queue_block_ready_paper_status(
        self,
        app_client,
        tmp_path,
    ):
        queue_action = (
            "Do not unlock new entries: reconcile broker/supervisor positions "
            "(broker-only: MCL, MYM; divergent: MNQ) before clearing the supervisor entry halt"
        )
        (tmp_path / "state" / "operator_queue_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "blocked",
                    "launch_blocked_count": 1,
                    "operator_queue": {
                        "source": "jarvis_status.operator_queue",
                        "summary": {"BLOCKED": 2, "OBSERVED": 11, "UNKNOWN": 0},
                        "launch_blocked_count": 1,
                        "top_blockers": [
                            {
                                "op_id": "OP-20",
                                "title": "Reconcile broker vs supervisor open positions",
                                "detail": "3 broker/supervisor mismatch(es): broker-only: MCL, MYM; divergent: MNQ.",
                                "evidence": {"launch_blocker": True},
                                "next_actions": [queue_action],
                            }
                        ],
                        "top_launch_blockers": [
                            {
                                "op_id": "OP-20",
                                "title": "Reconcile broker vs supervisor open positions",
                                "detail": "3 broker/supervisor mismatch(es): broker-only: MCL, MYM; divergent: MNQ.",
                                "next_actions": [queue_action],
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "effective_status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 9,
                    "gates": [],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        payload = r.json()
        assert payload["operator_queue"]["top_launch_blocker_op_id"] == "OP-20"
        assert payload["paper_live_transition"]["status"] == "ready_to_launch_paper_live"
        assert payload["paper_live_transition"]["effective_status"] == "blocked_by_operator_queue"
        assert payload["paper_live_transition"]["operator_queue_launch_blocked_count"] == 1
        assert payload["paper_live_transition"]["first_launch_blocker_op_id"] == "OP-20"
        assert payload["paper_live_transition"]["first_launch_next_action"].startswith("Do not unlock new entries")

    def test_dashboard_diagnostics_surfaces_effective_bracket_audit_hold(
        self,
        app_client,
        monkeypatch,
        tmp_path,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "bot_fleet_roster",
            lambda *_args, **_kwargs: {
                "bots": [],
                "confirmed_bots": 0,
                "summary": {
                    "bot_total": 12,
                    "active_bots": 3,
                    "truth_status": "live",
                    "paper_live_effective_status": "held_by_bracket_audit",
                    "paper_live_effective_detail": (
                        "paper transition ready, but broker bracket audit blocks prop dry-run"
                    ),
                    "paper_live_held_by_bracket_audit": True,
                    "broker_bracket_missing_count": 1,
                    "broker_bracket_primary_symbol": "MNQM6",
                    "broker_bracket_primary_venue": "ibkr",
                    "broker_bracket_primary_sec_type": "FUT",
                },
            },
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 12,
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        paper = r.json()["paper_live_transition"]
        assert paper["status"] == "ready_to_launch_paper_live"
        assert paper["effective_status"] == "held_by_bracket_audit"
        assert paper["held_by_bracket_audit"] is True
        assert paper["broker_bracket_missing_count"] == 1
        assert paper["broker_bracket_primary_symbol"] == "MNQM6"
        assert paper["broker_bracket_primary_venue"] == "ibkr"
        assert paper["broker_bracket_primary_sec_type"] == "FUT"
        assert paper["effective_detail"].startswith("paper transition ready")

    def test_dashboard_diagnostics_surfaces_daily_loss_shadow_advisory(
        self,
        app_client,
        monkeypatch,
        tmp_path,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda **_kwargs: {"summary": {"BLOCKED": 0, "OBSERVED": 0, "UNKNOWN": 0}, "launch_blocked_count": 0},
        )
        monkeypatch.setattr(
            mod,
            "bot_fleet_roster",
            lambda *_args, **_kwargs: {
                "bots": [],
                "confirmed_bots": 0,
                "summary": {
                    "bot_total": 9,
                    "active_bots": 0,
                    "runtime_active_bots": 0,
                    "running_bots": 0,
                    "staged_bots": 9,
                    "truth_status": "runtime_stopped",
                },
            },
        )
        monkeypatch.setattr(
            mod,
            "_daily_loss_killswitch_snapshot",
            lambda: {
                "source": "daily_loss_killswitch",
                "status": "tripped",
                "tripped": True,
                "disabled": False,
                "today_pnl_usd": -925.50,
                "limit_usd": -900.0,
                "reason": "day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
            },
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "effective_status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 9,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        payload = r.json()
        assert payload["daily_loss_killswitch"]["status"] == "tripped"
        assert payload["daily_loss_killswitch"]["today_pnl_usd"] == -925.50
        paper = payload["paper_live_transition"]
        assert paper["status"] == "ready_to_launch_paper_live"
        assert paper["effective_status"] == "shadow_paper_active"
        assert paper["held_by_daily_loss_stop"] is False
        assert paper["daily_loss_gate_mode"] == "advisory"
        assert paper["daily_loss_advisory_active"] is True
        assert paper["capital_lanes_held_by_daily_loss_stop"] is True
        assert "Shadow paper remains live" in paper["effective_detail"]
        assert "day_pnl=$-925.50" in paper["effective_detail"]
        assert payload["checks"]["daily_loss_killswitch_contract"] is True

    def test_daily_loss_killswitch_snapshot_normalizes_status(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.scripts import daily_loss_killswitch

        monkeypatch.setattr(
            daily_loss_killswitch,
            "killswitch_status",
            lambda: {
                "tripped": True,
                "disabled": False,
                "reason": "day_pnl=$-925.50 <= limit=$-900.00",
                "today_pnl_usd": -925.50,
                "limit_usd": -900.0,
            },
        )

        snapshot = mod._daily_loss_killswitch_snapshot()

        assert snapshot["source"] == "daily_loss_killswitch"
        assert snapshot["status"] == "tripped"
        assert snapshot["tripped"] is True
        assert snapshot["disabled"] is False
        assert snapshot["today_pnl_usd"] == -925.50
        assert snapshot["limit_usd"] == -900.0

    def test_current_bot_block_state_humanizes_session_gate(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        state = mod._current_bot_block_state(
            {
                "last_aggregation_reject_reason": "session_gate:outside_rth",
                "last_aggregation_reject_at": "2026-05-15T12:00:00+00:00",
            }
        )

        assert state["current_block_source"] == "aggregation"
        assert state["current_block_kind"] == "session_gate"
        assert state["current_block_reason"] == "session_gate:outside_rth"
        assert state["current_block_summary"] == "Entries paused by session gate: outside_rth"
        assert state["current_block_at"] == "2026-05-15T12:00:00+00:00"

    def test_dashboard_diagnostics_includes_proxy_watchdog_rollup(
        self,
        app_client,
        tmp_path,
    ):
        (tmp_path / "state" / "dashboard_proxy_watchdog_heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "component": "dashboard_proxy_watchdog",
                    "decision": {
                        "checked_at": datetime.now(UTC).isoformat(),
                        "action": "noop",
                        "task_name": "ETA-Proxy-8421",
                        "probe": {
                            "healthy": True,
                            "url": "http://127.0.0.1:8421/",
                            "status_code": 200,
                            "reason": "ok",
                            "elapsed_ms": 15,
                            "body_len": 77000,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        payload = r.json()
        watchdog = payload["dashboard_proxy_watchdog"]
        assert watchdog["status"] == "ok"
        assert watchdog["fresh"] is True
        assert watchdog["action"] == "noop"
        assert watchdog["task_name"] == "ETA-Proxy-8421"
        assert watchdog["probe_healthy"] is True
        assert watchdog["probe_reason"] == "ok"
        assert watchdog["status_code"] == 200
        assert watchdog["summary"] == "noop: ok"
        assert watchdog["detail"] == "noop: ok"
        assert watchdog["heartbeat_path"].endswith("dashboard_proxy_watchdog_heartbeat.json")
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_status"] == "ok"
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_detail"] == "noop: ok"
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_fresh"] is True
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_action"] == "noop"
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_task_name"] == "ETA-Proxy-8421"
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_probe_healthy"] is True
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_probe_reason"] == "ok"
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_status_code"] == 200
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_elapsed_ms"] == 15
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_heartbeat_age_s"] >= 0
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_checked_age_s"] >= 0
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_checked_at"]
        assert payload["bot_fleet"]["dashboard_proxy_watchdog_heartbeat_ts"]
        assert payload["checks"]["dashboard_proxy_watchdog_contract"] is True

    def test_dashboard_diagnostics_distinguishes_proxy_probe_ok_from_stale_watchdog(
        self,
        app_client,
        tmp_path,
    ):
        stale_ts = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        (tmp_path / "state" / "dashboard_proxy_watchdog_heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": stale_ts,
                    "component": "dashboard_proxy_watchdog",
                    "decision": {
                        "checked_at": stale_ts,
                        "action": "noop",
                        "task_name": "ETA-Proxy-8421",
                        "probe": {
                            "healthy": True,
                            "url": "http://127.0.0.1:8421/",
                            "status_code": 200,
                            "reason": "ok",
                        },
                    },
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        watchdog = r.json()["dashboard_proxy_watchdog"]
        assert watchdog["status"] == "probe_ok_watchdog_stale"
        assert watchdog["fresh"] is False
        assert watchdog["probe_healthy"] is True
        assert watchdog["probe_reason"] == "ok"
        assert "stale" in watchdog["summary"].lower()
        assert "healthy" in watchdog["detail"].lower()

    def test_dashboard_diagnostics_includes_eta_readiness_snapshot_rollup(
        self,
        app_client,
        tmp_path,
    ):
        (tmp_path / "state" / "eta_readiness_snapshot_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": "eta.readiness_snapshot.v1",
                    "checked_at_utc": datetime.now(UTC).isoformat(),
                    "summary": "BLOCKED",
                    "command_center_watchdog": {
                        "issue_status": "dashboard_task_contract_drift",
                        "issue_summary": (
                            "Canonical dashboard runtime task(s) missing: "
                            "ETA-Dashboard-API, ETA-Proxy-8421.; "
                            "findings=state=dashboard_task_contract_drift,endpoints=5,truncated=true."
                        ),
                        "dashboard_task_missing_task_names": [
                            "ETA-Dashboard-API",
                            "ETA-Proxy-8421",
                        ],
                        "local_contract_status": {
                            "status": "upstream_failure",
                        },
                        "operator_next_step": "reload_operator_service",
                        "operator_next_reason": "dashboard_task_contract_drift",
                        "operator_next_command": ".\\scripts\\reload-command-center-admin.cmd -PublicUrl https://ops.evolutionarytradingalgo.com",
                        "operator_next_requires_elevation": True,
                    },
                    "checks": [
                        {
                            "name": "closed_trade_ledger",
                            "status": "OK",
                            "exit_code": 0,
                            "payload": {
                                "closed_trade_count": 43511,
                                "total_realized_pnl": 27173899.25,
                                "win_rate_pct": 28.94,
                                "cumulative_r": 13608.10,
                            },
                        },
                        {
                            "name": "broker_bracket_audit",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "next_action": ("Verify manual broker OCO coverage or flatten current paper exposure."),
                                "position_summary": {
                                    "missing_bracket_count": 1,
                                    "broker_open_position_count": 1,
                                },
                            },
                        },
                        {
                            "name": "prop_live_readiness_gate",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "primary_bot": "volume_profile_mnq",
                                "scope_family": "futures_prop_ladder",
                                "scope_mode": "controlled_prop_dry_run",
                                "scope_note": (
                                    "This gate governs the futures prop-ladder controlled dry-run lane "
                                    "for volume_profile_mnq. Diamond or Wave-25 launch candidacy is "
                                    "tracked separately and can remain NO_GO independently."
                                ),
                                "parallel_launch_surface": "eta_engine.scripts.prop_launch_check",
                                "parallel_launch_scope": "diamond_wave25_launch_readiness",
                                "next_actions": ["Keep volume_profile_mnq in paper_soak until can_live_trade=true."],
                            },
                        },
                        {
                            "name": "prop_strategy_promotion_audit",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "primary_bot": "volume_profile_mnq",
                                "summary": "BLOCKED_PAPER_SOAK",
                                "ready_for_prop_dry_run_review": False,
                                "required_evidence": [
                                    "clear broker_native_brackets to PASS",
                                ],
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        payload = r.json()
        snapshot = payload["eta_readiness_snapshot"]
        assert snapshot["status"] == "blocked"
        assert snapshot["fresh"] is True
        assert snapshot["healthy"] is False
        assert snapshot["check_count"] == 4
        assert snapshot["blocked_count"] == 3
        assert snapshot["ok_count"] == 1
        assert snapshot["primary_blocker"] == "broker_bracket_audit"
        assert snapshot["closed_trade_count"] == 43511
        assert snapshot["total_realized_pnl"] == 27173899.25
        assert snapshot["win_rate_pct"] == 28.94
        assert snapshot["broker_missing_bracket_count"] == 1
        assert snapshot["broker_open_position_count"] == 1
        assert snapshot["brackets_summary"] == "BLOCKED"
        assert snapshot["brackets_next_action"].startswith("Verify manual broker OCO coverage")
        assert snapshot["prop_gate_summary"] == "BLOCKED"
        assert snapshot["prop_gate_primary_action"].startswith("Keep volume_profile_mnq in paper_soak")
        assert snapshot["prop_primary_bot"] == "volume_profile_mnq"
        assert snapshot["prop_gate_scope_family"] == "futures_prop_ladder"
        assert snapshot["prop_gate_scope_mode"] == "controlled_prop_dry_run"
        assert "Diamond or Wave-25 launch candidacy" in snapshot["prop_gate_scope_note"]
        assert snapshot["prop_gate_parallel_launch_surface"] == "eta_engine.scripts.prop_launch_check"
        assert snapshot["prop_gate_parallel_launch_scope"] == "diamond_wave25_launch_readiness"
        assert snapshot["promotion_summary"] == "BLOCKED_PAPER_SOAK"
        assert snapshot["ready_for_prop_dry_run_review"] is False
        assert snapshot["required_evidence"] == ["clear broker_native_brackets to PASS"]
        assert snapshot["promotion_required_evidence"] == ["clear broker_native_brackets to PASS"]
        assert snapshot["primary_action"].startswith("Keep volume_profile_mnq")
        assert snapshot["detail"].startswith("Keep volume_profile_mnq")
        assert snapshot["command_center_issue_status"] == "dashboard_task_contract_drift"
        assert snapshot["command_center_issue_summary"] == (
            "Canonical dashboard runtime task(s) missing: "
            "ETA-Dashboard-API, ETA-Proxy-8421."
        )
        assert snapshot["command_center_operator_next_step"] == "reload_operator_service"
        assert snapshot["command_center_operator_next_reason"] == "dashboard_task_contract_drift"
        assert snapshot["command_center_operator_next_command"].startswith(
            ".\\scripts\\reload-command-center-admin.cmd"
        )
        assert snapshot["command_center_operator_next_requires_elevation"] is True
        assert snapshot["command_center_dashboard_task_missing_task_names"] == [
            "ETA-Dashboard-API",
            "ETA-Proxy-8421",
        ]
        assert snapshot["command_center_local_contract_status"] == "upstream_failure"
        assert payload["bot_fleet"]["command_center_watchdog_status"] == "dashboard_task_contract_drift"
        assert payload["bot_fleet"]["command_center_watchdog_detail"] == (
            "Canonical dashboard runtime task(s) missing: ETA-Dashboard-API, ETA-Proxy-8421."
        )
        assert payload["bot_fleet"]["command_center_watchdog_age_s"] >= 0
        assert payload["bot_fleet"]["command_center_watchdog_healthy"] is False
        assert payload["bot_fleet"]["command_center_watchdog_checked_at"]
        assert payload["bot_fleet"]["command_center_watchdog_next_step"] == "reload_operator_service"
        assert payload["bot_fleet"]["command_center_watchdog_next_reason"] == "dashboard_task_contract_drift"
        assert payload["bot_fleet"]["command_center_watchdog_next_command"].startswith(
            ".\\scripts\\reload-command-center-admin.cmd"
        )
        assert payload["bot_fleet"]["command_center_watchdog_failure_class"] == "stale_service"
        assert payload["bot_fleet"]["command_center_watchdog_operator_contract_state"] == "stale_service"
        assert payload["bot_fleet"]["command_center_watchdog_recommended_action"] == "reload_operator_service"
        assert payload["bot_fleet"]["command_center_watchdog_primary_blocker"] == (
            "dashboard_task_contract_drift"
        )
        assert payload["bot_fleet"]["command_center_watchdog_instruction"] == (
            "Run the launcher and approve the UAC prompt."
        )
        assert payload["bot_fleet"]["command_center_watchdog_action_count"] == 2
        assert payload["bot_fleet"]["command_center_watchdog_follow_up_count"] == 1
        assert payload["bot_fleet"]["command_center_watchdog_dashboard_task_missing_task_names"] == [
            "ETA-Dashboard-API",
            "ETA-Proxy-8421",
        ]
        assert payload["bot_fleet"]["command_center_watchdog_repair_required"] is True
        assert payload["bot_fleet"]["command_center_watchdog_requires_elevation"] is True
        assert payload["bot_fleet"]["command_center_watchdog_watchdog_registered"] is False
        assert payload["bot_fleet"]["command_center_watchdog_watchdog_state"] == "missing"
        assert payload["bot_fleet"]["command_center_watchdog_can_launch_from_desktop"] is True
        assert payload["bot_fleet"]["command_center_watchdog_launch_context"] == "interactive_uac_launcher"
        assert payload["bot_fleet"]["command_center_watchdog_dashboard_task_contract_status"] == "missing_task"
        assert payload["bot_fleet"]["command_center_watchdog_local_contract_status"] == "upstream_failure"
        assert payload["checks"]["eta_readiness_snapshot_contract"] is True

    def test_dashboard_diagnostics_allows_readiness_scheduler_grace(
        self,
        app_client,
        tmp_path,
    ):
        (tmp_path / "state" / "eta_readiness_snapshot_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": "eta.readiness_snapshot.v1",
                    "checked_at_utc": (datetime.now(UTC) - timedelta(seconds=1000)).isoformat(),
                    "summary": "BLOCKED",
                    "checks": [
                        {
                            "name": "closed_trade_ledger",
                            "status": "OK",
                            "exit_code": 0,
                            "payload": {"closed_trade_count": 1},
                        },
                        {
                            "name": "broker_bracket_audit",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "next_action": "Verify broker OCO coverage.",
                                "position_summary": {
                                    "missing_bracket_count": 1,
                                    "broker_open_position_count": 1,
                                },
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        snapshot = r.json()["eta_readiness_snapshot"]
        assert snapshot["status"] == "blocked"
        assert snapshot["fresh"] is True
        assert snapshot["age_s"] >= 1000
        assert snapshot["primary_action"] == "Verify broker OCO coverage."
        assert snapshot["detail"] == "Verify broker OCO coverage."

    def test_dashboard_diagnostics_marks_stale_readiness_detail(
        self,
        app_client,
        tmp_path,
    ):
        (tmp_path / "state" / "eta_readiness_snapshot_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": "eta.readiness_snapshot.v1",
                    "checked_at_utc": (datetime.now(UTC) - timedelta(hours=3)).isoformat(),
                    "summary": "BLOCKED",
                    "checks": [
                        {
                            "name": "broker_bracket_audit",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "next_action": "Old readiness blocker detail.",
                                "position_summary": {
                                    "missing_bracket_count": 1,
                                    "broker_open_position_count": 1,
                                },
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        snapshot = r.json()["eta_readiness_snapshot"]
        assert snapshot["status"] == "stale_receipt"
        assert snapshot["fresh"] is False
        assert "stale" in snapshot["detail"].lower()
        assert ".\\scripts\\eta-readiness-snapshot.ps1" in snapshot["detail"]

    def test_dashboard_diagnostics_prefers_top_level_readiness_receipt_fields(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_live_broker_diagnostic_payload",
            lambda: {
                "ready": True,
                "broker_open_order_count": 258,
                "broker_checked_utc": "2026-05-16T21:59:53+00:00",
            },
        )
        (tmp_path / "state" / "diamond_retune_status_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_diamond_retune_status",
                    "generated_at_utc": "2026-05-16T21:25:28+00:00",
                    "status": "ok",
                    "summary": {},
                    "bots": [],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "eta_readiness_snapshot_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": "eta.readiness_snapshot.v1",
                    "checked_at_utc": datetime.now(UTC).isoformat(),
                    "status": "blocked",
                    "summary": "BLOCKED",
                    "detail": "top-level readiness detail",
                    "primary_blocker": "top_level_primary_blocker",
                    "primary_action": "top-level primary action",
                    "next_actions": ["top-level primary action", "top-level evidence"],
                    "command_center_issue_status": "healthy",
                    "command_center_issue_summary": "Command Center watchdog is healthy.",
                    "command_center_operator_next_step": "none",
                    "command_center_operator_next_command": "",
                    "command_center_operator_next_requires_elevation": False,
                    "command_center_dashboard_task_missing_task_names": [],
                    "public_fallback_reason": "non_authoritative_gateway_host",
                    "public_fallback_blocked_count": 7,
                    "public_fallback_primary_action": "top-level fallback primary action",
                    "public_live_broker_ready": False,
                    "public_live_broker_snapshot_state": "missing",
                    "public_live_broker_snapshot_source": "missing_ibkr_probe_cache",
                    "public_live_broker_source": "cached_live_broker_state_for_diagnostics",
                    "public_live_broker_degraded_display": (
                        "public broker_state degraded: missing_ibkr_probe_cache; "
                        "via cached_live_broker_state_for_diagnostics"
                    ),
                    "dashboard_api_runtime_current_live_broker_state_checked_utc": "",
                    "dashboard_api_runtime_current_live_broker_open_order_count": 0,
                    "dashboard_api_runtime_public_live_broker_degraded_display": "",
                    "dashboard_api_runtime_drift_display": (
                        "8421 master/status is still blank for public_live_broker_degraded_display "
                        "while readiness receipt says public broker_state degraded: "
                        "missing_ibkr_probe_cache; via cached_live_broker_state_for_diagnostics"
                    ),
                    "dashboard_api_runtime_probe_display": (
                        "8421 master/status probe failed: local_dashboard_master_status timed out after 15s"
                    ),
                    "dashboard_api_runtime_refresh_command": (
                        ".\\scripts\\reload-command-center-admin.cmd "
                        "-PublicUrl https://ops.evolutionarytradingalgo.com"
                    ),
                    "dashboard_api_runtime_refresh_requires_elevation": True,
                    "public_live_broker_state_checked_utc": "2026-05-16T20:59:53+00:00",
                    "public_live_broker_open_order_count": 252,
                    "public_fallback_broker_open_order_count": 19,
                    "public_fallback_broker_open_order_drift_display": (
                        "live broker_state reports 252 open orders vs "
                        "public-fallback stale-order audit 19"
                    ),
                    "public_fallback_stale_flat_open_order_count": 19,
                    "public_fallback_stale_flat_open_order_symbols": ["NQM6", "NGM26"],
                    "public_fallback_stale_flat_open_order_display": "19 stale broker orders (NQM6, NGM26)",
                    "public_fallback_stale_flat_open_order_relation_display": (
                        "all 19 broker open orders are stale flat orders"
                    ),
                    "public_live_retune_generated_at_utc": "2026-05-16T20:33:18+00:00",
                    "public_live_retune_focus_active_experiment_outcome_line": (
                        "partial_profit_disabled: awaiting first post-change close"
                    ),
                    "public_live_retune_sync_drift_display": (
                        "public retune truth refreshed at 2026-05-16T20:33:18+00:00 "
                        "after readiness cached 2026-05-16T19:33:18+00:00"
                    ),
                    "dashboard_api_runtime_public_live_retune_generated_at_utc": "",
                    "dashboard_api_runtime_public_live_retune_sync_drift_display": "",
                    "dashboard_api_runtime_retune_drift_display": (
                        "8421 master/status is still blank for public_live_retune_generated_at_utc "
                        "while readiness receipt has 2026-05-16T20:33:18+00:00"
                    ),
                    "local_retune_generated_at_utc": "2026-05-16T20:25:28+00:00",
                    "local_retune_focus_active_experiment_outcome_line": (
                        "partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
                    ),
                    "retune_focus_active_experiment_drift_display": (
                        "public retune says partial_profit_disabled: awaiting first post-change "
                        "close; local mirror says partial_profit_disabled: 1 post-change close "
                        "| R -0.82 | PnL $0.00"
                    ),
                    "public_fallback_brackets_summary": "TOP_LEVEL_FALLBACK_BRACKETS",
                    "promotion_required_evidence": ["top-level evidence"],
                    "public_fallback_brackets_next_action": "top-level fallback bracket action",
                    "public_fallback_prop_gate_summary": "TOP_LEVEL_FALLBACK_PROP_GATE",
                    "public_fallback_prop_gate_primary_action": "top-level fallback prop gate action",
                    "brackets_summary": "TOP_LEVEL_BRACKETS",
                    "brackets_next_action": "top-level bracket action",
                    "prop_gate_summary": "TOP_LEVEL_PROP_GATE",
                    "prop_gate_primary_action": "top-level prop gate action",
                    "prop_primary_bot": "top_level_bot",
                    "promotion_summary": "TOP_LEVEL_PROMOTION",
                    "ready_for_prop_dry_run_review": True,
                    "checks": [
                        {
                            "name": "closed_trade_ledger",
                            "status": "OK",
                            "exit_code": 0,
                            "payload": {"closed_trade_count": 1},
                        },
                        {
                            "name": "broker_bracket_audit",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "summary": "BLOCKED",
                                "next_action": "nested bracket action",
                                "position_summary": {
                                    "missing_bracket_count": 1,
                                    "broker_open_position_count": 1,
                                },
                            },
                        },
                        {
                            "name": "prop_strategy_promotion_audit",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "summary": "BLOCKED_PAPER_SOAK",
                                "ready_for_prop_dry_run_review": False,
                                "required_evidence": ["nested evidence"],
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        payload = r.json()
        snapshot = payload["eta_readiness_snapshot"]
        assert snapshot["command_center_issue_status"] == "healthy"
        assert snapshot["command_center_issue_summary"] == "Command Center watchdog is healthy."
        assert snapshot["command_center_operator_next_step"] == "none"
        assert snapshot["status"] == "blocked"
        assert snapshot["detail"] == "top-level readiness detail"
        assert snapshot["primary_blocker"] == "top_level_primary_blocker"
        assert snapshot["primary_action"] == "top-level primary action"
        assert snapshot["next_actions"][:2] == ["top-level primary action", "top-level evidence"]
        assert snapshot["public_fallback_blocked_count"] == 7
        assert snapshot["public_fallback_primary_action"] == "top-level fallback primary action"
        assert snapshot["public_live_broker_ready"] is False
        assert snapshot["public_live_broker_snapshot_state"] == "missing"
        assert snapshot["public_live_broker_snapshot_source"] == "missing_ibkr_probe_cache"
        assert snapshot["public_live_broker_source"] == "cached_live_broker_state_for_diagnostics"
        assert (
            snapshot["public_live_broker_degraded_display"]
            == "public broker_state degraded: missing_ibkr_probe_cache; "
            "via cached_live_broker_state_for_diagnostics"
        )
        assert snapshot["dashboard_api_runtime_current_live_broker_state_checked_utc"] == "2026-05-16T21:59:53+00:00"
        assert snapshot["dashboard_api_runtime_current_live_broker_open_order_count"] == 258
        assert (
            snapshot["dashboard_api_runtime_public_live_broker_degraded_display"]
            == "public broker_state degraded: missing_ibkr_probe_cache; "
            "via cached_live_broker_state_for_diagnostics"
        )
        assert snapshot["dashboard_api_runtime_drift_display"] == ""
        assert (
            snapshot["dashboard_api_runtime_probe_display"]
            == "8421 master/status probe failed: local_dashboard_master_status timed out after 15s"
        )
        assert (
            snapshot["dashboard_api_runtime_refresh_command"]
            == ".\\scripts\\reload-command-center-admin.cmd "
            "-PublicUrl https://ops.evolutionarytradingalgo.com"
        )
        assert snapshot["dashboard_api_runtime_refresh_requires_elevation"] is True
        assert snapshot["public_live_broker_state_checked_utc"] == "2026-05-16T20:59:53+00:00"
        assert snapshot["public_live_broker_open_order_count"] == 252
        assert snapshot["current_live_broker_state_checked_utc"] == "2026-05-16T21:59:53+00:00"
        assert snapshot["current_live_broker_open_order_count"] == 258
        assert snapshot["current_live_broker_degraded_display"] == ""
        assert (
            snapshot["current_live_broker_open_order_drift_display"]
            == "live broker_state now reports 258 open orders vs readiness cached 19 stale flat orders"
        )
        assert snapshot["public_fallback_broker_open_order_count"] == 19
        assert (
            snapshot["public_fallback_broker_open_order_drift_display"]
            == "live broker_state reports 252 open orders vs public-fallback stale-order audit 19"
        )
        assert snapshot["public_fallback_stale_flat_open_order_count"] == 19
        assert snapshot["public_fallback_stale_flat_open_order_symbols"] == ["NQM6", "NGM26"]
        assert snapshot["public_fallback_stale_flat_open_order_display"] == "19 stale broker orders (NQM6, NGM26)"
        assert (
            snapshot["public_fallback_stale_flat_open_order_relation_display"]
            == "all 19 broker open orders are stale flat orders"
        )
        assert snapshot["public_live_retune_generated_at_utc"] == "2026-05-16T20:33:18+00:00"
        assert (
            snapshot["public_live_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: awaiting first post-change close"
        )
        assert (
            snapshot["public_live_retune_sync_drift_display"]
            == "public retune truth refreshed at 2026-05-16T20:33:18+00:00 "
            "after readiness cached 2026-05-16T19:33:18+00:00"
        )
        assert snapshot["dashboard_api_runtime_public_live_retune_generated_at_utc"] == "2026-05-16T20:33:18+00:00"
        assert (
            snapshot["dashboard_api_runtime_public_live_retune_sync_drift_display"]
            == "public retune truth refreshed at 2026-05-16T20:33:18+00:00 "
            "after readiness cached 2026-05-16T19:33:18+00:00"
        )
        assert snapshot["dashboard_api_runtime_retune_drift_display"] == ""
        assert snapshot["local_retune_generated_at_utc"] == "2026-05-16T20:25:28+00:00"
        assert (
            snapshot["local_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
        )
        assert snapshot["current_local_retune_generated_at_utc"] == "2026-05-16T21:25:28+00:00"
        assert (
            snapshot["local_retune_sync_drift_display"]
            == "local retune snapshot refreshed at 2026-05-16T21:25:28+00:00 "
            "after readiness cached 2026-05-16T20:25:28+00:00"
        )
        assert (
            snapshot["retune_focus_active_experiment_drift_display"]
            == "public retune says partial_profit_disabled: awaiting first post-change close; "
            "local mirror says partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
        )
        assert payload["public_live_retune_generated_at_utc"] == "2026-05-16T20:33:18+00:00"
        assert (
            payload["public_live_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: awaiting first post-change close"
        )
        assert (
            payload["public_live_retune_sync_drift_display"]
            == "public retune truth refreshed at 2026-05-16T20:33:18+00:00 "
            "after readiness cached 2026-05-16T19:33:18+00:00"
        )
        assert payload["dashboard_api_runtime_public_live_retune_generated_at_utc"] == "2026-05-16T20:33:18+00:00"
        assert (
            payload["dashboard_api_runtime_public_live_retune_sync_drift_display"]
            == "public retune truth refreshed at 2026-05-16T20:33:18+00:00 "
            "after readiness cached 2026-05-16T19:33:18+00:00"
        )
        assert payload["dashboard_api_runtime_retune_drift_display"] == ""
        assert payload["local_retune_generated_at_utc"] == "2026-05-16T20:25:28+00:00"
        assert (
            payload["local_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
        )
        assert payload["current_local_retune_generated_at_utc"] == "2026-05-16T21:25:28+00:00"
        assert (
            payload["local_retune_sync_drift_display"]
            == "local retune snapshot refreshed at 2026-05-16T21:25:28+00:00 "
            "after readiness cached 2026-05-16T20:25:28+00:00"
        )
        assert (
            payload["retune_focus_active_experiment_drift_display"]
            == "public retune says partial_profit_disabled: awaiting first post-change close; "
            "local mirror says partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
        )
        assert snapshot["public_fallback_brackets_summary"] == "TOP_LEVEL_FALLBACK_BRACKETS"
        assert snapshot["promotion_required_evidence"] == ["top-level evidence"]
        assert snapshot["public_fallback_brackets_next_action"] == "top-level fallback bracket action"
        assert snapshot["public_fallback_prop_gate_summary"] == "TOP_LEVEL_FALLBACK_PROP_GATE"
        assert snapshot["public_fallback_prop_gate_primary_action"] == "top-level fallback prop gate action"
        assert snapshot["brackets_summary"] == "TOP_LEVEL_BRACKETS"
        assert snapshot["brackets_next_action"] == "top-level bracket action"
        assert snapshot["prop_gate_summary"] == "TOP_LEVEL_PROP_GATE"
        assert snapshot["prop_gate_primary_action"] == "top-level prop gate action"
        assert snapshot["prop_primary_bot"] == "top_level_bot"
        assert snapshot["promotion_summary"] == "TOP_LEVEL_PROMOTION"
        assert snapshot["ready_for_prop_dry_run_review"] is True

    def test_dashboard_diagnostics_uses_public_broker_state_when_local_cache_missing(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_live_broker_diagnostic_payload",
            lambda: {
                "ready": False,
                "status": "unavailable",
                "broker_probe_skipped": True,
                "broker_snapshot_state": "missing",
            },
        )
        monkeypatch.setattr(
            mod,
            "_public_operator_broker_state_payload",
            lambda **_kwargs: {
                "ibkr": {
                    "open_order_count": 261,
                    "checked_utc": "2026-05-16T22:09:53+00:00",
                }
            },
        )
        (tmp_path / "state" / "diamond_retune_status_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_diamond_retune_status",
                    "generated_at_utc": "2026-05-16T21:25:28+00:00",
                    "status": "ok",
                    "summary": {},
                    "bots": [],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "eta_readiness_snapshot_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": "eta.readiness_snapshot.v1",
                    "checked_at_utc": datetime.now(UTC).isoformat(),
                    "summary": "BLOCKED",
                    "public_fallback_active": True,
                    "public_fallback_reason": "non_authoritative_gateway_host",
                    "public_live_broker_open_order_count": 255,
                    "public_fallback_stale_flat_open_order_count": 255,
                    "checks": [],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        snapshot = r.json()["eta_readiness_snapshot"]
        assert snapshot["current_live_broker_state_checked_utc"] == "2026-05-16T22:09:53+00:00"
        assert snapshot["current_live_broker_open_order_count"] == 261
        assert snapshot["current_live_broker_degraded_display"] == ""
        assert (
            snapshot["current_live_broker_open_order_drift_display"]
            == "live broker_state now reports 261 open orders vs readiness cached 255 stale flat orders"
        )

    def test_dashboard_diagnostics_surfaces_current_public_broker_degraded_state_when_live_count_missing(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_live_broker_diagnostic_payload",
            lambda: {
                "ready": False,
                "status": "unavailable",
                "broker_probe_skipped": True,
                "broker_snapshot_state": "missing",
            },
        )
        monkeypatch.setattr(
            mod,
            "_public_operator_broker_state_payload",
            lambda **_kwargs: {
                "ready": False,
                "source": "cached_live_broker_state_for_diagnostics",
                "broker_snapshot_state": "missing",
                "broker_snapshot_source": "missing_ibkr_probe_cache",
            },
        )
        (tmp_path / "state" / "diamond_retune_status_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_diamond_retune_status",
                    "generated_at_utc": "2026-05-16T21:25:28+00:00",
                    "status": "ok",
                    "summary": {},
                    "bots": [],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "eta_readiness_snapshot_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": "eta.readiness_snapshot.v1",
                    "checked_at_utc": datetime.now(UTC).isoformat(),
                    "summary": "BLOCKED",
                    "public_fallback_active": True,
                    "public_fallback_reason": "non_authoritative_gateway_host",
                    "public_live_broker_ready": False,
                    "public_live_broker_snapshot_state": "warm",
                    "public_live_broker_snapshot_source": "ibkr_probe_cache",
                    "public_live_broker_source": "cached_live_broker_state_for_diagnostics",
                    "public_live_broker_degraded_display": (
                        "public broker_state degraded: ibkr_probe_cache; "
                        "via cached_live_broker_state_for_diagnostics"
                    ),
                    "checks": [],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        snapshot = r.json()["eta_readiness_snapshot"]
        assert snapshot["current_live_broker_state_checked_utc"] == ""
        assert snapshot["current_live_broker_open_order_count"] == 0
        assert (
            snapshot["current_live_broker_degraded_display"]
            == "live broker_state now degraded: missing_ibkr_probe_cache; "
            "via cached_live_broker_state_for_diagnostics"
        )
        assert snapshot["current_live_broker_open_order_drift_display"] == ""

    def test_public_operator_broker_state_payload_sets_user_agent(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        captured: dict[str, object] = {}

        class _FakeResponse:
            def __enter__(self) -> _FakeResponse:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def read(self):
                return (
                    b'{"ibkr":{"open_order_count":261,"checked_utc":"2026-05-16T22:09:53+00:00"}}'
                )

        def fake_urlopen(request, timeout=0.0):
            captured["url"] = request.full_url
            captured["headers"] = {key.lower(): value for key, value in request.header_items()}
            captured["timeout"] = timeout
            return _FakeResponse()

        monkeypatch.setenv("ETA_OPERATOR_URL", "https://ops.evolutionarytradingalgo.com")
        monkeypatch.setattr(mod.urllib_request, "urlopen", fake_urlopen)
        with mod._PUBLIC_OPERATOR_BROKER_STATE_CACHE_LOCK:
            mod._PUBLIC_OPERATOR_BROKER_STATE_CACHE["payload"] = None
            mod._PUBLIC_OPERATOR_BROKER_STATE_CACHE["ts"] = 0.0

        payload = mod._public_operator_broker_state_payload(force=True)

        assert payload["ibkr"]["open_order_count"] == 261
        assert captured["url"] == "https://ops.evolutionarytradingalgo.com/api/live/broker_state"
        assert captured["timeout"] == 5.0
        assert captured["headers"]["accept"] == "application/json"
        assert captured["headers"]["user-agent"] == "ETA-Operator/1.0"

    def test_public_operator_diamond_retune_status_payload_sets_user_agent(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        captured: dict[str, object] = {}

        class _FakeResponse:
            def __enter__(self) -> _FakeResponse:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def read(self):
                return (
                    b'{"generated_at_utc":"2026-05-17T01:25:18+00:00",'
                    b'"focus_active_experiment_outcome_line":"partial_profit_disabled: '
                    b'1 post-change close | R -0.82 | PnL $0.00"}'
                )

        def fake_urlopen(request, timeout=0.0):
            captured["url"] = request.full_url
            captured["headers"] = {key.lower(): value for key, value in request.header_items()}
            captured["timeout"] = timeout
            return _FakeResponse()

        monkeypatch.setenv("ETA_OPERATOR_URL", "https://ops.evolutionarytradingalgo.com")
        monkeypatch.setattr(mod.urllib_request, "urlopen", fake_urlopen)
        with mod._PUBLIC_OPERATOR_RETUNE_STATUS_CACHE_LOCK:
            mod._PUBLIC_OPERATOR_RETUNE_STATUS_CACHE["payload"] = None
            mod._PUBLIC_OPERATOR_RETUNE_STATUS_CACHE["ts"] = 0.0

        payload = mod._public_operator_diamond_retune_status_payload(force=True)

        assert payload["generated_at_utc"] == "2026-05-17T01:25:18+00:00"
        assert captured["url"] == "https://ops.evolutionarytradingalgo.com/api/jarvis/diamond_retune_status"
        assert captured["timeout"] == 5.0
        assert captured["headers"]["accept"] == "application/json"
        assert captured["headers"]["user-agent"] == "ETA-Operator/1.0"

    def test_dashboard_diagnostics_surfaces_current_public_retune_drift_when_receipt_is_stale(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_public_operator_diamond_retune_status_payload",
            lambda **_kwargs: {
                "generated_at_utc": "2026-05-17T01:25:18+00:00",
                "focus_active_experiment_outcome_line": (
                    "partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
                ),
            },
        )
        monkeypatch.setattr(
            mod,
            "_public_operator_broker_state_payload",
            lambda **_kwargs: {},
        )
        (tmp_path / "state" / "diamond_retune_status_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_diamond_retune_status",
                    "generated_at_utc": "2026-05-17T01:25:18+00:00",
                    "status": "ok",
                    "summary": {},
                    "bots": [],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "eta_readiness_snapshot_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": "eta.readiness_snapshot.v1",
                    "checked_at_utc": "2026-05-16T20:25:26+00:00",
                    "summary": "BLOCKED",
                    "public_live_retune_generated_at_utc": "2026-05-17T00:25:26+00:00",
                    "public_live_retune_focus_active_experiment_outcome_line": (
                        "partial_profit_disabled: awaiting first post-change close"
                    ),
                    "checks": [],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        snapshot = r.json()["eta_readiness_snapshot"]
        assert snapshot["fresh"] is False
        assert snapshot["current_live_retune_generated_at_utc"] == "2026-05-17T01:25:18+00:00"
        assert (
            snapshot["current_live_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
        )
        assert (
            snapshot["current_live_retune_sync_drift_display"]
            == "public retune truth now refreshed at 2026-05-17T01:25:18+00:00 "
            "after readiness cached 2026-05-17T00:25:26+00:00"
        )

    def test_dashboard_diagnostics_promotes_public_fallback_readiness_action(
        self,
        app_client,
        tmp_path,
    ):
        (tmp_path / "state" / "eta_readiness_snapshot_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": "eta.readiness_snapshot.v1",
                    "checked_at_utc": datetime.now(UTC).isoformat(),
                    "summary": "BLOCKED",
                    "public_fallback_reason": "local_fleet_truth_unavailable",
                    "public_fallback_checks": [
                        {
                            "name": "broker_bracket_audit_public_fallback",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "next_action": ("5 broker bracket-required positions missing broker-native OCO."),
                                "position_summary": {
                                    "missing_bracket_count": 5,
                                    "broker_open_position_count": 6,
                                },
                            },
                        },
                        {
                            "name": "prop_live_readiness_gate_public_fallback",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "next_actions": ["Keep volume_profile_mnq in paper_soak until bracket audit clears."],
                            },
                        },
                    ],
                    "checks": [
                        {
                            "name": "closed_trade_ledger",
                            "status": "OK",
                            "exit_code": 0,
                            "payload": {
                                "closed_trade_count": 43511,
                                "total_realized_pnl": 27173899.25,
                                "win_rate_pct": 28.94,
                            },
                        },
                        {
                            "name": "broker_bracket_audit",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "summary": "BLOCKED_FLEET_TRUTH_UNAVAILABLE",
                                "position_summary": {
                                    "missing_bracket_count": 0,
                                    "broker_open_position_count": 0,
                                },
                            },
                        },
                        {
                            "name": "prop_live_readiness_gate",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "primary_bot": "volume_profile_mnq",
                                "next_actions": ["Restore live /api/bot-fleet position truth."],
                            },
                        },
                        {
                            "name": "prop_strategy_promotion_audit",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "primary_bot": "volume_profile_mnq",
                                "summary": "BLOCKED_PAPER_SOAK",
                                "ready_for_prop_dry_run_review": False,
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        snapshot = r.json()["eta_readiness_snapshot"]
        assert snapshot["status"] == "blocked"
        assert snapshot["public_fallback_active"] is True
        assert snapshot["public_fallback_reason"] == "local_fleet_truth_unavailable"
        assert snapshot["public_fallback_blocked_count"] == 2
        assert snapshot["primary_blocker"] == "broker_bracket_audit_public_fallback"
        assert snapshot["primary_action"].startswith("5 broker bracket-required")
        assert snapshot["next_actions"][0].startswith("5 broker bracket-required")
        assert snapshot["broker_missing_bracket_count"] == 5
        assert snapshot["broker_open_position_count"] == 6
        assert snapshot["public_fallback_brackets_summary"] == "BLOCKED"
        assert snapshot["public_fallback_brackets_next_action"].startswith("5 broker bracket-required")
        assert snapshot["public_fallback_prop_gate_summary"] == "BLOCKED"
        assert snapshot["public_fallback_prop_gate_primary_action"].startswith(
            "Keep volume_profile_mnq in paper_soak"
        )

    def test_dashboard_diagnostics_activates_public_fallback_for_non_authoritative_gateway_host(
        self,
        app_client,
        tmp_path,
    ):
        (tmp_path / "state" / "eta_readiness_snapshot_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": "eta.readiness_snapshot.v1",
                    "checked_at_utc": datetime.now(UTC).isoformat(),
                    "summary": "BLOCKED",
                    "non_authoritative_gateway_host": True,
                    "public_fallback_reason": "non_authoritative_gateway_host",
                    "public_fallback_checks": [
                        {
                            "name": "broker_bracket_audit_public_fallback",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "next_action": "2 broker bracket-required positions missing broker-native OCO.",
                                "position_summary": {
                                    "missing_bracket_count": 2,
                                    "broker_open_position_count": 3,
                                },
                            },
                        },
                        {
                            "name": "prop_live_readiness_gate_public_fallback",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "next_actions": [
                                    "Keep volume_profile_mnq in paper_soak until public bracket audit clears."
                                ],
                            },
                        },
                    ],
                    "checks": [
                        {
                            "name": "closed_trade_ledger",
                            "status": "OK",
                            "exit_code": 0,
                            "payload": {
                                "closed_trade_count": 43511,
                                "total_realized_pnl": 27173899.25,
                                "win_rate_pct": 28.94,
                            },
                        },
                        {
                            "name": "broker_bracket_audit",
                            "status": "READY_NO_OPEN_EXPOSURE",
                            "exit_code": 0,
                            "payload": {
                                "summary": "READY_NO_OPEN_EXPOSURE",
                                "position_summary": {
                                    "missing_bracket_count": 0,
                                    "broker_open_position_count": 0,
                                },
                            },
                        },
                        {
                            "name": "prop_live_readiness_gate",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "primary_bot": "volume_profile_mnq",
                                "next_actions": [
                                    "On the VPS only: restore the authoritative paper-live gateway runtime."
                                ],
                            },
                        },
                        {
                            "name": "prop_strategy_promotion_audit",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "primary_bot": "volume_profile_mnq",
                                "summary": "BLOCKED_PAPER_SOAK",
                                "ready_for_prop_dry_run_review": False,
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        snapshot = r.json()["eta_readiness_snapshot"]
        assert snapshot["status"] == "blocked"
        assert snapshot["non_authoritative_gateway_host"] is True
        assert snapshot["public_fallback_active"] is True
        assert snapshot["public_fallback_reason"] == "non_authoritative_gateway_host"
        assert snapshot["public_fallback_blocked_count"] == 2
        assert snapshot["primary_blocker"] == "broker_bracket_audit_public_fallback"
        assert snapshot["primary_action"].startswith("2 broker bracket-required")
        assert snapshot["next_actions"][0].startswith("2 broker bracket-required")
        assert snapshot["broker_missing_bracket_count"] == 2
        assert snapshot["broker_open_position_count"] == 3
        assert snapshot["public_fallback_brackets_summary"] == "BLOCKED"
        assert snapshot["public_fallback_brackets_next_action"].startswith("2 broker bracket-required")
        assert snapshot["public_fallback_prop_gate_summary"] == "BLOCKED"
        assert snapshot["public_fallback_prop_gate_primary_action"].startswith(
            "Keep volume_profile_mnq in paper_soak"
        )

    def test_dashboard_diagnostics_includes_command_center_watchdog_rollup(
        self,
        app_client,
        tmp_path,
    ):
        receipt_path = tmp_path / "state" / "command_center_doctor_latest.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": "eta.command_center.doctor.v1",
                    "checked_at": datetime.now(UTC).isoformat(),
                    "healthy": False,
                    "failure_class": "stale_service",
                    "operator_contract_state": "stale_service",
                    "recommended_action": "reload_operator_service",
                    "repair_required": True,
                    "operator_action": {
                        "step": "reload_operator_service",
                        "reason": "stale_service",
                        "command": ".\\scripts\\reload-command-center-admin.cmd -PublicUrl https://ops.evolutionarytradingalgo.com",
                        "requires_elevation": True,
                    },
                    "failure_summary": {
                        "endpoint_failures": 1,
                        "contract_findings": 1,
                    },
                }
            ),
            encoding="utf-8",
        )
        status_path = tmp_path / "state" / "command_center_watchdog_status_latest.json"
        status_path.write_text(
            json.dumps(
                {
                    "issue_status": "dashboard_task_contract_drift",
                    "issue_summary": (
                        "Canonical dashboard runtime task(s) missing: "
                        "ETA-Dashboard-API, ETA-Proxy-8421."
                    ),
                    "primary_blocker": "dashboard_task_contract_drift",
                    "local_contract_status": {
                        "status": "upstream_failure",
                        "summary": "Local 8421 is reachable, but upstream is returning HTTP 5xx.",
                    },
                    "dashboard_task_contract_status": {
                        "status": "missing_task",
                        "summary": (
                            "Canonical dashboard runtime task(s) missing: "
                            "ETA-Dashboard-API, ETA-Proxy-8421."
                        ),
                        "missing_task_names": ["ETA-Dashboard-API", "ETA-Proxy-8421"],
                    },
                    "firm_command_center_dependency_gap_status": {
                        "status": "missing_module",
                        "missing_module": "portalocker",
                    },
                    "operator_action_plan": [
                        {
                            "role": "primary",
                            "step": "reload_operator_service",
                            "command": ".\\scripts\\reload-command-center-admin.cmd -PublicUrl https://ops.evolutionarytradingalgo.com",
                        },
                        {
                            "role": "follow_up",
                            "step": "register_watchdog",
                            "command": (
                                "powershell -ExecutionPolicy Bypass -File "
                                ".\\scripts\\register-command-center-watchdog.ps1 -RunNow"
                            ),
                        },
                    ],
                    "operator_action_count": 2,
                    "operator_follow_up_actions": [
                        {
                            "step": "register_watchdog",
                            "reason": "watchdog_missing",
                        }
                    ],
                    "operator_follow_up_count": 1,
                    "operator_next_can_launch_from_desktop": True,
                    "operator_next_launch_context": "interactive_uac_launcher",
                    "operator_next_instruction": "Run the launcher and approve the UAC prompt.",
                    "watchdog_registered": False,
                    "watchdog_state": "missing",
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        payload = r.json()
        watchdog = payload["command_center_watchdog"]
        assert watchdog["status"] == "missing_watchdog"
        assert watchdog["fresh"] is True
        assert watchdog["failure_class"] == "stale_service"
        assert watchdog["operator_contract_state"] == "stale_service"
        assert watchdog["next_reason"] == "dashboard_task_contract_drift"
        assert watchdog["next_step"] == "reload_operator_service"
        assert watchdog["operator_next_step"] == "reload_operator_service"
        assert watchdog["operator_next_reason"] == "dashboard_task_contract_drift"
        assert watchdog["operator_next_command"].startswith(
            ".\\scripts\\reload-command-center-admin.cmd"
        )
        assert watchdog["recommended_action"] == "reload_operator_service"
        assert watchdog["repair_required"] is True
        assert watchdog["requires_elevation"] is True
        assert watchdog["operator_next_requires_elevation"] is True
        assert watchdog["receipt_path"].endswith("command_center_doctor_latest.json")
        assert watchdog["status_receipt_path"].endswith("command_center_watchdog_status_latest.json")
        assert watchdog["action_count"] == 2
        assert watchdog["follow_up_count"] == 1
        assert watchdog["follow_up_actions"][0]["step"] == "register_watchdog"
        assert watchdog["watchdog_registered"] is False
        assert watchdog["watchdog_state"] == "missing"
        assert watchdog["can_launch_from_desktop"] is True
        assert watchdog["launch_context"] == "interactive_uac_launcher"
        assert watchdog["issue_status"] == "dashboard_task_contract_drift"
        assert watchdog["issue_summary"].startswith("Canonical dashboard runtime task(s) missing")
        assert watchdog["primary_blocker"] == "dashboard_task_contract_drift"
        assert watchdog["local_contract_status"]["status"] == "upstream_failure"
        assert watchdog["dashboard_task_contract_status"]["status"] == "missing_task"
        assert watchdog["dashboard_task_missing_task_names"] == [
            "ETA-Dashboard-API",
            "ETA-Proxy-8421",
        ]
        assert watchdog["firm_command_center_dependency_gap_status"]["missing_module"] == "portalocker"
        assert watchdog["summary_line"] == watchdog["summary"]
        assert payload["checks"]["command_center_watchdog_contract"] is True

    def test_dashboard_diagnostics_accepts_dashboard_task_contract_drift_watchdog_status(
        self,
        app_client,
        tmp_path,
    ):
        receipt_path = tmp_path / "state" / "command_center_doctor_latest.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": "eta.command_center.doctor.v1",
                    "checked_at": datetime.now(UTC).isoformat(),
                    "healthy": False,
                    "failure_class": "dashboard_task_contract_drift",
                    "operator_contract_state": "dashboard_task_contract_drift",
                    "recommended_action": "reload_operator_service",
                    "repair_required": True,
                    "operator_action": {
                        "step": "reload_operator_service",
                        "reason": "dashboard_task_contract_drift",
                        "command": ".\\scripts\\reload-command-center-admin.cmd -PublicUrl https://ops.evolutionarytradingalgo.com",
                        "requires_elevation": True,
                    },
                }
            ),
            encoding="utf-8",
        )
        status_path = tmp_path / "state" / "command_center_watchdog_status_latest.json"
        status_path.write_text(
            json.dumps(
                {
                    "overall_status": "degraded",
                    "effective_status": "dashboard_task_contract_drift",
                    "primary_blocker": "dashboard_task_contract_drift",
                    "issue_status": "dashboard_task_contract_drift",
                    "issue_summary": (
                        "Canonical dashboard runtime task(s) missing: "
                        "ETA-Dashboard-API, ETA-Proxy-8421.; "
                        "findings=state=dashboard_task_contract_drift,endpoints=5,truncated=true."
                    ),
                    "operator_issue_status": "dashboard_task_contract_drift",
                    "operator_next_step": "reload_operator_service",
                    "operator_next_reason": "dashboard_task_contract_drift",
                    "operator_next_command": ".\\scripts\\reload-command-center-admin.cmd -PublicUrl https://ops.evolutionarytradingalgo.com",
                    "operator_next_requires_elevation": True,
                    "watchdog_registered": True,
                    "watchdog_state": "Running",
                    "local_contract_status": {
                        "status": "upstream_failure",
                        "summary": "Local 8421 is reachable, but upstream is returning HTTP 5xx.",
                    },
                    "dashboard_task_contract_status": {
                        "status": "missing_task",
                        "summary": (
                            "Canonical dashboard runtime task(s) missing: "
                            "ETA-Dashboard-API, ETA-Proxy-8421."
                        ),
                        "missing_task_names": ["ETA-Dashboard-API", "ETA-Proxy-8421"],
                    },
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        payload = r.json()
        watchdog = payload["command_center_watchdog"]
        assert watchdog["status"] == "dashboard_task_contract_drift"
        assert watchdog["healthy"] is False
        assert watchdog["next_reason"] == "dashboard_task_contract_drift"
        assert watchdog["next_step"] == "reload_operator_service"
        assert watchdog["operator_next_step"] == "reload_operator_service"
        assert watchdog["operator_next_reason"] == "dashboard_task_contract_drift"
        assert watchdog["operator_next_command"].startswith(
            ".\\scripts\\reload-command-center-admin.cmd"
        )
        assert watchdog["operator_next_requires_elevation"] is True
        assert watchdog["issue_status"] == "dashboard_task_contract_drift"
        assert watchdog["display_issue_summary"] == (
            "Canonical dashboard runtime task(s) missing: "
            "ETA-Dashboard-API, ETA-Proxy-8421."
        )
        assert watchdog["display_summary"] == watchdog["display_issue_summary"]
        assert "findings=state=dashboard_task_contract_drift" in watchdog["issue_summary"]
        assert watchdog["summary_line"] == watchdog["summary"]
        assert watchdog["dashboard_task_missing_task_names"] == [
            "ETA-Dashboard-API",
            "ETA-Proxy-8421",
        ]
        assert payload["checks"]["command_center_watchdog_contract"] is True

    def test_dashboard_diagnostics_uses_status_receipt_when_doctor_receipt_stale(
        self,
        app_client,
        tmp_path,
    ):
        receipt_path = tmp_path / "state" / "command_center_doctor_latest.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": "eta.command_center.doctor.v1",
                    "checked_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                    "healthy": True,
                    "failure_class": "healthy",
                    "failure_detail": "Canonical dashboard runtime task(s) missing: ETA-Dashboard-API, ETA-Proxy-8421.",
                    "dashboard_task_contract": {
                        "status": "missing_task",
                        "summary": (
                            "Canonical dashboard runtime task(s) missing: "
                            "ETA-Dashboard-API, ETA-Proxy-8421."
                        ),
                        "missing_task_names": ["ETA-Dashboard-API", "ETA-Proxy-8421"],
                    },
                    "local_dependency_gap": {
                        "status": "missing_module",
                        "summary": "FirmCommandCenter mirror environment is missing module portalocker.",
                        "missing_module": "portalocker",
                    },
                    "operator_contract_state": "healthy",
                    "recommended_action": "none",
                    "repair_required": False,
                    "operator_action": {
                        "step": "none",
                        "reason": "healthy",
                        "command": None,
                        "requires_elevation": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        status_path = tmp_path / "state" / "command_center_watchdog_status_latest.json"
        status_path.write_text(
            json.dumps(
                {
                    "effective_status": "public_tunnel_token_rejected",
                    "primary_blocker": "public_tunnel_token_rejected",
                    "issue_summary": (
                        "Command Center watchdog is critical: public_tunnel_token_rejected; "
                        "next=repair_public_tunnel_token."
                    ),
                    "operator_next_step": "repair_public_tunnel_token",
                    "operator_next_reason": "public_tunnel_token_rejected",
                    "operator_next_command": ".\\scripts\\repair-public-tunnel-admin.cmd",
                    "operator_next_requires_elevation": True,
                    "watchdog_registered": False,
                    "watchdog_state": "missing",
                    "operator_action_plan": [
                        {
                            "role": "primary",
                            "step": "repair_public_tunnel_token",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        watchdog = r.json()["command_center_watchdog"]
        assert watchdog["status"] == "stale_receipt"
        assert watchdog["fresh"] is False
        assert watchdog["recommended_action"] == "repair_public_tunnel_token"
        assert watchdog["next_step"] == "repair_public_tunnel_token"
        assert watchdog["next_command"] == ".\\scripts\\repair-public-tunnel-admin.cmd"
        assert watchdog["repair_required"] is True
        assert watchdog["requires_elevation"] is True
        assert (
            watchdog["failure_detail"]
            == "Canonical dashboard runtime task(s) missing: ETA-Dashboard-API, ETA-Proxy-8421."
        )
        assert watchdog["issue_summary"].startswith(
            "Command Center watchdog is critical: public_tunnel_token_rejected; next=repair_public_tunnel_token."
        )
        assert "Canonical dashboard runtime task(s) missing: ETA-Dashboard-API, ETA-Proxy-8421." in watchdog[
            "issue_summary"
        ]
        assert "Canonical dashboard runtime task(s) missing: ETA-Dashboard-API, ETA-Proxy-8421." in watchdog["summary"]
        assert "public_tunnel_token_rejected" in watchdog["summary"]
        assert "next=repair_public_tunnel_token" in watchdog["summary"]
        assert watchdog["dashboard_task_contract_status"]["status"] == "missing_task"
        assert watchdog["dashboard_task_missing_task_names"] == [
            "ETA-Dashboard-API",
            "ETA-Proxy-8421",
        ]
        assert watchdog["firm_command_center_dependency_gap_status"]["missing_module"] == "portalocker"

    def test_dashboard_diagnostics_uses_fresh_degraded_watchdog_status_receipt(
        self,
        app_client,
        tmp_path,
    ):
        receipt_path = tmp_path / "state" / "command_center_doctor_latest.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": "eta.command_center.doctor.v1",
                    "checked_at": datetime.now(UTC).isoformat(),
                    "healthy": True,
                    "failure_class": "healthy",
                    "operator_contract_state": "healthy",
                    "recommended_action": "none",
                    "repair_required": False,
                    "operator_action": {
                        "step": "none",
                        "reason": "healthy",
                        "command": None,
                        "requires_elevation": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        status_path = tmp_path / "state" / "command_center_watchdog_status_latest.json"
        status_path.write_text(
            json.dumps(
                {
                    "overall_status": "degraded",
                    "effective_status": "public_tunnel_token_rejected",
                    "primary_blocker": "public_tunnel_token_rejected",
                    "operator_issue_status": "public_tunnel_token_rejected",
                    "operator_next_step": "repair_public_tunnel_token",
                    "operator_next_reason": "public_tunnel_token_rejected",
                    "operator_next_command": ".\\scripts\\repair-public-tunnel-admin.cmd",
                    "operator_next_requires_elevation": True,
                    "watchdog_registered": True,
                    "watchdog_state": "Running",
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        watchdog = r.json()["command_center_watchdog"]
        assert watchdog["status"] == "public_tunnel_token_rejected"
        assert watchdog["healthy"] is False
        assert watchdog["fresh"] is True
        assert watchdog["recommended_action"] == "repair_public_tunnel_token"
        assert watchdog["next_step"] == "repair_public_tunnel_token"
        assert watchdog["next_command"] == ".\\scripts\\repair-public-tunnel-admin.cmd"
        assert watchdog["repair_required"] is True
        assert watchdog["requires_elevation"] is True
        assert "public_tunnel_token_rejected" in watchdog["summary"]

    def test_dashboard_diagnostics_prefers_fresh_healthy_watchdog_status_receipt(
        self,
        app_client,
        tmp_path,
    ):
        receipt_path = tmp_path / "state" / "command_center_doctor_latest.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": "eta.command_center.doctor.v1",
                    "checked_at": datetime.now(UTC).isoformat(),
                    "healthy": False,
                    "failure_class": "contract_failure",
                    "operator_contract_state": "contract_failure",
                    "recommended_action": "inspect_operator_contract",
                    "repair_required": True,
                    "operator_action": {
                        "step": "inspect_operator_contract",
                        "reason": "contract_failure",
                        "command": ".\\scripts\\command-center-doctor.ps1 -Repair",
                        "requires_elevation": True,
                    },
                }
            ),
            encoding="utf-8",
        )
        status_path = tmp_path / "state" / "command_center_watchdog_status_latest.json"
        status_path.write_text(
            json.dumps(
                {
                    "checked_at": datetime.now(UTC).isoformat(),
                    "overall_status": "healthy",
                    "effective_status": "healthy",
                    "primary_blocker": "none",
                    "issue_status": "healthy",
                    "issue_summary": "Command Center watchdog is healthy.",
                    "display_summary": "Command Center watchdog is healthy.",
                    "operator_issue_status": "healthy",
                    "operator_next_step": "none",
                    "operator_next_reason": "healthy",
                    "operator_next_command": None,
                    "operator_next_requires_elevation": False,
                    "watchdog_registered": True,
                    "watchdog_state": "Ready",
                    "local_contract_status": {
                        "status": "healthy",
                        "summary": "Local 8421 exposes the canonical dashboard contract probes.",
                    },
                    "dashboard_task_contract_status": {
                        "status": "access_denied",
                        "summary": (
                            "Canonical dashboard runtime task(s) require elevation to inspect: "
                            "ETA-Dashboard-API, ETA-Proxy-8421."
                        ),
                        "missing_task_names": [],
                        "access_denied_task_names": ["ETA-Dashboard-API", "ETA-Proxy-8421"],
                        "needs_reload": False,
                    },
                    "firm_command_center_dependency_gap_status": {
                        "status": "healthy",
                        "summary": "FirmCommandCenter runtime import probe succeeded.",
                    },
                    "latest_fresh": True,
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        watchdog = r.json()["command_center_watchdog"]
        assert watchdog["status"] == "healthy"
        assert watchdog["healthy"] is True
        assert watchdog["failure_class"] == "healthy"
        assert watchdog["operator_contract_state"] == "healthy"
        assert watchdog["recommended_action"] == "none"
        assert watchdog["next_reason"] == "healthy"
        assert watchdog["next_step"] == "none"
        assert watchdog["next_command"] is None
        assert watchdog["repair_required"] is False
        assert watchdog["requires_elevation"] is False
        assert watchdog["issue_status"] == "healthy"
        assert watchdog["display_summary"] == "Command Center watchdog is healthy."

    def test_dashboard_diagnostics_uses_public_tunnel_service_drift_status_receipt(
        self,
        app_client,
        tmp_path,
    ):
        receipt_path = tmp_path / "state" / "command_center_doctor_latest.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": "eta.command_center.doctor.v1",
                    "checked_at": datetime.now(UTC).isoformat(),
                    "healthy": True,
                    "failure_class": "healthy",
                    "operator_contract_state": "healthy",
                    "recommended_action": "none",
                    "repair_required": False,
                    "operator_action": {
                        "step": "none",
                        "reason": "healthy",
                        "command": None,
                        "requires_elevation": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        status_path = tmp_path / "state" / "command_center_watchdog_status_latest.json"
        status_path.write_text(
            json.dumps(
                {
                    "overall_status": "degraded",
                    "effective_status": "public_tunnel_service_drift",
                    "primary_blocker": "public_tunnel_service_drift",
                    "operator_issue_status": "public_tunnel_service_drift",
                    "operator_next_step": "repair_public_tunnel_token",
                    "operator_next_reason": "public_tunnel_service_drift",
                    "operator_next_command": ".\\scripts\\repair-public-tunnel-admin.cmd",
                    "operator_next_requires_elevation": True,
                    "watchdog_registered": True,
                    "watchdog_state": "Running",
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        watchdog = r.json()["command_center_watchdog"]
        assert watchdog["status"] == "public_tunnel_service_drift"
        assert watchdog["healthy"] is False
        assert watchdog["fresh"] is True
        assert watchdog["recommended_action"] == "repair_public_tunnel_token"
        assert watchdog["next_step"] == "repair_public_tunnel_token"
        assert watchdog["next_command"] == ".\\scripts\\repair-public-tunnel-admin.cmd"
        assert watchdog["repair_required"] is True
        assert watchdog["requires_elevation"] is True
        assert "public_tunnel_service_drift" in watchdog["summary"]

    def test_dashboard_diagnostics_normalizes_internal_watchdog_reason_codes(
        self,
        app_client,
        tmp_path,
    ):
        receipt_path = tmp_path / "state" / "command_center_doctor_latest.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": "eta.command_center.doctor.v1",
                    "checked_at": datetime.now(UTC).isoformat(),
                    "healthy": True,
                    "failure_class": "healthy",
                    "operator_contract_state": "healthy",
                    "recommended_action": "none",
                    "repair_required": False,
                    "operator_action": {
                        "step": "none",
                        "reason": "healthy",
                        "command": None,
                        "requires_elevation": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        status_path = tmp_path / "state" / "command_center_watchdog_status_latest.json"
        status_path.write_text(
            json.dumps(
                {
                    "overall_status": "degraded",
                    "effective_status": "local_service_unreachable",
                    "primary_blocker": "local_service_unreachable",
                    "operator_issue_status": "local_service_unreachable",
                    "operator_next_step": "start_or_reload_operator_service",
                    "operator_next_reason": "local_service_unreachable",
                    "operator_next_command": ".\\scripts\\reload-command-center-admin.cmd",
                    "operator_next_requires_elevation": True,
                    "watchdog_registered": True,
                    "watchdog_state": "Running",
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        watchdog = r.json()["command_center_watchdog"]
        assert watchdog["status"] == "service_unreachable"
        assert watchdog["healthy"] is False
        assert watchdog["recommended_action"] == "start_or_reload_operator_service"
        assert watchdog["next_step"] == "start_or_reload_operator_service"
        assert watchdog["next_command"] == ".\\scripts\\reload-command-center-admin.cmd"
        assert "service_unreachable" in watchdog["summary"]

    def test_dashboard_diagnostics_blocks_green_when_watchdog_task_missing(
        self,
        app_client,
        tmp_path,
    ):
        receipt_path = tmp_path / "state" / "command_center_doctor_latest.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": "eta.command_center.doctor.v1",
                    "checked_at": datetime.now(UTC).isoformat(),
                    "healthy": True,
                    "failure_class": "healthy",
                    "operator_contract_state": "healthy",
                    "recommended_action": "none",
                    "repair_required": False,
                    "operator_action": {
                        "step": "none",
                        "reason": "healthy",
                        "command": None,
                        "requires_elevation": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        status_path = tmp_path / "state" / "command_center_watchdog_status_latest.json"
        status_path.write_text(
            json.dumps(
                {
                    "watchdog_registered": False,
                    "watchdog_state": "missing",
                    "operator_follow_up_actions": [
                        {
                            "step": "register_watchdog",
                            "reason": "watchdog_missing",
                            "command": (
                                "powershell -ExecutionPolicy Bypass -File "
                                ".\\scripts\\register-command-center-watchdog.ps1 -RunNow"
                            ),
                            "requires_elevation": True,
                        }
                    ],
                    "operator_follow_up_count": 1,
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        watchdog = r.json()["command_center_watchdog"]
        assert watchdog["status"] == "missing_watchdog"
        assert watchdog["healthy"] is False
        assert watchdog["recommended_action"] == "register_watchdog"
        assert watchdog["next_step"] == "register_watchdog"
        assert watchdog["repair_required"] is True
        assert watchdog["requires_elevation"] is True
        assert watchdog["watchdog_registered"] is False

    def test_master_status_uses_local_payload_not_self_proxy(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 0}, "launch_blocked_count": 0},
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 5,
                    "operator_queue_blocked_count": 0,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                }
            )
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["status"] == "live"
        assert payload["mode"] == "autonomous"
        assert payload["uptime"] == "connected"
        assert payload["cc_proxy"] == "local"
        assert payload["paper"]["mode"] == "paper_live"
        assert payload["paper"]["paper_ready_bots"] == 5
        assert payload["runtime"]["paper_live_ready"] is True
        assert payload["paper_live"]["status"] == "ready_to_launch_paper_live"
        assert payload["paper_live"]["critical_ready"] is True
        assert payload["paper_live"]["operator_queue_blocked_count"] == 0
        assert payload["systems"]["dashboard"]["status"] == "GREEN"
        assert payload["systems"]["paper_live"]["status"] == "GREEN"
        assert payload["systems"]["ibkr"]["source"] == "broker_gateway"
        assert payload["systems"]["broker"]["source"] == "broker_router"

    def test_master_status_includes_command_center_watchdog_system(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 0}, "launch_blocked_count": 0},
        )
        (tmp_path / "state" / "command_center_doctor_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": "eta.command_center.doctor.v1",
                    "checked_at": datetime.now(UTC).isoformat(),
                    "healthy": True,
                    "failure_class": "healthy",
                    "operator_contract_state": "healthy",
                    "recommended_action": "none",
                    "repair_required": False,
                    "operator_action": {
                        "step": "none",
                        "reason": "healthy",
                        "command": None,
                        "requires_elevation": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "command_center_watchdog_status_latest.json").write_text(
            json.dumps(
                {
                    "overall_status": "healthy",
                    "effective_status": "healthy",
                    "issue_status": "healthy",
                    "primary_blocker": "healthy",
                    "display_summary": "Command Center watchdog is healthy.",
                    "operator_next_step": "none",
                    "operator_next_reason": "healthy",
                    "operator_next_instruction": "No operator command is required.",
                    "operator_action_count": 0,
                    "operator_follow_up_count": 0,
                    "operator_next_can_launch_from_desktop": True,
                    "operator_next_launch_context": "ready",
                    "local_contract_status": {
                        "status": "healthy",
                    },
                    "dashboard_task_contract_status": {
                        "status": "access_denied",
                    },
                    "dashboard_task_missing_task_names": [],
                    "watchdog_registered": True,
                    "watchdog_state": "Running",
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 5,
                    "operator_queue_blocked_count": 0,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                }
            )
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["command_center_watchdog"]["status"] == "healthy"
        assert payload["surface_watch"] == payload["command_center_watchdog"]
        assert payload["systems"]["command_center_watchdog"]["status"] == "GREEN"
        assert payload["systems"]["surface_watch"] == payload["systems"]["command_center_watchdog"]
        assert payload["systems"]["command_center_watchdog"]["raw_status"] == "healthy"
        assert payload["systems"]["command_center_watchdog"]["effective_status"] == "healthy"
        assert payload["systems"]["command_center_watchdog"]["fresh"] is True
        assert payload["systems"]["command_center_watchdog"]["age_s"] >= 0
        assert payload["systems"]["command_center_watchdog"]["healthy"] is True
        assert payload["systems"]["command_center_watchdog"]["detail"] == "Command Center watchdog is healthy."
        assert payload["systems"]["command_center_watchdog"]["failure_class"] == "healthy"
        assert payload["systems"]["command_center_watchdog"]["operator_contract_state"] == "healthy"
        assert payload["systems"]["command_center_watchdog"]["primary_blocker"] == "healthy"
        assert payload["systems"]["command_center_watchdog"]["instruction"] == "No operator command is required."
        assert payload["systems"]["command_center_watchdog"]["action_count"] == 0
        assert payload["systems"]["command_center_watchdog"]["follow_up_count"] == 0
        assert payload["systems"]["command_center_watchdog"]["watchdog_registered"] is True
        assert payload["systems"]["command_center_watchdog"]["watchdog_state"] == "Running"
        assert payload["systems"]["command_center_watchdog"]["can_launch_from_desktop"] is True
        assert payload["systems"]["command_center_watchdog"]["launch_context"] == "ready"
        assert payload["systems"]["command_center_watchdog"]["dashboard_task_contract_status"] == "access_denied"
        assert payload["systems"]["command_center_watchdog"]["local_contract_status"] == "healthy"
        assert payload["systems"]["command_center_watchdog"]["dashboard_task_missing_task_names"] == []

    def test_master_status_includes_dashboard_proxy_watchdog_system(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 0}, "launch_blocked_count": 0},
        )
        (tmp_path / "state" / "dashboard_proxy_watchdog_heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "component": "dashboard_proxy_watchdog",
                    "decision": {
                        "checked_at": datetime.now(UTC).isoformat(),
                        "action": "noop",
                        "task_name": "ETA-Proxy-8421",
                        "probe": {
                            "healthy": True,
                            "url": "http://127.0.0.1:8421/",
                            "status_code": 200,
                            "reason": "ok",
                            "elapsed_ms": 15,
                            "body_len": 77000,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 5,
                    "operator_queue_blocked_count": 0,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                }
            )
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["dashboard_proxy_watchdog"]["status"] == "ok"
        assert payload["systems"]["dashboard_proxy_watchdog"]["status"] == "GREEN"
        assert payload["systems"]["dashboard_proxy_watchdog"]["raw_status"] == "ok"
        assert payload["systems"]["dashboard_proxy_watchdog"]["effective_status"] == "ok"
        assert payload["systems"]["dashboard_proxy_watchdog"]["detail"] == "noop: ok"
        assert payload["systems"]["dashboard_proxy_watchdog"]["task_name"] == "ETA-Proxy-8421"
        assert payload["systems"]["dashboard_proxy_watchdog"]["elapsed_ms"] == 15
        assert payload["systems"]["dashboard_proxy_watchdog"]["heartbeat_age_s"] >= 0
        assert payload["systems"]["dashboard_proxy_watchdog"]["checked_age_s"] >= 0

    def test_master_status_includes_stale_audit_systems(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 0}, "launch_blocked_count": 0},
        )
        state = tmp_path / "state"
        (state / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 5,
                    "operator_queue_blocked_count": 0,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                }
            )
        )
        (state / "eta_readiness_snapshot_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": "eta.readiness_snapshot.v1",
                    "checked_at_utc": (datetime.now(UTC) - timedelta(hours=3)).isoformat(),
                    "summary": "BLOCKED",
                    "checks": [
                        {
                            "name": "broker_bracket_audit",
                            "status": "BLOCKED",
                            "exit_code": 1,
                            "payload": {
                                "next_action": "Old readiness blocker detail.",
                                "position_summary": {
                                    "missing_bracket_count": 1,
                                    "broker_open_position_count": 1,
                                },
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "daily_stop_reset_audit_latest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "daily_stop_reset_audit",
                    "generated_at": (datetime.now(UTC) - timedelta(hours=6)).isoformat(),
                    "status": "reset_cleared_blocked",
                    "post_reset_ready": False,
                    "read_only": True,
                    "safe_to_trade_mutation": False,
                    "operator_next_action": "Old blocker detail",
                    "daily_loss_killswitch": {"status": "clear", "tripped": False},
                    "paper_live_transition": {"status": "blocked", "critical_ready": False},
                    "first_failed_gate": {"name": "tws_api_4002", "passed": False},
                }
            ),
            encoding="utf-8",
        )
        (state / "vps_ops_hardening_latest.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": (datetime.now(UTC) - timedelta(hours=5)).isoformat(),
                    "summary": {
                        "status": "RED_RUNTIME_DEGRADED",
                        "runtime_ready": False,
                        "dashboard_durable": False,
                        "paper_live_gate_ready": False,
                        "paper_live_status": "BLOCKED_RUNTIME",
                        "trading_gate_ready": False,
                        "prop_promotion_gate_ready": False,
                        "live_promotion_blocked": True,
                        "admin_ai_ready": True,
                        "admin_ai_status": "PASS",
                        "promotion_allowed": False,
                        "order_action_allowed": False,
                    },
                    "safety_gates": {
                        "jarvis_hermes_admin_ai": {
                            "status": "PASS",
                            "ready": True,
                            "warned": 0,
                            "blocked": 0,
                            "next_actions": ["Old hardening detail"],
                        }
                    },
                    "next_actions": ["Old hardening action"],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["eta_readiness_snapshot"]["status"] == "stale_receipt"
        assert payload["daily_stop_reset_audit"]["status"] == "stale_receipt"
        assert payload["vps_ops_hardening"]["status"] == "stale_receipt"
        assert payload["hardening"] == payload["vps_ops_hardening"]
        assert payload["systems"]["eta_readiness_snapshot"]["status"] == "YELLOW"
        assert payload["systems"]["eta_readiness_snapshot"]["raw_status"] == "stale_receipt"
        assert "stale" in payload["systems"]["eta_readiness_snapshot"]["detail"].lower()
        assert payload["systems"]["daily_stop_reset_audit"]["status"] == "YELLOW"
        assert payload["systems"]["daily_stop_reset_audit"]["raw_status"] == "stale_receipt"
        assert "stale" in payload["systems"]["daily_stop_reset_audit"]["detail"].lower()
        assert payload["systems"]["vps_ops_hardening"]["status"] == "YELLOW"
        assert payload["systems"]["hardening"] == payload["systems"]["vps_ops_hardening"]
        assert payload["systems"]["vps_ops_hardening"]["raw_status"] == "stale_receipt"
        assert payload["systems"]["vps_ops_hardening"]["admin_ai_status"] == "PASS"
        assert "stale" in payload["systems"]["vps_ops_hardening"]["detail"].lower()

    def test_master_status_marks_stale_paper_live_receipt_as_yellow_system(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 0}, "launch_blocked_count": 0},
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-05-07T23:40:00+00:00",
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 5,
                    "operator_queue_blocked_count": 0,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                }
            )
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["paper_live"]["status"] == "ready_to_launch_paper_live"
        assert payload["paper_live"]["stale_receipt"] is True
        assert payload["paper_live"]["effective_status"] == "stale_receipt"
        assert payload["systems"]["paper_live"]["status"] == "YELLOW"
        assert payload["systems"]["paper_live"]["raw_status"] == "ready_to_launch_paper_live"
        assert "stale" in payload["systems"]["paper_live"]["detail"].lower()

    def test_master_status_marks_non_authoritative_gateway_as_yellow_system(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 0}, "launch_blocked_count": 0},
        )
        monkeypatch.setattr(
            mod,
            "_broker_gateway_snapshot",
            lambda: {
                "status": "down",
                "detail": "gateway process not running",
                "ibkr": {
                    "status": "down",
                    "detail": "gateway process not running",
                    "checked_at": None,
                },
            },
        )
        monkeypatch.setattr(
            mod,
            "_gateway_authority_payload",
            lambda: {
                "allowed": False,
                "source": "missing_marker",
                "reason": "This host is not marked as the ETA IBKR Gateway authority.",
            },
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 5,
                    "operator_queue_blocked_count": 0,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                }
            )
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["systems"]["ibkr"]["status"] == "YELLOW"
        assert payload["systems"]["ibkr"]["raw_status"] == "down"
        assert payload["systems"]["ibkr"]["effective_status"] == "vps_only"
        assert payload["systems"]["ibkr"]["non_authoritative_host"] is True
        assert "gateway authority" in payload["systems"]["ibkr"]["detail"].lower()

    def test_master_status_suppresses_local_daily_loss_red_on_non_authoritative_paper_live(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 1}, "launch_blocked_count": 1},
        )
        monkeypatch.setattr(
            mod,
            "_daily_loss_killswitch_snapshot",
            lambda: {
                "source": "daily_loss_killswitch",
                "status": "tripped",
                "tripped": True,
                "disabled": False,
                "today_pnl_usd": -925.50,
                "limit_usd": -900.0,
                "reason": "day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
            },
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "blocked",
                    "critical_ready": False,
                    "non_authoritative_gateway_host": True,
                    "operator_queue_blocked_count": 1,
                    "operator_queue_launch_blocked_count": 1,
                    "operator_queue_first_launch_next_action": (
                        "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass "
                        "-File .\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 "
                        "-Apply -Role vps"
                    ),
                    "paper_ready_bots": 9,
                    "gates": [
                        {
                            "name": "tws_api_4002",
                            "passed": False,
                            "next_action": (
                                "On the VPS only: python -m eta_engine.scripts.tws_watchdog "
                                "--host 127.0.0.1 --port 4002"
                            ),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["paper_live"]["effective_status"] == "blocked"
        assert payload["paper_live"]["held_by_daily_loss_stop"] is False
        assert payload["paper_live"]["daily_loss_suppressed_non_authoritative_gateway_host"] is True
        assert payload["systems"]["paper_live"]["status"] == "YELLOW"
        assert payload["systems"]["paper_live"]["raw_status"] == "blocked"
        assert payload["systems"]["paper_live"]["effective_status"] == "blocked"
        assert payload["systems"]["paper_live"]["non_authoritative_gateway_host"] is True
        assert (
            payload["systems"]["paper_live"]["first_launch_next_action"]
            == "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass "
            "-File .\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 -Apply -Role vps"
        )
        assert payload["systems"]["paper_live"]["held_by_daily_loss_stop"] is False
        assert (
            payload["systems"]["paper_live"]["detail"]
            == "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass "
            "-File .\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 -Apply -Role vps"
        )

    def test_master_status_marks_authoritative_stale_bracket_hold_as_yellow_system(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 0}, "launch_blocked_count": 0},
        )
        monkeypatch.setattr(
            mod,
            "_broker_bracket_audit_payload",
            lambda **kwargs: {
                "summary": "READY_NO_OPEN_EXPOSURE",
                "ready_for_prop_dry_run": True,
                "position_summary": {
                    "broker_open_position_count": 0,
                    "broker_bracket_required_position_count": 0,
                    "broker_bracket_count": 0,
                    "missing_bracket_count": 0,
                },
                "next_action": "",
            },
        )
        monkeypatch.setattr(
            mod,
            "_maybe_promote_validated_broker_bracket_artifact",
            lambda report, **kwargs: report,
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 5,
                    "operator_queue_blocked_count": 0,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "eta_readiness_snapshot_latest.json").write_text(
            json.dumps(
                {
                    "checked_at_utc": "2026-05-15T01:00:00+00:00",
                    "summary": "BLOCKED",
                    "non_authoritative_gateway_host": True,
                    "public_fallback_reason": "non_authoritative_gateway_host",
                    "public_fallback_primary_action": (
                        "Wait for or clear 159 stale broker order(s) before prop dry-run."
                    ),
                    "public_fallback_brackets_summary": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
                    "public_fallback_checks": [
                        {
                            "name": "broker_bracket_audit_public_fallback",
                            "status": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
                            "payload": {
                                "summary": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
                                "next_action": "Wait for or clear 159 stale broker order(s) before prop dry-run.",
                            },
                        }
                    ],
                    "checks": [
                        {
                            "name": "broker_bracket_audit",
                            "status": "READY_NO_OPEN_EXPOSURE",
                            "payload": {
                                "summary": "READY_NO_OPEN_EXPOSURE",
                                "position_summary": {
                                    "missing_bracket_count": 0,
                                    "broker_open_position_count": 0,
                                },
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        audit_system = payload["systems"]["broker_bracket_audit"]
        assert audit_system["status"] == "YELLOW"
        assert audit_system["raw_status"] == "READY_NO_OPEN_EXPOSURE"
        assert audit_system["effective_status"] == "AUTHORITATIVE_STALE_REVIEW"
        assert audit_system["authoritative_advisory_active"] is True
        assert audit_system["authoritative_public_fallback_stale"] is True
        assert audit_system["ready_for_prop_dry_run"] is False
        assert audit_system["raw_ready_for_prop_dry_run"] is True
        assert "stale and last reported blocked stale flat open orders" in audit_system["detail"].lower()

    def test_master_status_includes_retune_focus_active_experiment(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 0}, "launch_blocked_count": 0},
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 5,
                    "operator_queue_blocked_count": 0,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                }
            )
        )
        (tmp_path / "state" / "diamond_retune_status_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_diamond_retune_status",
                    "status": "ready",
                    "focus_bot": "mnq_futures_sage",
                    "focus_active_experiment": {
                        "experiment_id": "partial_profit_disabled",
                        "started_at": "2026-05-16T01:44:06+00:00",
                        "partial_profit_enabled": False,
                        "awaiting_first_post_change_close": True,
                        "post_change_closed_trade_count": 0,
                        "post_change_total_realized_pnl": 0.0,
                        "post_change_profit_factor": None,
                    },
                    "summary": {
                        "broker_truth_focus_bot_id": "mnq_futures_sage",
                        "broker_truth_focus_active_experiment": {
                            "experiment_id": "partial_profit_disabled",
                            "started_at": "2026-05-16T01:44:06+00:00",
                            "partial_profit_enabled": False,
                            "awaiting_first_post_change_close": True,
                            "post_change_closed_trade_count": 0,
                            "post_change_total_realized_pnl": 0.0,
                            "post_change_profit_factor": None,
                        },
                        "broker_truth_focus_active_experiment_summary_line": (
                            "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
                        ),
                    },
                    "bots": [],
                    "research_backlog": [],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            mod,
            "_eta_readiness_snapshot_payload",
            lambda **_kwargs: {
                "status": "blocked",
                "detail": (
                    "Keep volume_profile_mnq in paper_soak until can_live_trade=true "
                    "and the futures prop ladder clears."
                ),
                "primary_blocker": "prop_live_readiness_gate",
                "primary_action": (
                    "Keep volume_profile_mnq in paper_soak until can_live_trade=true "
                    "and the futures prop ladder clears."
                ),
                "public_fallback_reason": "non_authoritative_gateway_host",
                "public_fallback_brackets_summary": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
                "public_fallback_brackets_next_action": "252 stale flat broker orders still need review.",
                "brackets_summary": "BLOCKED_FLEET_TRUTH_UNAVAILABLE",
                "brackets_next_action": (
                    "Bot-fleet position truth is unavailable; restore /api/bot-fleet "
                    "before treating broker exposure as flat."
                ),
                "public_live_broker_ready": False,
                "public_live_broker_snapshot_state": "missing",
                "public_live_broker_snapshot_source": "missing_ibkr_probe_cache",
                "public_live_broker_source": "cached_live_broker_state_for_diagnostics",
                "public_live_broker_degraded_display": (
                    "public broker_state degraded: missing_ibkr_probe_cache; "
                    "via cached_live_broker_state_for_diagnostics"
                ),
                "dashboard_api_runtime_current_live_broker_state_checked_utc": "",
                "dashboard_api_runtime_current_live_broker_open_order_count": 0,
                "dashboard_api_runtime_public_live_broker_degraded_display": "",
                "dashboard_api_runtime_drift_display": (
                    "8421 master/status is still blank for public_live_broker_degraded_display "
                    "while readiness receipt says public broker_state degraded: "
                    "missing_ibkr_probe_cache; via cached_live_broker_state_for_diagnostics"
                ),
                "dashboard_api_runtime_probe_display": (
                    "8421 master/status probe failed: local_dashboard_master_status timed out after 15s"
                ),
                "dashboard_api_runtime_refresh_command": (
                    ".\\scripts\\reload-command-center-admin.cmd "
                    "-PublicUrl https://ops.evolutionarytradingalgo.com"
                ),
                "dashboard_api_runtime_refresh_requires_elevation": True,
                "public_live_broker_open_order_count": 252,
                "public_fallback_broker_open_order_count": 252,
                "public_fallback_broker_open_order_drift_display": "",
                "public_fallback_stale_flat_open_order_count": 252,
                "public_fallback_stale_flat_open_order_relation_display": (
                    "all 252 broker open orders are stale flat orders"
                ),
                "public_live_retune_generated_at_utc": "2026-05-16T20:33:18+00:00",
                "public_live_retune_focus_active_experiment_outcome_line": (
                    "partial_profit_disabled: awaiting first post-change close"
                ),
                "public_live_retune_sync_drift_display": (
                    "public retune truth refreshed at 2026-05-16T20:33:18+00:00 "
                    "after readiness cached 2026-05-16T19:33:18+00:00"
                ),
                "dashboard_api_runtime_public_live_retune_generated_at_utc": "",
                "dashboard_api_runtime_public_live_retune_sync_drift_display": "",
                "dashboard_api_runtime_retune_drift_display": (
                    "8421 master/status is still blank for public_live_retune_generated_at_utc "
                    "while readiness receipt has 2026-05-16T20:33:18+00:00"
                ),
                "local_retune_generated_at_utc": "2026-05-16T20:25:28+00:00",
                "local_retune_focus_active_experiment_outcome_line": (
                    "partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
                ),
                "retune_focus_active_experiment_drift_display": (
                    "public retune says partial_profit_disabled: awaiting first post-change close; "
                    "local mirror says partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
                ),
                "current_local_retune_generated_at_utc": "2026-05-16T21:25:12+00:00",
                "local_retune_sync_drift_display": (
                    "local retune snapshot refreshed at 2026-05-16T21:25:12+00:00 "
                    "after readiness cached 2026-05-16T20:25:28+00:00"
                ),
            },
        )
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_gateway_reconcile",
            lambda: {},
        )
        monkeypatch.setattr(
            mod,
            "_public_operator_broker_state_payload",
            lambda **_kwargs: {
                "ibkr": {
                    "open_order_count": 258,
                    "checked_utc": "2026-05-16T21:59:53+00:00",
                }
            },
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["diamond_retune_status"]["focus_active_experiment"]["experiment_id"] == "partial_profit_disabled"
        assert payload["retune_focus_active_experiment"]["partial_profit_enabled"] is False
        assert payload["retune_focus_active_experiment_summary_line"] == (
            "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
        )
        assert payload["retune_focus_active_experiment_outcome_line"] == (
            "partial_profit_disabled: awaiting first post-change close"
        )
        assert payload["eta_readiness_status"] == "blocked"
        assert payload["eta_readiness_primary_blocker"] == "prop_live_readiness_gate"
        assert (
            payload["eta_readiness_detail"]
            == "Keep volume_profile_mnq in paper_soak until can_live_trade=true "
            "and the futures prop ladder clears."
        )
        assert (
            payload["eta_readiness_primary_action"]
            == "Keep volume_profile_mnq in paper_soak until can_live_trade=true "
            "and the futures prop ladder clears."
        )
        assert payload["public_fallback_reason"] == "non_authoritative_gateway_host"
        assert payload["public_fallback_brackets_summary"] == "BLOCKED_STALE_FLAT_OPEN_ORDERS"
        assert payload["public_fallback_brackets_next_action"] == "252 stale flat broker orders still need review."
        assert payload["brackets_summary"] == "BLOCKED_FLEET_TRUTH_UNAVAILABLE"
        assert (
            payload["brackets_next_action"]
            == "Bot-fleet position truth is unavailable; restore /api/bot-fleet "
            "before treating broker exposure as flat."
        )
        assert (
            payload["systems"]["eta_readiness_snapshot"]["detail"]
            == "Keep volume_profile_mnq in paper_soak until can_live_trade=true "
            "and the futures prop ladder clears."
        )
        assert payload["systems"]["eta_readiness_snapshot"]["primary_blocker"] == (
            "prop_live_readiness_gate"
        )
        assert payload["systems"]["eta_readiness_snapshot"]["primary_action"] == (
            "Keep volume_profile_mnq in paper_soak until can_live_trade=true "
            "and the futures prop ladder clears."
        )
        assert payload["systems"]["eta_readiness_snapshot"]["brackets_summary"] == (
            "BLOCKED_FLEET_TRUTH_UNAVAILABLE"
        )
        assert (
            payload["systems"]["eta_readiness_snapshot"]["brackets_next_action"]
            == "Bot-fleet position truth is unavailable; restore /api/bot-fleet "
            "before treating broker exposure as flat."
        )
        assert (
            payload["systems"]["eta_readiness_snapshot"]["public_fallback_reason"]
            == "non_authoritative_gateway_host"
        )
        assert (
            payload["systems"]["eta_readiness_snapshot"]["public_fallback_brackets_summary"]
            == "BLOCKED_STALE_FLAT_OPEN_ORDERS"
        )
        assert (
            payload["systems"]["eta_readiness_snapshot"]["public_fallback_brackets_next_action"]
            == "252 stale flat broker orders still need review."
        )
        assert payload["public_live_broker_ready"] is False
        assert payload["public_live_broker_snapshot_state"] == "missing"
        assert payload["public_live_broker_snapshot_source"] == "missing_ibkr_probe_cache"
        assert payload["public_live_broker_source"] == "cached_live_broker_state_for_diagnostics"
        assert (
            payload["public_live_broker_degraded_display"]
            == "public broker_state degraded: missing_ibkr_probe_cache; "
            "via cached_live_broker_state_for_diagnostics"
        )
        assert payload["dashboard_api_runtime_current_live_broker_state_checked_utc"] == "2026-05-16T21:59:53+00:00"
        assert payload["dashboard_api_runtime_current_live_broker_open_order_count"] == 258
        assert (
            payload["dashboard_api_runtime_public_live_broker_degraded_display"]
            == "public broker_state degraded: missing_ibkr_probe_cache; "
            "via cached_live_broker_state_for_diagnostics"
        )
        assert payload["dashboard_api_runtime_drift_display"] == ""
        assert (
            payload["dashboard_api_runtime_probe_display"]
            == "8421 master/status probe failed: local_dashboard_master_status timed out after 15s"
        )
        assert (
            payload["dashboard_api_runtime_refresh_command"]
            == ".\\scripts\\reload-command-center-admin.cmd "
            "-PublicUrl https://ops.evolutionarytradingalgo.com"
        )
        assert payload["dashboard_api_runtime_refresh_requires_elevation"] is True
        assert payload["public_live_broker_open_order_count"] == 252
        assert payload["current_live_broker_state_checked_utc"] == "2026-05-16T21:59:53+00:00"
        assert payload["current_live_broker_open_order_count"] == 258
        assert payload["current_live_broker_degraded_display"] == ""
        assert (
            payload["current_live_broker_open_order_drift_display"]
            == "live broker_state now reports 258 open orders vs readiness cached 252 stale flat orders"
        )
        assert payload["public_fallback_broker_open_order_count"] == 252
        assert payload["public_fallback_broker_open_order_drift_display"] == ""
        assert payload["public_fallback_stale_flat_open_order_count"] == 252
        assert (
            payload["public_fallback_stale_flat_open_order_relation_display"]
            == "all 252 broker open orders are stale flat orders"
        )
        assert (
            payload["public_live_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: awaiting first post-change close"
        )
        assert payload["public_live_retune_generated_at_utc"] == "2026-05-16T20:33:18+00:00"
        assert (
            payload["public_live_retune_sync_drift_display"]
            == "public retune truth refreshed at 2026-05-16T20:33:18+00:00 "
            "after readiness cached 2026-05-16T19:33:18+00:00"
        )
        assert payload["current_live_retune_generated_at_utc"] == ""
        assert payload["current_live_retune_focus_active_experiment_outcome_line"] == ""
        assert payload["current_live_retune_sync_drift_display"] == ""
        assert payload["dashboard_api_runtime_public_live_retune_generated_at_utc"] == "2026-05-16T20:33:18+00:00"
        assert (
            payload["dashboard_api_runtime_public_live_retune_sync_drift_display"]
            == "public retune truth refreshed at 2026-05-16T20:33:18+00:00 "
            "after readiness cached 2026-05-16T19:33:18+00:00"
        )
        assert payload["dashboard_api_runtime_retune_drift_display"] == ""
        assert payload["local_retune_generated_at_utc"] == "2026-05-16T20:25:28+00:00"
        assert (
            payload["local_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
        )
        assert (
            payload["retune_focus_active_experiment_drift_display"]
            == "public retune says partial_profit_disabled: awaiting first post-change close; "
            "local mirror says partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
        )
        assert payload["current_local_retune_generated_at_utc"] == "2026-05-16T21:25:12+00:00"
        assert (
            payload["local_retune_sync_drift_display"]
            == "local retune snapshot refreshed at 2026-05-16T21:25:12+00:00 "
            "after readiness cached 2026-05-16T20:25:28+00:00"
        )

    def test_master_status_surfaces_daily_loss_shadow_advisory(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 0}, "launch_blocked_count": 0},
        )
        monkeypatch.setattr(
            mod,
            "_daily_loss_killswitch_snapshot",
            lambda: {
                "source": "daily_loss_killswitch",
                "status": "tripped",
                "tripped": True,
                "disabled": False,
                "today_pnl_usd": -925.50,
                "limit_usd": -900.0,
                "reason": "day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
            },
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 9,
                    "operator_queue_blocked_count": 0,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["systems"]["paper_live"]["status"] == "YELLOW"
        assert payload["systems"]["paper_live"]["detail"] == "shadow_paper_active"
        assert payload["systems"]["paper_live"]["held_by_daily_loss_stop"] is False
        assert payload["paper_live"]["effective_status"] == "shadow_paper_active"
        assert payload["paper_live"]["held_by_daily_loss_stop"] is False
        assert payload["paper_live"]["daily_loss_gate_mode"] == "advisory"
        assert payload["paper_live"]["daily_loss_advisory_active"] is True
        assert payload["paper_live"]["capital_lanes_held_by_daily_loss_stop"] is True
        assert payload["paper_live"]["daily_loss_killswitch"]["status"] == "tripped"
        assert payload["daily"]["soft_stop_active"] is True
        assert payload["daily"]["today_pnl_usd"] == -925.50

        runtime = app_client.get("/api/runtime-status")
        assert runtime.status_code == 200
        runtime_payload = runtime.json()
        assert runtime_payload["runtime"]["paper_live_held_by_daily_loss_stop"] is False
        assert runtime_payload["runtime"]["paper_live_daily_loss_advisory_active"] is True
        assert runtime_payload["runtime"]["paper_live_capital_lanes_held_by_daily_loss_stop"] is True
        assert runtime_payload["held_by_daily_loss_stop"] is False
        assert runtime_payload["daily_loss_advisory_active"] is True

    def test_master_status_marks_live_shadow_runtime_active(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 1}, "launch_blocked_count": 1},
        )
        state = tmp_path / "state"
        now_iso = datetime.now(UTC).isoformat()
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": now_iso,
                    "mode": "paper_live",
                    "feed": "composite",
                    "bots": [
                        {
                            "bot_id": "volume_profile_mnq",
                            "symbol": "MNQ1",
                            "strategy_kind": "confluence_scorecard",
                            "execution_lane": "shadow_paper",
                            "last_bar_ts": now_iso,
                            "open_position": {},
                            "n_entries": 0,
                            "n_exits": 0,
                            "realized_pnl": 0.0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": now_iso,
                    "status": "blocked",
                    "critical_ready": False,
                    "paper_ready_bots": 11,
                    "operator_queue_blocked_count": 5,
                    "operator_queue_launch_blocked_count": 1,
                    "operator_queue_first_launch_blocker_op_id": "OP-19",
                    "gates": [],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["paper_live"]["status"] == "blocked"
        assert payload["paper_live"]["raw_status"] == "blocked"
        assert payload["paper_live"]["effective_status"] == "shadow_paper_active"
        assert payload["paper_live"]["effective_detail"] == "live shadow paper lane active on 1 attached bot(s)"
        assert payload["systems"]["paper_live"]["status"] == "YELLOW"
        assert payload["systems"]["paper_live"]["detail"] == "shadow_paper_active"

    def test_master_status_reconciles_cached_ibkr_live_positions(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import time

        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 0}, "launch_blocked_count": 0},
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 5,
                    "operator_queue_blocked_count": 0,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                },
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "tws_watchdog.json").write_text(
            json.dumps(
                {
                    "checked_at": datetime.now(UTC).isoformat(),
                    "healthy": True,
                    "details": {
                        "socket_ok": True,
                        "handshake_ok": True,
                        "handshake_detail": (
                            "serverVersion=176; clientId=9011; attempt=1; positions=0 open; executions=0"
                        ),
                    },
                },
            ),
            encoding="utf-8",
        )
        mod._IBKR_PROBE_CACHE["snapshot"] = {
            "ready": True,
            "open_position_count": 1,
            "open_positions": [
                {
                    "symbol": "MNQM6",
                    "secType": "FUT",
                    "position": 3,
                },
            ],
        }
        mod._IBKR_PROBE_CACHE["ts"] = time.time()

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        detail = r.json()["systems"]["ibkr"]["detail"]
        assert "positions=0 open" in detail
        assert "live broker exposure: 1 IBKR open (MNQM6)" in detail

    def test_master_status_marks_recent_gateway_flap_yellow_during_self_heal(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        from datetime import UTC, datetime, timedelta

        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 0}, "launch_blocked_count": 0},
        )
        now = datetime.now(UTC)
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": now.isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 5,
                    "operator_queue_blocked_count": 0,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                },
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "tws_watchdog.json").write_text(
            json.dumps(
                {
                    "checked_at": now.isoformat(),
                    "healthy": False,
                    "consecutive_failures": 1,
                    "last_healthy_at": (now - timedelta(seconds=45)).isoformat(),
                    "details": {
                        "host": "127.0.0.1",
                        "port": 4002,
                        "socket_ok": True,
                        "handshake_ok": False,
                        "handshake_detail": "attempt 1 clientId=9011: TimeoutError()",
                        "gateway_process": {
                            "running": True,
                            "gateway_dir": r"C:\Jts\ibgateway\1046",
                            "name": "java.exe",
                        },
                    },
                },
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "ibgateway_reauth.json").write_text(
            json.dumps(
                {
                    "status": "waiting_for_failures",
                    "operator_action_required": False,
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["paper_live"]["status"] == "ready_to_launch_paper_live"
        assert payload["paper_live"]["critical_ready"] is True
        assert payload["systems"]["ibkr"]["status"] == "YELLOW"
        assert payload["systems"]["ibkr"]["raw_status"] == "degraded"
        assert "recovery: waiting_for_failures" in payload["systems"]["ibkr"]["detail"]
        assert "self-heal grace active" in payload["systems"]["ibkr"]["detail"]

    def test_master_status_surfaces_target_exit_missing_bracket_risk(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import time

        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 0}, "launch_blocked_count": 0},
        )
        monkeypatch.setattr(
            mod,
            "_broker_router_snapshot",
            lambda: {"status": "ok", "active_blocker_count": 0},
        )
        monkeypatch.setattr(
            mod,
            "_supervisor_roster_rows",
            lambda now_ts: [
                {
                    "id": "mnq_futures_sage",
                    "name": "mnq_futures_sage",
                    "symbol": "MNQ1",
                    "open_positions": 1,
                    "position_state": {
                        "state": "open",
                        "bracket_target": 29362.75,
                        "bracket_stop": 29323.75,
                        "target_exit_visibility": {
                            "status": "watching",
                            "owner": "supervisor",
                            "target_distance_points": 27.25,
                            "stop_distance_points": -11.75,
                        },
                    },
                },
            ],
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 5,
                    "operator_queue_blocked_count": 0,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                },
            ),
            encoding="utf-8",
        )
        mod._IBKR_PROBE_CACHE["snapshot"] = {
            "ready": True,
            "open_position_count": 1,
            "open_positions": [
                {
                    "symbol": "MNQM6",
                    "secType": "FUT",
                    "position": 3,
                },
            ],
        }
        mod._IBKR_PROBE_CACHE["ts"] = time.time()

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["target_exit_summary"]["status"] == "missing_brackets"
        assert payload["target_exit_summary"]["broker_position_scope"] == "ibkr_cached"
        assert payload["target_exit_summary"]["missing_bracket_count"] == 1
        assert payload["target_exit"]["status"] == "missing_brackets"
        assert payload["target_exit"]["broker_position_scope"] == "ibkr_cached"
        assert payload["target_exit"]["missing_bracket_count"] == 1
        assert payload["systems"]["target_exit"]["status"] == "YELLOW"
        assert "1 IBKR cached broker open" in payload["systems"]["target_exit"]["detail"]
        assert "1 missing bracket" in payload["systems"]["target_exit"]["detail"]
        assert payload["systems"]["broker"]["status"] == "YELLOW"
        assert payload["systems"]["broker"]["raw_status"] == "ok"
        assert payload["systems"]["broker"]["target_exit_status"] == "missing_brackets"
        assert payload["systems"]["broker_bracket_audit"]["status"] == "YELLOW"
        assert payload["systems"]["broker_bracket_audit"]["raw_status"] == "BLOCKED_UNBRACKETED_EXPOSURE"
        assert payload["systems"]["broker_bracket_audit"]["operator_action_required"] is True
        assert payload["systems"]["broker_bracket_audit"]["prop_dry_run_blocked"] is True
        assert payload["systems"]["broker_bracket_audit"]["operator_action_count"] == 2
        assert payload["systems"]["broker_bracket_audit"]["operator_action_labels"] == [
            "Verify broker OCO coverage",
            "Flatten unprotected paper exposure",
        ]
        assert payload["systems"]["broker_bracket_audit"]["order_action_count"] == 1
        assert payload["systems"]["broker_bracket_audit"]["primary_action_label"] == ("Verify broker OCO coverage")
        assert payload["systems"]["broker_bracket_audit"]["order_action_label"] == (
            "Flatten unprotected paper exposure"
        )
        assert payload["systems"]["broker_bracket_audit"]["primary_symbol"] == "MNQM6"
        assert payload["systems"]["broker_bracket_audit"]["unprotected_symbols"] == ["MNQM6"]
        assert payload["systems"]["paper_live"]["status"] == "YELLOW"
        assert payload["systems"]["paper_live"]["detail"] == "held_by_bracket_audit"
        assert payload["systems"]["paper_live"]["effective_status"] == "held_by_bracket_audit"
        assert payload["systems"]["paper_live"]["held_by_bracket_audit"] is True
        assert payload["paper_live"]["raw_status"] == "ready_to_launch_paper_live"
        assert payload["paper_live"]["status"] == "ready_to_launch_paper_live"
        assert payload["paper_live"]["effective_status"] == "held_by_bracket_audit"
        assert payload["paper_live"]["held_by_bracket_audit"] is True
        assert payload["paper_live"]["effective_detail"] == (
            "held by Bracket Audit: Verify broker OCO coverage or Flatten unprotected paper exposure"
        )
        runtime = app_client.get("/api/runtime-status")
        assert runtime.status_code == 200
        runtime_payload = runtime.json()
        assert runtime_payload["paper"]["status"] == "ready_to_launch_paper_live"
        assert runtime_payload["paper_live"]["effective_status"] == "held_by_bracket_audit"
        assert runtime_payload["paper_live"]["held_by_bracket_audit"] is True
        assert runtime_payload["runtime"]["paper_live_effective_status"] == "held_by_bracket_audit"
        assert runtime_payload["runtime"]["paper_live_held_by_bracket_audit"] is True
        assert runtime_payload["effective_status"] == "held_by_bracket_audit"
        assert runtime_payload["held_by_bracket_audit"] is True
        bridge = app_client.get("/api/bridge-status")
        assert bridge.status_code == 200
        assert bridge.json()["paper"]["status"] == "ready_to_launch_paper_live"
        assert bridge.json()["paper_live"]["effective_status"] == "held_by_bracket_audit"
        assert bridge.json()["paper_live"]["held_by_bracket_audit"] is True
        assert payload["broker_bracket_audit"]["position_summary"]["broker_bracket_required_position_count"] == 1

    def test_master_status_keeps_advisory_queue_separate_from_launch_status(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_operator_queue_payload",
            lambda: {"summary": {"BLOCKED": 1}, "launch_blocked_count": 3},
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 4,
                    "operator_queue_blocked_count": 1,
                    "operator_queue_launch_blocked_count": 0,
                    "operator_queue_first_blocker_op_id": "OP-16",
                    "operator_queue_first_launch_blocker_op_id": None,
                    "gates": [],
                }
            )
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["paper"]["status"] == "ready_to_launch_paper_live"
        assert payload["runtime"]["operator_queue_blocked_count"] == 1
        assert payload["runtime"]["operator_queue_launch_blocked_count"] == 0
        assert payload["systems"]["paper_live"]["status"] == "GREEN"

    def test_master_status_retries_fresh_broker_state_for_stale_flat_validation_failure(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        del app_client
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 9,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                },
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "_operator_queue_payload", lambda: {"summary": {}, "launch_blocked_count": 0})
        monkeypatch.setattr(mod, "_broker_gateway_snapshot", lambda: {"status": "connected"})
        monkeypatch.setattr(mod, "_broker_router_snapshot", lambda: {"status": "ok"})
        monkeypatch.setattr(
            mod,
            "_target_exit_summary_for_master_status",
            lambda: {
                "status": "watching",
                "broker_open_position_count": 0,
                "broker_bracket_required_position_count": 0,
                "broker_bracket_count": 0,
                "missing_bracket_count": 0,
            },
        )
        monkeypatch.setattr(mod, "_daily_loss_killswitch_snapshot", lambda: {"tripped": False})
        monkeypatch.setattr(mod, "_vps_root_reconciliation_payload", lambda: {"status": "ok", "risk_level": "low"})
        monkeypatch.setattr(mod, "_cached_live_broker_state_for_gateway_reconcile", lambda: {"source": "cached"})
        monkeypatch.setattr(mod, "_live_broker_state_payload", lambda: {"source": "fresh"})

        audit_sources: list[str] = []

        def fake_bracket_audit_payload(*, target_exit_summary, live_broker_state):
            del target_exit_summary
            audit_sources.append(str(live_broker_state.get("source") or ""))
            if live_broker_state.get("source") == "cached":
                return {
                    "summary": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
                    "ready_for_prop_dry_run": False,
                    "operator_action_required": True,
                    "next_action": "Cancel stale active broker order(s) for MBTK6.",
                    "operator_actions": [{"label": "Cancel stale flat-symbol orders", "order_action": True}],
                    "position_summary": {"stale_flat_open_order_symbols": ["MBTK6"]},
                    "stale_flat_open_order_validation": {"status": "live_socket_validation_failed"},
                }
            return {
                "summary": "READY_NO_OPEN_EXPOSURE",
                "ready_for_prop_dry_run": True,
                "operator_action_required": False,
                "next_action": "Broker-native bracket/OCO audit is clear.",
                "operator_actions": [],
                "position_summary": {},
                "stale_flat_open_order_validation": {"status": "not_requested"},
            }

        monkeypatch.setattr(mod, "_broker_bracket_audit_payload", fake_bracket_audit_payload)

        payload = mod._local_master_status_payload()

        assert audit_sources == ["cached", "fresh"]
        assert payload["broker_bracket_audit"]["summary"] == "READY_NO_OPEN_EXPOSURE"
        assert payload["broker_bracket_audit"]["refreshed_after_stale_validation_failure"] is True
        assert payload["paper_live"]["status"] == "ready_to_launch_paper_live"
        assert payload["paper_live"]["effective_status"] == "ready_to_launch_paper_live"
        assert payload["paper_live"]["held_by_bracket_audit"] is False

    def test_local_master_status_promotes_recent_validated_bracket_audit_artifact(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        del app_client
        artifact_path = tmp_path / "state" / "broker_bracket_audit_latest.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "generated_at_utc": datetime.now(UTC).isoformat(),
                    "summary": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
                    "ready_for_prop_dry_run": False,
                    "operator_action_required": True,
                    "next_action": (
                        "Wait for or clear 3 stale PendingCancel broker order(s) for MCLN6, NGM26, NQM6; "
                        "they still have no matching broker open position."
                    ),
                    "operator_actions": [
                        {
                            "label": "Clear pending stale flat-symbol orders",
                            "detail": "3 orders already show PendingCancel with IBKR.",
                            "order_action": True,
                        },
                    ],
                    "position_summary": {
                        "broker_open_position_count": 1,
                        "broker_bracket_required_position_count": 1,
                        "broker_bracket_count": 1,
                        "missing_bracket_count": 0,
                        "stale_flat_open_order_count": 3,
                        "stale_flat_open_order_symbols": ["MCLN6", "NGM26", "NQM6"],
                    },
                    "stale_flat_open_order_validation": {"status": "live_socket_validated"},
                },
            ),
            encoding="utf-8",
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-05-15T15:42:00+00:00",
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 9,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                },
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("ETA_BROKER_BRACKET_AUDIT_PATH", str(artifact_path))
        monkeypatch.setattr(mod, "_operator_queue_payload", lambda: {"summary": {}, "launch_blocked_count": 0})
        monkeypatch.setattr(mod, "_broker_gateway_snapshot", lambda: {"status": "connected"})
        monkeypatch.setattr(mod, "_broker_router_snapshot", lambda: {"status": "ok"})
        monkeypatch.setattr(
            mod,
            "_target_exit_summary_for_master_status",
            lambda: {
                "status": "watching",
                "broker_open_position_count": 1,
                "broker_bracket_required_position_count": 1,
                "broker_bracket_count": 1,
                "missing_bracket_count": 0,
            },
        )
        monkeypatch.setattr(mod, "_daily_loss_killswitch_snapshot", lambda: {"tripped": False})
        monkeypatch.setattr(mod, "_vps_root_reconciliation_payload", lambda: {"status": "ok", "risk_level": "low"})
        monkeypatch.setattr(mod, "_cached_live_broker_state_for_gateway_reconcile", lambda: {"source": "cached"})
        monkeypatch.setattr(mod, "_live_broker_state_payload", lambda: pytest.fail("fresh broker probe should not run"))

        def fake_bracket_audit_payload(*, target_exit_summary, live_broker_state):
            del target_exit_summary, live_broker_state
            return {
                "summary": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
                "ready_for_prop_dry_run": False,
                "operator_action_required": True,
                "next_action": "Cancel stale active broker order(s) for MCLN6, NGM26, NQM6.",
                "operator_actions": [{"label": "Cancel stale flat-symbol orders", "order_action": True}],
                "position_summary": {
                    "broker_open_position_count": 1,
                    "broker_bracket_required_position_count": 1,
                    "broker_bracket_count": 1,
                    "missing_bracket_count": 0,
                    "stale_flat_open_order_count": 3,
                    "stale_flat_open_order_symbols": ["MCLN6", "NGM26", "NQM6"],
                },
                "stale_flat_open_order_validation": {"status": "not_requested"},
            }

        monkeypatch.setattr(mod, "_broker_bracket_audit_payload", fake_bracket_audit_payload)

        payload = mod._local_master_status_payload()

        assert payload["broker_bracket_audit"]["promoted_validated_artifact"] is True
        assert payload["broker_bracket_audit"]["stale_flat_open_order_validation"]["status"] == "live_socket_validated"
        assert payload["broker_bracket_audit"]["next_action"].startswith(
            "Wait for or clear 3 stale PendingCancel broker order(s)",
        )
        assert payload["broker_bracket_audit"]["operator_actions"][0]["label"] == (
            "Clear pending stale flat-symbol orders"
        )

    def test_broker_bracket_artifact_promotion_requires_matching_fingerprint(self, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        artifact_path = tmp_path / "broker_bracket_audit_latest.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "generated_at_utc": datetime.now(UTC).isoformat(),
                    "summary": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
                    "position_summary": {
                        "broker_open_position_count": 1,
                        "broker_bracket_required_position_count": 1,
                        "broker_bracket_count": 1,
                        "missing_bracket_count": 0,
                        "stale_flat_open_order_count": 1,
                        "stale_flat_open_order_symbols": ["MBTK6"],
                    },
                    "stale_flat_open_order_validation": {"status": "live_socket_validated"},
                },
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("ETA_BROKER_BRACKET_AUDIT_PATH", str(artifact_path))

        current = {
            "summary": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
            "position_summary": {
                "broker_open_position_count": 1,
                "broker_bracket_required_position_count": 1,
                "broker_bracket_count": 1,
                "missing_bracket_count": 0,
                "stale_flat_open_order_count": 3,
                "stale_flat_open_order_symbols": ["MCLN6", "NGM26", "NQM6"],
            },
            "stale_flat_open_order_validation": {"status": "not_requested"},
        }

        promoted = mod._maybe_promote_validated_broker_bracket_artifact(
            current,
            server_ts=datetime.now(UTC).timestamp(),
            target_exit_summary={},
            live_broker_state={},
        )

        assert promoted is current

    def test_vps_root_reconciliation_endpoint_surfaces_review_plan(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_workspace_checkout_payload",
            lambda refresh=False: {
                "status": "clean",
                "summary_line": "root clean abc1234 | eta dirty def5678 | submodule aligned def5678",
                "eta_engine": {
                    "status": "dirty",
                    "branch": "codex/runtime-review",
                    "head_short": "def5678",
                    "tracked_change_count": 26,
                    "untracked_change_count": 4,
                    "summary_line": "eta dirty def5678 | 26 tracked, 4 untracked",
                },
                "submodule": {
                    "path": "eta_engine",
                    "status": "aligned",
                    "state_label": "aligned",
                    "expected_short": "def5678",
                },
                "wiring": {
                    "status": "review",
                    "summary_line": (
                        "dirty/diverged child integration plus optional missing submodule checkout "
                        "blocks gitlink wiring (eta_engine, mnq_backtest)"
                    ),
                },
            },
        )
        plan = {
            "status": "ok",
            "mode": "review_plan_only",
            "risk_level": "high",
            "cleanup_allowed": False,
            "destructive_actions_performed": False,
            "counts": {"status": 279, "submodule_drift": 6, "dirty_companion_repos": 3},
            "summary": {
                "source_or_governance_deleted": 124,
                "unknown_deleted": 2,
                "generated_deleted": 11,
                "generated_untracked": 141,
                "source_or_governance_untracked": 2,
                "submodule_drift": 6,
                "dirty_companion_repos": 3,
            },
            "steps": [
                {
                    "id": "freeze-and-backup",
                    "action": "Keep root cleanup disabled until review is approved.",
                },
                {
                    "id": "restore-source-governance",
                    "decision": "manual_review_required",
                    "evidence": [
                        "source_or_governance_deleted=124",
                        "scripts/command-center-watchdog-status.ps1",
                        "scripts/reload-operator-service.ps1",
                    ],
                },
                {
                    "id": "align-submodules",
                    "decision": "manual_review_required",
                    "evidence": [
                        "dirty_companion_repos=3",
                        "eta_engine:submodule_pointer_changed",
                    ],
                },
            ],
            "source_review_items": [
                {
                    "path": "scripts/command-center-watchdog-status.ps1",
                    "basename": "command-center-watchdog-status.ps1",
                    "change_class": "deleted",
                    "area": "operator_watchdog_truth",
                    "rationale": (
                        "Tracks Command Center watchdog truth, task contract drift, "
                        "and public route health semantics."
                    ),
                    "change_summary": (
                        "Adds runtime dependency-gap probing, watchdog/dashboard "
                        "task-contract checks, and display-safe operator summaries."
                    ),
                    "verification_command": (
                        "powershell -ExecutionPolicy Bypass -File "
                        ".\\scripts\\command-center-watchdog-status.ps1 -Json"
                    ),
                    "verification_goal": (
                        "Confirm the live watchdog contract, task-contract status, "
                        "and display-safe operator summary on the authoritative host."
                    ),
                    "verification_mode": "status_probe",
                    "verification_side_effects": "refreshes the canonical watchdog receipt",
                    "suggested_decision": "preserve_if_it_matches_live_watchdog_contract",
                },
                {
                    "path": "scripts/reload-operator-service.ps1",
                    "basename": "reload-operator-service.ps1",
                    "change_class": "deleted",
                    "area": "operator_reload_runtime",
                    "rationale": (
                        "Controls canonical 8421 reload behavior and the task-owned "
                        "Command Center runtime path on the VPS."
                    ),
                    "change_summary": (
                        "Replaces brittle raw 8421 waits with unified local-truth "
                        "verification and uses the canonical runtime Python."
                    ),
                    "verification_command": (
                        "powershell -ExecutionPolicy Bypass -File "
                        ".\\scripts\\reload-operator-service.ps1 "
                        "-SkipPublicCheck -SkipWatchdogRegistration "
                        "-NoAutoElevate -TimeoutSeconds 30"
                    ),
                    "verification_goal": (
                        "Confirm the VPS reload flow exits cleanly and re-verifies "
                        "the local 8421 operator contract."
                    ),
                    "verification_mode": "runtime_reload",
                    "verification_side_effects": (
                        "re-registers dashboard tasks, refreshes live service wiring, "
                        "and reloads the 8421 operator surface"
                    ),
                    "suggested_decision": "preserve_if_it_matches_live_8421_reload_flow",
                },
            ],
            "companion_review_items": [
                {
                    "target": "eta_engine",
                    "reason": "submodule_pointer_changed",
                    "rationale": (
                        "The authoritative ETA child repo is dirty/diverged and needs "
                        "a child-repo decision before the root gitlink is updated."
                    ),
                    "suggested_decision": "commit_preserve_or_pin_before_root_update",
                }
            ],
        }
        (tmp_path / "state" / "vps_root_reconciliation_plan.json").write_text(json.dumps(plan))

        r = app_client.get("/api/vps/root-reconciliation")
        alias = app_client.get("/api/vps/root/reconciliation")

        assert r.status_code == 200
        assert alias.status_code == 200
        payload = r.json()
        alias_payload = alias.json()
        assert payload["status"] == "review_required"
        assert alias_payload["status"] == payload["status"]
        assert alias_payload["summary"] == payload["summary"]
        assert payload["source"] == "vps_root_reconciliation_plan"
        assert payload["risk_level"] == "high"
        assert payload["cleanup_allowed"] is False
        assert payload["destructive_actions_performed"] is False
        assert payload["summary"]["source_or_governance_deleted"] == 124
        assert payload["counts"]["submodule_drift"] == 6
        assert payload["summary"]["dirty_companion_repos"] == 3
        assert payload["source_review_files"] == [
            "command-center-watchdog-status.ps1",
            "reload-operator-service.ps1",
        ]
        assert payload["source_review_items"] == plan["source_review_items"]
        assert payload["companion_review_targets"] == ["eta_engine"]
        assert payload["companion_review_items"] == plan["companion_review_items"]
        assert payload["review_focus_summary_line"] == (
            "2 root source file(s) under review; "
            "eta_engine: eta dirty def5678 | 26 tracked, 4 untracked; "
            "manual review before root update"
        )
        assert payload["review_focus_primary_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo diff -- scripts/command-center-watchdog-status.ps1"
        )
        assert payload["review_focus_primary_action"] == {
            "scope": "source",
            "path": "scripts/command-center-watchdog-status.ps1",
            "basename": "command-center-watchdog-status.ps1",
            "area": "operator_watchdog_truth",
            "change_summary": (
                "Adds runtime dependency-gap probing, watchdog/dashboard "
                "task-contract checks, and display-safe operator summaries."
            ),
            "verification_command": (
                "powershell -ExecutionPolicy Bypass -File "
                ".\\scripts\\command-center-watchdog-status.ps1 -Json"
            ),
            "verification_goal": (
                "Confirm the live watchdog contract, task-contract status, "
                "and display-safe operator summary on the authoritative host."
            ),
            "verification_mode": "status_probe",
            "verification_side_effects": "refreshes the canonical watchdog receipt",
            "suggested_decision": "preserve_if_it_matches_live_watchdog_contract",
            "command": "git -C C:\\EvolutionaryTradingAlgo diff -- scripts/command-center-watchdog-status.ps1",
        }
        review_focus = payload["review_focus"]
        assert review_focus["source_modified_count"] == 0
        assert review_focus["dirty_companion_repos"] == 3
        assert review_focus["source_review_item_count"] == 2
        assert review_focus["companion_review_item_count"] == 1
        assert review_focus["source_review_files"] == [
            "command-center-watchdog-status.ps1",
            "reload-operator-service.ps1",
        ]
        assert review_focus["source_review_context"] == {}
        assert review_focus["summary_line"] == payload["review_focus_summary_line"]
        assert review_focus["source_review_commands"] == [
            "git -C C:\\EvolutionaryTradingAlgo diff -- scripts/command-center-watchdog-status.ps1",
            "git -C C:\\EvolutionaryTradingAlgo diff -- scripts/reload-operator-service.ps1",
        ]
        assert review_focus["companion_review_targets"] == ["eta_engine"]
        assert review_focus["companion_review_reasons"] == ["submodule_pointer_changed"]
        assert review_focus["companion_review_suggested_decisions"] == [
            "commit_preserve_or_pin_before_root_update",
        ]
        assert review_focus["companion_review_status"] == ""
        assert review_focus["companion_review_summary_line"] == ""
        assert review_focus["companion_review_verified_count"] == 0
        assert review_focus["companion_review_decision_required_count"] == 0
        assert review_focus["companion_review_details"][0] == {
            "target": "eta_engine",
            "reason": "submodule_pointer_changed",
            "rationale": (
                "The authoritative ETA child repo is dirty/diverged and needs "
                "a child-repo decision before the root gitlink is updated."
            ),
            "suggested_decision": "commit_preserve_or_pin_before_root_update",
            "status": "dirty",
            "branch": "codex/runtime-review",
            "head_short": "def5678",
            "tracked_change_count": 26,
            "untracked_change_count": 4,
            "summary_line": "eta dirty def5678 | 26 tracked, 4 untracked",
            "submodule_status": "aligned",
            "submodule_state_label": "aligned",
            "submodule_expected_short": "def5678",
            "wiring_status": "review",
            "wiring_summary": (
                "dirty/diverged child integration plus optional missing submodule checkout "
                "blocks gitlink wiring (eta_engine, mnq_backtest)"
            ),
            "status_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "inspection_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
        }
        assert review_focus["review_actions"][-1] == {
            "scope": "companion",
            "target": "eta_engine",
            "reason": "submodule_pointer_changed",
            "suggested_decision": "commit_preserve_or_pin_before_root_update",
            "command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "overview_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "overview_summary": "",
            "drilldown_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "review_sequence": [
                {
                    "step": 1,
                    "kind": "overview",
                    "label": "Batch overview",
                    "command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
                }
            ],
            "review_sequence_summary": "overview",
            "status_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "inspection_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "inspection_group_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "inspection_sample_paths": [],
            "inspection_sample_commands": [],
            "batch_scope_command": "",
            "batch_scope_stat_command": "",
            "batch_scope_shortstat": "",
            "batch_scope_paths": [],
            "batch_scope_path_count": 0,
            "inspection_focus": "",
            "summary_line": "eta dirty def5678 | 26 tracked, 4 untracked",
        }
        assert review_focus["primary_review_action"] == payload["review_focus_primary_action"]
        assert review_focus["source_step_id"] == "restore-source-governance"
        assert review_focus["source_step_decision"] == "manual_review_required"
        assert review_focus["companion_step_id"] == "align-submodules"
        assert review_focus["companion_step_decision"] == "manual_review_required"
        assert alias_payload["source_review_files"] == payload["source_review_files"]
        assert alias_payload["source_review_items"] == payload["source_review_items"]
        assert alias_payload["companion_review_targets"] == payload["companion_review_targets"]
        assert alias_payload["companion_review_items"] == payload["companion_review_items"]
        assert alias_payload["review_focus"] == payload["review_focus"]
        assert payload["recommended_action"] == "Keep root cleanup disabled until review is approved."
        assert payload["plan_updated_at"] is not None
        assert payload["plan_age_s"] is not None
        assert payload["artifact_stale"] is False

    def test_vps_root_reconciliation_endpoint_merges_observed_review_results(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_workspace_checkout_payload",
            lambda refresh=False: {
                "status": "clean",
                "summary_line": "root clean abc1234 | eta clean def5678 | submodule aligned def5678",
            },
        )
        plan = {
            "status": "ok",
            "risk_level": "medium",
            "cleanup_allowed": False,
            "destructive_actions_performed": False,
            "counts": {"status": 1, "dirty_companion_repos": 0},
            "summary": {"source_or_governance_modified": 1, "dirty_companion_repos": 1},
            "steps": [
                {
                    "id": "restore-source-governance",
                    "decision": "manual_review_required",
                    "evidence": [
                        "source_or_governance_modified=1",
                        "scripts/command-center-watchdog-status.ps1",
                    ],
                },
                {
                    "id": "align-submodules",
                    "decision": "manual_review_required",
                    "evidence": [
                        "dirty_companion_repos=1",
                        "eta_engine:dirty_diverged_child_integration",
                    ],
                },
            ],
            "source_review_items": [
                {
                    "path": "scripts/command-center-watchdog-status.ps1",
                    "basename": "command-center-watchdog-status.ps1",
                    "change_class": "modified",
                    "area": "operator_watchdog_truth",
                    "rationale": "Tracks Command Center watchdog truth.",
                    "change_summary": "Adds runtime dependency-gap probing.",
                    "verification_command": (
                        "powershell -ExecutionPolicy Bypass -File "
                        ".\\scripts\\command-center-watchdog-status.ps1 -Json"
                    ),
                    "verification_goal": "Confirm the live watchdog contract.",
                    "verification_mode": "status_probe",
                    "verification_side_effects": "refreshes the canonical watchdog receipt",
                    "suggested_decision": "preserve_if_it_matches_live_watchdog_contract",
                }
            ],
            "companion_review_items": [
                {
                    "target": "eta_engine",
                    "reason": "dirty_diverged_child_integration",
                    "rationale": (
                        "The authoritative ETA child repo is dirty/diverged and needs a child-repo "
                        "decision before the root gitlink is updated."
                    ),
                    "suggested_decision": "commit_preserve_or_pin_before_root_update",
                }
            ],
        }
        review = {
            "status": "ok",
            "verified_at": "2026-05-16T15:30:00+00:00",
            "verified_ok_count": 1,
            "preserve_candidate_count": 1,
            "summary_line": "1/1 source review item(s) verified; 1 preserve candidate(s)",
            "companion_review_status": "review_required",
            "companion_item_count": 1,
            "companion_verified_count": 1,
            "companion_decision_required_count": 1,
            "companion_summary_line": "1/1 companion review target(s) checked; 1 decision-required companion target(s)",
            "results_by_basename": {
                "command-center-watchdog-status.ps1": {
                    "basename": "command-center-watchdog-status.ps1",
                    "review_status": "verified_ok",
                    "review_summary": "Watchdog status probe succeeded; overall_status=healthy",
                    "recommended_outcome": "preserve_candidate",
                    "verified_at": "2026-05-16T15:30:00+00:00",
                    "exit_code": 0,
                    "output_excerpt": '{"overall_status":"healthy"}',
                }
            },
            "companion_results_by_target": {
                "eta_engine": {
                    "target": "eta_engine",
                    "reason": "dirty_diverged_child_integration",
                    "suggested_decision": "commit_preserve_or_pin_before_root_update",
                    "review_status": "verified_review_required",
                    "review_summary": (
                        "Companion eta_engine remains dirty/diverged; branch=detached; head=29f9c07; "
                        "tracked=26; untracked=6."
                    ),
                    "recommended_outcome": "commit_preserve_or_pin_before_root_update",
                    "verified_at": "2026-05-16T15:31:00+00:00",
                    "exit_code": 0,
                    "output_excerpt": " M eta_engine\\deploy\\scripts\\dashboard_api.py",
                    "tracked_change_count": 5,
                    "untracked_change_count": 2,
                    "tracked_files": [
                        "deploy/scripts/dashboard_api.py",
                        "scripts/broker_bracket_audit.py",
                        "scripts/project_kaizen_closeout.py",
                        "scripts/prop_launch_check.py",
                        "scripts/health_check.py",
                    ],
                    "untracked_files": [
                        "deploy/scripts/verify_vps_root_reconciliation.ps1",
                        "scripts/retune_advisory_cache.py",
                    ],
                    "change_groups": [
                        {
                            "group": "scripts",
                            "tracked_count": 4,
                            "untracked_count": 1,
                            "total_count": 5,
                            "sample_paths": [
                                "scripts/broker_bracket_audit.py",
                                "scripts/closed_trade_ledger.py",
                                "scripts/daily_loss_killswitch.py",
                            ],
                            "inspection_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts",
                        },
                        {
                            "group": "deploy/scripts",
                            "tracked_count": 1,
                            "untracked_count": 1,
                            "total_count": 2,
                            "sample_paths": [
                                "deploy/scripts/dashboard_api.py",
                                "deploy/scripts/verify_vps_root_reconciliation.ps1",
                            ],
                            "inspection_command": (
                                "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- "
                                "deploy/scripts"
                            ),
                        },
                    ],
                    "change_group_summary": "scripts t=4 u=1; deploy/scripts t=1 u=1",
                    "focus_areas": [
                        {
                            "area": "ops_readiness_truth",
                            "tracked_count": 3,
                            "untracked_count": 1,
                            "total_count": 4,
                            "sample_paths": [
                                "scripts/project_kaizen_closeout.py",
                                "scripts/prop_launch_check.py",
                                "scripts/health_check.py",
                            ],
                        },
                        {
                            "area": "operator_surface_reconciliation",
                            "tracked_count": 1,
                            "untracked_count": 1,
                            "total_count": 2,
                            "sample_paths": [
                                "deploy/scripts/dashboard_api.py",
                                "deploy/scripts/verify_vps_root_reconciliation.ps1",
                            ],
                        },
                        {
                            "area": "broker_runtime_controls",
                            "tracked_count": 1,
                            "untracked_count": 0,
                            "total_count": 1,
                            "sample_paths": [
                                "scripts/broker_bracket_audit.py",
                            ],
                        },
                    ],
                    "focus_area_summary": (
                        "ops_readiness_truth t=3 u=1; "
                        "operator_surface_reconciliation t=1 u=1; broker_runtime_controls t=1"
                    ),
                    "batch_label": "runtime_hardening_batch",
                    "batch_coherence": "coherent",
                    "batch_recommended_handling": "preserve_or_commit_as_single_child_batch",
                    "batch_summary": (
                        "top areas ops_readiness_truth + operator_surface_reconciliation "
                        "cover 6/7 file changes"
                    ),
                    "batch_top_areas": [
                        "ops_readiness_truth",
                        "operator_surface_reconciliation",
                    ],
                    "batch_scope_paths": [
                        "scripts",
                        "deploy/scripts",
                    ],
                    "batch_scope_command": (
                        "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts "
                        "deploy/scripts"
                    ),
                    "batch_scope_stat_command": (
                        "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff --shortstat -- "
                        "scripts deploy/scripts"
                    ),
                    "batch_scope_shortstat": (
                        "5 files changed, 123 insertions(+), 17 deletions(-); plus 2 "
                        "untracked path(s)"
                    ),
                    "batch_scope_path_count": 2,
                }
            },
        }
        (tmp_path / "state" / "vps_root_reconciliation_plan.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )
        (tmp_path / "state" / "vps_root_reconciliation_review.json").write_text(
            json.dumps(review), encoding="utf-8"
        )

        payload = app_client.get("/api/vps/root-reconciliation").json()
        master = app_client.get("/api/master/status").json()
        fleet = app_client.get("/api/bot-fleet").json()
        commit_command = (
            'git -C C:\\EvolutionaryTradingAlgo\\eta_engine commit -m '
            '"eta_engine: harden VPS runtime readiness and operator truth surfaces"'
        )
        decision_basis = (
            "top areas ops_readiness_truth + operator_surface_reconciliation cover 6/7 file changes; "
            "5 files changed, 123 insertions(+), 17 deletions(-); plus 2 untracked path(s)"
        )
        child_next_action = (
            "Prefer commit for eta_engine as one runtime-hardening child batch if the reviewed slice "
            "is intended shared child-repo work; otherwise preserve or pin it."
        )
        root_recommended_action = (
            "Prefer commit for eta_engine as one runtime-hardening child batch if this VPS-reviewed "
            "slice is intended shared child-repo work; otherwise preserve or pin it before updating "
            "the superproject root; root source preserve candidates are "
            "command-center-watchdog-status.ps1."
        )
        review_focus_summary_line = (
            "1 root source file(s) under review; eta_engine: Companion eta_engine remains "
            "dirty/diverged; branch=detached; head=29f9c07; tracked=26; untracked=6.; "
            "1 item(s) live-verified; 1 preserve candidate(s) pending decision"
        )
        companion_commit_detail = (
            'companion_commit_cmd=git -C C:\\EvolutionaryTradingAlgo\\eta_engine commit -m '
            '"eta_engine: harden VPS runtime readiness and operator truth surfaces"'
        )
        companion_next_action_detail = (
            "companion_next_action=Prefer commit for eta_engine as one runtime-hardening child "
            "batch if the reviewed slice is intended shared child-repo work; otherwise preserve "
            "or pin it."
        )

        assert payload["review_status"] == "ok"
        assert payload["review_summary_line"] == "1/1 source review item(s) verified; 1 preserve candidate(s)"
        assert payload["source_review_decision_ready"] is True
        assert payload["review_decision_ready"] is False
        assert payload["review_decision_blocked_by_companion"] is True
        assert payload["review_preserve_candidate_files"] == ["command-center-watchdog-status.ps1"]
        assert payload["review_revisit_required_files"] == []
        assert payload["companion_review_status"] == "review_required"
        assert payload["companion_review_decision_ready"] is True
        assert payload["companion_review_decision_summary"] == (
            "live-verified coherent runtime_hardening_batch ready for preserve/commit/pin decision"
        )
        assert payload["companion_review_decision_recommended_handling"] == (
            "preserve_or_commit_as_single_child_batch"
        )
        assert payload["companion_review_decision_recommended_option"] == "commit"
        assert payload["companion_review_decision_recommended_reason"] == (
            "eta_engine is a live-verified coherent runtime_hardening_batch; commit is the cleanest path "
            "if this reviewed slice is intended shared child-repo work."
        )
        assert payload["companion_review_decision_basis"] == decision_basis
        assert payload["companion_review_decision_recommended_paths"] == ["scripts", "deploy/scripts"]
        assert payload["companion_review_decision_recommended_commit_message"] == (
            "eta_engine: harden VPS runtime readiness and operator truth surfaces"
        )
        assert payload["companion_review_decision_recommended_stage_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine add -- scripts deploy/scripts"
        )
        assert payload["companion_review_decision_recommended_commit_command"] == commit_command
        assert payload["companion_review_decision_recommended_commands"] == [
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine add -- scripts deploy/scripts",
            commit_command,
        ]
        assert payload["companion_review_decision_options"][0] == {
            "option": "commit",
            "label": "Commit child batch",
            "summary": (
                "Commit eta_engine as one coherent runtime_hardening_batch inside the child repo, "
                "then the superproject can move the gitlink later."
            ),
            "when_to_choose": (
                "Choose when the reviewed changes are intended shared child-repo updates and the batch "
                "review is complete."
            ),
            "root_update_ready": True,
        }
        assert payload["companion_review_decision_next_action"] == child_next_action
        assert payload["companion_review_summary_line"] == (
            "1/1 companion review target(s) checked; 1 decision-required companion target(s)"
        )
        assert payload["recommended_action"] == root_recommended_action
        assert payload["review_focus_active_lane"] == "companion"
        assert payload["review_focus"]["companion_review_decision_ready"] is True
        assert payload["review_focus"]["companion_review_decision_summary"] == (
            "live-verified coherent runtime_hardening_batch ready for preserve/commit/pin decision"
        )
        assert payload["review_focus"]["companion_review_decision_recommended_handling"] == (
            "preserve_or_commit_as_single_child_batch"
        )
        assert payload["review_focus"]["companion_review_decision_recommended_option"] == "commit"
        assert payload["review_focus"]["companion_review_decision_recommended_reason"] == (
            "eta_engine is a live-verified coherent runtime_hardening_batch; commit is the cleanest path "
            "if this reviewed slice is intended shared child-repo work."
        )
        assert payload["review_focus"]["companion_review_decision_basis"] == decision_basis
        assert payload["review_focus"]["companion_review_decision_recommended_paths"] == [
            "scripts",
            "deploy/scripts",
        ]
        assert payload["review_focus"]["companion_review_decision_recommended_commit_message"] == (
            "eta_engine: harden VPS runtime readiness and operator truth surfaces"
        )
        assert payload["review_focus"]["companion_review_decision_recommended_stage_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine add -- scripts deploy/scripts"
        )
        assert payload["review_focus"]["companion_review_decision_recommended_commit_command"] == commit_command
        assert payload["review_focus"]["companion_review_decision_next_action"] == child_next_action
        assert payload["review_focus_summary_line"] == review_focus_summary_line
        assert payload["review_focus"]["observed_review_status"] == "ok"
        assert payload["review_focus"]["observed_review_summary_line"] == (
            "1/1 source review item(s) verified; 1 preserve candidate(s)"
        )
        assert payload["review_focus"]["companion_review_status"] == "review_required"
        assert payload["review_focus"]["companion_review_summary_line"] == (
            "1/1 companion review target(s) checked; 1 decision-required companion target(s)"
        )
        assert payload["review_focus"]["source_review_details"][0]["observed_review_status"] == "verified_ok"
        assert payload["review_focus"]["source_review_details"][0]["observed_recommended_outcome"] == (
            "preserve_candidate"
        )
        assert payload["review_focus"]["companion_review_details"][0]["reason"] == "dirty_diverged_child_integration"
        assert payload["review_focus"]["companion_review_details"][0]["suggested_decision"] == (
            "commit_preserve_or_pin_before_root_update"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_review_status"] == (
            "verified_review_required"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_recommended_outcome"] == (
            "commit_preserve_or_pin_before_root_update"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_tracked_change_count"] == 5
        assert payload["review_focus"]["companion_review_details"][0]["observed_untracked_change_count"] == 2
        assert payload["review_focus"]["companion_review_details"][0]["observed_tracked_files"] == [
            "deploy/scripts/dashboard_api.py",
            "scripts/broker_bracket_audit.py",
            "scripts/project_kaizen_closeout.py",
            "scripts/prop_launch_check.py",
            "scripts/health_check.py",
        ]
        assert payload["review_focus"]["companion_review_details"][0]["observed_untracked_files"] == [
            "deploy/scripts/verify_vps_root_reconciliation.ps1",
            "scripts/retune_advisory_cache.py",
        ]
        assert payload["review_focus"]["companion_review_details"][0]["observed_change_group_summary"] == (
            "scripts t=4 u=1; deploy/scripts t=1 u=1"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_focus_area_summary"] == (
            "ops_readiness_truth t=3 u=1; operator_surface_reconciliation t=1 u=1; broker_runtime_controls t=1"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_batch_label"] == (
            "runtime_hardening_batch"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_batch_coherence"] == "coherent"
        assert payload["review_focus"]["companion_review_details"][0]["observed_batch_recommended_handling"] == (
            "preserve_or_commit_as_single_child_batch"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_batch_summary"] == (
            "top areas ops_readiness_truth + operator_surface_reconciliation cover 6/7 file changes"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_batch_top_areas"] == [
            "ops_readiness_truth",
            "operator_surface_reconciliation",
        ]
        assert payload["review_focus"]["companion_review_details"][0]["observed_batch_scope_paths"] == [
            "scripts",
            "deploy/scripts",
        ]
        assert payload["review_focus"]["companion_review_details"][0]["observed_batch_scope_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts deploy/scripts"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_batch_scope_stat_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff --shortstat -- scripts deploy/scripts"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_batch_scope_shortstat"] == (
            "5 files changed, 123 insertions(+), 17 deletions(-); plus 2 untracked path(s)"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_focus_areas"][0]["area"] == (
            "ops_readiness_truth"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_focus_areas"][0]["sample_paths"] == [
            "scripts/project_kaizen_closeout.py",
            "scripts/prop_launch_check.py",
            "scripts/health_check.py",
        ]
        assert payload["review_focus"]["companion_review_details"][0]["observed_change_groups"][0]["group"] == (
            "scripts"
        )
        assert payload["review_focus"]["companion_review_details"][0]["observed_change_groups"][1]["group"] == (
            "deploy/scripts"
        )
        assert payload["review_focus"]["companion_review_details"][0]["status_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short"
        )
        assert payload["review_focus"]["companion_review_details"][0]["inspection_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts"
        )
        assert payload["review_focus"]["companion_review_details"][0]["inspection_focus"] == "scripts"
        assert payload["review_focus"]["companion_review_details"][0]["inspection_group_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts"
        )
        assert payload["review_focus"]["companion_review_details"][0]["inspection_sample_paths"] == [
            "scripts/project_kaizen_closeout.py",
            "scripts/prop_launch_check.py",
            "scripts/health_check.py",
        ]
        assert payload["review_focus"]["companion_review_details"][0]["inspection_sample_commands"] == [
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts/project_kaizen_closeout.py",
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts/prop_launch_check.py",
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts/health_check.py",
        ]
        assert payload["review_focus"]["active_review_lane"] == "companion"
        assert payload["review_focus_primary_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff --shortstat -- scripts deploy/scripts"
        )
        assert payload["review_focus_primary_action"]["scope"] == "companion"
        assert payload["review_focus_primary_action"]["command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff --shortstat -- scripts deploy/scripts"
        )
        assert payload["review_focus_primary_action"]["overview_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff --shortstat -- scripts deploy/scripts"
        )
        assert payload["review_focus_primary_action"]["overview_summary"] == (
            "5 files changed, 123 insertions(+), 17 deletions(-); plus 2 untracked path(s)"
        )
        assert payload["review_focus_primary_action"]["drilldown_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts deploy/scripts"
        )
        assert payload["review_focus_primary_action"]["review_sequence_summary"] == (
            "overview -> drilldown -> group -> 3 sample file diffs"
        )
        assert payload["review_focus_primary_action"]["review_sequence"] == [
            {
                "step": 1,
                "kind": "overview",
                "label": "Batch overview",
                "command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff --shortstat -- scripts deploy/scripts",
                "summary": "5 files changed, 123 insertions(+), 17 deletions(-); plus 2 untracked path(s)",
            },
            {
                "step": 2,
                "kind": "drilldown",
                "label": "Full batch diff",
                "command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts deploy/scripts",
                "path_count": 2,
                "paths": ["scripts", "deploy/scripts"],
            },
            {
                "step": 3,
                "kind": "group",
                "label": "Dominant group diff",
                "command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts",
                "focus": "scripts",
            },
            {
                "step": 4,
                "kind": "sample",
                "label": "Sample diff: project_kaizen_closeout.py",
                "command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts/project_kaizen_closeout.py",
                "path": "scripts/project_kaizen_closeout.py",
            },
            {
                "step": 5,
                "kind": "sample",
                "label": "Sample diff: prop_launch_check.py",
                "command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts/prop_launch_check.py",
                "path": "scripts/prop_launch_check.py",
            },
            {
                "step": 6,
                "kind": "sample",
                "label": "Sample diff: health_check.py",
                "command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts/health_check.py",
                "path": "scripts/health_check.py",
            },
        ]
        assert payload["review_focus_primary_action"]["status_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short"
        )
        assert payload["review_focus_primary_action"]["inspection_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts"
        )
        assert payload["review_focus_primary_action"]["inspection_focus"] == "scripts"
        assert payload["review_focus_primary_action"]["inspection_group_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts"
        )
        assert payload["review_focus_primary_action"]["inspection_sample_paths"] == [
            "scripts/project_kaizen_closeout.py",
            "scripts/prop_launch_check.py",
            "scripts/health_check.py",
        ]
        assert payload["review_focus_primary_action"]["inspection_sample_commands"] == [
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts/project_kaizen_closeout.py",
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts/prop_launch_check.py",
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts/health_check.py",
        ]
        assert payload["review_focus_primary_action"]["observed_review_status"] == "verified_review_required"
        assert payload["review_focus_primary_action"]["observed_recommended_outcome"] == (
            "commit_preserve_or_pin_before_root_update"
        )
        assert payload["review_focus_primary_action"]["observed_tracked_change_count"] == 5
        assert payload["review_focus_primary_action"]["observed_untracked_change_count"] == 2
        assert payload["review_focus_primary_action"]["observed_change_group_summary"] == (
            "scripts t=4 u=1; deploy/scripts t=1 u=1"
        )
        assert payload["review_focus_primary_action"]["observed_focus_area_summary"] == (
            "ops_readiness_truth t=3 u=1; operator_surface_reconciliation t=1 u=1; broker_runtime_controls t=1"
        )
        assert payload["review_focus_primary_action"]["observed_batch_label"] == "runtime_hardening_batch"
        assert payload["review_focus_primary_action"]["observed_batch_coherence"] == "coherent"
        assert payload["review_focus_primary_action"]["observed_batch_recommended_handling"] == (
            "preserve_or_commit_as_single_child_batch"
        )
        assert payload["review_focus_primary_action"]["observed_batch_summary"] == (
            "top areas ops_readiness_truth + operator_surface_reconciliation cover 6/7 file changes"
        )
        assert payload["review_focus_primary_action"]["observed_decision_recommended_option"] == "commit"
        assert payload["review_focus_primary_action"]["observed_decision_recommended_reason"] == (
            "eta_engine is a live-verified coherent runtime_hardening_batch; commit is the cleanest path "
            "if this reviewed slice is intended shared child-repo work."
        )
        assert payload["review_focus_primary_action"]["observed_decision_basis"] == (
            "top areas ops_readiness_truth + operator_surface_reconciliation cover 6/7 file changes; "
            "5 files changed, 123 insertions(+), 17 deletions(-); plus 2 untracked path(s)"
        )
        assert payload["review_focus_primary_action"]["observed_decision_recommended_paths"] == [
            "scripts",
            "deploy/scripts",
        ]
        assert payload["review_focus_primary_action"]["observed_decision_recommended_commit_message"] == (
            "eta_engine: harden VPS runtime readiness and operator truth surfaces"
        )
        assert payload["review_focus_primary_action"]["observed_decision_recommended_stage_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine add -- scripts deploy/scripts"
        )
        assert payload["review_focus_primary_action"]["observed_decision_recommended_commit_command"] == (
            commit_command
        )
        assert payload["review_focus_primary_action"]["observed_decision_recommended_commands"] == [
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine add -- scripts deploy/scripts",
            commit_command,
        ]
        assert payload["review_focus_primary_action"]["observed_decision_options"][0]["option"] == "commit"
        assert payload["review_focus_primary_action"]["observed_decision_options"][0]["root_update_ready"] is True
        assert payload["review_focus_primary_action"]["observed_batch_top_areas"] == [
            "ops_readiness_truth",
            "operator_surface_reconciliation",
        ]
        assert payload["review_focus_primary_action"]["batch_scope_paths"] == [
            "scripts",
            "deploy/scripts",
        ]
        assert payload["review_focus_primary_action"]["batch_scope_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts deploy/scripts"
        )
        assert payload["review_focus_primary_action"]["batch_scope_stat_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff --shortstat -- scripts deploy/scripts"
        )
        assert payload["review_focus_primary_action"]["batch_scope_shortstat"] == (
            "5 files changed, 123 insertions(+), 17 deletions(-); plus 2 untracked path(s)"
        )
        assert payload["review_focus_primary_action"]["observed_focus_areas"][0]["area"] == (
            "ops_readiness_truth"
        )
        assert master["systems"]["vps_root"]["source_review_decision_ready"] is True
        assert master["systems"]["vps_root"]["review_decision_ready"] is False
        assert master["systems"]["vps_root"]["review_decision_blocked_by_companion"] is True
        assert master["systems"]["vps_root"]["review_preserve_candidate_files"] == [
            "command-center-watchdog-status.ps1"
        ]
        assert master["systems"]["vps_root"]["review_revisit_required_files"] == []
        assert master["systems"]["vps_root"]["companion_review_status"] == "review_required"
        assert master["systems"]["vps_root"]["companion_review_decision_ready"] is True
        assert master["systems"]["vps_root"]["companion_review_decision_summary"] == (
            "live-verified coherent runtime_hardening_batch ready for preserve/commit/pin decision"
        )
        assert master["systems"]["vps_root"]["companion_review_decision_recommended_handling"] == (
            "preserve_or_commit_as_single_child_batch"
        )
        assert master["systems"]["vps_root"]["companion_review_decision_recommended_option"] == "commit"
        assert master["systems"]["vps_root"]["companion_review_decision_recommended_reason"] == (
            "eta_engine is a live-verified coherent runtime_hardening_batch; commit is the cleanest path "
            "if this reviewed slice is intended shared child-repo work."
        )
        assert master["systems"]["vps_root"]["companion_review_decision_basis"] == decision_basis
        assert master["systems"]["vps_root"]["companion_review_decision_recommended_paths"] == [
            "scripts",
            "deploy/scripts",
        ]
        assert master["systems"]["vps_root"]["companion_review_decision_recommended_commit_message"] == (
            "eta_engine: harden VPS runtime readiness and operator truth surfaces"
        )
        assert master["systems"]["vps_root"]["companion_review_decision_recommended_stage_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine add -- scripts deploy/scripts"
        )
        assert master["systems"]["vps_root"]["companion_review_decision_recommended_commit_command"] == (
            commit_command
        )
        assert master["systems"]["vps_root"]["companion_review_decision_next_action"] == child_next_action
        assert master["systems"]["vps_root"]["companion_review_summary_line"] == (
            "1/1 companion review target(s) checked; 1 decision-required companion target(s)"
        )
        assert master["systems"]["vps_root"]["review_focus_active_lane"] == "companion"
        assert master["systems"]["vps_root"]["recommended_action"] == root_recommended_action
        assert "review_status=ok" in master["systems"]["vps_root"]["detail"]
        assert (
            "review_verdict=1/1 source review item(s) verified; 1 preserve candidate(s)"
            in master["systems"]["vps_root"]["detail"]
        )
        assert "companion_review_status=review_required" in master["systems"]["vps_root"]["detail"]
        assert (
            "companion_review_verdict=1/1 companion review target(s) checked; 1 decision-required companion target(s)"
            in master["systems"]["vps_root"]["detail"]
        )
        assert "companion_decision_ready=true" in master["systems"]["vps_root"]["detail"]
        assert (
            "companion_decision=live-verified coherent runtime_hardening_batch ready for preserve/commit/pin decision"
            in master["systems"]["vps_root"]["detail"]
        )
        assert "companion_handling=preserve_or_commit_as_single_child_batch" in (
            master["systems"]["vps_root"]["detail"]
        )
        assert "companion_preferred=commit" in master["systems"]["vps_root"]["detail"]
        assert (
            "companion_commit_msg=eta_engine: harden VPS runtime readiness and operator truth surfaces"
            in master["systems"]["vps_root"]["detail"]
        )
        assert (
            "companion_stage_cmd=git -C C:\\EvolutionaryTradingAlgo\\eta_engine add -- scripts deploy/scripts"
            in master["systems"]["vps_root"]["detail"]
        )
        assert companion_commit_detail in master["systems"]["vps_root"]["detail"]
        assert companion_next_action_detail in master["systems"]["vps_root"]["detail"]
        assert "companion_groups=scripts t=4 u=1; deploy/scripts t=1 u=1" in master["systems"]["vps_root"]["detail"]
        assert (
            "companion_areas=ops_readiness_truth t=3 u=1; "
            "operator_surface_reconciliation t=1 u=1; broker_runtime_controls t=1"
        ) in master["systems"]["vps_root"]["detail"]
        assert "companion_batch=runtime_hardening_batch:coherent" in master["systems"]["vps_root"]["detail"]
        assert (
            "companion_batch_summary=top areas ops_readiness_truth + "
            "operator_surface_reconciliation cover 6/7 file changes"
        ) in master["systems"]["vps_root"]["detail"]
        assert "companion_batch_scope=scripts,deploy/scripts" in master["systems"]["vps_root"]["detail"]
        assert (
            "companion_batch_stat=5 files changed, 123 insertions(+), 17 deletions(-); "
            "plus 2 untracked path(s)"
        ) in master["systems"]["vps_root"]["detail"]
        assert "companion_samples=project_kaizen_closeout.py,prop_launch_check.py,health_check.py" in (
            master["systems"]["vps_root"]["detail"]
        )
        assert "review_lane=companion" in master["systems"]["vps_root"]["detail"]
        assert "source_review_decision_ready=true" in master["systems"]["vps_root"]["detail"]
        assert "; review_decision_ready=true" not in master["systems"]["vps_root"]["detail"]
        assert "review_decision_blocked_by_companion=true" in master["systems"]["vps_root"]["detail"]
        assert "preserve_candidates=command-center-watchdog-status.ps1" in master["systems"]["vps_root"]["detail"]
        assert (
            "review_cmd=git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff --shortstat -- "
            "scripts deploy/scripts"
        ) in master["systems"]["vps_root"]["detail"]
        assert (
            "review_overview=5 files changed, 123 insertions(+), 17 deletions(-); plus 2 "
            "untracked path(s)"
        ) in master["systems"]["vps_root"]["detail"]
        assert (
            "review_drilldown_cmd=git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- "
            "scripts deploy/scripts"
        ) in master["systems"]["vps_root"]["detail"]
        assert "review_sequence=overview -> drilldown -> group -> 3 sample file diffs" in (
            master["systems"]["vps_root"]["detail"]
        )
        assert "review_outcome=commit_preserve_or_pin_before_root_update" in master["systems"]["vps_root"]["detail"]
        assert fleet["summary"]["vps_root_source_review_decision_ready"] is True
        assert fleet["summary"]["vps_root_review_decision_ready"] is False
        assert fleet["summary"]["vps_root_review_decision_blocked_by_companion"] is True
        assert fleet["summary"]["vps_root_review_preserve_candidate_files"] == [
            "command-center-watchdog-status.ps1"
        ]
        assert fleet["summary"]["vps_root_review_revisit_required_files"] == []
        assert fleet["summary"]["vps_root_companion_review_status"] == "review_required"
        assert fleet["summary"]["vps_root_companion_review_decision_ready"] is True
        assert fleet["summary"]["vps_root_companion_review_decision_summary"] == (
            "live-verified coherent runtime_hardening_batch ready for preserve/commit/pin decision"
        )
        assert (
            fleet["summary"]["vps_root_companion_review_decision_recommended_handling"]
            == "preserve_or_commit_as_single_child_batch"
        )
        assert fleet["summary"]["vps_root_companion_review_decision_recommended_option"] == "commit"
        assert fleet["summary"]["vps_root_companion_review_decision_recommended_reason"] == (
            "eta_engine is a live-verified coherent runtime_hardening_batch; commit is the cleanest path "
            "if this reviewed slice is intended shared child-repo work."
        )
        assert fleet["summary"]["vps_root_companion_review_decision_basis"] == decision_basis
        assert fleet["summary"]["vps_root_companion_review_decision_recommended_paths"] == [
            "scripts",
            "deploy/scripts",
        ]
        assert fleet["summary"]["vps_root_companion_review_decision_recommended_commit_message"] == (
            "eta_engine: harden VPS runtime readiness and operator truth surfaces"
        )
        assert fleet["summary"]["vps_root_companion_review_decision_recommended_stage_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine add -- scripts deploy/scripts"
        )
        assert fleet["summary"]["vps_root_companion_review_decision_recommended_commit_command"] == (
            commit_command
        )
        assert fleet["summary"]["vps_root_companion_review_decision_next_action"] == child_next_action
        assert fleet["summary"]["vps_root_companion_review_summary_line"] == (
            "1/1 companion review target(s) checked; 1 decision-required companion target(s)"
        )
        assert fleet["summary"]["vps_root_review_focus_active_lane"] == "companion"
        assert fleet["summary"]["vps_root_recommended_action"] == root_recommended_action
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["observed_review_status"] == (
            "verified_review_required"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["observed_change_group_summary"] == (
            "scripts t=4 u=1; deploy/scripts t=1 u=1"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["observed_focus_area_summary"] == (
            "ops_readiness_truth t=3 u=1; operator_surface_reconciliation t=1 u=1; broker_runtime_controls t=1"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["observed_batch_label"] == (
            "runtime_hardening_batch"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["observed_batch_coherence"] == (
            "coherent"
        )
        assert (
            fleet["summary"]["vps_root_review_focus_primary_action"]["observed_batch_recommended_handling"]
            == "preserve_or_commit_as_single_child_batch"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["observed_batch_summary"] == (
            "top areas ops_readiness_truth + operator_surface_reconciliation cover 6/7 file changes"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["batch_scope_paths"] == [
            "scripts",
            "deploy/scripts",
        ]
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["batch_scope_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts deploy/scripts"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["batch_scope_stat_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff --shortstat -- scripts deploy/scripts"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["batch_scope_shortstat"] == (
            "5 files changed, 123 insertions(+), 17 deletions(-); plus 2 untracked path(s)"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["overview_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff --shortstat -- scripts deploy/scripts"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["overview_summary"] == (
            "5 files changed, 123 insertions(+), 17 deletions(-); plus 2 untracked path(s)"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["drilldown_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts deploy/scripts"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["review_sequence_summary"] == (
            "overview -> drilldown -> group -> 3 sample file diffs"
        )
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["inspection_sample_paths"] == [
            "scripts/project_kaizen_closeout.py",
            "scripts/prop_launch_check.py",
            "scripts/health_check.py",
        ]
        assert fleet["summary"]["vps_root_review_focus_primary_action"]["inspection_sample_commands"] == [
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts/project_kaizen_closeout.py",
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts/prop_launch_check.py",
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine diff -- scripts/health_check.py",
        ]

    def test_workspace_checkout_payload_preserves_aligned_submodule_prefix(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        expected_commit = "c08210db16fd0fd6f632a7c46c14168d883880fa"

        def fake_git(repo_path, *args):
            repo_text = str(repo_path)
            if args == ("rev-parse", "--show-toplevel"):
                return 0, repo_text, ""
            if args == ("rev-parse", "HEAD"):
                return 0, "e063e40b4f3f1aa0d00000000000000000000000", ""
            if args == ("branch", "--show-current"):
                return 0, "codex/test", ""
            if args == ("status", "--porcelain=v1"):
                return 0, "", ""
            if repo_text.endswith("EvolutionaryTradingAlgo") and args == ("submodule", "status", "eta_engine"):
                return 0, f" {expected_commit} eta_engine (heads/codex/symbol-intel-data-spine)", ""
            raise AssertionError(f"unexpected git command: repo={repo_text} args={args}")

        monkeypatch.setattr(mod, "_git_command", fake_git)
        monkeypatch.setattr(
            mod,
            "_workspace_submodule_wiring_payload",
            lambda: {
                "status": "aligned",
                "summary_line": "gitlink wiring ready",
                "summary_short": "ready",
                "modules": {},
            },
        )

        payload = mod._workspace_checkout_payload_uncached()

        assert payload["status"] == "clean"
        assert payload["submodule"]["status"] == "aligned"
        assert payload["submodule"]["state"] == " "
        assert payload["submodule"]["state_label"] == "aligned"
        assert payload["submodule"]["expected_commit"] == expected_commit
        assert payload["submodule"]["expected_short"] == expected_commit[:7]

    def test_vps_root_reconciliation_clean_root_is_clear_with_cleanup_locked(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_workspace_checkout_payload",
            lambda refresh=False: {
                "status": "clean",
                "summary_line": "root clean abc1234 | eta dirty def5678 | submodule aligned def5678",
                "eta_engine": {
                    "status": "dirty",
                    "branch": "codex/runtime-review",
                    "head_short": "def5678",
                    "tracked_change_count": 26,
                    "untracked_change_count": 4,
                    "summary_line": "eta dirty def5678 | 26 tracked, 4 untracked",
                },
                "submodule": {
                    "path": "eta_engine",
                    "status": "aligned",
                    "state_label": "aligned",
                    "expected_short": "def5678",
                },
                "wiring": {
                    "status": "review",
                    "summary_line": (
                        "dirty/diverged child integration plus optional missing submodule checkout "
                        "blocks gitlink wiring (eta_engine, mnq_backtest)"
                    ),
                },
            },
        )
        plan = {
            "status": "ok",
            "mode": "review_plan_only",
            "risk_level": "low",
            "cleanup_allowed": False,
            "destructive_actions_performed": False,
            "recommended_action": (
                "Rerun the read-only inventory and live probes; no root cleanup is authorized by this plan."
            ),
            "counts": {
                "status": 0,
                "submodule_drift": 0,
                "submodule_uninitialized": 4,
                "dirty_companion_repos": 0,
            },
            "summary": {
                "source_or_governance_deleted": 0,
                "unknown_deleted": 0,
                "generated_untracked": 0,
                "source_or_governance_untracked": 0,
                "submodule_drift": 0,
                "submodule_uninitialized": 4,
                "dirty_companion_repos": 0,
            },
            "steps": [
                {
                    "id": "freeze-and-backup",
                    "title": "Root cleanup remains locked; no dirty work detected",
                    "risk": "low",
                    "decision": "clear",
                    "action": "No root cleanup is needed.",
                    "evidence": ["status_count=0"],
                },
            ],
        }
        (tmp_path / "state" / "vps_root_reconciliation_plan.json").write_text(
            json.dumps(plan),
        )

        r = app_client.get("/api/master/status")
        bot_fleet = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        assert bot_fleet.status_code == 200
        payload = r.json()
        assert payload["vps_root_reconciliation"]["status"] == "ready_for_review"
        assert payload["vps_root_reconciliation"]["cleanup_allowed"] is False
        assert payload["vps_root_reconciliation"]["summary"]["submodule_uninitialized"] == 4
        assert payload["systems"]["vps_root"]["status"] == "GREEN"
        assert payload["systems"]["vps_root"]["review_focus_primary_command"] == ""
        assert payload["systems"]["vps_root"]["review_focus_primary_action"] == {}
        assert "generated_untracked=0" in payload["systems"]["vps_root"]["detail"]
        assert "status_rows=0" in payload["systems"]["vps_root"]["detail"]
        assert "dormant_submodules=4" in payload["systems"]["vps_root"]["detail"]
        bot_payload = bot_fleet.json()
        assert bot_payload["summary"]["vps_root_reconciliation_status"] == "ready_for_review"
        assert bot_payload["summary"]["vps_root_status_rows"] == 0
        assert bot_payload["summary"]["vps_root_generated_untracked"] == 0
        assert bot_payload["summary"]["vps_root_submodule_uninitialized"] == 4
        assert bot_payload["summary"]["vps_root_review_focus_primary_command"] == ""
        assert bot_payload["summary"]["vps_root_review_focus_primary_action"] == {}

    def test_vps_root_reconciliation_includes_live_checkout_health(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_workspace_checkout_payload",
            lambda refresh=False: {
                "status": "clean",
                "summary_line": "root clean dec9423 | eta clean c08210d | submodule aligned c08210d",
                "root": {
                    "status": "clean",
                    "dirty": False,
                    "head_short": "dec9423",
                },
                "eta_engine": {
                    "status": "clean",
                    "dirty": False,
                    "head_short": "c08210d",
                },
                "submodule": {
                    "status": "aligned",
                    "state": " ",
                    "state_label": "aligned",
                    "expected_short": "c08210d",
                },
            },
        )
        (tmp_path / "state" / "vps_root_reconciliation_plan.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "review_plan_only",
                    "risk_level": "low",
                    "cleanup_allowed": False,
                    "destructive_actions_performed": False,
                    "counts": {"status": 0, "submodule_drift": 0},
                    "summary": {
                        "source_or_governance_deleted": 0,
                        "unknown_deleted": 0,
                        "generated_untracked": 0,
                        "source_or_governance_untracked": 0,
                        "submodule_drift": 0,
                        "dirty_companion_repos": 0,
                    },
                    "steps": [],
                },
            ),
            encoding="utf-8",
        )

        response = app_client.get("/api/vps/root-reconciliation")

        assert response.status_code == 200
        payload = response.json()
        assert payload["live_checkout"]["status"] == "clean"
        assert payload["live_checkout"]["root"]["head_short"] == "dec9423"
        assert payload["live_checkout"]["eta_engine"]["head_short"] == "c08210d"
        assert payload["live_checkout"]["submodule"]["state_label"] == "aligned"
        assert "submodule aligned" in payload["live_checkout"]["summary_line"]

    def test_workspace_checkout_payload_uncached_preserves_aligned_submodule_marker(self, monkeypatch, tmp_path):
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        workspace = tmp_path / "workspace"
        eta_root = workspace / "eta_engine"
        eta_root.mkdir(parents=True, exist_ok=True)

        def fake_git_command(repo_path, *args):
            if args == ("rev-parse", "--show-toplevel"):
                return 0, str(repo_path), ""
            if args == ("rev-parse", "HEAD"):
                if Path(repo_path) == workspace:
                    return 0, "e063e40abcdef1234567890", ""
                return 0, "9cfb7d8abcdef1234567890", ""
            if args == ("branch", "--show-current"):
                if Path(repo_path) == workspace:
                    return 0, "codex/vps-data-pipeline-hardening", ""
                return 0, "codex/symbol-intel-data-spine", ""
            if args == ("status", "--porcelain=v1"):
                return 0, "", ""
            if args == ("submodule", "status", "eta_engine"):
                return 0, " 9cfb7d8abcdef1234567890 eta_engine (heads/codex/symbol-intel-data-spine)", ""
            raise AssertionError(f"unexpected git args: {args!r}")

        monkeypatch.setattr(mod, "_WORKSPACE_ROOT", workspace)
        monkeypatch.setattr(mod, "_REPO_ROOT", eta_root)
        monkeypatch.setattr(mod, "_git_command", fake_git_command)
        monkeypatch.setattr(
            mod,
            "_workspace_submodule_wiring_payload",
            lambda: {
                "status": "aligned",
                "summary_line": "gitlink wiring ready",
                "summary_short": "ready",
                "modules": {},
            },
        )

        payload = mod._workspace_checkout_payload_uncached()

        assert payload["status"] == "clean"
        assert payload["submodule"]["status"] == "aligned"
        assert payload["submodule"]["state"] == " "
        assert payload["submodule"]["state_label"] == "aligned"
        assert payload["submodule"]["expected_short"] == "9cfb7d8"

    def test_workspace_checkout_payload_uncached_filters_optional_dormant_root_deletion(self, monkeypatch, tmp_path):
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        workspace = tmp_path / "workspace"
        eta_root = workspace / "eta_engine"
        eta_root.mkdir(parents=True, exist_ok=True)

        def fake_git_command(repo_path, *args):
            if args == ("rev-parse", "--show-toplevel"):
                return 0, str(repo_path), ""
            if args == ("rev-parse", "HEAD"):
                if Path(repo_path) == workspace:
                    return 0, "e063e40abcdef1234567890", ""
                return 0, "9cfb7d8abcdef1234567890", ""
            if args == ("branch", "--show-current"):
                if Path(repo_path) == workspace:
                    return 0, "codex/vps-root-truth", ""
                return 0, "codex/symbol-intel-data-spine", ""
            if args == ("status", "--porcelain=v1"):
                if Path(repo_path) == workspace:
                    workspace_status = "\n".join(
                        [
                            " D mnq_backtest",
                            " M eta_engine",
                            " M scripts/verify_operator_source_of_truth.py",
                            "?? scripts/tmp_probe.py",
                        ]
                    )
                    return (
                        0,
                        workspace_status,
                        "",
                    )
                return 0, "", ""
            if args == ("submodule", "status", "eta_engine"):
                return 0, " 9cfb7d8abcdef1234567890 eta_engine (heads/codex/symbol-intel-data-spine)", ""
            raise AssertionError(f"unexpected git args: {args!r}")

        monkeypatch.setattr(mod, "_WORKSPACE_ROOT", workspace)
        monkeypatch.setattr(mod, "_REPO_ROOT", eta_root)
        monkeypatch.setattr(mod, "_git_command", fake_git_command)
        monkeypatch.setattr(
            mod,
            "_workspace_submodule_wiring_payload",
            lambda: {
                "status": "aligned",
                "summary_line": "gitlink wiring ready",
                "summary_short": "ready",
                "modules": {},
            },
        )

        payload = mod._workspace_checkout_payload_uncached()

        assert payload["status"] == "review"
        assert payload["root"]["change_count"] == 3
        assert payload["root"]["tracked_change_count"] == 2
        assert payload["root"]["non_companion_tracked_change_count"] == 1
        assert payload["root"]["companion_tracked_change_count"] == 1
        assert payload["root"]["untracked_change_count"] == 1
        assert payload["root"]["top_changes"] == [
            "eta_engine",
            "scripts/verify_operator_source_of_truth.py",
            "scripts/tmp_probe.py",
        ]
        assert payload["root"]["summary_line"] == (
            "root dirty e063e40 | 1 root tracked, 1 companion tracked, 1 untracked"
        )
        assert payload["summary_line"] == (
            "root dirty e063e40 | 1 root tracked, 1 companion tracked, 1 untracked | "
            "eta clean 9cfb7d8 | submodule aligned 9cfb7d8"
        )

    def test_classify_workspace_submodule_wiring_treats_optional_missing_as_review(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        payload = mod._classify_workspace_submodule_wiring(
            {
                "eta_engine": {"blockers": ["dirty worktree", "gitlink diverged"]},
                "firm": {"blockers": []},
                "mnq_backtest": {"blockers": ["missing submodule checkout", "gitlink uninitialized"]},
            }
        )

        assert payload["status"] == "review"
        assert payload["summary_short"] == "dirty/diverged integration + optional missing (eta_engine, mnq_backtest)"
        assert payload["summary_line"] == (
            "dirty/diverged child integration plus optional missing submodule checkout "
            "blocks gitlink wiring (eta_engine, mnq_backtest)"
        )

    def test_workspace_checkout_payload_uncached_includes_wiring_review_summary(self, monkeypatch, tmp_path):
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        workspace = tmp_path / "workspace"
        eta_root = workspace / "eta_engine"
        eta_root.mkdir(parents=True, exist_ok=True)

        def fake_git_command(repo_path, *args):
            if args == ("rev-parse", "--show-toplevel"):
                return 0, str(repo_path), ""
            if args == ("rev-parse", "HEAD"):
                if Path(repo_path) == workspace:
                    return 0, "e063e40abcdef1234567890", ""
                return 0, "9cfb7d8abcdef1234567890", ""
            if args == ("branch", "--show-current"):
                if Path(repo_path) == workspace:
                    return 0, "codex/vps-data-pipeline-hardening", ""
                return 0, "codex/symbol-intel-data-spine", ""
            if args == ("status", "--porcelain=v1"):
                return 0, "", ""
            if args == ("submodule", "status", "eta_engine"):
                return 0, " 9cfb7d8abcdef1234567890 eta_engine (heads/codex/symbol-intel-data-spine)", ""
            raise AssertionError(f"unexpected git args: {args!r}")

        monkeypatch.setattr(mod, "_WORKSPACE_ROOT", workspace)
        monkeypatch.setattr(mod, "_REPO_ROOT", eta_root)
        monkeypatch.setattr(mod, "_git_command", fake_git_command)
        monkeypatch.setattr(
            mod,
            "_workspace_submodule_wiring_payload",
            lambda: {
                "status": "review",
                "summary_line": (
                    "dirty/diverged child integration plus optional missing submodule checkout "
                    "blocks gitlink wiring (eta_engine, mnq_backtest)"
                ),
                "summary_short": "dirty/diverged integration + optional missing (eta_engine, mnq_backtest)",
                "modules": {},
            },
        )

        payload = mod._workspace_checkout_payload_uncached()

        assert payload["status"] == "review"
        assert payload["wiring"]["status"] == "review"
        assert (
            "wiring dirty/diverged integration + optional missing (eta_engine, mnq_backtest)"
            in payload["summary_line"]
        )

    def test_master_status_vps_root_card_warns_on_live_checkout_review(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_workspace_checkout_payload",
            lambda refresh=False: {
                "status": "review",
                "summary_line": "root clean dec9423 | eta dirty c08210d | 1 tracked | submodule aligned c08210d",
                "root": {
                    "status": "clean",
                    "dirty": False,
                    "head_short": "dec9423",
                },
                "eta_engine": {
                    "status": "dirty",
                    "dirty": True,
                    "head_short": "c08210d",
                    "tracked_change_count": 1,
                    "untracked_change_count": 0,
                },
                "submodule": {
                    "status": "aligned",
                    "state": " ",
                    "state_label": "aligned",
                    "expected_short": "c08210d",
                },
            },
        )
        (tmp_path / "state" / "vps_root_reconciliation_plan.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "review_plan_only",
                    "risk_level": "low",
                    "cleanup_allowed": False,
                    "destructive_actions_performed": False,
                    "counts": {"status": 0, "submodule_drift": 0, "dirty_companion_repos": 0},
                    "summary": {
                        "source_or_governance_deleted": 0,
                        "unknown_deleted": 0,
                        "generated_untracked": 0,
                        "source_or_governance_untracked": 0,
                        "submodule_drift": 0,
                        "dirty_companion_repos": 0,
                        "submodule_uninitialized": 0,
                    },
                    "steps": [],
                },
            ),
            encoding="utf-8",
        )

        response = app_client.get("/api/master/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["vps_root_reconciliation"]["live_checkout"]["status"] == "review"
        assert payload["systems"]["vps_root"]["status"] == "YELLOW"
        assert "live_checkout=review" in payload["systems"]["vps_root"]["detail"]
        assert "eta dirty c08210d" in payload["systems"]["vps_root"]["detail"]

    def test_master_status_vps_root_card_includes_live_checkout_wiring_detail(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_workspace_checkout_payload",
            lambda refresh=False: {
                "status": "review",
                "summary_line": (
                    "root clean dec9423 | eta dirty c08210d | submodule aligned c08210d | "
                    "wiring dirty/diverged integration + optional missing (eta_engine, mnq_backtest)"
                ),
                "wiring": {
                    "status": "review",
                    "summary_line": (
                        "dirty/diverged child integration plus optional missing submodule checkout "
                        "blocks gitlink wiring (eta_engine, mnq_backtest)"
                    ),
                },
            },
        )
        (tmp_path / "state" / "vps_root_reconciliation_plan.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "review_plan_only",
                    "risk_level": "low",
                    "cleanup_allowed": False,
                    "destructive_actions_performed": False,
                    "counts": {"status": 0, "submodule_drift": 0, "dirty_companion_repos": 0},
                    "summary": {
                        "source_or_governance_deleted": 0,
                        "unknown_deleted": 0,
                        "generated_untracked": 0,
                        "source_or_governance_untracked": 0,
                        "submodule_drift": 0,
                        "dirty_companion_repos": 0,
                        "submodule_uninitialized": 1,
                    },
                    "steps": [],
                },
            ),
            encoding="utf-8",
        )

        response = app_client.get("/api/master/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["systems"]["vps_root"]["status"] == "YELLOW"
        assert (
            "wiring=dirty/diverged child integration plus optional missing submodule checkout "
            "blocks gitlink wiring (eta_engine, mnq_backtest)"
        ) in payload["systems"]["vps_root"]["detail"]

    def test_vps_root_reconciliation_prefers_plan_recommended_action(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_workspace_checkout_payload",
            lambda refresh=False: {
                "status": "clean",
                "summary_line": "root clean abc1234 | eta dirty def5678 | submodule aligned def5678",
                "eta_engine": {
                    "status": "dirty",
                    "branch": "codex/runtime-review",
                    "head_short": "def5678",
                    "tracked_change_count": 26,
                    "untracked_change_count": 4,
                    "summary_line": "eta dirty def5678 | 26 tracked, 4 untracked",
                },
                "submodule": {
                    "path": "eta_engine",
                    "status": "aligned",
                    "state_label": "aligned",
                    "expected_short": "def5678",
                },
                "wiring": {
                    "status": "review",
                    "summary_line": (
                        "dirty/diverged child integration plus optional missing submodule checkout "
                        "blocks gitlink wiring (eta_engine, mnq_backtest)"
                    ),
                },
            },
        )
        plan = {
            "status": "ok",
            "mode": "review_plan_only",
            "risk_level": "medium",
            "cleanup_allowed": False,
            "destructive_actions_performed": False,
            "recommended_action": (
                "Review dirty companion worktrees and commit, preserve, or intentionally pin "
                "them before updating the superproject root."
            ),
            "counts": {"status": 4, "submodule_drift": 6, "dirty_companion_repos": 4},
            "summary": {
                "source_or_governance_deleted": 0,
                "unknown_deleted": 0,
                "submodule_drift": 6,
                "dirty_companion_repos": 4,
            },
            "steps": [
                {
                    "id": "freeze-and-backup",
                    "action": "Keep root cleanup disabled until source restore is approved.",
                },
            ],
        }
        (tmp_path / "state" / "vps_root_reconciliation_plan.json").write_text(json.dumps(plan))

        r = app_client.get("/api/vps/root-reconciliation")

        assert r.status_code == 200
        payload = r.json()
        assert payload["status"] == "review_required"
        assert payload["summary"]["source_or_governance_deleted"] == 0
        assert payload["summary"]["submodule_drift"] == 6
        assert payload["recommended_action"] == plan["recommended_action"]

    def test_vps_root_reconciliation_marks_old_review_plan_stale(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_workspace_checkout_payload",
            lambda refresh=False: {
                "status": "clean",
                "summary_line": "root clean abc1234 | eta dirty def5678 | submodule aligned def5678",
                "eta_engine": {
                    "status": "dirty",
                    "branch": "codex/runtime-review",
                    "head_short": "def5678",
                    "tracked_change_count": 26,
                    "untracked_change_count": 4,
                    "summary_line": "eta dirty def5678 | 26 tracked, 4 untracked",
                },
                "submodule": {
                    "path": "eta_engine",
                    "status": "aligned",
                    "state_label": "aligned",
                    "expected_short": "def5678",
                },
                "wiring": {
                    "status": "review",
                    "summary_line": (
                        "dirty/diverged child integration plus optional missing submodule checkout "
                        "blocks gitlink wiring (eta_engine, mnq_backtest)"
                    ),
                },
            },
        )
        plan_path = tmp_path / "state" / "vps_root_reconciliation_plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "risk_level": "medium",
                    "cleanup_allowed": False,
                    "destructive_actions_performed": False,
                    "counts": {"status": 3, "submodule_drift": 5, "dirty_companion_repos": 3},
                    "summary": {"submodule_drift": 5, "dirty_companion_repos": 3},
                    "steps": [],
                },
            ),
        )
        os.utime(plan_path, (1, 1))

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        vps_root = payload["vps_root_reconciliation"]
        assert vps_root["status"] == "stale_review_required"
        assert vps_root["artifact_stale"] is True
        assert vps_root["plan_age_s"] > 7200
        assert payload["systems"]["vps_root"]["status"] == "YELLOW"
        assert "artifact_stale=True" in payload["systems"]["vps_root"]["detail"]

    def test_master_status_includes_vps_root_reconciliation_card(self, app_client, tmp_path, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_workspace_checkout_payload",
            lambda refresh=False: {
                "status": "review",
                "summary_line": "root dirty abc1234 | eta dirty def5678 | submodule aligned def5678",
                "root": {
                    "status": "dirty",
                    "branch": "codex/root-review",
                    "head_short": "abc1234",
                    "tracked_change_count": 3,
                    "untracked_change_count": 0,
                    "summary_line": "root dirty abc1234 | 3 root tracked",
                },
                "eta_engine": {
                    "status": "dirty",
                    "branch": "detached",
                    "head_short": "def5678",
                    "tracked_change_count": 26,
                    "untracked_change_count": 4,
                    "summary_line": "eta dirty def5678 | 26 tracked, 4 untracked",
                },
                "submodule": {
                    "path": "eta_engine",
                    "status": "aligned",
                    "state_label": "aligned",
                    "expected_short": "def5678",
                },
                "wiring": {
                    "status": "review",
                    "summary_line": (
                        "dirty/diverged child integration plus optional missing submodule checkout "
                        "blocks gitlink wiring (eta_engine, mnq_backtest)"
                    ),
                },
            },
        )
        (tmp_path / "state" / "vps_root_reconciliation_plan.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "risk_level": "high",
                    "cleanup_allowed": False,
                    "destructive_actions_performed": False,
                    "counts": {
                        "status": 279,
                        "submodule_drift": 6,
                        "dirty_companion_repos": 3,
                        "optional_dormant_deleted_tracked": 1,
                    },
                    "summary": {
                        "source_or_governance_deleted": 124,
                        "source_or_governance_modified": 3,
                        "optional_dormant_deleted_tracked": 1,
                        "unknown_deleted": 2,
                        "submodule_drift": 6,
                        "dirty_companion_repos": 3,
                    },
                    "steps": [
                        {
                            "id": "restore-source-governance",
                            "title": "Review tracked source and governance modifications",
                            "risk": "medium",
                            "decision": "manual_review_required",
                            "action": "Review tracked root source/governance modifications.",
                            "evidence": [
                                "source_or_governance_deleted=124",
                                "source_or_governance_modified=3",
                                "scripts/command-center-watchdog-status.ps1",
                                "scripts/reload-operator-service.ps1",
                                "scripts/verify_operator_source_of_truth.py",
                            ],
                        },
                        {
                            "id": "align-submodules",
                            "title": "Align companion repositories",
                            "risk": "medium",
                            "decision": "manual_review_required",
                            "action": "Review dirty companion worktrees.",
                            "evidence": [
                                "submodule_drift=6",
                                "dirty_companion_repos=3",
                                "eta_engine:submodule_pointer_changed",
                            ],
                        }
                    ],
                    "source_review_items": [
                        {
                            "path": "scripts/command-center-watchdog-status.ps1",
                            "basename": "command-center-watchdog-status.ps1",
                            "change_class": "modified",
                            "area": "operator_watchdog_truth",
                            "rationale": (
                                "Tracks Command Center watchdog truth, task contract "
                                "drift, and public route health semantics."
                            ),
                            "change_summary": (
                                "Adds runtime dependency-gap probing, watchdog/dashboard "
                                "task-contract checks, and display-safe operator summaries."
                            ),
                            "verification_command": (
                                "powershell -ExecutionPolicy Bypass -File "
                                ".\\scripts\\command-center-watchdog-status.ps1 -Json"
                            ),
                        "verification_goal": (
                            "Confirm the live watchdog contract, task-contract status, "
                            "and display-safe operator summary on the authoritative host."
                        ),
                        "verification_mode": "status_probe",
                        "verification_side_effects": "refreshes the canonical watchdog receipt",
                        "suggested_decision": "preserve_if_it_matches_live_watchdog_contract",
                    },
                        {
                            "path": "scripts/reload-operator-service.ps1",
                            "basename": "reload-operator-service.ps1",
                            "change_class": "modified",
                            "area": "operator_reload_runtime",
                            "rationale": (
                                "Controls canonical 8421 reload behavior and the "
                                "task-owned Command Center runtime path on the VPS."
                            ),
                            "change_summary": (
                                "Replaces brittle raw 8421 waits with unified local-truth "
                                "verification and uses the canonical runtime Python."
                            ),
                            "verification_command": (
                                "powershell -ExecutionPolicy Bypass -File "
                                ".\\scripts\\reload-operator-service.ps1 "
                                "-SkipPublicCheck -SkipWatchdogRegistration "
                                "-NoAutoElevate -TimeoutSeconds 30"
                            ),
                        "verification_goal": (
                            "Confirm the VPS reload flow exits cleanly and re-verifies "
                            "the local 8421 operator contract."
                        ),
                        "verification_mode": "runtime_reload",
                        "verification_side_effects": (
                            "re-registers dashboard tasks, refreshes live service wiring, "
                            "and reloads the 8421 operator surface"
                        ),
                        "suggested_decision": "preserve_if_it_matches_live_8421_reload_flow",
                    },
                        {
                            "path": "scripts/verify_operator_source_of_truth.py",
                            "basename": "verify_operator_source_of_truth.py",
                            "change_class": "modified",
                            "area": "operator_contract_verification",
                            "rationale": (
                                "Verifies operator truth surfaces, including transient "
                                "endpoint retries and upstream failure classification."
                            ),
                            "change_summary": (
                                "Adds transient endpoint retries, upstream 5xx "
                                "classification, and display-summary leakage checks."
                            ),
                            "verification_command": (
                                "python .\\scripts\\verify_operator_source_of_truth.py "
                                "--base-url http://127.0.0.1:8421 --timeout 30"
                            ),
                        "verification_goal": (
                            "Confirm the canonical local operator verifier accepts "
                            "the live 8421 payloads and failure classification contract."
                        ),
                        "verification_mode": "read_only_contract_probe",
                        "verification_side_effects": "none",
                        "suggested_decision": "preserve_if_it_matches_current_operator_contract",
                    },
                    ],
                    "companion_review_items": [
                        {
                            "target": "eta_engine",
                            "reason": "submodule_pointer_changed",
                            "rationale": (
                                "The authoritative ETA child repo is dirty/diverged "
                                "and needs a child-repo decision before the root "
                                "gitlink is updated."
                            ),
                            "suggested_decision": "commit_preserve_or_pin_before_root_update",
                        }
                    ],
                },
            ),
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["vps_root_reconciliation"]["status"] == "review_required"
        assert payload["systems"]["vps_root"]["status"] == "YELLOW"
        assert payload["systems"]["vps_root"]["source"] == "vps_root_reconciliation"
        assert payload["systems"]["vps_root"]["source_review_files"] == [
            "command-center-watchdog-status.ps1",
            "reload-operator-service.ps1",
            "verify_operator_source_of_truth.py",
        ]
        assert (
            payload["systems"]["vps_root"]["source_review_items"][0]["basename"]
            == "command-center-watchdog-status.ps1"
        )
        assert payload["systems"]["vps_root"]["companion_review_targets"] == ["eta_engine"]
        assert payload["systems"]["vps_root"]["companion_review_items"][0]["target"] == "eta_engine"
        assert payload["systems"]["vps_root"]["review_focus_summary_line"] == (
            "root dirty abc1234 | 3 root tracked; "
            "eta_engine: eta dirty def5678 | 26 tracked, 4 untracked; "
            "manual review before root update"
        )
        assert payload["systems"]["vps_root"]["review_focus_primary_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo diff -- scripts/command-center-watchdog-status.ps1"
        )
        assert payload["systems"]["vps_root"]["review_focus_primary_action"] == {
            "scope": "source",
            "path": "scripts/command-center-watchdog-status.ps1",
            "basename": "command-center-watchdog-status.ps1",
            "area": "operator_watchdog_truth",
            "change_summary": (
                "Adds runtime dependency-gap probing, watchdog/dashboard "
                "task-contract checks, and display-safe operator summaries."
            ),
            "verification_command": (
                "powershell -ExecutionPolicy Bypass -File "
                ".\\scripts\\command-center-watchdog-status.ps1 -Json"
            ),
            "verification_goal": (
                "Confirm the live watchdog contract, task-contract status, "
                "and display-safe operator summary on the authoritative host."
            ),
            "verification_mode": "status_probe",
            "verification_side_effects": "refreshes the canonical watchdog receipt",
            "suggested_decision": "preserve_if_it_matches_live_watchdog_contract",
            "command": "git -C C:\\EvolutionaryTradingAlgo diff -- scripts/command-center-watchdog-status.ps1",
        }
        review_focus = payload["systems"]["vps_root"]["review_focus"]
        assert review_focus["source_modified_count"] == 3
        assert review_focus["dirty_companion_repos"] == 3
        assert review_focus["source_review_item_count"] == 3
        assert review_focus["companion_review_item_count"] == 1
        assert review_focus["source_review_context"] == {
            "status": "dirty",
            "branch": "codex/root-review",
            "head_short": "abc1234",
            "tracked_change_count": 3,
            "untracked_change_count": 0,
            "summary_line": "root dirty abc1234 | 3 root tracked",
        }
        assert review_focus["summary_line"] == payload["systems"]["vps_root"]["review_focus_summary_line"]
        assert review_focus["companion_review_targets"] == ["eta_engine"]
        assert review_focus["companion_review_reasons"] == ["submodule_pointer_changed"]
        assert review_focus["companion_review_suggested_decisions"] == [
            "commit_preserve_or_pin_before_root_update",
        ]
        assert review_focus["companion_review_status"] == ""
        assert review_focus["companion_review_summary_line"] == ""
        assert review_focus["companion_review_details"][0] == {
            "target": "eta_engine",
            "reason": "submodule_pointer_changed",
            "rationale": (
                "The authoritative ETA child repo is dirty/diverged "
                "and needs a child-repo decision before the root "
                "gitlink is updated."
            ),
            "suggested_decision": "commit_preserve_or_pin_before_root_update",
            "status": "dirty",
            "branch": "detached",
            "head_short": "def5678",
            "tracked_change_count": 26,
            "untracked_change_count": 4,
            "summary_line": "eta dirty def5678 | 26 tracked, 4 untracked",
            "submodule_status": "aligned",
            "submodule_state_label": "aligned",
            "submodule_expected_short": "def5678",
            "wiring_status": "review",
            "wiring_summary": (
                "dirty/diverged child integration plus optional missing submodule checkout "
                "blocks gitlink wiring (eta_engine, mnq_backtest)"
            ),
            "status_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "inspection_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
        }
        assert review_focus["review_actions"][-1] == {
            "scope": "companion",
            "target": "eta_engine",
            "reason": "submodule_pointer_changed",
            "suggested_decision": "commit_preserve_or_pin_before_root_update",
            "command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "overview_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "overview_summary": "",
            "drilldown_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "review_sequence": [
                {
                    "step": 1,
                    "kind": "overview",
                    "label": "Batch overview",
                    "command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
                }
            ],
            "review_sequence_summary": "overview",
            "status_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "inspection_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "inspection_group_command": "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short",
            "inspection_sample_paths": [],
            "inspection_sample_commands": [],
            "batch_scope_command": "",
            "batch_scope_stat_command": "",
            "batch_scope_shortstat": "",
            "batch_scope_paths": [],
            "batch_scope_path_count": 0,
            "inspection_focus": "",
            "summary_line": "eta dirty def5678 | 26 tracked, 4 untracked",
        }
        assert review_focus["primary_review_action"] == payload["systems"]["vps_root"]["review_focus_primary_action"]
        assert review_focus["source_step_id"] == "restore-source-governance"
        assert review_focus["source_step_decision"] == "manual_review_required"
        assert review_focus["companion_step_id"] == "align-submodules"
        assert review_focus["companion_step_decision"] == "manual_review_required"
        assert "source_deleted=124" in payload["systems"]["vps_root"]["detail"]
        assert "source_modified=3" in payload["systems"]["vps_root"]["detail"]
        assert "optional_dormant_deleted=1" in payload["systems"]["vps_root"]["detail"]
        assert (
            "source_review_files=command-center-watchdog-status.ps1,reload-operator-service.ps1,"
            "verify_operator_source_of_truth.py"
        ) in payload["systems"]["vps_root"]["detail"]
        assert "companion_review_targets=eta_engine" in payload["systems"]["vps_root"]["detail"]
        assert (
            "review_focus=root dirty abc1234 | 3 root tracked; eta_engine: eta dirty def5678 | "
            "26 tracked, 4 untracked; manual review before root update"
        ) in payload["systems"]["vps_root"]["detail"]
        assert (
            "review_cmd=git -C C:\\EvolutionaryTradingAlgo diff -- scripts/command-center-watchdog-status.ps1"
        ) in payload["systems"]["vps_root"]["detail"]
        assert "review_scope=source" in payload["systems"]["vps_root"]["detail"]
        assert "dirty_companions=3" in payload["systems"]["vps_root"]["detail"]

    def test_master_status_marks_vps_root_yellow_when_live_checkout_needs_review(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_workspace_checkout_payload",
            lambda refresh=False: {
                "status": "review",
                "summary_line": "root clean abc1234 | eta dirty def5678 | submodule aligned c08210d",
            },
        )
        (tmp_path / "state" / "vps_root_reconciliation_plan.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "risk_level": "low",
                    "cleanup_allowed": False,
                    "destructive_actions_performed": False,
                    "counts": {"status": 0, "submodule_drift": 0, "dirty_companion_repos": 0},
                    "summary": {"generated_untracked": 0, "submodule_uninitialized": 1},
                    "steps": [],
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/master/status")

        assert r.status_code == 200
        payload = r.json()
        assert payload["systems"]["vps_root"]["status"] == "YELLOW"
        assert "live_checkout=review" in payload["systems"]["vps_root"]["detail"]
        assert "eta dirty def5678" in payload["systems"]["vps_root"]["detail"]

    def test_runtime_and_bridge_status_use_local_master_payload(self, app_client, tmp_path):
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-05-07T23:40:00+00:00",
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 5,
                    "gates": [],
                }
            )
        )

        runtime = app_client.get("/api/runtime-status")
        bridge = app_client.get("/api/bridge-status")

        assert runtime.status_code == 200
        assert runtime.json()["mode"] == "paper_live"
        assert bridge.status_code == 200
        assert bridge.json()["paper"]["status"] == "ready_to_launch_paper_live"

    def test_kaizen_summary(self, app_client):
        r = app_client.get("/api/kaizen")
        assert r.status_code == 200
        j = r.json()
        assert j["retrospectives"] == 1
        assert j["tickets_total"] == 1
        assert j["tickets_open"] == 1

    def test_tasks_list(self, app_client):
        r = app_client.get("/api/tasks")
        assert r.status_code == 200
        assert len(r.json()["tasks"]) == 25

    def test_fire_unknown_task(self, app_client):
        r = app_client.post("/api/tasks/nonsense/fire")
        # The pre-cutover hardening (commit ee41d98) added an auth gate
        # in front of the /api/tasks/* routes. Unauthenticated requests
        # now get 401 before the route handler can return 404. Either
        # status is a refusal -- accept both since the contract that
        # callers care about is "an unauthenticated bad task is rejected".
        assert r.status_code in (401, 404)

    def test_state_file_safelist(self, app_client):
        r = app_client.get("/api/state/random_file.json")
        assert r.status_code == 403

    def test_state_file_allowed(self, app_client):
        r = app_client.get("/api/state/avengers_heartbeat.json")
        assert r.status_code == 200

    def test_missing_state_file(self, app_client):
        r = app_client.get("/api/state/shadow_ledger.json")
        assert r.status_code == 404

    # ------------------------------------------------------------------ #
    # JARVIS Decision Log endpoints
    # ------------------------------------------------------------------ #

    def test_jarvis_decisions_empty_returns_note(self, app_client):
        r = app_client.get("/api/jarvis/decisions")
        assert r.status_code == 200
        j = r.json()
        assert j["decisions"] == []
        assert "no jarvis audit log yet" in j["note"]

    def test_jarvis_summary_empty_returns_zero(self, app_client):
        r = app_client.get("/api/jarvis/summary")
        assert r.status_code == 200
        j = r.json()
        assert j["total"] == 0
        assert j["by_subsystem"] == {}
        assert j["by_verdict"] == {}

    def test_jarvis_decisions_tails_audit_log(self, tmp_path, app_client):
        """Seed an audit log and verify the endpoint returns it newest-first."""
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        audit_dir = state / "jarvis_audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit = audit_dir / f"{today}.jsonl"
        # 3 entries: one approved, one denied, one conditional
        rows = [
            {
                "ts": "2026-04-24T10:00:00+00:00",
                "request": {"subsystem": "bot.mnq", "action": "ORDER_PLACE"},
                "response": {
                    "verdict": "APPROVED",
                    "reason_code": "ok",
                    "reason": "all clear",
                    "size_cap_mult": None,
                },
                "stress_composite": 0.1,
                "session_phase": "MORNING",
                "jarvis_action": "TRADE",
            },
            {
                "ts": "2026-04-24T10:01:00+00:00",
                "request": {"subsystem": "bot.btc_hybrid", "action": "ORDER_PLACE"},
                "response": {
                    "verdict": "CONDITIONAL",
                    "reason_code": "dd_reduce",
                    "reason": "daily dd triggered reduce",
                    "size_cap_mult": 0.5,
                },
                "stress_composite": 0.55,
                "session_phase": "OVERNIGHT",
                "jarvis_action": "REDUCE",
            },
            {
                "ts": "2026-04-24T10:02:00+00:00",
                "request": {"subsystem": "bot.mnq", "action": "ORDER_PLACE"},
                "response": {
                    "verdict": "DENIED",
                    "reason_code": "kill_blocks_all",
                    "reason": "kill switch active",
                    "size_cap_mult": None,
                },
                "stress_composite": 0.95,
                "session_phase": "MORNING",
                "jarvis_action": "KILL",
            },
        ]
        audit.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n",
            encoding="utf-8",
        )

        r = app_client.get("/api/jarvis/decisions?n=10")
        assert r.status_code == 200
        j = r.json()
        assert j["total"] == 3
        assert j["returned"] == 3
        # Newest first
        assert j["decisions"][0]["verdict"] == "DENIED"
        assert j["decisions"][1]["verdict"] == "CONDITIONAL"
        assert j["decisions"][2]["verdict"] == "APPROVED"
        assert j["decisions"][1]["size_cap_mult"] == 0.5

    def test_jarvis_decisions_subsystem_filter(self, tmp_path, app_client):
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        audit_dir = state / "jarvis_audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit = audit_dir / f"{today}.jsonl"
        rows = [
            {
                "ts": "2026-04-24T10:00:00+00:00",
                "request": {"subsystem": "bot.mnq", "action": "ORDER_PLACE"},
                "response": {
                    "verdict": "APPROVED",
                    "reason_code": "ok",
                    "reason": "all clear",
                    "size_cap_mult": None,
                },
                "stress_composite": 0.1,
                "session_phase": "MORNING",
                "jarvis_action": "TRADE",
            },
            {
                "ts": "2026-04-24T10:01:00+00:00",
                "request": {"subsystem": "bot.btc_hybrid", "action": "ORDER_PLACE"},
                "response": {
                    "verdict": "APPROVED",
                    "reason_code": "ok",
                    "reason": "all clear",
                    "size_cap_mult": None,
                },
                "stress_composite": 0.1,
                "session_phase": "OVERNIGHT",
                "jarvis_action": "TRADE",
            },
        ]
        audit.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n",
            encoding="utf-8",
        )

        r = app_client.get("/api/jarvis/decisions?subsystem=bot.mnq")
        assert r.status_code == 200
        j = r.json()
        assert j["returned"] == 1
        assert j["decisions"][0]["subsystem"] == "bot.mnq"

    def test_jarvis_summary_aggregates(self, tmp_path, app_client):
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        audit_dir = state / "jarvis_audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit = audit_dir / f"{today}.jsonl"
        rows = [
            {
                "request": {"subsystem": "bot.mnq"},
                "response": {"verdict": "APPROVED"},
            },
            {
                "request": {"subsystem": "bot.mnq"},
                "response": {"verdict": "APPROVED"},
            },
            {
                "request": {"subsystem": "bot.mnq"},
                "response": {"verdict": "DENIED"},
            },
            {
                "request": {"subsystem": "bot.btc_hybrid"},
                "response": {"verdict": "CONDITIONAL"},
            },
        ]
        audit.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n",
            encoding="utf-8",
        )

        r = app_client.get("/api/jarvis/summary?window=100")
        assert r.status_code == 200
        j = r.json()
        assert j["total"] == 4
        assert j["by_subsystem"]["bot.mnq"] == 3
        assert j["by_subsystem"]["bot.btc_hybrid"] == 1
        assert j["by_verdict"]["APPROVED"] == 2
        assert j["by_verdict"]["DENIED"] == 1
        assert j["by_verdict"]["CONDITIONAL"] == 1
        assert j["by_sub_verdict"]["bot.mnq"]["APPROVED"] == 2
        assert j["by_sub_verdict"]["bot.mnq"]["DENIED"] == 1

    def test_recent_verdict_rows_from_log_reads_canonical_daily_audit_dir(self, app_client):
        import os
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        state = Path(os.environ["ETA_STATE_DIR"])
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        audit_dir = state / "jarvis_audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / f"{today}.jsonl").write_text(
            json.dumps(
                {
                    "ts": "2026-05-15T10:00:00+00:00",
                    "bot_id": "mnq_futures_sage",
                    "request": {
                        "subsystem": "bot.mnq",
                        "action": "ORDER_PLACE",
                    },
                    "response": {
                        "verdict": "APPROVED",
                        "reason": "canonical daily audit hit",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        rows = mod._recent_verdict_rows_from_log(bot_id="mnq_futures_sage", limit=5)

        assert len(rows) == 1
        assert rows[0]["verdict"] == "APPROVED"
        assert rows[0]["reason"] == "canonical daily audit hit"

    # ------------------------------------------------------------------ #
    # Broker readiness + BTC fleet endpoints
    # ------------------------------------------------------------------ #

    def test_brokers_endpoint_returns_futures_focus_readiness_reports(self, app_client):
        r = app_client.get("/api/brokers")
        assert r.status_code == 200
        j = r.json()
        # Alpaca remains importable, but is paused in the cellar while the
        # operator focuses on regulated futures, CME crypto futures, and
        # commodities.
        assert set(j["brokers"].keys()) == {"ibkr", "tastytrade", "alpaca"}
        # All three adapters must at least be importable -- they all carry
        # `adapter_available=True` in their readiness output.
        assert j["brokers"]["ibkr"]["adapter_available"] is True
        assert j["brokers"]["tastytrade"]["adapter_available"] is True
        assert j["brokers"]["alpaca"]["adapter_available"] is True
        assert j["brokers"]["alpaca"]["policy_status"] == "paused_cellar"
        assert "alpaca" in j["paused_brokers"]
        assert "alpaca" not in j["active_brokers"]
        assert j["pending_brokers"] == []
        assert j["dormant_brokers"] == ["tradovate"]
        # active_brokers is a sorted list of ready names (may be empty
        # in a test env with no creds, which is fine).
        assert isinstance(j["active_brokers"], list)

    def test_btc_lanes_empty_when_fleet_dir_absent(self, app_client):
        r = app_client.get("/api/btc/lanes")
        assert r.status_code == 200
        j = r.json()
        # No fleet artifacts exist under the test STATE_DIR -- endpoint
        # must respond with an empty-list structure + note, not 500.
        assert j["lanes"] == []
        assert "fleet dir" in j.get("note", "") or j.get("manifest") is None

    def test_btc_lanes_reads_state_files(self, tmp_path, app_client):
        """Seed a fleet dir under STATE_DIR/broker_fleet and verify the endpoint
        returns the lane snapshots."""
        import os
        from datetime import UTC, datetime
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        fleet_dir = state / "broker_fleet"
        fleet_dir.mkdir(parents=True, exist_ok=True)

        # Manifest
        (fleet_dir / "btc_broker_fleet_latest.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": datetime.now(UTC).isoformat(),
                    "fleet": "btc_broker_paper_fleet",
                    "requested_workers": 4,
                    "running_workers": 2,
                    "paper_balance_tracking": "not_tracked",
                    "paper_balance_note": (
                        "starting cash is configuration only; current worker cash/equity "
                        "is not tracked by the BTC broker-paper heartbeat"
                    ),
                }
            ),
            encoding="utf-8",
        )

        # Two lane state files
        (fleet_dir / "btc-grid-ibkr.lane.json").write_text(
            json.dumps(
                {
                    "worker_id": "btc-grid-ibkr",
                    "broker": "ibkr",
                    "lane": "grid",
                    "symbol": "BTCUSD",
                    "active_order_id": "srv-I-1",
                    "active_order_status": "OPEN",
                    "active_order_filled_qty": 0.0,
                    "active_order_avg_price": 0.0,
                    "submitted_orders": 1,
                    "reconciled_orders": 3,
                    "terminal_orders": 0,
                    "last_event": "submitted:OPEN",
                    "last_event_utc": "2026-04-24T10:00:05+00:00",
                    "last_reconcile_utc": "2026-04-24T10:00:30+00:00",
                }
            ),
            encoding="utf-8",
        )
        (fleet_dir / "btc-directional-tastytrade.lane.json").write_text(
            json.dumps(
                {
                    "worker_id": "btc-directional-tastytrade",
                    "broker": "tastytrade",
                    "lane": "directional",
                    "symbol": "BTCUSD",
                    "active_order_id": None,
                    "active_order_status": "NONE",
                    "submitted_orders": 0,
                    "reconciled_orders": 0,
                    "terminal_orders": 0,
                    "last_event": "",
                    "last_event_utc": "",
                    "last_reconcile_utc": "2026-04-24T10:00:30+00:00",
                }
            ),
            encoding="utf-8",
        )
        # Heartbeat for one of them
        (fleet_dir / "btc-grid-ibkr.json").write_text(
            json.dumps(
                {
                    "worker_id": "btc-grid-ibkr",
                    "status": "RUNNING",
                    "pid": 12345,
                    "execution_state": "ACTIVE",
                    "paper_starting_cash": 5000.0,
                    "paper_cash": None,
                    "paper_equity": None,
                    "paper_balance_tracking": "not_tracked",
                    "paper_balance_note": (
                        "starting cash is configuration only; worker heartbeat does not "
                        "mark cash/equity to market"
                    ),
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/btc/lanes")
        assert r.status_code == 200
        j = r.json()
        assert j["lane_count"] == 2
        assert j["manifest_stale"] is False
        assert j["manifest_stale_after_s"] > 0
        assert j["paper_balance_tracking"] == "not_tracked"
        # Sorted by filename so directional comes first (d < g)
        directional = next(lane for lane in j["lanes"] if lane["lane"] == "directional")
        grid = next(lane for lane in j["lanes"] if lane["lane"] == "grid")
        assert directional["broker"] == "tastytrade"
        assert grid["broker"] == "ibkr"
        assert grid["active_order_id"] == "srv-I-1"
        assert grid["heartbeat_status"] == "RUNNING"
        assert grid["pid"] == 12345
        assert grid["execution_state"] == "ACTIVE"
        assert grid["paper_cash"] is None
        assert grid["paper_equity"] is None
        assert grid["paper_balance_tracking"] == "not_tracked"
        assert j["manifest"]["fleet"] == "btc_broker_paper_fleet"

    def test_btc_lanes_marks_stale_manifest(self, tmp_path, app_client):
        import os
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        fleet_dir = state / "broker_fleet"
        fleet_dir.mkdir(parents=True, exist_ok=True)

        (fleet_dir / "btc_broker_fleet_latest.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                    "fleet": "btc_broker_paper_fleet",
                    "requested_workers": 4,
                    "running_workers": 0,
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/btc/lanes")
        assert r.status_code == 200
        j = r.json()
        assert j["manifest_stale"] is True
        assert j["manifest_age_s"] is not None
        assert j["manifest_age_s"] > j["manifest_stale_after_s"]

    def test_btc_lanes_does_not_fall_back_to_repo_docs_when_state_dir_is_overridden(
        self,
        tmp_path,
        monkeypatch,
    ):
        import importlib

        monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("ETA_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("ETA_DASHBOARD_DISABLE_BROKER_PROBES", "1")
        monkeypatch.delenv("ETA_BTC_FLEET_DIR", raising=False)
        (tmp_path / "state").mkdir()
        (tmp_path / "logs").mkdir()

        import eta_engine.deploy.scripts.dashboard_api as mod

        importlib.reload(mod)
        try:
            state = tmp_path / "state"
            assert state != mod._DEFAULT_STATE
            docs_fleet_dir = mod._WORKSPACE_ROOT / "eta_engine" / "docs" / "btc_live" / "broker_fleet"
            assert docs_fleet_dir.exists()

            with TestClient(mod.app) as client:
                r = client.get("/api/btc/lanes")
                assert r.status_code == 200
                j = r.json()
                assert j["lanes"] == []
                assert j["manifest"] is None
                assert j["fleet_dir"] == str(state / "broker_fleet")
                assert "fleet dir not found" in j.get("note", "")
        finally:
            importlib.reload(mod)

    def test_btc_trades_tails_ledger(self, tmp_path, app_client):
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        fleet_dir = state / "broker_fleet"
        fleet_dir.mkdir(parents=True, exist_ok=True)
        ledger = fleet_dir / "btc_paper_trades.jsonl"
        rows = [
            {
                "ts_utc": "2026-04-24T10:00:00+00:00",
                "worker_id": "btc-grid-ibkr",
                "event": "submit",
                "order_id": "srv-1",
                "status": "OPEN",
            },
            {
                "ts_utc": "2026-04-24T10:00:30+00:00",
                "worker_id": "btc-grid-ibkr",
                "event": "transition",
                "order_id": "srv-1",
                "status": "FILLED",
                "prior_status": "OPEN",
            },
            {
                "ts_utc": "2026-04-24T10:01:00+00:00",
                "worker_id": "btc-directional-tastytrade",
                "event": "submit",
                "order_id": "srv-2",
                "status": "OPEN",
            },
        ]
        ledger.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n",
            encoding="utf-8",
        )
        r = app_client.get("/api/btc/trades?n=10")
        assert r.status_code == 200
        j = r.json()
        # Newest first
        assert j["trades"][0]["order_id"] == "srv-2"
        assert j["trades"][1]["order_id"] == "srv-1"
        assert j["trades"][1]["event"] == "transition"
        assert j["total"] == 3
        assert j["returned"] == 3

    def test_btc_trades_respects_n_cap(self, tmp_path, app_client):
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        fleet_dir = state / "broker_fleet"
        fleet_dir.mkdir(parents=True, exist_ok=True)
        ledger = fleet_dir / "btc_paper_trades.jsonl"
        ledger.write_text(
            "\n".join(json.dumps({"i": i, "worker_id": "x", "event": "submit"}) for i in range(50)) + "\n",
            encoding="utf-8",
        )
        r = app_client.get("/api/btc/trades?n=5")
        j = r.json()
        assert j["returned"] == 5
        assert j["total"] == 50

    # ------------------------------------------------------------------ #
    # MNQ supervisor endpoint
    # ------------------------------------------------------------------ #

    def test_mnq_supervisor_empty_when_dir_absent(self, app_client):
        r = app_client.get("/api/mnq/supervisor")
        assert r.status_code == 200
        j = r.json()
        assert j["state"] is None
        assert j["recent_events"] == []
        assert "mnq_live dir not found" in j.get("note", "")

    def test_mnq_supervisor_surfaces_state_and_events(
        self,
        tmp_path,
        app_client,
        monkeypatch,
    ):
        mnq_dir = tmp_path / "mnq_live"
        mnq_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("ETA_MNQ_SUPERVISOR_DIR", str(mnq_dir))

        # Seed state
        (mnq_dir / "mnq_live_state.json").write_text(
            json.dumps(
                {
                    "worker": "mnq_live",
                    "heartbeat_count": 42,
                    "bars_consumed": 42,
                    "signals_routed": 3,
                    "signals_blocked": 1,
                    "paused": False,
                    "router_name": "IbkrClientPortalVenue",
                    "symbol": "MNQ",
                    "tradovate_symbol": "MNQH6",
                    "started_at_utc": "2026-04-24T14:00:00+00:00",
                    "last_heartbeat_utc": "2026-04-24T14:42:00+00:00",
                    "last_bar_ts": "2026-04-24T14:42:00+00:00",
                    "last_event": "ok",
                    "jarvis_audit_tail_len": 48,
                }
            ),
            encoding="utf-8",
        )

        # Seed a few journal rows (jsonl-ish DecisionJournal format)
        from datetime import UTC, datetime

        from eta_engine.obs.decision_journal import (
            Actor,
            DecisionJournal,
            Outcome,
        )

        journal = DecisionJournal(mnq_dir / "mnq_live_decisions.jsonl")
        journal.record(
            actor=Actor.TRADE_ENGINE,
            intent="mnq_start",
            rationale="ok",
            outcome=Outcome.EXECUTED,
            ts=datetime(2026, 4, 24, 14, 0, tzinfo=UTC),
        )
        journal.record(
            actor=Actor.TRADE_ENGINE,
            intent="mnq_order_routed",
            rationale="routed",
            outcome=Outcome.EXECUTED,
            ts=datetime(2026, 4, 24, 14, 1, tzinfo=UTC),
        )

        r = app_client.get("/api/mnq/supervisor")
        assert r.status_code == 200
        j = r.json()
        assert j["state"]["bars_consumed"] == 42
        assert j["state"]["router_name"] == "IbkrClientPortalVenue"
        # Newest first
        events = j["recent_events"]
        assert len(events) == 2
        assert events[0]["intent"] == "mnq_order_routed"
        assert events[1]["intent"] == "mnq_start"

    # ------------------------------------------------------------------ #
    # /api/systems rollup
    # ------------------------------------------------------------------ #

    def test_systems_rollup_handles_missing_components(self, app_client):
        r = app_client.get("/api/systems")
        assert r.status_code == 200
        j = r.json()
        assert "overall" in j
        assert j["overall"] in {"GREEN", "YELLOW", "RED"}
        # Every subsystem entry has both status + detail
        for entry in j["systems"].values():
            assert entry["status"] in {"GREEN", "YELLOW", "RED"}
            assert "detail" in entry
        # Dashboard is always GREEN when this endpoint answers
        assert j["systems"]["dashboard"]["status"] == "GREEN"

    def test_systems_rollup_red_on_paused_mnq(
        self,
        tmp_path,
        app_client,
        monkeypatch,
    ):
        mnq_dir = tmp_path / "mnq_live"
        mnq_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("ETA_MNQ_SUPERVISOR_DIR", str(mnq_dir))
        (mnq_dir / "mnq_live_state.json").write_text(
            json.dumps(
                {
                    "paused": True,
                    "bars_consumed": 5,
                }
            ),
            encoding="utf-8",
        )
        r = app_client.get("/api/systems")
        j = r.json()
        assert j["systems"]["mnq_supervisor"]["status"] == "RED"
        assert "paused" in j["systems"]["mnq_supervisor"]["detail"].lower()
        # Overall takes the worst tier
        assert j["overall"] == "RED"

    def test_systems_rollup_green_on_full_active_fleet(
        self,
        tmp_path,
        app_client,
        monkeypatch,
    ):
        fleet_dir = tmp_path / "state" / "broker_fleet"
        fleet_dir.mkdir(parents=True, exist_ok=True)
        # Shadow the pinned env so /api/systems sees this path
        monkeypatch.setenv("ETA_BTC_FLEET_DIR", str(fleet_dir))
        for i, (lane, broker) in enumerate(
            [
                ("directional", "ibkr"),
                ("directional", "tastytrade"),
                ("grid", "ibkr"),
                ("grid", "tastytrade"),
            ]
        ):
            (fleet_dir / f"btc-{lane}-{broker}.lane.json").write_text(
                json.dumps(
                    {
                        "worker_id": f"btc-{lane}-{broker}",
                        "broker": broker,
                        "lane": lane,
                        "active_order_id": f"srv-{i:03d}",
                        "active_order_status": "OPEN",
                    }
                ),
                encoding="utf-8",
            )
        r = app_client.get("/api/systems")
        j = r.json()
        fleet = j["systems"]["btc_fleet"]
        assert fleet["status"] == "GREEN"
        assert "4/4" in fleet["detail"]

    def test_systems_rollup_yellow_when_btc_manifest_is_stale(
        self,
        tmp_path,
        app_client,
        monkeypatch,
    ):
        from datetime import UTC, datetime, timedelta

        fleet_dir = tmp_path / "state" / "broker_fleet"
        fleet_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("ETA_BTC_FLEET_DIR", str(fleet_dir))
        (fleet_dir / "btc_broker_fleet_latest.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                    "fleet": "btc_broker_paper_fleet",
                    "requested_workers": 4,
                    "running_workers": 4,
                }
            ),
            encoding="utf-8",
        )
        for i, (lane, broker) in enumerate(
            [
                ("directional", "ibkr"),
                ("directional", "tastytrade"),
                ("grid", "ibkr"),
                ("grid", "tastytrade"),
            ]
        ):
            (fleet_dir / f"btc-{lane}-{broker}.lane.json").write_text(
                json.dumps(
                    {
                        "worker_id": f"btc-{lane}-{broker}",
                        "broker": broker,
                        "lane": lane,
                        "active_order_id": f"srv-{i:03d}",
                        "active_order_status": "OPEN",
                    }
                ),
                encoding="utf-8",
            )
        r = app_client.get("/api/systems")
        j = r.json()
        fleet = j["systems"]["btc_fleet"]
        assert fleet["status"] == "YELLOW"
        assert "4/4" in fleet["detail"]
        assert "manifest stale" in fleet["detail"]

    def test_default_state_dir_is_repo_relative(self):
        """_DEFAULT_STATE must be under the eta_engine repo, not LOCALAPPDATA."""
        from eta_engine.deploy.scripts.dashboard_api import _DEFAULT_STATE

        s = str(_DEFAULT_STATE).replace("\\", "/")
        assert "AppData" not in s, f"state dir leaked into AppData: {s}"
        assert "eta_engine" in s.lower(), f"state dir not under eta_engine: {s}"

    def test_bot_fleet_enriches_state_bots_from_readiness_snapshot(self, app_client, tmp_path, monkeypatch):
        """Plain state/bots rows inherit launch-lane posture from the canonical readiness snapshot."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        bot_dir = state / "bots" / "mcl_sweep_reclaim"
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "status.json").write_text(
            json.dumps(
                {
                    "name": "mcl_sweep_reclaim",
                    "symbol": "MCL1",
                    "tier": "confluence_scorecard",
                    "venue": "paper-sim",
                    "status": "running",
                    "todays_pnl": 0.0,
                    "last_aggregation_reject_reason": "session_gate:outside_rth",
                    "last_aggregation_reject_at": "2026-05-15T12:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        readiness = tmp_path / "bot_strategy_readiness_latest.json"
        readiness.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-04-29T21:20:00+00:00",
                    "source": "bot_strategy_readiness",
                    "summary": {"total_bots": 1, "launch_lanes": {"paper_soak": 1}},
                    "rows": [
                        {
                            "bot_id": "mcl_sweep_reclaim",
                            "strategy_id": "mcl_sweep_reclaim_v1",
                            "strategy_kind": "confluence_scorecard",
                            "symbol": "MCL1",
                            "timeframe": "5m",
                            "active": True,
                            "promotion_status": "paper_ready",
                            "baseline_status": "baseline_present",
                            "data_status": "ready",
                            "launch_lane": "paper_soak",
                            "can_paper_trade": True,
                            "can_live_trade": False,
                            "next_action": "Run paper-soak and broker drift checks before live routing.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH", str(readiness))

        r = app_client.get("/api/bot-fleet")
        assert r.status_code == 200
        bot_row = next(b for b in r.json()["bots"] if b["name"] == "mcl_sweep_reclaim")
        assert bot_row["strategy_readiness"]["strategy_id"] == "mcl_sweep_reclaim_v1"
        assert bot_row["launch_lane"] == "paper_soak"
        assert bot_row["can_paper_trade"] is True
        assert bot_row["can_live_trade"] is False
        assert bot_row["readiness_next_action"] == "Run paper-soak and broker drift checks before live routing."

        drill = app_client.get("/api/bot-fleet/mcl_sweep_reclaim")
        assert drill.status_code == 200
        drill_data = drill.json()
        assert drill_data["status"]["strategy_readiness"]["launch_lane"] == "paper_soak"
        assert drill_data["strategy_readiness"]["can_paper_trade"] is True
        assert drill_data["readiness_next_action"].startswith("Run paper-soak")

    def test_bot_fleet_includes_readiness_only_bots(self, app_client, tmp_path, monkeypatch):
        """Snapshot-only bots remain discoverable before their runtime status row exists."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "bots").mkdir(parents=True, exist_ok=True)
        readiness = tmp_path / "bot_strategy_readiness_latest.json"
        readiness.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-04-29T21:30:00+00:00",
                    "source": "bot_strategy_readiness",
                    "summary": {
                        "total_bots": 2,
                        "launch_lanes": {"live_preflight": 1, "deactivated": 1},
                    },
                    "rows": [
                        {
                            "bot_id": "volume_profile_nq",
                            "strategy_id": "volume_profile_nq_v1",
                            "strategy_kind": "confluence_scorecard",
                            "symbol": "NQ1",
                            "timeframe": "1d",
                            "active": True,
                            "promotion_status": "production",
                            "baseline_status": "baseline_present",
                            "data_status": "ready",
                            "launch_lane": "live_preflight",
                            "can_paper_trade": True,
                            "can_live_trade": False,
                            "next_action": "Run per-bot promotion preflight before live routing.",
                        },
                        {
                            "bot_id": "removed_legacy_bot",
                            "strategy_id": "removed_legacy_bot_v1",
                            "strategy_kind": "legacy",
                            "symbol": "NQ",
                            "timeframe": "1d",
                            "active": False,
                            "promotion_status": "retired",
                            "baseline_status": "removed",
                            "data_status": "disabled",
                            "launch_lane": "deactivated",
                            "can_paper_trade": False,
                            "can_live_trade": False,
                            "next_action": "Removed from the active fleet.",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH", str(readiness))

        r = app_client.get("/api/bot-fleet")
        assert r.status_code == 200
        data = r.json()
        assert "removed_legacy_bot" not in [b["name"] for b in data["bots"]]
        nq = next(b for b in data["bots"] if b["name"] == "volume_profile_nq")
        assert nq["source"] == "bot_strategy_readiness_snapshot"
        assert nq["status"] == "readiness_only"
        assert nq["strategy_readiness"]["strategy_id"] == "volume_profile_nq_v1"
        assert nq["launch_lane"] == "live_preflight"
        assert nq["can_paper_trade"] is True
        assert nq["readiness_next_action"].startswith("Run per-bot promotion")

        filtered = app_client.get("/api/bot-fleet?bot=volume_profile_nq")
        assert filtered.status_code == 200
        assert [row["name"] for row in filtered.json()["bots"]] == ["volume_profile_nq"]

        hidden = app_client.get("/api/bot-fleet?bot=removed_legacy_bot")
        assert hidden.status_code == 200
        assert hidden.json()["bots"] == []

        hidden_debug = app_client.get("/api/bot-fleet?bot=removed_legacy_bot&include_disabled=true")
        assert hidden_debug.status_code == 200
        assert [row["name"] for row in hidden_debug.json()["bots"]] == ["removed_legacy_bot"]

        drill = app_client.get("/api/bot-fleet/volume_profile_nq")
        assert drill.status_code == 200
        drill_data = drill.json()
        assert drill_data["status"]["source"] == "bot_strategy_readiness_snapshot"
        assert drill_data["status"]["status"] == "readiness_only"
        assert drill_data["strategy_readiness"]["launch_lane"] == "live_preflight"
        assert "_warning" not in drill_data

    def test_bot_fleet_counts_fresh_idle_supervisor_rows_as_attached_not_staged(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        """Fresh shadow-paper bots can be flat/idle without being mislabeled as staged."""
        import json
        import os
        from datetime import UTC, datetime
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "today_actual_fills": 0,
                "today_realized_pnl": 0.0,
                "total_unrealized_pnl": 0.0,
                "open_position_count": 0,
                "win_rate_30d": None,
                "alpaca": {"ready": True, "open_positions": [], "open_position_count": 0},
                "ibkr": {"ready": True, "open_positions": [], "open_position_count": 0},
            },
        )
        monkeypatch.setattr(
            mod,
            "_registry_active_by_bot",
            lambda: {
                "mnq_futures_sage": True,
                "ng_sweep_reclaim": True,
                "eth_sage_daily": True,
            },
        )

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "bots").mkdir(parents=True, exist_ok=True)
        readiness = tmp_path / "bot_strategy_readiness_latest.json"
        readiness.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-05-15T12:00:00+00:00",
                    "source": "bot_strategy_readiness",
                    "summary": {"total_bots": 1, "launch_lanes": {"paper_soak": 1}},
                    "rows": [
                        {
                            "bot_id": "eth_sage_daily",
                            "strategy_id": "eth_sage_daily_v1",
                            "strategy_kind": "confluence_scorecard",
                            "symbol": "ETH",
                            "timeframe": "1d",
                            "active": True,
                            "promotion_status": "paper_ready",
                            "baseline_status": "baseline_present",
                            "data_status": "ready",
                            "launch_lane": "paper_soak",
                            "can_paper_trade": True,
                            "can_live_trade": False,
                            "next_action": "Wait for supervisor attachment.",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH", str(readiness))

        now = datetime.now(UTC).isoformat()
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": now,
                    "mode": "paper_live",
                    "bots": [
                        {
                            "bot_id": "mnq_futures_sage",
                            "symbol": "MNQ1",
                            "strategy_kind": "orb",
                            "status": "idle",
                            "direction": "long",
                            "n_entries": 0,
                            "n_exits": 0,
                            "realized_pnl": 0.0,
                            "open_position": None,
                            "last_bar_ts": now,
                        },
                        {
                            "bot_id": "ng_sweep_reclaim",
                            "symbol": "NG1",
                            "strategy_kind": "sweep",
                            "status": "running",
                            "direction": "long",
                            "n_entries": 1,
                            "n_exits": 0,
                            "realized_pnl": 0.0,
                            "open_position": {
                                "side": "BUY",
                                "qty": 1,
                                "entry_price": 3.25,
                                "entry_ts": now,
                                "mark_price": 3.3,
                                "bracket_stop": 3.1,
                                "bracket_target": 3.45,
                                "last_bar_high": 3.31,
                                "last_bar_low": 3.22,
                                "broker_bracket": False,
                                "bracket_src": "supervisor_local",
                            },
                            "last_bar_ts": now,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        r = app_client.get("/api/bot-fleet")
        assert r.status_code == 200
        data = r.json()
        summary = data["summary"]
        assert summary["active_bots"] == 2
        assert summary["runtime_active_bots"] == 2
        assert summary["running_bots"] == 1
        assert summary["live_attached_bots"] == 2
        assert summary["live_in_trade_bots"] == 1
        assert summary["idle_live_bots"] == 1
        assert summary["inactive_runtime_bots"] == 0
        assert summary["staged_bots"] == 1
        assert data["active_bots"] == 2
        assert data["live_attached_bots"] == 2
        assert data["live_in_trade_bots"] == 1
        assert data["idle_live_bots"] == 1
        assert data["inactive_runtime_bots"] == 0
        assert data["staged_bots"] == 1
        assert data["truth_summary_line"].startswith(
            "Live ETA truth: 2/3 bot heartbeat(s) are fresh; 2 attached, 1 in trade, 1 flat/idle."
        )
        assert "1 readiness-only inventory row(s) remain staged." in data["truth_summary_line"]

    def test_mnq_runtime_summary_excludes_readiness_only_inventory(self, app_client, tmp_path, monkeypatch):
        """MNQ runtime headline should not imply snapshot-only rows are down bots."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        bot_dir = state / "bots" / "mnq_anchor_sweep"
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "status.json").write_text(
            json.dumps(
                {
                    "name": "mnq_anchor_sweep",
                    "symbol": "MNQ1",
                    "tier": "anchor_sweep",
                    "venue": "paper-sim",
                    "status": "running",
                    "todays_pnl": 0.0,
                    "last_aggregation_reject_reason": "session_gate:outside_rth",
                    "last_aggregation_reject_at": "2026-05-15T12:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        readiness = tmp_path / "bot_strategy_readiness_latest.json"
        readiness.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-05-08T07:20:00+00:00",
                    "source": "bot_strategy_readiness",
                    "summary": {"total_bots": 1, "launch_lanes": {"paper_soak": 1}},
                    "rows": [
                        {
                            "bot_id": "mnq_futures_optimized",
                            "strategy_id": "mnq_futures_optimized_v1",
                            "strategy_kind": "confluence_scorecard",
                            "symbol": "MNQ1",
                            "timeframe": "5m",
                            "active": True,
                            "promotion_status": "paper_ready",
                            "baseline_status": "baseline_present",
                            "data_status": "ready",
                            "launch_lane": "paper_soak",
                            "can_paper_trade": True,
                            "can_live_trade": False,
                            "next_action": "Await runtime supervisor lane before counting as running.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH", str(readiness))

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        summary = r.json()["summary"]
        assert summary["mnq_running"] == 1
        assert summary["mnq_total"] == 1
        assert summary["mnq_runtime_total"] == 1
        assert summary["mnq_inventory_total"] == 2
        assert summary["mnq_readiness_only"] == 1
        assert summary["active_bots"] == 1
        assert summary["runtime_active_bots"] == 1
        assert summary["running_bots"] == 1
        assert summary["staged_bots"] == 1
        assert summary["current_blocked_bots"] == 1
        assert summary["current_blocked_kinds"] == {"session_gate": 1}

    def test_bot_fleet_counts_stale_runtime_rows_separately_from_live_attached(self, app_client):
        """Runtime-active rows should stay visible even when the live lane heartbeat is stale."""
        import json
        import os
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        bot_dir = state / "bots" / "mnq_futures_sage"
        bot_dir.mkdir(parents=True, exist_ok=True)
        stale_hb = (datetime.now(UTC) - timedelta(minutes=8)).isoformat()
        (bot_dir / "status.json").write_text(
            json.dumps(
                {
                    "name": "mnq_futures_sage",
                    "symbol": "MNQ1",
                    "tier": "orb_sage",
                    "venue": "ibkr",
                    "status": "running",
                    "heartbeat_ts": stale_hb,
                    "todays_pnl": 0.0,
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        data = r.json()
        summary = data["summary"]
        assert summary["active_bots"] == 1
        assert summary["runtime_active_bots"] == 1
        assert summary["running_bots"] == 1
        assert summary["live_attached_bots"] == 0
        assert summary["live_in_trade_bots"] == 0
        assert summary["idle_live_bots"] == 0
        assert summary["inactive_runtime_bots"] == 1
        assert summary["staged_bots"] == 0
        assert data["active_bots"] == 1
        assert data["runtime_active_bots"] == 1
        assert data["live_attached_bots"] == 0
        assert data["inactive_runtime_bots"] == 1
        assert "none have a fresh heartbeat" in data["truth_summary_line"]
        assert "1 bot row" in data["truth_summary_line"]

    def test_bot_fleet_summary_carries_broker_net_without_fake_lifetime(
        self,
        app_client,
        monkeypatch,
    ):
        """Broker session truth is exposed without pretending it is lifetime PnL."""
        import os
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "bots").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "today_actual_fills": 7,
                "today_realized_pnl": 125.5,
                "total_unrealized_pnl": -25.25,
                "open_position_count": 2,
                "win_rate_30d": 0.625,
                "win_rate_today": 0.5,
                "win_rate_source": "alpaca_filled_order_pairs",
                "closed_outcome_count_today": 4,
                "alpaca": {"ready": True},
                "ibkr": {"ready": False},
            },
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        summary = r.json()["summary"]
        assert summary["broker_net_pnl"] == 100.25
        assert summary["broker_today_realized_pnl"] == 125.5
        assert summary["broker_total_unrealized_pnl"] == -25.25
        assert summary["broker_today_actual_fills"] == 7
        assert summary["broker_open_position_count"] == 2
        assert summary["broker_win_rate_30d"] == 0.625
        assert summary["broker_win_rate_today"] == 0.5
        assert summary["broker_win_rate_source"] == "alpaca_filled_order_pairs"
        assert summary["broker_closed_outcomes_today"] == 4
        assert summary["pnl_summary_source"] == "live_broker_state"
        assert "total_pnl" not in summary

    def test_bot_fleet_embeds_paper_live_transition_summary(self, app_client, tmp_path, monkeypatch):
        """Bot-fleet consumers should see the same paper-live readiness as master status."""
        import time

        import eta_engine.deploy.scripts.dashboard_api as mod

        mod._IBKR_PROBE_CACHE["snapshot"] = {"ready": True, "open_position_count": 0, "open_positions": []}
        mod._IBKR_PROBE_CACHE["ts"] = time.time()
        monkeypatch.setattr(
            mod,
            "_broker_bracket_audit_payload",
            lambda **_: {
                "summary": "READY_NO_OPEN_EXPOSURE",
                "ready_for_prop_dry_run": True,
                "operator_action_required": False,
                "position_summary": {},
            },
        )
        monkeypatch.setattr(
            mod,
            "_dashboard_proxy_watchdog_payload",
            lambda server_ts=None: {
                "status": "ok",
                "fresh": True,
                "action": "noop",
                "task_name": "ETA-Proxy-8421",
                "probe_healthy": True,
                "probe_reason": "ok",
                "status_code": 200,
                "elapsed_ms": 15,
                "heartbeat_age_s": 4,
                "checked_age_s": 3,
                "checked_at": "2026-05-17T00:00:00+00:00",
                "heartbeat_ts": "2026-05-17T00:00:01+00:00",
                "detail": "noop: ok",
                "summary": "noop: ok",
            },
        )
        monkeypatch.setattr(
            mod,
            "_command_center_watchdog_payload",
            lambda server_ts=None: {
                "status": "healthy",
                "issue_status": "healthy",
                "display_summary": "Command Center watchdog is healthy.",
                "fresh": True,
                "age_s": 0,
                "healthy": True,
                "checked_at": "2026-05-17T00:00:00+00:00",
                "operator_next_step": "none",
                "operator_next_reason": "healthy",
                "operator_next_command": None,
                "failure_class": "healthy",
                "operator_contract_state": "healthy",
                "recommended_action": "none",
                "instruction": "",
                "repair_required": False,
                "operator_next_requires_elevation": False,
                "requires_elevation": False,
                "watchdog_registered": True,
                "watchdog_state": "Ready",
                "can_launch_from_desktop": True,
                "launch_context": "ready",
                "dashboard_task_contract_status": {"status": "access_denied"},
                "local_contract_status": {"status": "healthy"},
            },
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 12,
                    "operator_queue_launch_blocked_count": 0,
                    "operator_queue_first_launch_blocker_op_id": "",
                    "operator_queue_first_launch_next_action": "",
                    "gates": [],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        payload = r.json()
        assert payload["paper_live_transition"]["status"] == "ready_to_launch_paper_live"
        assert payload["paper_live_transition"]["critical_ready"] is True
        assert payload["summary"]["paper_live_status"] == "ready_to_launch_paper_live"
        assert payload["summary"]["paper_live_detail"] == ""
        assert payload["summary"]["paper_live_effective_status"] == "ready_to_launch_paper_live"
        assert payload["summary"]["paper_live_first_launch_next_action"] == ""
        assert payload["summary"]["paper_live_non_authoritative_gateway_host"] is False
        assert payload["summary"]["paper_live_held_by_bracket_audit"] is False
        assert payload["summary"]["paper_live_critical_ready"] is True
        assert payload["summary"]["paper_live_ready_bots"] == 12
        assert payload["summary"]["paper_live_launch_blocked_count"] == 0
        assert payload["summary"]["command_center_watchdog_fresh"] is True
        assert payload["summary"]["command_center_watchdog_age_s"] == 0
        assert payload["summary"]["command_center_watchdog_healthy"] is True
        assert payload["summary"]["command_center_watchdog_checked_at"] == "2026-05-17T00:00:00+00:00"
        assert payload["summary"]["command_center_watchdog_next_step"] == "none"
        assert payload["summary"]["command_center_watchdog_next_reason"] == "healthy"
        assert payload["summary"]["command_center_watchdog_next_command"] == ""
        assert payload["summary"]["command_center_watchdog_failure_class"] == "healthy"
        assert payload["summary"]["command_center_watchdog_operator_contract_state"] == "healthy"
        assert payload["summary"]["command_center_watchdog_recommended_action"] == "none"
        assert payload["summary"]["command_center_watchdog_primary_blocker"] == "healthy"
        assert payload["summary"]["command_center_watchdog_instruction"] == ""
        assert payload["summary"]["command_center_watchdog_action_count"] == 0
        assert payload["summary"]["command_center_watchdog_follow_up_count"] == 0
        assert payload["summary"]["command_center_watchdog_dashboard_task_missing_task_names"] == []
        assert payload["summary"]["command_center_watchdog_repair_required"] is False
        assert payload["summary"]["command_center_watchdog_requires_elevation"] is False
        assert payload["summary"]["command_center_watchdog_watchdog_registered"] is True
        assert payload["summary"]["command_center_watchdog_watchdog_state"] == "Ready"
        assert payload["summary"]["command_center_watchdog_can_launch_from_desktop"] is True
        assert payload["summary"]["command_center_watchdog_launch_context"] == "ready"
        assert payload["summary"]["command_center_watchdog_dashboard_task_contract_status"] == "access_denied"
        assert payload["summary"]["command_center_watchdog_local_contract_status"] == "healthy"
        assert payload["summary"]["dashboard_proxy_watchdog_status"] == "ok"
        assert payload["summary"]["dashboard_proxy_watchdog_detail"] == "noop: ok"
        assert payload["summary"]["dashboard_proxy_watchdog_fresh"] is True
        assert payload["summary"]["dashboard_proxy_watchdog_action"] == "noop"
        assert payload["summary"]["dashboard_proxy_watchdog_task_name"] == "ETA-Proxy-8421"
        assert payload["summary"]["dashboard_proxy_watchdog_probe_healthy"] is True
        assert payload["summary"]["dashboard_proxy_watchdog_probe_reason"] == "ok"
        assert payload["summary"]["dashboard_proxy_watchdog_status_code"] == 200
        assert payload["summary"]["dashboard_proxy_watchdog_elapsed_ms"] == 15
        assert payload["summary"]["dashboard_proxy_watchdog_heartbeat_age_s"] == 4
        assert payload["summary"]["dashboard_proxy_watchdog_checked_age_s"] == 3
        assert payload["summary"]["dashboard_proxy_watchdog_checked_at"] == "2026-05-17T00:00:00+00:00"
        assert payload["summary"]["dashboard_proxy_watchdog_heartbeat_ts"] == "2026-05-17T00:00:01+00:00"

    def test_bot_fleet_marks_stale_paper_live_summary_as_stale_receipt(self, app_client, tmp_path, monkeypatch):
        """Stale paper-live cache should not keep derived bot-fleet readiness blocked."""
        import time

        import eta_engine.deploy.scripts.dashboard_api as mod

        mod._IBKR_PROBE_CACHE["snapshot"] = {"ready": True, "open_position_count": 0, "open_positions": []}
        mod._IBKR_PROBE_CACHE["ts"] = time.time()
        monkeypatch.setattr(
            mod,
            "_broker_bracket_audit_payload",
            lambda **_: {
                "summary": "READY_NO_OPEN_EXPOSURE",
                "ready_for_prop_dry_run": True,
                "operator_action_required": False,
                "position_summary": {},
            },
        )
        (tmp_path / "state" / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-05-09T08:00:00+00:00",
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 12,
                    "operator_queue_launch_blocked_count": 0,
                    "operator_queue_first_launch_blocker_op_id": "",
                    "operator_queue_first_launch_next_action": "",
                    "gates": [],
                }
            ),
            encoding="utf-8",
        )
        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        payload = r.json()
        assert payload["paper_live_transition"]["status"] == "ready_to_launch_paper_live"
        assert payload["paper_live_transition"]["stale_receipt"] is True
        assert payload["summary"]["paper_live_status"] == "ready_to_launch_paper_live"
        assert payload["summary"]["paper_live_effective_status"] == "stale_receipt"
        assert payload["summary"]["paper_live_stale_receipt"] is True
        assert payload["summary"]["paper_live_detail"] == ""
        assert payload["summary"]["paper_live_first_launch_next_action"] == ""
        assert "stale" in payload["summary"]["paper_live_stale_detail"].lower()

    def test_bot_fleet_marks_shadow_paper_active_when_attached_runtime_is_live(self, app_client, tmp_path):
        """Fresh attached shadow-paper rows should override a stale launch label in the summary."""
        now_iso = datetime.now(UTC).isoformat()
        state = tmp_path / "state"
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": now_iso,
                    "mode": "paper_live",
                    "feed": "composite",
                    "bots": [
                        {
                            "bot_id": "mnq_futures_sage",
                            "symbol": "MNQ1",
                            "strategy_kind": "orb_sage_gated",
                            "execution_lane": "shadow_paper",
                            "last_bar_ts": now_iso,
                            "open_position": {},
                            "n_entries": 0,
                            "n_exits": 0,
                            "realized_pnl": 0.0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": now_iso,
                    "status": "blocked",
                    "critical_ready": False,
                    "paper_ready_bots": 11,
                    "operator_queue_launch_blocked_count": 1,
                    "operator_queue_first_launch_blocker_op_id": "OP-19",
                    "operator_queue_first_launch_next_action": "apply authority on VPS",
                    "gates": [],
                }
            ),
            encoding="utf-8",
        )
        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        payload = r.json()
        assert payload["summary"]["paper_live_status"] == "blocked"
        assert payload["summary"]["paper_live_effective_status"] == "shadow_paper_active"
        assert "live shadow paper lane active on 1 attached bot(s)" in payload["summary"]["paper_live_effective_detail"]
        assert payload["summary"]["live_attached_bots"] == 1
        assert payload["summary"]["idle_live_bots"] == 1

    def test_bot_fleet_keeps_bracket_audit_hold_over_shadow_runtime(self, app_client, tmp_path, monkeypatch):
        """Active shadow-paper bots must not mask an explicit bracket-audit hold."""
        import eta_engine.deploy.scripts.dashboard_api as mod

        now_iso = datetime.now(UTC).isoformat()
        state = tmp_path / "state"
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            mod,
            "_broker_bracket_audit_payload",
            lambda **_: {
                "summary": "BLOCKED_UNBRACKETED_EXPOSURE",
                "ready_for_prop_dry_run": False,
                "operator_action_required": True,
                "operator_actions": [{"id": "verify_manual_broker_oco", "label": "Verify broker OCO coverage"}],
                "position_summary": {},
            },
        )
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": now_iso,
                    "mode": "paper_live",
                    "feed": "composite",
                    "bots": [
                        {
                            "bot_id": "mnq_futures_sage",
                            "symbol": "MNQ1",
                            "strategy_kind": "orb_sage_gated",
                            "execution_lane": "shadow_paper",
                            "last_bar_ts": now_iso,
                            "open_position": {},
                            "n_entries": 0,
                            "n_exits": 0,
                            "realized_pnl": 0.0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": now_iso,
                    "status": "ready_to_launch_paper_live",
                    "critical_ready": True,
                    "paper_ready_bots": 11,
                    "operator_queue_launch_blocked_count": 0,
                    "gates": [],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        payload = r.json()
        assert payload["summary"]["paper_live_effective_status"] == "held_by_bracket_audit"
        assert payload["summary"]["paper_live_held_by_bracket_audit"] is True
        assert payload["summary"]["paper_live_effective_detail"].startswith("held by Bracket Audit")

    def test_global_daily_loss_stop_dominates_bot_fleet_block_rollup(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        rows = [
            {
                "name": "mnq_futures_sage",
                "symbol": "MNQ1",
                "current_block_kind": "broker_router_filled_but_broker_flat",
                "current_block_reason": "broker_router_filled_but_broker_flat",
                "current_block_summary": "Broker router filled but broker flat",
                "current_block_at": "2026-05-15T13:57:34+00:00",
            }
        ]

        mod._apply_global_daily_loss_killswitch_to_roster_rows(
            rows,
            {
                "tripped": True,
                "reason": "day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
                "timezone": "America/New_York",
                "reset_display": "2026-05-16 00:00 EDT",
                "checked_at": "2026-05-15T14:00:00+00:00",
            },
        )
        rollup = mod._blocked_bot_rollup(rows)

        assert rows[0]["current_block_kind"] == "daily_kill_switch"
        assert rows[0]["current_block_secondary_kind"] == "broker_router_filled_but_broker_flat"
        assert rollup["kinds"] == {"daily_kill_switch": 1}
        assert rollup["summary_line"].startswith("Current blockers: 1 bot(s) held - 1 daily kill switch")

    def test_blocked_bot_rollup_separates_actionable_holds_from_session_gates(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        rows = [
            {
                "name": "mnq_futures_sage",
                "symbol": "MNQ1",
                "current_block_kind": "broker_router_signal_cooldown",
                "current_block_reason": "broker_router_signal_cooldown",
                "current_block_summary": "Broker router signal cooldown active",
                "current_block_at": "2026-05-16T13:57:34+00:00",
            },
            {
                "name": "volume_profile_nq",
                "symbol": "NQ1",
                "current_block_kind": "session_gate",
                "current_block_reason": "session_gate:outside_rth",
                "current_block_summary": "Entries paused by session gate: outside_rth",
                "current_block_at": "2026-05-16T13:58:34+00:00",
            },
        ]

        rollup = mod._blocked_bot_rollup(rows)

        assert rollup["count"] == 2
        assert rollup["kinds"] == {"broker_router_signal_cooldown": 1, "session_gate": 1}
        assert rollup["actionable_count"] == 1
        assert rollup["actionable_kinds"] == {"broker_router_signal_cooldown": 1}
        assert rollup["actionable_summary_line"] == (
            "Actionable blockers: 1 bot(s) - 1 broker router signal cooldown. "
            "Top: mnq_futures_sage. Awaiting session: 1 bot(s)."
        )
        assert rollup["session_gated_count"] == 1
        assert rollup["session_gated_kinds"] == {"session_gate": 1}
        assert rollup["session_gated_summary_line"] == (
            "Awaiting session: 1 bot(s) - 1 session gate. Top: volume_profile_nq."
        )

    def test_global_daily_loss_stop_leaves_shadow_paper_rows_unblocked(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        rows = [
            {
                "name": "mnq_futures_sage",
                "symbol": "MNQ1",
                "mode": "paper_live",
                "execution_lane": "shadow_paper",
                "daily_loss_gate_mode": "advisory",
                "current_block_kind": "",
                "current_block_reason": "",
                "current_block_summary": "",
                "current_block_at": "",
            }
        ]

        mod._apply_global_daily_loss_killswitch_to_roster_rows(
            rows,
            {
                "tripped": True,
                "reason": "day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
                "timezone": "America/New_York",
                "reset_display": "2026-05-16 00:00 EDT",
                "checked_at": "2026-05-15T14:00:00+00:00",
            },
        )

        assert rows[0]["current_block_kind"] == ""
        assert rows[0]["daily_loss_advisory_active"] is True
        assert rows[0]["capital_lanes_held_by_daily_loss_stop"] is True

    def test_non_authoritative_host_suppresses_local_daily_loss_row_blocks(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        rows = [
            {
                "name": "mnq_futures_sage",
                "symbol": "MNQ1",
                "current_block_source": "aggregation",
                "current_block_kind": "daily_kill_switch",
                "current_block_reason": "daily_kill_switch:day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
                "current_block_summary": "Entries halted by daily kill switch",
                "current_block_at": "2026-05-15T14:00:00+00:00",
                "daily_loss_gate_active": True,
            }
        ]

        mod._suppress_non_authoritative_local_daily_loss_row_blocks(rows)

        assert rows[0]["current_block_kind"] == ""
        assert rows[0]["current_block_reason"] == ""
        assert rows[0]["daily_loss_gate_active"] is False
        assert rows[0]["daily_loss_suppressed_non_authoritative_gateway_host"] is True
        assert rows[0]["suppressed_current_block_kind"] == "daily_kill_switch"

    def test_bot_fleet_embeds_vps_root_reconciliation_summary(self, app_client, tmp_path, monkeypatch):
        """Bot-fleet consumers need the root dirty-tree review state without another probe."""
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_workspace_checkout_payload",
            lambda refresh=False: {
                "status": "clean",
                "summary_line": "root clean abc1234 | eta clean def5678 | submodule aligned c08210d",
            },
        )
        (tmp_path / "state" / "vps_root_reconciliation_plan.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "risk_level": "medium",
                    "cleanup_allowed": False,
                    "destructive_actions_performed": False,
                    "counts": {
                        "status": 4,
                        "submodule_drift": 5,
                        "dirty_companion_repos": 3,
                        "optional_dormant_deleted_tracked": 1,
                    },
                    "summary": {
                        "source_or_governance_deleted": 0,
                        "source_or_governance_modified": 3,
                        "optional_dormant_deleted_tracked": 1,
                        "unknown_deleted": 0,
                        "submodule_drift": 5,
                        "dirty_companion_repos": 3,
                    },
                    "recommended_action": (
                        "Review tracked root source/governance modifications and dirty companion worktrees "
                        "before updating the superproject root."
                    ),
                    "steps": [
                        {
                            "id": "restore-source-governance",
                            "title": "Review tracked source and governance modifications",
                            "risk": "medium",
                            "decision": "manual_review_required",
                            "action": (
                                "Review tracked root source/governance modifications and decide whether they "
                                "should be committed, preserved as local runtime edits, or reverted before "
                                "branch updates."
                            ),
                            "evidence": [
                                "source_or_governance_deleted=0",
                                "source_or_governance_modified=3",
                                "scripts/command-center-watchdog-status.ps1",
                                "scripts/reload-operator-service.ps1",
                                "scripts/verify_operator_source_of_truth.py",
                            ],
                        },
                        {
                            "id": "align-submodules",
                            "title": "Align companion repositories",
                            "risk": "medium",
                            "decision": "manual_review_required",
                            "action": (
                                "Choose whether each companion repo follows root, live branch, or remains pinned."
                            ),
                            "evidence": [
                                "submodule_drift=5",
                                "dirty_companion_repos=3",
                                "eta_engine:submodule_pointer_changed",
                            ],
                        }
                    ],
                    "source_review_items": [
                        {
                            "path": "scripts/command-center-watchdog-status.ps1",
                            "basename": "command-center-watchdog-status.ps1",
                            "change_class": "modified",
                            "area": "operator_watchdog_truth",
                            "rationale": (
                                "Tracks Command Center watchdog truth, task contract "
                                "drift, and public route health semantics."
                            ),
                            "change_summary": (
                                "Adds runtime dependency-gap probing, watchdog/dashboard "
                                "task-contract checks, and display-safe operator summaries."
                            ),
                            "verification_command": (
                                "powershell -ExecutionPolicy Bypass -File "
                                ".\\scripts\\command-center-watchdog-status.ps1 -Json"
                            ),
                        "verification_goal": (
                            "Confirm the live watchdog contract, task-contract status, "
                            "and display-safe operator summary on the authoritative host."
                        ),
                        "verification_mode": "status_probe",
                        "verification_side_effects": "refreshes the canonical watchdog receipt",
                        "suggested_decision": "preserve_if_it_matches_live_watchdog_contract",
                    },
                        {
                            "path": "scripts/reload-operator-service.ps1",
                            "basename": "reload-operator-service.ps1",
                            "change_class": "modified",
                            "area": "operator_reload_runtime",
                            "rationale": (
                                "Controls canonical 8421 reload behavior and the "
                                "task-owned Command Center runtime path on the VPS."
                            ),
                            "change_summary": (
                                "Replaces brittle raw 8421 waits with unified local-truth "
                                "verification and uses the canonical runtime Python."
                            ),
                            "verification_command": (
                                "powershell -ExecutionPolicy Bypass -File "
                                ".\\scripts\\reload-operator-service.ps1 "
                                "-SkipPublicCheck -SkipWatchdogRegistration "
                                "-NoAutoElevate -TimeoutSeconds 30"
                            ),
                        "verification_goal": (
                            "Confirm the VPS reload flow exits cleanly and re-verifies "
                            "the local 8421 operator contract."
                        ),
                        "verification_mode": "runtime_reload",
                        "verification_side_effects": (
                            "re-registers dashboard tasks, refreshes live service wiring, "
                            "and reloads the 8421 operator surface"
                        ),
                        "suggested_decision": "preserve_if_it_matches_live_8421_reload_flow",
                    },
                        {
                            "path": "scripts/verify_operator_source_of_truth.py",
                            "basename": "verify_operator_source_of_truth.py",
                            "change_class": "modified",
                            "area": "operator_contract_verification",
                            "rationale": (
                                "Verifies operator truth surfaces, including transient "
                                "endpoint retries and upstream failure classification."
                            ),
                            "change_summary": (
                                "Adds transient endpoint retries, upstream 5xx "
                                "classification, and display-summary leakage checks."
                            ),
                            "verification_command": (
                                "python .\\scripts\\verify_operator_source_of_truth.py "
                                "--base-url http://127.0.0.1:8421 --timeout 30"
                            ),
                        "verification_goal": (
                            "Confirm the canonical local operator verifier accepts "
                            "the live 8421 payloads and failure classification contract."
                        ),
                        "verification_mode": "read_only_contract_probe",
                        "verification_side_effects": "none",
                        "suggested_decision": "preserve_if_it_matches_current_operator_contract",
                    },
                    ],
                    "companion_review_items": [
                        {
                            "target": "eta_engine",
                            "reason": "submodule_pointer_changed",
                            "rationale": (
                                "The authoritative ETA child repo is dirty/diverged "
                                "and needs a child-repo decision before the root "
                                "gitlink is updated."
                            ),
                            "suggested_decision": "commit_preserve_or_pin_before_root_update",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        payload = r.json()
        assert payload["vps_root_reconciliation"]["status"] == "review_required"
        assert payload["vps_root_reconciliation"]["risk_level"] == "medium"
        assert payload["summary"]["vps_root_reconciliation_status"] == "review_required"
        assert payload["summary"]["vps_root_risk_level"] == "medium"
        assert payload["summary"]["vps_root_cleanup_allowed"] is False
        assert payload["summary"]["vps_root_source_deleted_count"] == 0
        assert payload["summary"]["vps_root_source_modified_count"] == 3
        assert payload["summary"]["vps_root_optional_dormant_deleted_count"] == 1
        assert payload["summary"]["vps_root_submodule_drift"] == 5
        assert payload["summary"]["vps_root_dirty_companion_repos"] == 3
        assert payload["summary"]["vps_root_recommended_action"] == (
            "Review tracked root source/governance modifications and dirty companion worktrees "
            "before updating the superproject root."
        )
        assert payload["summary"]["vps_root_review_focus_summary_line"] == (
            "3 root source file(s) under review; eta_engine companion review pending; "
            "manual review before root update"
        )
        assert payload["summary"]["vps_root_review_focus_primary_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo diff -- scripts/command-center-watchdog-status.ps1"
        )
        assert payload["summary"]["vps_root_review_focus_primary_action"] == {
            "scope": "source",
            "path": "scripts/command-center-watchdog-status.ps1",
            "basename": "command-center-watchdog-status.ps1",
            "area": "operator_watchdog_truth",
            "change_summary": (
                "Adds runtime dependency-gap probing, watchdog/dashboard "
                "task-contract checks, and display-safe operator summaries."
            ),
            "verification_command": (
                "powershell -ExecutionPolicy Bypass -File "
                ".\\scripts\\command-center-watchdog-status.ps1 -Json"
            ),
            "verification_goal": (
                "Confirm the live watchdog contract, task-contract status, "
                "and display-safe operator summary on the authoritative host."
            ),
            "verification_mode": "status_probe",
            "verification_side_effects": "refreshes the canonical watchdog receipt",
            "suggested_decision": "preserve_if_it_matches_live_watchdog_contract",
            "command": "git -C C:\\EvolutionaryTradingAlgo diff -- scripts/command-center-watchdog-status.ps1",
        }
        assert payload["summary"]["vps_root_review_step_count"] == 2
        assert payload["summary"]["vps_root_top_step_id"] == "restore-source-governance"
        assert payload["summary"]["vps_root_top_step_title"] == "Review tracked source and governance modifications"
        assert payload["summary"]["vps_root_top_step_risk"] == "medium"
        assert payload["summary"]["vps_root_top_step_decision"] == "manual_review_required"
        assert payload["summary"]["vps_root_top_step_action"] == (
            "Review tracked root source/governance modifications and decide whether they "
            "should be committed, preserved as local runtime edits, or reverted before "
            "branch updates."
        )
        assert payload["summary"]["vps_root_reconciliation_top_step_summary"] == (
            "Review tracked root source/governance modifications and decide whether they "
            "should be committed, preserved as local runtime edits, or reverted before "
            "branch updates."
        )
        assert payload["summary"]["vps_root_top_step_evidence_count"] == 5
        assert payload["summary"]["vps_root_top_step_evidence"] == [
            "source_or_governance_deleted=0",
            "source_or_governance_modified=3",
            "scripts/command-center-watchdog-status.ps1",
            "scripts/reload-operator-service.ps1",
            "scripts/verify_operator_source_of_truth.py",
        ]
        assert payload["summary"]["vps_root_source_step_id"] == "restore-source-governance"
        assert payload["summary"]["vps_root_source_step_title"] == "Review tracked source and governance modifications"
        assert payload["summary"]["vps_root_source_step_risk"] == "medium"
        assert payload["summary"]["vps_root_source_step_decision"] == "manual_review_required"
        assert payload["summary"]["vps_root_source_step_action"] == (
            "Review tracked root source/governance modifications and decide whether they "
            "should be committed, preserved as local runtime edits, or reverted before "
            "branch updates."
        )
        assert payload["summary"]["vps_root_source_step_evidence_count"] == 5
        assert payload["summary"]["vps_root_source_step_evidence"] == [
            "source_or_governance_deleted=0",
            "source_or_governance_modified=3",
            "scripts/command-center-watchdog-status.ps1",
            "scripts/reload-operator-service.ps1",
            "scripts/verify_operator_source_of_truth.py",
        ]
        assert payload["summary"]["vps_root_source_review_files"] == [
            "command-center-watchdog-status.ps1",
            "reload-operator-service.ps1",
            "verify_operator_source_of_truth.py",
        ]
        assert payload["summary"]["vps_root_source_review_items"][0]["basename"] == "command-center-watchdog-status.ps1"
        assert payload["summary"]["vps_root_companion_review_targets"] == ["eta_engine"]
        assert payload["summary"]["vps_root_companion_review_items"][0]["target"] == "eta_engine"
        review_focus = payload["summary"]["vps_root_review_focus"]
        assert review_focus["source_modified_count"] == 3
        assert review_focus["dirty_companion_repos"] == 3
        assert review_focus["source_review_item_count"] == 3
        assert review_focus["companion_review_item_count"] == 1
        assert review_focus["source_review_context"] == {}
        assert review_focus["summary_line"] == payload["summary"]["vps_root_review_focus_summary_line"]
        assert review_focus["companion_review_targets"] == ["eta_engine"]
        assert review_focus["companion_review_reasons"] == ["submodule_pointer_changed"]
        assert review_focus["companion_review_suggested_decisions"] == [
            "commit_preserve_or_pin_before_root_update",
        ]
        assert review_focus["companion_review_status"] == ""
        assert review_focus["companion_review_summary_line"] == ""
        assert review_focus["companion_review_details"][0]["target"] == "eta_engine"
        assert review_focus["companion_review_details"][0]["reason"] == "submodule_pointer_changed"
        assert review_focus["companion_review_details"][0]["status_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short"
        )
        assert review_focus["companion_review_details"][0]["inspection_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short"
        )
        assert review_focus["review_actions"][-1]["scope"] == "companion"
        assert review_focus["review_actions"][-1]["target"] == "eta_engine"
        assert review_focus["review_actions"][-1]["reason"] == "submodule_pointer_changed"
        assert review_focus["review_actions"][-1]["suggested_decision"] == (
            "commit_preserve_or_pin_before_root_update"
        )
        assert review_focus["review_actions"][-1]["command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short"
        )
        assert review_focus["review_actions"][-1]["status_command"] == (
            "git -C C:\\EvolutionaryTradingAlgo\\eta_engine status --short"
        )
        assert review_focus["review_actions"][-1]["inspection_focus"] == ""
        assert review_focus["primary_review_action"] == payload["summary"]["vps_root_review_focus_primary_action"]
        assert review_focus["source_step_id"] == "restore-source-governance"
        assert review_focus["source_step_decision"] == "manual_review_required"
        assert review_focus["companion_step_id"] == "align-submodules"
        assert review_focus["companion_step_decision"] == "manual_review_required"
        assert payload["summary"]["vps_root_companion_step_id"] == "align-submodules"
        assert payload["summary"]["vps_root_companion_step_title"] == "Align companion repositories"
        assert payload["summary"]["vps_root_companion_step_risk"] == "medium"
        assert payload["summary"]["vps_root_companion_step_decision"] == "manual_review_required"
        assert payload["summary"]["vps_root_companion_step_action"] == (
            "Choose whether each companion repo follows root, live branch, or remains pinned."
        )
        assert payload["summary"]["vps_root_companion_step_evidence_count"] == 3
        assert payload["summary"]["vps_root_companion_step_evidence"] == [
            "submodule_drift=5",
            "dirty_companion_repos=3",
            "eta_engine:submodule_pointer_changed",
        ]

    def test_bot_fleet_exposes_portfolio_summary_for_allocation_and_pnl_graphs(
        self,
        app_client,
        monkeypatch,
    ):
        """Portfolio graphs should consume API truth, not browser-only math."""
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_supervisor_roster_rows",
            lambda now_ts, bot=None: [
                {
                    "id": "mnq_futures_sage",
                    "name": "mnq_futures_sage",
                    "symbol": "MNQ1",
                    "status": "running",
                    "source": "jarvis_strategy_supervisor",
                    "open_positions": 1,
                    "todays_pnl": 0.0,
                    "can_paper_trade": True,
                    "confirmed": True,
                },
                {
                    "id": "btc_optimized",
                    "name": "btc_optimized",
                    "symbol": "BTC",
                    "status": "running",
                    "source": "jarvis_strategy_supervisor",
                    "open_positions": 1,
                    "todays_pnl": 0.0,
                    "can_paper_trade": True,
                    "confirmed": True,
                },
            ],
        )
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "today_actual_fills": 2,
                "today_realized_pnl": 10.0,
                "total_unrealized_pnl": 25.0,
                "open_position_count": 1,
                "all_venue_today_actual_fills": 2,
                "all_venue_today_realized_pnl": 10.0,
                "all_venue_total_unrealized_pnl": 40.0,
                "all_venue_open_position_count": 3,
                "cellar_today_actual_fills": 0,
                "cellar_today_realized_pnl": 0.0,
                "cellar_total_unrealized_pnl": 10.0,
                "cellar_open_position_count": 2,
                "alpaca": {
                    "ready": True,
                    "policy_status": "paused_cellar",
                    "open_positions": [
                        {
                            "symbol": "BTCUSD",
                            "qty": 0.1,
                            "current_price": 100000.0,
                            "market_value": 10000.0,
                            "unrealized_pl": 15.0,
                        },
                        {
                            "symbol": "ETHUSD",
                            "qty": -0.2,
                            "current_price": 2500.0,
                            "market_value": -500.0,
                            "unrealized_pl": -5.0,
                        },
                    ],
                },
                "ibkr": {
                    "ready": True,
                    "open_positions": [
                        {
                            "symbol": "MNQM6",
                            "position": 1,
                            "market_price": 29000.0,
                            "market_value": 29000.0,
                            "unrealized_pnl": 25.0,
                            "secType": "FUT",
                        }
                    ],
                },
            },
        )
        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        portfolio = r.json()["portfolio_summary"]
        assert portfolio["source"] == "live_broker_state"
        assert portfolio["focus_policy"]["mode"] == "futures_focus"
        assert portfolio["focus_policy"]["active_venues"] == ["ibkr"]
        assert portfolio["focus_policy"]["standby_venues"] == ["tastytrade"]
        assert portfolio["focus_policy"]["dormant_venues"] == ["tradovate"]
        assert portfolio["focus_policy"]["paused_venues"] == ["alpaca"]
        assert portfolio["broker_net_pnl"] == 35.0
        assert portfolio["hidden_disabled_count"] == 0
        assert portfolio["unassigned_broker_position_count"] == 0
        assert portfolio["unassigned_broker_symbols"] == []
        assert portfolio["focus_open_position_count"] == 1
        assert portfolio["cellar_summary"]["hidden_bot_count"] == 1
        assert portfolio["cellar_summary"]["hidden_position_count"] == 2
        assert portfolio["cellar_summary"]["hidden_symbols"] == ["BTC", "BTCUSD", "ETHUSD"]
        sleeves = {row["sleeve"]: row for row in portfolio["allocation_sleeves"]}
        assert sleeves["equity_index_futures"]["open_position_count"] == 1
        assert "crypto" not in sleeves
        contributors = {(row["venue"], row["symbol"]): row for row in portfolio["pnl_contributors"]}
        assert contributors[("ibkr", "MNQM6")]["sleeve"] == "equity_index_futures"
        assert contributors[("ibkr", "MNQM6")]["ownership_status"] == "managed_symbol"
        assert contributors[("ibkr", "MNQM6")]["unrealized_pnl"] == 25.0
        assert ("alpaca", "BTCUSD") not in contributors
        assert ("alpaca", "ETHUSD") not in contributors

    def test_portfolio_symbol_roots_handle_dated_futures_contracts(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        assert mod._portfolio_sleeve_for_symbol("MNQM6") == "equity_index_futures"
        assert mod._portfolio_sleeve_for_symbol("NQM6") == "equity_index_futures"
        assert mod._portfolio_sleeve_for_symbol("MCLM6") == "commodities"
        assert mod._portfolio_sleeve_for_symbol("METK6") == "crypto_futures"
        assert mod._portfolio_sleeve_for_symbol("ETHUSD") == "crypto"

    def test_aggregate_portfolio_contributors_rolls_up_repeated_strategies_and_tickers(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        contributors = [
            {
                "type": "recent_close_realized",
                "bot_id": "propagate_bot",
                "symbol": "MNQ",
                "sleeve": "equity_index_futures",
                "realized_pnl": 1783.0,
                "source": "live_broker_state.position_exposure",
            },
            {
                "type": "recent_close_realized",
                "bot_id": "propagate_bot",
                "symbol": "MNQ",
                "sleeve": "equity_index_futures",
                "realized_pnl": 1783.0,
                "source": "live_broker_state.position_exposure",
            },
            {
                "type": "recent_close_realized",
                "bot_id": "t1",
                "symbol": "BTC",
                "sleeve": "crypto",
                "realized_pnl": 1.5,
                "source": "live_broker_state.position_exposure",
            },
            {
                "type": "open_position_unrealized",
                "venue": "ibkr",
                "symbol": "MNQM6",
                "symbol_root": "MNQ",
                "sleeve": "equity_index_futures",
                "market_value": 29000.0,
                "unrealized_pnl": -45.0,
                "source": "live_broker_state.position_exposure",
            },
            {
                "type": "open_position_unrealized",
                "venue": "ibkr",
                "symbol": "MNQU6",
                "symbol_root": "MNQ",
                "sleeve": "equity_index_futures",
                "market_value": 29500.0,
                "unrealized_pnl": -55.0,
                "source": "live_broker_state.position_exposure",
            },
        ]

        rolled = mod._aggregate_portfolio_contributors(contributors)
        keyed = {f"{row['aggregation']}:{row['aggregation_key']}": row for row in rolled}

        assert keyed["strategy:propagate_bot"]["pnl"] == 3566.0
        assert keyed["strategy:propagate_bot"]["realized_pnl"] == 3566.0
        assert keyed["strategy:propagate_bot"]["close_count"] == 2
        assert keyed["strategy:propagate_bot"]["symbol"] == "MNQ"

        assert keyed["strategy:t1"]["pnl"] == 1.5
        assert keyed["strategy:t1"]["close_count"] == 1
        assert keyed["strategy:t1"]["symbol"] == "BTC"

        assert keyed["ticker:MNQ"]["pnl"] == -100.0
        assert keyed["ticker:MNQ"]["unrealized_pnl"] == -100.0
        assert keyed["ticker:MNQ"]["open_count"] == 2
        assert keyed["ticker:MNQ"]["market_value"] == 58500.0
        assert keyed["ticker:MNQ"]["venue"] == "ibkr"

    def test_bot_fleet_includes_supervisor_bots(self, app_client, tmp_path, monkeypatch):
        """Supervisor heartbeat bots appear in /api/bot-fleet even when state/bots/ is empty."""
        import json
        import os
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "today_actual_fills": 0,
                "today_realized_pnl": 0.0,
                "total_unrealized_pnl": 0.0,
                "open_position_count": 0,
                "win_rate_30d": None,
                "alpaca": {"ready": True, "open_positions": [], "open_position_count": 0},
                "ibkr": {"ready": True, "open_positions": [], "open_position_count": 0},
            },
        )

        state = Path(os.environ["ETA_STATE_DIR"])
        # Ensure state/bots/ exists but is empty (no legacy bots)
        (state / "bots").mkdir(parents=True, exist_ok=True)

        # Write supervisor heartbeat
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        mnq_signal_at = (now_dt - timedelta(minutes=1)).isoformat()
        btc_entry_at = (now_dt - timedelta(minutes=2)).isoformat()
        hb = {
            "ts": now,
            "mode": "paper_sim",
            "bots": [
                {
                    "bot_id": "mnq_futures_sage",
                    "symbol": "MNQ1",
                    "strategy_kind": "orb",
                    "direction": "long",
                    "n_entries": 5,
                    "n_exits": 5,
                    "realized_pnl": 2.0,
                    "open_position": None,
                    "last_jarvis_verdict": "APPROVED",
                    "last_signal_at": mnq_signal_at,
                    "last_bar_ts": now,
                    "strategy_readiness": {
                        "status": "ready",
                        "launch_lane": "live_preflight",
                        "can_paper_trade": True,
                        "can_live_trade": False,
                        "next_action": "Run per-bot promotion preflight before live routing.",
                    },
                },
                {
                    "bot_id": "btc_hybrid",
                    "symbol": "BTC",
                    "strategy_kind": "hybrid",
                    "direction": "long",
                    "n_entries": 2,
                    "n_exits": 1,
                    "realized_pnl": -0.5,
                    "open_position": {
                        "side": "BUY",
                        "qty": 0.05,
                        "entry_price": 67000.0,
                        "entry_ts": btc_entry_at,
                        "mark_price": 67350.0,
                        "bracket_stop": 66200.0,
                        "bracket_target": 68400.0,
                        "last_bar_high": 67400.0,
                        "last_bar_low": 67100.0,
                        "broker_bracket": False,
                        "bracket_src": "supervisor_local",
                        "signal_id": "btc_hybrid_001",
                    },
                    "last_jarvis_verdict": "CONDITIONAL",
                    "last_bar_ts": now,
                },
            ],
        }
        (sup_dir / "heartbeat.json").write_text(json.dumps(hb))

        r = app_client.get("/api/bot-fleet")
        assert r.status_code == 200
        data = r.json()
        names = [b["name"] for b in data["bots"]]
        assert "mnq_futures_sage" in names, f"mnq_futures_sage missing from roster: {names}"
        assert "btc_hybrid" in names, f"btc_hybrid missing from roster: {names}"

        mnq = next(b for b in data["bots"] if b["name"] == "mnq_futures_sage")
        assert mnq["todays_pnl"] == 2.0
        assert mnq["status"] == "running"
        assert mnq["source"] == "jarvis_strategy_supervisor"
        assert mnq["last_trade_ts"] is None
        assert mnq["last_trade_side"] is None
        assert mnq["last_trade_qty"] is None
        assert mnq["last_trade_r"] is None
        assert mnq["last_signal_ts"] == mnq_signal_at
        assert mnq["last_signal_side"] == "LONG"
        assert mnq["last_activity_ts"] == mnq_signal_at
        assert mnq["last_activity_side"] == "LONG"
        assert mnq["last_activity_type"] == "signal"
        assert mnq["last_bar_ts"] == now
        assert mnq["venue"] == "paper-sim"
        assert mnq["tier"] == "orb"
        assert mnq["strategy_readiness"]["launch_lane"] == "live_preflight"
        assert mnq["launch_lane"] == "live_preflight"
        assert mnq["can_paper_trade"] is True
        assert mnq["can_live_trade"] is False
        btc = next(b for b in data["bots"] if b["name"] == "btc_hybrid")
        assert btc["open_position"]["entry_price"] == 67000.0
        assert btc["position_state"]["state"] == "open"
        assert btc["position_state"]["side"] == "BUY"
        assert btc["position_state"]["qty"] == 0.05
        assert btc["position_state"]["mark_price"] == 67350.0
        assert btc["position_state"]["target_distance_points"] == 1050.0
        assert btc["position_state"]["stop_distance_points"] == 1150.0
        assert btc["position_state"]["target_exit_visibility"]["status"] == "watching"
        assert btc["position_state"]["target_exit_visibility"]["owner"] == "supervisor"
        assert btc["position_state"]["target_exit_visibility"]["target_progress_pct"] == 25.0
        assert btc["position_state"]["target_exit_visibility"]["stop_cushion_pct"] == 143.75
        assert btc["position_state"]["target_progress_pct"] == 25.0
        assert btc["position_state"]["stop_cushion_pct"] == 143.75
        assert btc["open_positions"] == 1
        assert btc["last_signal_ts"] == btc_entry_at
        assert btc["last_activity_type"] == "signal"
        assert btc["bracket_stop"] == 66200.0
        assert btc["bracket_target"] == 68400.0
        assert btc["broker_bracket"] is False
        assert btc["bracket_src"] == "supervisor_local"
        assert data["latest_signal_ts"] == mnq_signal_at
        assert data["summary"]["latest_signal_ts"] == mnq_signal_at
        assert data["signal_cadence"]["status"] == "staggered"
        assert data["signal_cadence"]["signal_update_count"] == 2
        assert data["signal_cadence"]["unique_signal_seconds"] == 2
        exit_summary = data["target_exit_summary"]
        assert exit_summary["status"] == "paper_watching"
        assert exit_summary["open_position_count"] == 1
        assert exit_summary["broker_open_position_count"] == 0
        assert exit_summary["supervisor_local_position_count"] == 1
        assert exit_summary["supervisor_watch_count"] == 1
        assert exit_summary["broker_bracket_count"] == 0
        assert exit_summary["missing_bracket_count"] == 0
        assert "0 broker open" in exit_summary["summary_line"]
        assert "1 supervisor paper-local open" in exit_summary["summary_line"]
        assert exit_summary["nearest_target_bot"] == "btc_hybrid"
        assert exit_summary["nearest_target_distance_points"] == 1050.0
        assert data["summary"]["target_exit_status"] == "paper_watching"
        assert data["summary"]["open_position_count_visible"] == 1
        assert data["summary"]["supervisor_exit_watch_count"] == 1
        embedded_exposure = data["live_broker_state"]["position_exposure"]
        assert embedded_exposure["target_exit_visibility"]["status"] == "paper_watching"
        assert embedded_exposure["supervisor_local_position_count"] == 1
        assert embedded_exposure["supervisor_watch_count"] == 1
        assert data["signal_cadence"]["max_same_second"] == 1
        assert data["summary"]["signal_cadence_status"] == "staggered"
        assert data["summary"]["active_bots"] == 2
        assert data["summary"]["active_bot_count"] == 2
        assert data["summary"]["runtime_active_bots"] == 2
        assert data["summary"]["running_bots"] == 2
        assert data["summary"]["live_attached_bots"] == 2
        assert data["summary"]["live_in_trade_bots"] == 1
        assert data["summary"]["idle_live_bots"] == 1
        assert data["summary"]["inactive_runtime_bots"] == 0
        assert data["summary"]["staged_bots"] == 0
        assert data["active_bots"] == 2
        assert data["runtime_active_bots"] == 2
        assert data["live_attached_bots"] == 2
        assert data["live_in_trade_bots"] == 1
        assert data["idle_live_bots"] == 1
        assert data["inactive_runtime_bots"] == 0
        assert data["staged_bots"] == 0

        drill = app_client.get("/api/bot-fleet/mnq_futures_sage")
        assert drill.status_code == 200
        drill_data = drill.json()
        assert drill_data["status"]["strategy_readiness"]["launch_lane"] == "live_preflight"
        assert drill_data["status"]["can_paper_trade"] is True
        assert drill_data["status"]["readiness_next_action"] == "Run per-bot promotion preflight before live routing."
        assert drill_data["strategy_readiness"]["launch_lane"] == "live_preflight"
        assert "_warning" not in drill_data

        assert data["confirmed_bots"] == 2
        assert data["dashboard_version"] == "v1"
        assert data["release_stage"] == "pre_beta"
        assert data["beta_launched"] is False
        assert set(data["required_data"]) == {
            "bot_fleet",
            "fleet_equity",
            "auth_session",
            "source_freshness",
        }

    def test_bot_fleet_summary_exposes_stale_position_sla(self, app_client, monkeypatch):
        """Top cards should distinguish tighten-stop warnings from force-flatten due."""
        import json
        import os
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        server_dt = datetime(2026, 4, 28, 12, 10, 0, tzinfo=UTC)
        monkeypatch.setattr(mod.time, "time", lambda: server_dt.timestamp())
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "today_actual_fills": 0,
                "today_realized_pnl": 0.0,
                "total_unrealized_pnl": 0.0,
                "open_position_count": 0,
                "win_rate_30d": None,
                "alpaca": {"ready": True, "open_positions": [], "open_position_count": 0},
                "ibkr": {"ready": True, "open_positions": [], "open_position_count": 0},
            },
        )
        state = Path(os.environ["ETA_STATE_DIR"])
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": server_dt.isoformat(),
                    "mode": "paper_live",
                    "bots": [
                        {
                            "bot_id": "btc_hybrid",
                            "symbol": "BTC",
                            "strategy_kind": "hybrid",
                            "direction": "long",
                            "n_entries": 1,
                            "n_exits": 0,
                            "realized_pnl": 0.0,
                            "open_position": {
                                "side": "BUY",
                                "qty": 0.05,
                                "entry_price": 67000.0,
                                "entry_ts": "2026-04-28T11:00:00+00:00",
                                "mark_price": 67350.0,
                                "bracket_stop": 66200.0,
                                "bracket_target": 68400.0,
                                "broker_bracket": False,
                                "bracket_src": "supervisor_local",
                            },
                            "last_bar_ts": server_dt.isoformat(),
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        summary = r.json()["summary"]
        assert summary["stale_position_status"] == "tighten_stop_due"
        assert summary["tighten_stop_due_count"] == 1
        assert summary["force_flatten_due_count"] == 0
        assert summary["require_ack_count"] == 0
        assert summary["stale_position_oldest_bot"] == "btc_hybrid"
        assert summary["stale_position_oldest_symbol"] == "BTC"
        assert summary["stale_position_oldest_age_s"] == 4200
        assert summary["stale_position_oldest_next_action"] == "tighten_stop_or_ack"
        assert summary["stale_position_seconds_to_next_action"] == 3000

    def test_bot_fleet_keeps_bar_refreshes_out_of_signal_times(self, app_client):
        """Supervisor bar timestamps are freshness evidence, not trade signals."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "bots").mkdir(parents=True, exist_ok=True)
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        hb = {
            "ts": "2026-04-28T12:00:05+00:00",
            "mode": "paper_live",
            "bots": [
                {
                    "bot_id": "bar_only_mnq",
                    "symbol": "MNQ1",
                    "strategy_kind": "orb",
                    "direction": "long",
                    "n_entries": 0,
                    "n_exits": 0,
                    "realized_pnl": 0.0,
                    "open_position": None,
                    "last_jarvis_verdict": "NONE",
                    "last_bar_ts": "2026-04-28T12:00:00+00:00",
                }
            ],
        }
        (sup_dir / "heartbeat.json").write_text(json.dumps(hb))

        r = app_client.get("/api/bot-fleet")
        assert r.status_code == 200
        row = next(b for b in r.json()["bots"] if b["name"] == "bar_only_mnq")
        assert row["last_signal_ts"] is None
        assert row["last_signal_side"] is None
        assert row["last_activity_ts"] == "2026-04-28T12:00:00+00:00"
        assert row["last_activity_side"] is None
        assert row["last_activity_type"] == "bar"
        assert row["last_bar_ts"] == "2026-04-28T12:00:00+00:00"
        assert r.json()["signal_cadence"]["status"] == "no_signals"

    def test_bot_fleet_signal_cadence_flags_same_second_clusters(self, app_client):
        """Same-second signal bursts should be visible instead of hand-waved."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "bots").mkdir(parents=True, exist_ok=True)
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        signal_ts = "2026-04-28T12:01:15.123456+00:00"
        hb = {
            "ts": "2026-04-28T12:01:20+00:00",
            "mode": "paper_live",
            "bots": [
                {
                    "bot_id": f"clustered_{idx}",
                    "symbol": symbol,
                    "strategy_kind": "confluence",
                    "direction": "long",
                    "n_entries": 1,
                    "n_exits": 0,
                    "realized_pnl": 0.0,
                    "open_position": None,
                    "last_signal_ts": signal_ts,
                    "last_signal_side": "BUY",
                    "last_bar_ts": "2026-04-28T12:00:00+00:00",
                }
                for idx, symbol in enumerate(["BTC", "ETH", "SOL"], start=1)
            ],
        }
        (sup_dir / "heartbeat.json").write_text(json.dumps(hb))

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        cadence = r.json()["signal_cadence"]
        assert cadence["status"] == "clustered"
        assert cadence["signal_update_count"] == 3
        assert cadence["unique_signal_seconds"] == 1
        assert cadence["max_same_second"] == 3
        assert cadence["same_second_ratio"] == 1.0
        assert cadence["top_signal_second"] == "2026-04-28T12:01:15Z"
        assert cadence["synchronized_signal_seconds"] == 1

    def test_signal_cadence_marks_watching_when_bars_are_fresh(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        server_dt = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
        rows = [
            {
                "name": "mnq_futures_sage",
                "last_signal_ts": "2026-04-28T09:30:00+00:00",
                "last_signal_side": "BUY",
                "last_bar_ts": "2026-04-28T11:59:40+00:00",
                "open_positions": 1,
            },
            {
                "name": "volume_profile_nq",
                "last_signal_ts": "2026-04-28T10:10:00+00:00",
                "last_signal_side": "BUY",
                "last_bar_ts": "2026-04-28T11:59:45+00:00",
                "open_positions": 1,
            },
        ]

        cadence = mod._signal_cadence_summary(rows, server_ts=server_dt.timestamp())

        assert cadence["status"] == "watching"
        assert cadence["freshness_status"] == "watching_fresh_bars"
        assert cadence["latest_bar_age_s"] == 15
        assert cadence["open_position_count"] == 2
        assert "paper position(s) are being watched" in cadence["detail"]

    def test_bot_fleet_hides_registry_deactivated_supervisor_rows(self, app_client):
        """Registry-retired bots should not leak through live supervisor heartbeats."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "bots").mkdir(parents=True, exist_ok=True)
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        hb = {
            "ts": "2026-05-08T03:40:00+00:00",
            "mode": "paper_live",
            "bots": [
                {
                    "bot_id": "rsi_mr_mnq",
                    "symbol": "MNQ1",
                    "strategy_kind": "confluence_scorecard",
                    "direction": "long",
                    "n_entries": 1,
                    "n_exits": 0,
                    "realized_pnl": 0.0,
                    "open_position": None,
                    "last_signal_ts": "2026-05-08T03:39:00+00:00",
                    "last_signal_side": "BUY",
                    "last_bar_ts": "2026-05-08T03:39:30+00:00",
                },
                {
                    "bot_id": "mnq_futures_sage",
                    "symbol": "MNQ1",
                    "strategy_kind": "confluence_scorecard",
                    "direction": "long",
                    "n_entries": 1,
                    "n_exits": 0,
                    "realized_pnl": 0.0,
                    "open_position": None,
                    "last_signal_ts": "2026-05-08T03:39:10+00:00",
                    "last_signal_side": "BUY",
                    "last_bar_ts": "2026-05-08T03:39:30+00:00",
                },
            ],
        }
        (sup_dir / "heartbeat.json").write_text(json.dumps(hb), encoding="utf-8")

        visible = app_client.get("/api/bot-fleet")

        assert visible.status_code == 200
        names = {row["name"] for row in visible.json()["bots"]}
        assert "mnq_futures_sage" in names
        assert "rsi_mr_mnq" not in names

        debug = app_client.get("/api/bot-fleet?include_disabled=true")
        rows = {row["name"]: row for row in debug.json()["bots"]}
        assert rows["rsi_mr_mnq"]["registry_deactivated"] is True
        assert rows["rsi_mr_mnq"]["registry_active"] is False
        assert rows["mnq_futures_sage"]["registry_active"] is True

    def test_bot_fleet_drilldown_prefers_supervisor_open_position(self, app_client):
        """Per-bot drilldown must not hide live supervisor positions behind legacy status."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        legacy_dir = state / "bots" / "btc_hybrid"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "status.json").write_text(
            json.dumps(
                {
                    "name": "btc_hybrid",
                    "symbol": "BTC",
                    "status": "idle",
                    "open_positions": 0,
                    "open_position": {},
                    "position_state": {"state": "flat"},
                }
            ),
            encoding="utf-8",
        )
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": "2026-05-08T00:00:00+00:00",
                    "mode": "paper_live",
                    "bots": [
                        {
                            "bot_id": "btc_hybrid",
                            "symbol": "BTC",
                            "strategy_kind": "hybrid",
                            "direction": "long",
                            "n_entries": 0,
                            "n_exits": 0,
                            "realized_pnl": 0.0,
                            "open_position": {
                                "side": "BUY",
                                "qty": 0.05,
                                "entry_price": 67000.0,
                                "mark_price": 67350.0,
                                "bracket_stop": 66200.0,
                                "bracket_target": 68400.0,
                                "last_bar_high": 67400.0,
                                "last_bar_low": 67100.0,
                                "broker_bracket": False,
                                "bracket_src": "supervisor_local",
                            },
                            "last_bar_ts": "2026-05-08T00:00:00+00:00",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet/btc_hybrid")

        assert r.status_code == 200
        status = r.json()["status"]
        assert status["status"] == "running"
        assert status["open_positions"] == 1
        assert status["position_state"]["state"] == "open"
        assert status["position_state"]["qty"] == 0.05
        assert status["position_state"]["target_distance_points"] == 1050.0
        assert status["position_state"]["stop_distance_points"] == 1150.0
        assert status["position_state"]["target_exit_visibility"]["status"] == "watching"
        assert status["bracket_target"] == 68400.0
        assert status["bracket_stop"] == 66200.0

    def test_bot_fleet_and_drilldown_surface_retune_focus_active_experiment(self, app_client, monkeypatch):
        import json
        import os
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "diamond_retune_status_latest.json").write_text(
            json.dumps(
                {
                    "kind": "eta_diamond_retune_status",
                    "status": "ready",
                    "focus_bot": "mnq_futures_sage",
                    "focus_issue": "broker_pnl_negative",
                    "focus_state": "COLLECT_MORE_SAMPLE",
                    "focus_next_action": "Let fresh post-fix closes accumulate.",
                    "focus_active_experiment": {
                        "experiment_id": "partial_profit_disabled",
                        "started_at": "2026-05-16T01:44:06+00:00",
                        "partial_profit_enabled": False,
                        "post_change_closed_trade_count": 2,
                        "post_change_cumulative_r": 0.8192,
                        "post_change_total_realized_pnl": 40.0,
                        "post_change_profit_factor": 1.5,
                    },
                    "summary": {
                        "broker_truth_focus_bot_id": "mnq_futures_sage",
                        "broker_truth_focus_state": "COLLECT_MORE_SAMPLE",
                        "broker_truth_focus_next_action": "Let fresh post-fix closes accumulate.",
                        "broker_truth_focus_active_experiment": {
                            "experiment_id": "partial_profit_disabled",
                            "started_at": "2026-05-16T01:44:06+00:00",
                            "partial_profit_enabled": False,
                            "post_change_closed_trade_count": 2,
                            "post_change_cumulative_r": 0.8192,
                            "post_change_total_realized_pnl": 40.0,
                            "post_change_profit_factor": 1.5,
                        },
                        "broker_truth_focus_active_experiment_summary_line": (
                            "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
                        ),
                    },
                    "bots": [],
                    "research_backlog": [],
                }
            ),
            encoding="utf-8",
        )
        original_readiness_payload = mod._eta_readiness_snapshot_payload

        def _patched_readiness_payload(*, server_ts: float):
            payload = original_readiness_payload(server_ts=server_ts)
            payload.update(
                {
                    "public_live_retune_focus_active_experiment_outcome_line": (
                        "partial_profit_disabled: awaiting first post-change close"
                    ),
                    "local_retune_focus_active_experiment_outcome_line": (
                        "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
                    ),
                    "retune_focus_active_experiment_drift_display": (
                        "public retune says partial_profit_disabled: awaiting first post-change close; "
                        "local mirror says partial_profit_disabled: 2 post-change closes | R +0.82 | "
                        "PnL $40.00 | PF 1.50"
                    ),
                }
            )
            return payload

        monkeypatch.setattr(mod, "_eta_readiness_snapshot_payload", _patched_readiness_payload)
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": "2026-05-16T02:00:00+00:00",
                    "mode": "paper_live",
                    "bots": [
                        {
                            "bot_id": "mnq_futures_sage",
                            "symbol": "MNQ1",
                            "strategy_kind": "orb",
                            "direction": "long",
                            "n_entries": 5,
                            "n_exits": 5,
                            "realized_pnl": 2.0,
                            "open_position": None,
                            "last_jarvis_verdict": "APPROVED",
                            "last_signal_at": "2026-05-16T01:55:00+00:00",
                            "last_bar_ts": "2026-05-16T02:00:00+00:00",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        roster = app_client.get("/api/bot-fleet?bot=mnq_futures_sage")
        assert roster.status_code == 200
        roster_data = roster.json()
        row = roster_data["bots"][0]
        assert roster_data["summary"]["retune_focus_bot_id"] == "mnq_futures_sage"
        assert roster_data["summary"]["retune_focus_active_experiment_summary_line"] == (
            "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
        )
        assert (
            roster_data["summary"]["public_live_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: awaiting first post-change close"
        )
        assert (
            roster_data["summary"]["local_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        )
        assert (
            roster_data["summary"]["retune_focus_active_experiment_drift_display"]
            == "public retune says partial_profit_disabled: awaiting first post-change close; "
            "local mirror says partial_profit_disabled: 2 post-change closes | R +0.82 | "
            "PnL $40.00 | PF 1.50"
        )
        assert row["retune_focus_active_experiment"]["experiment_id"] == "partial_profit_disabled"
        assert row["retune_focus_active_experiment_outcome_line"] == (
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        )
        assert (
            row["public_live_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: awaiting first post-change close"
        )
        assert (
            row["local_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        )
        assert (
            row["retune_focus_active_experiment_drift_display"]
            == "public retune says partial_profit_disabled: awaiting first post-change close; "
            "local mirror says partial_profit_disabled: 2 post-change closes | R +0.82 | "
            "PnL $40.00 | PF 1.50"
        )
        assert row["retune_focus_next_action"] == "Let fresh post-fix closes accumulate."

        drill = app_client.get("/api/bot-fleet/mnq_futures_sage")
        assert drill.status_code == 200
        drill_data = drill.json()
        assert drill_data["status"]["retune_focus_active_experiment"]["partial_profit_enabled"] is False
        assert drill_data["retune_focus_active_experiment_summary_line"] == (
            "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
        )
        assert drill_data["retune_focus_active_experiment_outcome_line"] == (
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        )
        assert (
            drill_data["public_live_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: awaiting first post-change close"
        )
        assert (
            drill_data["local_retune_focus_active_experiment_outcome_line"]
            == "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        )
        assert (
            drill_data["retune_focus_active_experiment_drift_display"]
            == "public retune says partial_profit_disabled: awaiting first post-change close; "
            "local mirror says partial_profit_disabled: 2 post-change closes | R +0.82 | "
            "PnL $40.00 | PF 1.50"
        )

    def test_supervisor_heartbeat_freshness_is_not_last_signal_freshness(self, app_client):
        """A quiet bot should stay live when the supervisor heartbeat is fresh."""
        import os
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "bots").mkdir(parents=True, exist_ok=True)
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        old_signal = (now - timedelta(minutes=20)).isoformat()
        hb_ts = now.isoformat()
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": hb_ts,
                    "mode": "paper_live",
                    "bots": [
                        {
                            "bot_id": "mnq_quiet",
                            "symbol": "MNQ1",
                            "strategy_kind": "orb",
                            "direction": "long",
                            "n_entries": 0,
                            "n_exits": 0,
                            "realized_pnl": 0.0,
                            "open_position": None,
                            "last_signal_at": old_signal,
                            "last_bar_ts": old_signal,
                        },
                    ],
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        data = r.json()
        row = next(b for b in data["bots"] if b["name"] == "mnq_quiet")
        assert data["truth_status"] == "live"
        assert row["heartbeat_ts"] == hb_ts
        assert row["heartbeat_age_s"] <= 10
        assert row["last_signal_ts"] == old_signal
        assert row["last_signal_age_s"] >= 20 * 60

    def test_bot_fleet_reports_working_when_keepalive_is_fresh_but_main_snapshot_stale(
        self,
        app_client,
    ):
        """Keepalive prevents a busy supervisor tick from being shown as dead/stale."""
        import os
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "bots").mkdir(parents=True, exist_ok=True)
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        stale_ts = (now - timedelta(minutes=20)).isoformat()
        keepalive_ts = now.isoformat()
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": stale_ts,
                    "mode": "paper_live",
                    "bots": [
                        {
                            "bot_id": "mnq_busy",
                            "symbol": "MNQ1",
                            "strategy_kind": "orb",
                            "direction": "long",
                            "n_entries": 0,
                            "n_exits": 0,
                            "realized_pnl": 0.0,
                            "open_position": None,
                            "last_bar_ts": stale_ts,
                        },
                    ],
                },
            ),
            encoding="utf-8",
        )
        (sup_dir / "heartbeat_keepalive.json").write_text(
            json.dumps({"keepalive_ts": keepalive_ts}),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        data = r.json()
        assert data["truth_status"] == "working"
        assert "supervisor process is alive" in data["truth_summary_line"]
        assert data["supervisor_liveness"]["keepalive_fresh"] is True
        assert data["supervisor_liveness"]["main_heartbeat_fresh"] is False

    def test_bot_fleet_truth_summary_prioritizes_active_order_hold_when_roster_stale(
        self,
        app_client,
    ):
        """A stale roster should still lead with the active execution hold."""
        import json
        import os
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "bots").mkdir(parents=True, exist_ok=True)
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        stale_ts = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": stale_ts,
                    "mode": "paper_live",
                    "bots": [
                        {
                            "bot_id": "mnq_stale",
                            "symbol": "MNQ1",
                            "strategy_kind": "orb",
                            "direction": "long",
                            "n_entries": 0,
                            "n_exits": 0,
                            "realized_pnl": 0.0,
                            "open_position": None,
                            "last_bar_ts": stale_ts,
                        },
                    ],
                },
            ),
            encoding="utf-8",
        )
        (state / "order_entry_hold.json").write_text(
            json.dumps(
                {
                    "active": True,
                    "reason": "ibgateway_waiting_for_manual_login_or_2fa",
                    "operator": "codex",
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        data = r.json()
        assert data["truth_status"] == "stale"
        assert data["truth_execution_hold"]["reason"] == "ibgateway_waiting_for_manual_login_or_2fa"
        assert data["truth_summary_line"].startswith(
            "Paper-live execution is held: ibgateway_waiting_for_manual_login_or_2fa"
        )
        assert "none have a fresh heartbeat" in data["truth_summary_line"]
        assert "order_entry_hold: ibgateway_waiting_for_manual_login_or_2fa" in data["truth_warnings"]

    def test_truth_snapshot_suppresses_legacy_state_warnings_when_supervisor_rows_are_live(
        self,
        app_client,
        monkeypatch,
    ):
        """Fresh supervisor rows are the live truth source, even if legacy bot files are absent."""
        import os
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        state = Path(os.environ["ETA_STATE_DIR"])
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            mod,
            "_read_runtime_state",
            lambda: {
                "_warning": "missing_runtime_state",
                "_path": r"C:\EvolutionaryTradingAlgo\firm_command_center\var\data\runtime_state.json",
            },
        )

        truth = mod._truth_snapshot([{"heartbeat_age_s": 12}], server_ts=1778160000.0)

        assert truth["truth_status"] == "live"
        assert "missing_runtime_state" not in truth["truth_warnings"]
        assert not any("missing bot status directory" in w for w in truth["truth_warnings"])

    def test_runtime_state_defaults_to_canonical_state_dir(self, app_client):
        """Runtime state should follow the active canonical state root."""
        import os
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        state = Path(os.environ["ETA_STATE_DIR"])

        assert mod._runtime_state_path() == state / "runtime_state.json"

    def test_truth_snapshot_derives_runtime_from_fresh_supervisor_rows(
        self,
        app_client,
        monkeypatch,
    ):
        """Fresh supervisor rows should not expose a missing runtime file as headline state."""
        import os
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        state = Path(os.environ["ETA_STATE_DIR"])
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text(
            json.dumps({"ts": "2026-05-08T08:00:00+00:00"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            mod,
            "_read_runtime_state",
            lambda: {"_warning": "missing_runtime_state", "_path": str(state / "runtime_state.json")},
        )

        truth = mod._truth_snapshot([{"heartbeat_age_s": 12}], server_ts=1778227200.0)

        assert truth["truth_status"] == "live"
        assert truth["runtime"]["source"] == "derived_from_supervisor_heartbeats"
        assert truth["runtime_mode"] == "running"
        assert truth["runtime_detail"] == "fresh_supervisor_heartbeats"
        assert "missing_runtime_state" not in truth["truth_warnings"]
        assert not any("runtime reports" in w for w in truth["truth_warnings"])

    def test_truth_snapshot_reports_state_warnings_when_no_live_rows(
        self,
        app_client,
        monkeypatch,
    ):
        """State warnings still surface when no live heartbeat rows can prove fleet truth."""
        import os
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        state = Path(os.environ["ETA_STATE_DIR"])
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            mod,
            "_read_runtime_state",
            lambda: {"_warning": "missing_runtime_state", "_path": "runtime_state.json"},
        )

        truth = mod._truth_snapshot([], server_ts=1778160000.0)

        assert truth["truth_status"] == "empty"
        assert "missing_runtime_state" in truth["truth_warnings"]
        assert any("missing bot status directory" in w for w in truth["truth_warnings"])

    def test_bot_fleet_gateway_detail_reports_process_not_running(self, app_client):
        """Gateway process absence should be explicit, not hidden as missing metadata."""
        import json
        import os
        from datetime import UTC, datetime
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        (state / "tws_watchdog.json").write_text(
            json.dumps(
                {
                    "checked_at": datetime.now(UTC).isoformat(),
                    "healthy": False,
                    "consecutive_failures": 7,
                    "last_healthy_at": "2026-05-06T04:43:25+00:00",
                    "details": {
                        "host": "127.0.0.1",
                        "port": 4002,
                        "socket_ok": False,
                        "handshake_ok": False,
                        "handshake_detail": "ConnectionRefusedError",
                        "gateway_process": {
                            "running": False,
                            "gateway_dir": r"C:\Jts\ibgateway\1046",
                            "name": "ibgateway.exe",
                        },
                    },
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        ibkr = r.json()["broker_gateway"]["ibkr"]
        assert ibkr["status"] == "down"
        assert r.json()["broker_gateway"]["status"] == "down"
        assert r.json()["summary"]["ibkr_gateway_status"] == "down"
        assert ibkr["process"]["running"] is False
        assert "gateway process not running" in ibkr["detail"]
        assert "gateway process not running" in r.json()["summary"]["ibkr_gateway_detail"]

    def test_bot_fleet_marks_recent_gateway_flap_degraded_during_self_heal(self, app_client):
        """A fresh watchdog flap after recent health should not look like a dead Gateway."""
        import json
        import os
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        (state / "tws_watchdog.json").write_text(
            json.dumps(
                {
                    "checked_at": now.isoformat(),
                    "healthy": False,
                    "consecutive_failures": 1,
                    "last_healthy_at": (now - timedelta(seconds=45)).isoformat(),
                    "details": {
                        "host": "127.0.0.1",
                        "port": 4002,
                        "socket_ok": True,
                        "handshake_ok": False,
                        "handshake_detail": "TimeoutError",
                        "gateway_process": {
                            "running": True,
                            "gateway_dir": r"C:\Jts\ibgateway\1046",
                            "name": "java.exe",
                        },
                    },
                },
            ),
            encoding="utf-8",
        )
        (state / "ibgateway_reauth.json").write_text(
            json.dumps(
                {
                    "status": "auth_pending",
                    "operator_action_required": False,
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        ibkr = r.json()["broker_gateway"]["ibkr"]
        assert ibkr["status"] == "degraded"
        assert ibkr["healthy"] is False
        assert r.json()["broker_gateway"]["status"] == "degraded"
        assert r.json()["summary"]["ibkr_gateway_status"] == "degraded"
        assert r.json()["summary"]["ibkr_gateway_raw_status"] == "degraded"
        assert ibkr["transient_self_heal_grace_active"] is True
        assert 0 <= float(ibkr["last_healthy_age_s"]) <= 180
        assert "recovery: auth_pending" in ibkr["detail"]
        assert "self-heal grace active" in ibkr["detail"]

    def test_bot_fleet_exposes_non_authoritative_gateway_overlay(self, app_client, monkeypatch):
        """Local desktop should expose gateway-authority truth without hiding raw socket state."""
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_broker_gateway_snapshot",
            lambda: {
                "status": "down",
                "detail": "gateway process not running",
                "ibkr": {
                    "status": "down",
                    "healthy": False,
                    "detail": "gateway process not running",
                    "checked_at": None,
                },
            },
        )
        monkeypatch.setattr(
            mod,
            "_gateway_authority_payload",
            lambda: {
                "allowed": False,
                "source": "missing_marker",
                "reason": "This host is not marked as the ETA IBKR Gateway authority.",
            },
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        data = r.json()
        assert data["broker_gateway"]["status"] == "down"
        assert data["broker_gateway"]["non_authoritative_host"] is True
        assert data["broker_gateway"]["gateway_authority"]["allowed"] is False
        assert data["broker_gateway"]["ibkr"]["non_authoritative_host"] is True
        assert "gateway authority" in data["broker_gateway"]["detail"].lower()
        assert data["summary"]["ibkr_gateway_status"] == "vps_only"
        assert data["summary"]["ibkr_gateway_raw_status"] == "down"
        assert data["summary"]["ibkr_gateway_non_authoritative_host"] is True
        assert "gateway authority" in data["summary"]["ibkr_gateway_detail"].lower()

    def test_bot_fleet_surfaces_authoritative_stale_bracket_advisory(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_broker_bracket_audit_payload",
            lambda **kwargs: {
                "summary": "READY_NO_OPEN_EXPOSURE",
                "ready_for_prop_dry_run": True,
                "position_summary": {
                    "broker_open_position_count": 0,
                    "broker_bracket_required_position_count": 0,
                    "broker_bracket_count": 0,
                    "missing_bracket_count": 0,
                },
                "next_action": "",
            },
        )
        monkeypatch.setattr(
            mod,
            "_maybe_promote_validated_broker_bracket_artifact",
            lambda report, **kwargs: report,
        )
        monkeypatch.setattr(
            mod,
            "_paper_live_transition_payload",
            lambda *, refresh=False: {
                "generated_at": datetime.now(UTC).isoformat(),
                "status": "ready_to_launch_paper_live",
                "critical_ready": True,
                "paper_ready_bots": 7,
                "operator_queue_blocked_count": 0,
                "operator_queue_launch_blocked_count": 0,
                "gates": [],
            },
        )
        (tmp_path / "state" / "eta_readiness_snapshot_latest.json").write_text(
            json.dumps(
                {
                    "checked_at_utc": "2026-05-15T01:00:00+00:00",
                    "summary": "BLOCKED",
                    "non_authoritative_gateway_host": True,
                    "public_fallback_reason": "non_authoritative_gateway_host",
                    "public_fallback_primary_action": (
                        "Wait for or clear 159 stale broker order(s) before prop dry-run."
                    ),
                    "public_fallback_brackets_summary": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
                    "public_fallback_checks": [
                        {
                            "name": "broker_bracket_audit_public_fallback",
                            "status": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
                            "payload": {
                                "summary": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
                                "next_action": "Wait for or clear 159 stale broker order(s) before prop dry-run.",
                            },
                        }
                    ],
                    "checks": [
                        {
                            "name": "broker_bracket_audit",
                            "status": "READY_NO_OPEN_EXPOSURE",
                            "payload": {
                                "summary": "READY_NO_OPEN_EXPOSURE",
                                "position_summary": {
                                    "missing_bracket_count": 0,
                                    "broker_open_position_count": 0,
                                },
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        data = r.json()
        audit = data["broker_bracket_audit"]
        assert audit["summary"] == "READY_NO_OPEN_EXPOSURE"
        assert audit["effective_summary"] == "AUTHORITATIVE_STALE_REVIEW"
        assert audit["authoritative_advisory_active"] is True
        assert audit["authoritative_public_fallback_stale"] is True
        assert data["summary"]["broker_bracket_audit_status"] == "AUTHORITATIVE_STALE_REVIEW"
        assert data["summary"]["broker_bracket_prop_dry_run_blocked"] is True
        assert data["summary"]["broker_bracket_authoritative_advisory_stale"] is True
        assert "stale and last reported blocked stale flat open orders" in (
            data["summary"]["paper_live_effective_detail"].lower()
        )

    def test_bot_fleet_reconciles_gateway_detail_with_live_ibkr_positions(
        self,
        app_client,
        monkeypatch,
    ):
        """Gateway health detail should not hide fresher live IBKR exposure."""
        import json
        import os
        from datetime import UTC, datetime
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "tws_watchdog.json").write_text(
            json.dumps(
                {
                    "checked_at": datetime.now(UTC).isoformat(),
                    "healthy": True,
                    "consecutive_failures": 0,
                    "details": {
                        "host": "127.0.0.1",
                        "port": 4002,
                        "socket_ok": True,
                        "handshake_ok": True,
                        "handshake_detail": (
                            "serverVersion=176; clientId=9011; attempt=1; positions=0 open; executions=0"
                        ),
                    },
                },
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "today_actual_fills": 0,
                "today_realized_pnl": 0.0,
                "total_unrealized_pnl": -33.79,
                "open_position_count": 1,
                "win_rate_30d": None,
                "alpaca": {"ready": True, "open_positions": [], "open_position_count": 0},
                "ibkr": {
                    "ready": True,
                    "open_position_count": 1,
                    "open_positions": [
                        {
                            "symbol": "MNQM6",
                            "secType": "FUT",
                            "position": 3,
                            "market_price": 29335.0,
                            "market_value": 176010.0,
                            "unrealized_pnl": -33.79,
                        },
                    ],
                },
            },
        )
        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        payload = r.json()
        detail = payload["summary"]["ibkr_gateway_detail"]
        assert "positions=0 open" in detail
        assert "live broker exposure: 1 IBKR open (MNQM6)" in detail
        assert payload["broker_gateway"]["ibkr"]["live_broker_open_position_count"] == 1
        assert payload["broker_gateway"]["ibkr"]["live_broker_open_symbols"] == ["MNQM6"]

    def test_bot_fleet_exposes_broker_bracket_audit_from_target_exit_summary(
        self,
        app_client,
        monkeypatch,
    ):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.scripts import broker_bracket_audit

        monkeypatch.setattr(
            broker_bracket_audit,
            "_adapter_support",
            lambda: {
                "ibkr_futures_server_oco": True,
                "alpaca_equity_server_bracket": True,
                "tradovate_order_payload_brackets": True,
            },
        )
        monkeypatch.setattr(
            mod,
            "_supervisor_roster_rows",
            lambda now_ts, bot=None: [
                {
                    "id": "mnq_futures_sage",
                    "name": "mnq_futures_sage",
                    "symbol": "MNQ1",
                    "open_positions": 1,
                    "position_state": {
                        "state": "open",
                        "bracket_target": 29362.75,
                        "bracket_stop": 29323.75,
                        "target_exit_visibility": {
                            "status": "watching",
                            "owner": "supervisor",
                            "target_distance_points": 27.25,
                            "stop_distance_points": -11.75,
                        },
                    },
                },
            ],
        )
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "today_actual_fills": 0,
                "today_realized_pnl": 0.0,
                "total_unrealized_pnl": -33.79,
                "open_position_count": 1,
                "win_rate_30d": None,
                "alpaca": {"ready": True, "open_positions": [], "open_position_count": 0},
                "ibkr": {
                    "ready": True,
                    "open_position_count": 1,
                    "open_positions": [
                        {
                            "symbol": "MNQM6",
                            "secType": "FUT",
                            "position": 3,
                            "avg_cost": 58680.0,
                            "market_price": 29335.0,
                            "market_value": 176010.0,
                            "unrealized_pnl": -33.79,
                        },
                    ],
                },
            },
        )
        monkeypatch.setattr(
            mod,
            "_paper_live_transition_payload",
            lambda *, refresh=False: {
                "status": "ready_to_launch_paper_live",
                "critical_ready": True,
                "paper_ready_bots": 12,
                "operator_queue_launch_blocked_count": 0,
                "gates": [],
            },
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        payload = r.json()
        audit = payload["broker_bracket_audit"]
        assert audit["summary"] == "BLOCKED_UNBRACKETED_EXPOSURE"
        assert payload["target_exit_summary"]["broker_position_scope"] == "futures_focus"
        assert "futures-focus venues" in payload["target_exit_summary"]["broker_position_scope_detail"]
        assert payload["summary"]["target_exit_broker_position_scope"] == "futures_focus"
        assert audit["position_summary"]["broker_bracket_required_position_count"] == 1
        assert audit["position_summary"]["missing_bracket_count"] == 1
        assert audit["position_summary"]["unprotected_symbols"] == ["MNQM6"]
        assert audit["primary_unprotected_position"]["symbol"] == "MNQM6"
        assert audit["primary_unprotected_position"]["venue"] == "ibkr"
        assert audit["primary_unprotected_position"]["sec_type"] == "FUT"
        assert audit["primary_unprotected_position"]["avg_entry_price"] == 29340.0
        assert audit["primary_unprotected_position"]["current_price"] == 29335.0
        assert audit["primary_unprotected_position"]["unrealized_pct"] is None
        assert audit["unprotected_positions"][0]["broker_bracket_required"] is True
        assert audit["unprotected_positions"][0]["avg_entry_price"] == 29340.0
        assert audit["unprotected_positions"][0]["current_price"] == 29335.0
        assert audit["operator_action_required"] is True
        assert audit["operator_action"] == audit["next_action"]
        assert [action["id"] for action in audit["operator_actions"]] == [
            "verify_manual_broker_oco",
            "flatten_unprotected_paper_exposure",
        ]
        assert audit["operator_actions"][0]["symbol"] == "MNQM6"
        assert audit["operator_actions"][0]["order_action"] is False
        assert audit["operator_actions"][1]["order_action"] is True
        assert "MNQM6 IBKR FUT" in audit["next_action"]
        assert ".;" not in audit["next_action"]
        assert payload["summary"]["broker_bracket_audit_status"] == "BLOCKED_UNBRACKETED_EXPOSURE"
        assert payload["summary"]["broker_bracket_audit_ready"] is False
        assert payload["summary"]["broker_bracket_operator_action_required"] is True
        assert payload["summary"]["broker_bracket_prop_dry_run_blocked"] is True
        assert payload["summary"]["paper_live_effective_status"] == "held_by_bracket_audit"
        assert payload["summary"]["paper_live_held_by_bracket_audit"] is True
        assert payload["summary"]["paper_live_effective_detail"] == (
            "held by Bracket Audit: Verify broker OCO coverage or Flatten unprotected paper exposure"
        )
        assert payload["summary"]["broker_bracket_missing_count"] == 1
        assert payload["summary"]["broker_bracket_unprotected_symbols"] == ["MNQM6"]
        assert payload["summary"]["broker_bracket_operator_action_count"] == 2
        assert payload["summary"]["broker_bracket_operator_action_ids"] == [
            "verify_manual_broker_oco",
            "flatten_unprotected_paper_exposure",
        ]
        assert payload["summary"]["broker_bracket_operator_action_labels"] == [
            "Verify broker OCO coverage",
            "Flatten unprotected paper exposure",
        ]
        assert payload["summary"]["broker_bracket_manual_action_count"] == 2
        assert payload["summary"]["broker_bracket_order_action_count"] == 1
        assert payload["summary"]["broker_bracket_primary_action_label"] == "Verify broker OCO coverage"
        assert payload["summary"]["broker_bracket_primary_action_detail"] == (
            "Confirm MNQM6 IBKR FUT has broker-native TP/SL OCO attached outside ETA."
        )
        assert payload["summary"]["broker_bracket_order_action_label"] == "Flatten unprotected paper exposure"
        assert payload["summary"]["broker_bracket_order_action_detail"] == (
            "Alternative: flatten MNQM6 IBKR FUT before prop dry-run if no OCO exists."
        )
        assert "MNQM6 IBKR FUT" in payload["summary"]["broker_bracket_next_action"]
        assert payload["summary"]["broker_bracket_primary_symbol"] == "MNQM6"
        assert payload["summary"]["broker_bracket_primary_venue"] == "ibkr"
        assert payload["summary"]["broker_bracket_primary_sec_type"] == "FUT"
        assert payload["summary"]["broker_bracket_primary_side"] == "long"
        assert payload["summary"]["broker_bracket_primary_qty"] == 3.0
        assert payload["summary"]["broker_bracket_primary_market_value"] == 176010.0
        assert payload["summary"]["broker_bracket_primary_unrealized_pnl"] == -33.79
        assert payload["summary"]["broker_bracket_primary_coverage_status"] == ("requires_manual_oco_verification")

    def test_bot_fleet_defaults_to_cached_broker_state(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        def fail_live_probe() -> dict:
            raise AssertionError("default bot-fleet roster must not open a fresh broker probe")

        monkeypatch.setattr(mod, "_live_broker_state_payload", fail_live_probe)
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "source": "cached_live_broker_state_for_diagnostics",
                "probe_skipped": True,
                "broker_snapshot_source": "ibkr_probe_cache",
                "broker_snapshot_age_s": 6.4,
                "broker_snapshot_state": "warm",
                "today_actual_fills": 12,
                "today_realized_pnl": 125.5,
                "total_unrealized_pnl": 42.25,
                "open_position_count": 3,
                "server_ts": 1778119427.0,
            },
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        payload = r.json()
        assert payload["summary"]["live_broker_probe_mode"] == "cached_diagnostics"
        assert payload["summary"]["broker_snapshot_source"] == "ibkr_probe_cache"
        assert payload["summary"]["broker_snapshot_age_s"] == 6.4
        assert payload["summary"]["broker_snapshot_state"] == "warm"
        assert payload["summary"]["broker_probe_skipped"] is True
        assert payload["summary"]["broker_refresh_probe_failed"] is False
        live_broker = payload["live_broker_state"]
        assert live_broker["ready"] is True
        assert live_broker["today_actual_fills"] == 12
        assert live_broker["open_position_count"] == 3

    def test_bot_fleet_live_broker_probe_opt_in_embeds_live_state(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_live_broker_state_payload",
            lambda: {
                "ready": True,
                "source": "live_broker_rest",
                "probe_skipped": False,
                "broker_snapshot_source": "live_broker_rest",
                "broker_snapshot_age_s": 0.0,
                "broker_snapshot_state": "fresh",
                "today_actual_fills": 12,
                "today_realized_pnl": 125.5,
                "total_unrealized_pnl": 42.25,
                "open_position_count": 3,
                "server_ts": 1778119427.0,
            },
        )

        r = app_client.get("/api/bot-fleet?live_broker_probe=true")

        assert r.status_code == 200
        payload = r.json()
        assert payload["summary"]["live_broker_probe_mode"] == "live"
        assert payload["summary"]["broker_snapshot_source"] == "live_broker_rest"
        assert payload["summary"]["broker_snapshot_age_s"] == 0.0
        assert payload["summary"]["broker_snapshot_state"] == "fresh"
        assert payload["summary"]["broker_probe_skipped"] is False
        assert payload["live_broker_state"]["source"] == "live_broker_rest"
        assert payload["live_broker_state"]["today_actual_fills"] == 12
        assert payload["live_broker_state"]["open_position_count"] == 3

    def test_bot_fleet_live_probe_falls_back_to_last_good_after_ibkr_timeout(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_live_broker_state_payload",
            lambda: {
                "source": "live_broker_rest",
                "broker_snapshot_source": "live_broker_rest",
                "broker_snapshot_state": "fresh",
                "today_actual_fills": 0,
                "today_realized_pnl": 0.0,
                "open_position_count": 0,
                "ibkr": {
                    "ready": False,
                    "error": "ibkr_probe_failed:TimeoutError: TimeoutError()",
                },
            },
        )
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "source": "cached_live_broker_state_for_diagnostics",
                "probe_skipped": True,
                "broker_snapshot_source": "ibkr_probe_cache",
                "broker_snapshot_state": "persisted",
                "today_actual_fills": 17,
                "today_realized_pnl": -321.25,
                "open_position_count": 3,
                "ibkr": {"ready": True},
            },
        )

        r = app_client.get("/api/bot-fleet?live_broker_probe=true")

        assert r.status_code == 200
        payload = r.json()
        summary = payload["summary"]
        live_broker = payload["live_broker_state"]
        assert summary["broker_today_actual_fills"] == 17
        assert summary["broker_open_position_count"] == 3
        assert summary["broker_today_realized_pnl"] == -321.25
        assert summary["broker_refresh_probe_failed"] is True
        assert summary["broker_refresh_probe_error"].startswith("ibkr_probe_failed:TimeoutError")
        assert summary["broker_refresh_probe_source"] == "live_broker_rest"
        assert live_broker["broker_snapshot_state"] == "persisted"
        assert live_broker["refresh_probe_failed"] is True
        assert live_broker["refresh_probe_error"].startswith("ibkr_probe_failed:TimeoutError")

    def test_bot_fleet_live_probe_exception_uses_cached_broker_truth(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        def fail_live_probe():
            raise TimeoutError("ibkr socket timed out")

        monkeypatch.setattr(mod, "_live_broker_state_payload", fail_live_probe)
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "source": "cached_live_broker_state_for_diagnostics",
                "probe_skipped": True,
                "broker_snapshot_source": "ibkr_probe_cache",
                "broker_snapshot_state": "warm",
                "today_actual_fills": 22,
                "today_realized_pnl": 44.5,
                "open_position_count": 4,
                "ibkr": {"ready": True},
            },
        )

        r = app_client.get("/api/bot-fleet?live_broker_probe=true")

        assert r.status_code == 200
        payload = r.json()
        assert payload["summary"]["broker_today_actual_fills"] == 22
        assert payload["summary"]["broker_open_position_count"] == 4
        assert payload["summary"]["broker_refresh_probe_failed"] is True
        assert payload["summary"]["broker_refresh_probe_source"] == "bot_fleet_live_probe_exception"
        assert payload["live_broker_state"]["refresh_probe_failed"] is True
        assert payload["live_broker_state"]["refresh_probe_source"] == "bot_fleet_live_probe_exception"

    def test_bot_fleet_exposes_close_history_windows_top_level(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        close_history = {
            "source": "trade_close_ledger",
            "default_window": "mtd",
            "windows": {
                "wtd": {
                    "label": "WTD",
                    "realized_pnl": 30123.45,
                    "closed_outcome_count": 280,
                    "evaluated_outcome_count": 278,
                    "win_rate": 0.5216,
                    "since": "2026-05-04T00:00:00+00:00",
                    "until": "2026-05-09T03:00:00+00:00",
                    "source": "trade_close_ledger",
                },
                "mtd": {
                    "label": "MTD",
                    "realized_pnl": 32579.18,
                    "closed_outcome_count": 320,
                    "evaluated_outcome_count": 318,
                    "winning_outcomes": 165,
                    "losing_outcomes": 153,
                    "win_rate": 0.5181,
                    "since": "2026-05-01T00:00:00+00:00",
                    "until": "2026-05-09T03:00:00+00:00",
                    "source": "trade_close_ledger",
                    "recent_outcomes": [
                        {
                            "ts": "2026-05-09T02:46:48+00:00",
                            "bot_id": "mnq_anchor_sweep",
                            "symbol": "MNQ1",
                            "realized_pnl": -18.0,
                        },
                    ],
                },
                "all": {
                    "label": "All",
                    "realized_pnl": 32901.18,
                    "closed_outcome_count": 340,
                    "evaluated_outcome_count": 335,
                    "win_rate": 0.5224,
                    "since": None,
                    "until": "2026-05-09T03:00:00+00:00",
                    "source": "trade_close_ledger",
                },
            },
        }
        monkeypatch.setattr(
            mod,
            "_live_broker_state_payload",
            lambda: {
                "ready": True,
                "today_actual_fills": 0,
                "today_realized_pnl": 41.3,
                "total_unrealized_pnl": -40.13,
                "open_position_count": 0,
                "close_history": close_history,
                "server_ts": 1778119427.0,
            },
        )

        r = app_client.get("/api/bot-fleet?live_broker_probe=true")

        assert r.status_code == 200
        payload = r.json()
        assert payload["default_close_history_window"] == "mtd"
        assert payload["close_history"]["default_label"] == "MTD"
        assert payload["live_broker_state"]["close_history"]["default_label"] == "MTD"
        assert payload["close_history"]["windows"]["mtd"]["realized_pnl"] == 32579.18
        assert payload["close_history"]["windows"]["mtd"]["count"] == 320
        assert payload["history_window_pnl"]["wtd"]["pnl"] == 30123.45
        assert payload["history_window_pnl"]["wtd"]["count"] == 280
        assert payload["history_window_pnl"]["mtd"]["closed_outcome_count"] == 320
        assert payload["history_window_pnl"]["mtd"]["count"] == 320
        assert payload["history_window_pnl"]["mtd"]["win_rate"] == 0.5181
        assert payload["history_window_pnl"]["all"]["pnl"] == 32901.18
        assert payload["history_window_pnl"]["all"]["count"] == 340
        assert payload["close_history_window"]["window"] == "mtd"
        assert payload["close_history_window"]["realized_pnl"] == 32579.18
        assert payload["close_history_window"]["closed_outcome_count"] == 320
        assert payload["close_history_window"]["count"] == 320
        assert payload["close_history_window"]["win_rate"] == 0.5181
        assert payload["close_history_row_count"] == 1
        assert payload["close_history_rows"][0]["bot_id"] == "mnq_anchor_sweep"
        assert payload["summary"]["close_history_window"] == "mtd"
        assert payload["summary"]["close_history_label"] == "MTD"
        assert payload["summary"]["close_history_realized_pnl"] == 32579.18
        assert payload["summary"]["close_history_closed_outcome_count"] == 320
        assert payload["summary"]["close_history_win_rate"] == 0.5181

    def test_close_history_windows_cap_rows_without_changing_totals(self):
        from datetime import UTC, datetime

        import eta_engine.deploy.scripts.dashboard_api as mod

        now = datetime.now(UTC)
        rows = [
            {
                "ts": now.isoformat(),
                "bot_id": f"mnq_bot_{idx}",
                "symbol": "MNQ1",
                "realized_pnl": 10.0 if idx % 2 else -5.0,
            }
            for idx in range(35)
        ]

        history = mod._close_history_windows(rows, now=now)
        mtd = history["windows"]["mtd"]

        assert mtd["count"] == 35
        assert mtd["closed_outcome_count"] == 35
        assert len(mtd["recent_outcomes"]) == mod._DASHBOARD_CLOSE_HISTORY_RECENT_ROW_LIMIT

    def test_close_history_pnl_map_aggregates_full_window_not_capped(self):
        from datetime import UTC, datetime

        import eta_engine.deploy.scripts.dashboard_api as mod

        now = datetime.now(UTC)
        rows = [
            {
                "ts": now.isoformat(),
                "bot_id": "mnq_futures_sage",
                "symbol": "MNQ1",
                "realized_pnl": -25.0,
            }
            for _idx in range(24)
        ]
        rows.extend(
            [
                {
                    "ts": now.isoformat(),
                    "bot_id": "ng_sweep_reclaim",
                    "symbol": "NG1",
                    "realized_pnl": 100.0,
                },
                {
                    "ts": now.isoformat(),
                    "bot_id": "ng_sweep_reclaim",
                    "symbol": "NG1",
                    "realized_pnl": 75.0,
                },
            ],
        )

        history = mod._close_history_windows(rows, now=now)
        today = history["windows"]["today"]

        assert len(today["recent_outcomes"]) == mod._DASHBOARD_CLOSE_HISTORY_RECENT_ROW_LIMIT
        assert today["closed_outcome_count"] == 26
        assert today["pnl_map"]["top_losers"][0]["bot_id"] == "mnq_futures_sage"
        assert today["pnl_map"]["top_losers"][0]["closes"] == 24
        assert today["pnl_map"]["top_losers"][0]["realized_pnl"] == -600.0
        assert today["pnl_map"]["top_winners"][0]["bot_id"] == "ng_sweep_reclaim"
        assert today["pnl_map"]["top_winners"][0]["realized_pnl"] == 175.0

    def test_portfolio_summary_exposes_daily_pnl_map(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        portfolio = mod._portfolio_summary_payload(
            [],
            {},
            close_history={
                "source": "trade_close_ledger",
                "windows": {
                    "today": {
                        "label": "Today",
                        "source": "trade_close_ledger",
                        "closed_outcome_count": 2,
                        "realized_pnl": -425.0,
                        "pnl_map": {
                            "top_winners": [
                                {"bot_id": "ng_sweep_reclaim", "symbol": "NG1", "closes": 1, "realized_pnl": 175.0},
                            ],
                            "top_losers": [
                                {"bot_id": "mnq_futures_sage", "symbol": "MNQ1", "closes": 1, "realized_pnl": -600.0},
                            ],
                        },
                    },
                },
            },
        )

        assert portfolio["pnl_map"]["window"] == "today"
        assert portfolio["pnl_map"]["closed_outcome_count"] == 2
        assert portfolio["pnl_map"]["realized_pnl"] == -425.0
        assert portfolio["pnl_map"]["top_winners"][0]["bot_id"] == "ng_sweep_reclaim"
        assert portfolio["pnl_map"]["top_losers"][0]["bot_id"] == "mnq_futures_sage"

    def test_dashboard_close_history_endpoint_limits_rows(self, app_client, monkeypatch):
        from datetime import UTC, datetime

        import eta_engine.deploy.scripts.dashboard_api as mod

        now = datetime.now(UTC)
        rows = [
            {
                "ts": now.isoformat(),
                "bot_id": f"mnq_bot_{idx}",
                "symbol": "MNQ1",
                "realized_pnl": 10.0 if idx % 2 else -5.0,
            }
            for idx in range(24)
        ]
        monkeypatch.setattr(mod, "_recent_trade_closes", lambda limit=5000: rows)

        r = app_client.get("/api/dashboard/close-history?window=mtd&limit=3")

        assert r.status_code == 200
        payload = r.json()
        assert payload["window"] == "mtd"
        assert payload["close_history_window"]["count"] == 24
        assert payload["close_history_row_count"] == 3
        assert len(payload["close_history_rows"]) == 3

    def test_dashboard_live_summary_uses_cached_broker_without_live_probe(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        from datetime import UTC, datetime

        import eta_engine.deploy.scripts.dashboard_api as mod

        def fail_live_probe():
            raise AssertionError("live summary must not open the broker probe")

        monkeypatch.setattr(mod, "_live_broker_state_payload", fail_live_probe)
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_diagnostics",
            lambda: {
                "ready": True,
                "source": "cached_live_broker_state_for_diagnostics",
                "probe_skipped": True,
                "broker_snapshot_source": "ibkr_probe_cache",
                "broker_snapshot_age_s": 7.5,
                "today_actual_fills": 2,
                "today_realized_pnl": 88.0,
                "total_unrealized_pnl": -5.0,
                "open_position_count": 0,
                "focus_policy": mod._dashboard_focus_policy_payload(),
                "close_history": mod._close_history_windows([], now=datetime.now(UTC)),
            },
        )
        monkeypatch.setattr(
            mod,
            "_command_center_watchdog_payload",
            lambda server_ts=None: {
                "status": "healthy",
                "issue_status": "healthy",
                "display_summary": "Command Center watchdog is healthy.",
                "fresh": True,
                "age_s": 0,
                "healthy": True,
                "checked_at": "2026-05-17T00:00:00+00:00",
                "operator_next_step": "none",
                "operator_next_reason": "healthy",
                "operator_next_command": None,
                "failure_class": "healthy",
                "operator_contract_state": "healthy",
                "recommended_action": "none",
                "instruction": "",
                "repair_required": False,
                "operator_next_requires_elevation": False,
                "requires_elevation": False,
                "watchdog_registered": True,
                "watchdog_state": "Ready",
                "can_launch_from_desktop": True,
                "launch_context": "ready",
                "dashboard_task_contract_status": {"status": "access_denied"},
                "local_contract_status": {"status": "healthy"},
            },
        )
        (tmp_path / "state" / "dashboard_proxy_watchdog_heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "component": "dashboard_proxy_watchdog",
                    "decision": {
                        "checked_at": datetime.now(UTC).isoformat(),
                        "action": "noop",
                        "task_name": "ETA-Proxy-8421",
                        "probe": {
                            "healthy": True,
                            "url": "http://127.0.0.1:8421/",
                            "status_code": 200,
                            "reason": "ok",
                            "elapsed_ms": 15,
                            "body_len": 77000,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/dashboard/live-summary")

        assert r.status_code == 200
        payload = r.json()
        assert payload["fast_summary"] is True
        assert payload["summary"]["dashboard_payload_tier"] == "live_summary"
        assert payload["summary"]["live_broker_probe_mode"] == "cached_diagnostics"
        assert payload["summary"]["command_center_watchdog_fresh"] is True
        assert payload["summary"]["command_center_watchdog_age_s"] == 0
        assert payload["summary"]["command_center_watchdog_healthy"] is True
        assert payload["summary"]["command_center_watchdog_checked_at"] == "2026-05-17T00:00:00+00:00"
        assert payload["summary"]["command_center_watchdog_next_step"] == "none"
        assert payload["summary"]["command_center_watchdog_next_reason"] == "healthy"
        assert payload["summary"]["command_center_watchdog_next_command"] == ""
        assert payload["summary"]["command_center_watchdog_failure_class"] == "healthy"
        assert payload["summary"]["command_center_watchdog_operator_contract_state"] == "healthy"
        assert payload["summary"]["command_center_watchdog_recommended_action"] == "none"
        assert payload["summary"]["command_center_watchdog_primary_blocker"] == "healthy"
        assert payload["summary"]["command_center_watchdog_instruction"] == ""
        assert payload["summary"]["command_center_watchdog_action_count"] == 0
        assert payload["summary"]["command_center_watchdog_follow_up_count"] == 0
        assert payload["summary"]["command_center_watchdog_dashboard_task_missing_task_names"] == []
        assert payload["summary"]["command_center_watchdog_repair_required"] is False
        assert payload["summary"]["command_center_watchdog_requires_elevation"] is False
        assert payload["summary"]["command_center_watchdog_watchdog_registered"] is True
        assert payload["summary"]["command_center_watchdog_watchdog_state"] == "Ready"
        assert payload["summary"]["command_center_watchdog_can_launch_from_desktop"] is True
        assert payload["summary"]["command_center_watchdog_launch_context"] == "ready"
        assert payload["summary"]["command_center_watchdog_dashboard_task_contract_status"] == "access_denied"
        assert payload["summary"]["command_center_watchdog_local_contract_status"] == "healthy"
        assert payload["summary"]["dashboard_proxy_watchdog_status"] == "ok"
        assert payload["summary"]["dashboard_proxy_watchdog_detail"] == "noop: ok"
        assert payload["summary"]["dashboard_proxy_watchdog_fresh"] is True
        assert payload["summary"]["dashboard_proxy_watchdog_action"] == "noop"
        assert payload["summary"]["dashboard_proxy_watchdog_task_name"] == "ETA-Proxy-8421"
        assert payload["summary"]["dashboard_proxy_watchdog_probe_healthy"] is True
        assert payload["summary"]["dashboard_proxy_watchdog_probe_reason"] == "ok"
        assert payload["summary"]["dashboard_proxy_watchdog_status_code"] == 200
        assert payload["summary"]["dashboard_proxy_watchdog_elapsed_ms"] == 15
        assert payload["summary"]["dashboard_proxy_watchdog_heartbeat_age_s"] >= 0
        assert payload["summary"]["dashboard_proxy_watchdog_checked_age_s"] >= 0
        assert payload["summary"]["dashboard_proxy_watchdog_checked_at"]
        assert payload["summary"]["dashboard_proxy_watchdog_heartbeat_ts"]
        assert payload["live_broker_state"]["probe_skipped"] is True
        assert payload["live_broker_state"]["broker_snapshot_age_s"] == 7.5

    def test_cached_diagnostics_uses_persisted_ibkr_mtd_without_probe_cache(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        from datetime import UTC, datetime

        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(mod, "_recent_trade_closes", lambda limit=5000: [])
        with mod._IBKR_PROBE_LOCK:
            mod._IBKR_PROBE_CACHE.clear()
        tracker_dir = tmp_path / "state" / "broker_mtd"
        tracker_dir.mkdir(parents=True)
        month_key = datetime.now(UTC).strftime("%Y-%m")
        (tracker_dir / "ibkr_net_liq_month_tracker.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "updated_at": "2026-05-14T16:20:18+00:00",
                    "accounts": {
                        "DU123": {
                            month_key: {
                                "month": month_key,
                                "baseline_net_liquidation": 1_000_000.0,
                                "baseline_origin": "manual_override",
                                "baseline_set_at": "2026-05-01T00:00:00+00:00",
                                "last_net_liquidation": 1_024_387.0,
                                "last_seen_at": "2026-05-14T16:20:18+00:00",
                            },
                        },
                    },
                },
            ),
            encoding="utf-8",
        )

        live = mod._cached_live_broker_state_for_diagnostics()

        assert live["probe_skipped"] is True
        assert live["ready"] is False
        assert live["broker_mtd_pnl"] == 24387.0
        assert live["broker_mtd_return_pct"] == 2.44
        assert live["reporting_timezone"] == mod.DASHBOARD_LOCAL_TIME_ZONE_NAME
        assert live["today_day_boundary"] == "local_midnight"
        assert live["sources"]["broker_mtd_pnl"] == "ibkr_net_liquidation_month_manual_override"
        assert live["ibkr"]["account_mtd_baseline_set_at"] == "2026-05-01T00:00:00+00:00"

    def test_cached_diagnostics_restores_persisted_ibkr_probe_after_restart(
        self,
        tmp_path,
        monkeypatch,
    ):
        import time

        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setattr(mod, "_recent_trade_closes", lambda limit=5000: [])
        with mod._IBKR_PROBE_LOCK:
            mod._IBKR_PROBE_CACHE.clear()
        cache_path = tmp_path / "state" / "broker_cache" / "ibkr_probe_cache.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "snapshot": {
                        "ready": True,
                        "today_executions": 17,
                        "today_realized_pnl": -321.25,
                        "unrealized_pnl": 42.5,
                        "open_position_count": 3,
                        "account_mtd_pnl": 22209.0,
                        "account_mtd_return_pct": 2.22,
                        "account_mtd_source": "ibkr_probe_cache_persisted",
                    },
                },
            ),
            encoding="utf-8",
        )

        live = mod._cached_live_broker_state_for_diagnostics()

        assert live["broker_snapshot_state"] == "persisted"
        assert live["today_actual_fills"] == 17
        assert live["today_realized_pnl"] == -321.25
        assert live["total_unrealized_pnl"] == 42.5
        assert live["open_position_count"] == 3
        assert live["broker_mtd_pnl"] == 22209.0

    def test_cached_diagnostics_keeps_stale_ibkr_probe_visible_but_not_ready(
        self,
        tmp_path,
        monkeypatch,
    ):
        import time

        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setattr(mod, "_recent_trade_closes", lambda limit=5000: [])
        with mod._IBKR_PROBE_LOCK:
            mod._IBKR_PROBE_CACHE.clear()
        stale_ts = time.time() - mod._IBKR_PROBE_DISK_CACHE_MAX_AGE_S - 30
        cache_path = tmp_path / "state" / "broker_cache" / "ibkr_probe_cache.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text(
            json.dumps(
                {
                    "ts": stale_ts,
                    "snapshot": {
                        "ready": True,
                        "today_executions": 115,
                        "today_realized_pnl": -947.85,
                        "unrealized_pnl": 108.85,
                        "open_position_count": 3,
                        "account_mtd_pnl": 22297.0,
                        "account_mtd_return_pct": 2.23,
                        "account_mtd_source": "ibkr_net_liquidation_month_manual_override",
                    },
                },
            ),
            encoding="utf-8",
        )

        live = mod._cached_live_broker_state_for_diagnostics()
        summary = mod._broker_summary_fields(live)

        assert live["ready"] is False
        assert live["broker_snapshot_state"] == "stale_persisted"
        assert live["today_actual_fills"] == 115
        assert live["today_realized_pnl"] == -947.85
        assert live["total_unrealized_pnl"] == 108.85
        assert live["open_position_count"] == 3
        assert live["broker_mtd_pnl"] == 22297.0
        assert summary["broker_ready"] is False
        assert summary["broker_today_realized_pnl"] == -947.85
        assert summary["broker_total_unrealized_pnl"] == 108.85
        assert summary["broker_snapshot_state"] == "stale_persisted"

    def test_cached_diagnostics_carries_today_close_ledger_counts(self, monkeypatch):
        from datetime import UTC, datetime

        import eta_engine.deploy.scripts.dashboard_api as mod

        now = datetime.now(UTC)
        rows = [
            {"ts": now.isoformat(), "bot_id": "mnq_anchor_sweep", "symbol": "MNQ1", "realized_pnl": 120.0},
            {"ts": now.isoformat(), "bot_id": "mnq_anchor_sweep", "symbol": "MNQ1", "realized_pnl": -40.0},
            {"ts": now.isoformat(), "bot_id": "volume_profile_mnq", "symbol": "MNQ1", "realized_pnl": 20.0},
        ]
        monkeypatch.setattr(
            mod,
            "_cached_live_broker_state_for_gateway_reconcile",
            lambda: {
                "ibkr": {
                    "ready": True,
                    "today_executions": 7,
                    "today_realized_pnl": 100.0,
                    "open_position_count": 2,
                },
                "ibkr_cache_state": "warm",
                "ibkr_cache_age_s": 3.0,
            },
        )
        monkeypatch.setattr(mod, "_ibkr_cached_mtd_tracker_snapshot", lambda *args, **kwargs: {})
        monkeypatch.setattr(mod, "_recent_trade_closes", lambda limit=5000: rows)

        live = mod._cached_live_broker_state_for_diagnostics()
        summary = mod._broker_summary_fields(live)

        assert live["closed_outcome_count_today"] == 3
        assert live["evaluated_outcome_count_today"] == 3
        assert live["win_rate_today"] == 0.6667
        assert live["win_rate_source"] == "trade_close_ledger_today"
        assert summary["broker_ready"] is True
        assert summary["broker_probe_skipped"] is True
        assert summary["broker_refresh_probe_failed"] is False
        assert summary["broker_snapshot_source"] == "ibkr_probe_cache"
        assert summary["broker_snapshot_state"] == "warm"
        assert summary["broker_snapshot_age_s"] == 3.0
        assert summary["broker_closed_outcomes_today"] == 3
        assert summary["broker_win_rate_today"] == 0.6667

    def test_cached_diagnostics_prefers_last_good_disk_cache_after_probe_failure(
        self,
        tmp_path,
        monkeypatch,
    ):
        import time

        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setattr(mod, "_recent_trade_closes", lambda limit=5000: [])
        cache_path = tmp_path / "state" / "broker_cache" / "ibkr_probe_cache.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "snapshot": {
                        "ready": True,
                        "today_executions": 17,
                        "today_realized_pnl": -321.25,
                        "unrealized_pnl": 42.5,
                        "open_position_count": 3,
                        "account_mtd_pnl": 22209.0,
                        "account_mtd_source": "ibkr_probe_cache_persisted",
                    },
                },
            ),
            encoding="utf-8",
        )
        with mod._IBKR_PROBE_LOCK:
            mod._IBKR_PROBE_CACHE["snapshot"] = {
                "ready": False,
                "error": "ibkr_probe_failed:TimeoutError",
                "today_executions": 0,
                "today_realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "open_position_count": 0,
            }
            mod._IBKR_PROBE_CACHE["ts"] = time.time()

        live = mod._cached_live_broker_state_for_diagnostics()

        assert live["broker_snapshot_state"] == "persisted"
        assert live["today_actual_fills"] == 17
        assert live["today_realized_pnl"] == -321.25
        assert live["open_position_count"] == 3
        assert live["ibkr"]["ready"] is True

    def test_derive_ibkr_today_realized_pnl_prefers_futures_bucket(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        assert mod._derive_ibkr_today_realized_pnl({"futures_pnl": 10133.83, "unrealized_pnl": 0.0}) == 10133.83
        assert mod._derive_ibkr_today_realized_pnl({"futures_pnl": 10133.83, "unrealized_pnl": 133.83}) == 10000.0
        assert mod._derive_ibkr_today_realized_pnl({"account_summary_realized_pnl": 321.98}) == 321.98

    def test_extract_ibkr_mtd_performance_from_account_bucket(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        payload = {
            "DU1234567": {
                "MTD": {
                    "nav": [101_000.0, 103_450.25],
                    "cps": [0.004, 0.02426],
                    "dates": ["2026-05-01", "2026-05-12"],
                    "startNAV": {"date": "2026-04-30", "val": 101_000.0},
                }
            }
        }

        extracted = mod._ibkr_extract_mtd_performance(payload, account_id="DU1234567")

        assert extracted["ready"] is True
        assert extracted["account_id"] == "DU1234567"
        assert extracted["mtd_pnl"] == 2450.25
        assert extracted["mtd_return_pct"] == 2.43
        assert extracted["source"] == "ibkr_client_portal_pa_performance_mtd"

    def test_extract_ibkr_mtd_performance_from_series_payload(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        payload = {
            "nav": {
                "data": [
                    {
                        "id": "DU7654321",
                        "navs": [50_000.0, 50_210.0, 50_800.0],
                        "dates": ["2026-05-01", "2026-05-02", "2026-05-12"],
                    }
                ],
                "startNAV": {"date": "2026-04-30", "val": 50_000.0},
            },
            "cps": {
                "data": [
                    {
                        "id": "DU7654321",
                        "returns": [0.0, 0.0042, 0.016],
                    }
                ]
            },
        }

        extracted = mod._ibkr_extract_mtd_performance(payload, account_id="DU7654321")

        assert extracted["ready"] is True
        assert extracted["account_id"] == "DU7654321"
        assert extracted["mtd_pnl"] == 800.0
        assert extracted["mtd_return_pct"] == 1.6

    def test_ibkr_net_liq_mtd_tracker_persists_month_baseline(self, tmp_path, monkeypatch):
        import importlib

        monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path / "state"))

        import eta_engine.deploy.scripts.dashboard_api as mod

        importlib.reload(mod)
        first = mod._ibkr_net_liquidation_mtd_snapshot(
            account_id="DU7770001",
            net_liquidation=100_000.0,
            checked_at="2026-05-12T14:00:00+00:00",
            now_utc=datetime(2026, 5, 12, 14, 0, tzinfo=UTC),
        )
        second = mod._ibkr_net_liquidation_mtd_snapshot(
            account_id="DU7770001",
            net_liquidation=101_250.5,
            checked_at="2026-05-12T15:00:00+00:00",
            now_utc=datetime(2026, 5, 12, 15, 0, tzinfo=UTC),
        )

        assert first["ready"] is True
        assert first["source"] == "ibkr_net_liquidation_month_tracker_bootstrap"
        assert first["mtd_pnl"] == 0.0
        assert first["start_nav"] == 100000.0
        assert first["baseline_initialized"] is True

        assert second["ready"] is True
        assert second["source"] == "ibkr_net_liquidation_month_tracker"
        assert second["mtd_pnl"] == 1250.5
        assert second["start_nav"] == 100000.0
        assert second["end_nav"] == 101250.5
        assert second["mtd_return_pct"] == 1.25
        assert second["baseline_initialized"] is False

        tracker_path = tmp_path / "state" / "broker_mtd" / "ibkr_net_liq_month_tracker.json"
        payload = json.loads(tracker_path.read_text(encoding="utf-8"))
        month_state = payload["accounts"]["DU7770001"]["2026-05"]
        assert month_state["baseline_net_liquidation"] == 100000.0
        assert month_state["last_net_liquidation"] == 101250.5
        assert month_state["last_seen_at"] == "2026-05-12T15:00:00+00:00"

    def test_ibkr_net_liq_mtd_tracker_honors_manual_month_override(self, tmp_path, monkeypatch):
        import importlib

        monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path / "state"))
        override_path = tmp_path / "state" / "broker_mtd" / "ibkr_net_liq_month_overrides.json"
        override_path.parent.mkdir(parents=True, exist_ok=True)
        override_path.write_text(
            "\ufeff"
            + json.dumps(
                {
                    "schema_version": 1,
                    "accounts": {
                        "DU7770001": {
                            "2026-05": {
                                "baseline_net_liquidation": 100000.0,
                                "baseline_set_at": "2026-05-01T00:00:00+00:00",
                                "source": "manual_override",
                                "note": "operator seeded May 1 paper baseline",
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        import eta_engine.deploy.scripts.dashboard_api as mod

        importlib.reload(mod)
        snapshot = mod._ibkr_net_liquidation_mtd_snapshot(
            account_id="DU7770001",
            net_liquidation=101_250.5,
            checked_at="2026-05-12T15:00:00+00:00",
            now_utc=datetime(2026, 5, 12, 15, 0, tzinfo=UTC),
        )

        assert snapshot["ready"] is True
        assert snapshot["source"] == "ibkr_net_liquidation_month_manual_override"
        assert snapshot["mtd_pnl"] == 1250.5
        assert snapshot["start_nav"] == 100000.0
        assert snapshot["baseline_set_at"] == "2026-05-01T00:00:00+00:00"
        assert snapshot["baseline_origin"] == "manual_override"
        assert snapshot["baseline_note"] == "operator seeded May 1 paper baseline"
        assert snapshot["baseline_initialized"] is False

        tracker_path = tmp_path / "state" / "broker_mtd" / "ibkr_net_liq_month_tracker.json"
        payload = json.loads(tracker_path.read_text(encoding="utf-8"))
        month_state = payload["accounts"]["DU7770001"]["2026-05"]
        assert month_state["baseline_origin"] == "manual_override"
        assert month_state["baseline_set_at"] == "2026-05-01T00:00:00+00:00"
        assert month_state["baseline_note"] == "operator seeded May 1 paper baseline"

    def test_closed_outcomes_from_alpaca_filled_order_pairs(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        outcomes = mod._closed_outcomes_from_filled_orders(
            [
                {
                    "symbol": "BTC/USD",
                    "side": "buy",
                    "filled_qty": "1.0",
                    "filled_avg_price": "100.00",
                    "filled_at": "2026-05-07T14:00:00Z",
                    "status": "filled",
                },
                {
                    "symbol": "BTC/USD",
                    "side": "sell",
                    "filled_qty": "0.5",
                    "filled_avg_price": "110.00",
                    "filled_at": "2026-05-07T15:00:00Z",
                    "status": "filled",
                },
                {
                    "symbol": "BTC/USD",
                    "side": "sell",
                    "filled_qty": "0.5",
                    "filled_avg_price": "90.00",
                    "filled_at": "2026-05-07T16:00:00Z",
                    "status": "filled",
                },
            ]
        )

        assert outcomes["closed_outcome_count"] == 2
        assert outcomes["evaluated_outcome_count"] == 2
        assert outcomes["winning_outcomes"] == 1
        assert outcomes["losing_outcomes"] == 1
        assert outcomes["win_rate"] == 0.5
        assert [row["realized_pnl"] for row in outcomes["recent_outcomes"]] == [-5.0, 5.0]

    def test_closed_outcomes_prefer_broker_fill_ts_when_present(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        outcomes = mod._closed_outcomes_from_filled_orders(
            [
                {
                    "symbol": "ETH/USD",
                    "side": "buy",
                    "filled_qty": "1.0",
                    "filled_avg_price": "200.00",
                    "broker_fill_ts": "2026-05-07T14:00:00Z",
                    "ts": "2026-05-07T14:00:09Z",
                    "status": "filled",
                },
                {
                    "symbol": "ETH/USD",
                    "side": "sell",
                    "filled_qty": "1.0",
                    "filled_avg_price": "210.00",
                    "broker_fill_ts": "2026-05-07T15:00:00Z",
                    "ts": "2026-05-07T15:00:08Z",
                    "status": "filled",
                },
            ]
        )

        assert outcomes["closed_outcome_count"] == 1
        assert outcomes["evaluated_outcome_count"] == 1
        assert outcomes["winning_outcomes"] == 1
        assert outcomes["recent_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_closed_outcomes_fall_back_to_execution_time_when_fill_ts_missing(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        outcomes = mod._closed_outcomes_from_filled_orders(
            [
                {
                    "symbol": "SOL/USD",
                    "side": "buy",
                    "filled_qty": "1.0",
                    "filled_avg_price": "120.00",
                    "execution_time": "2026-05-07T14:00:00Z",
                    "ts": "2026-05-07T14:00:09Z",
                    "status": "filled",
                },
                {
                    "symbol": "SOL/USD",
                    "side": "sell",
                    "filled_qty": "1.0",
                    "filled_avg_price": "125.00",
                    "execution_time": "2026-05-07T15:00:00Z",
                    "ts": "2026-05-07T15:00:08Z",
                    "status": "filled",
                },
            ]
        )

        assert outcomes["closed_outcome_count"] == 1
        assert outcomes["evaluated_outcome_count"] == 1
        assert outcomes["winning_outcomes"] == 1
        assert outcomes["recent_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_closed_outcomes_fall_back_to_executed_at_when_fill_ts_missing(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        outcomes = mod._closed_outcomes_from_filled_orders(
            [
                {
                    "symbol": "XRP/USD",
                    "side": "buy",
                    "filled_qty": "1.0",
                    "filled_avg_price": "0.50",
                    "executed_at": "2026-05-07T14:00:00Z",
                    "ts": "2026-05-07T14:00:09Z",
                    "status": "filled",
                },
                {
                    "symbol": "XRP/USD",
                    "side": "sell",
                    "filled_qty": "1.0",
                    "filled_avg_price": "0.55",
                    "executed_at": "2026-05-07T15:00:00Z",
                    "ts": "2026-05-07T15:00:08Z",
                    "status": "filled",
                },
            ]
        )

        assert outcomes["closed_outcome_count"] == 1
        assert outcomes["evaluated_outcome_count"] == 1
        assert outcomes["winning_outcomes"] == 1
        assert outcomes["recent_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_closed_outcomes_fall_back_to_last_fill_time_when_fill_ts_missing(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        outcomes = mod._closed_outcomes_from_filled_orders(
            [
                {
                    "symbol": "ADA/USD",
                    "side": "buy",
                    "filled_qty": "1.0",
                    "filled_avg_price": "0.40",
                    "lastFillTime": "2026-05-07T14:00:00Z",
                    "ts": "2026-05-07T14:00:09Z",
                    "status": "filled",
                },
                {
                    "symbol": "ADA/USD",
                    "side": "sell",
                    "filled_qty": "1.0",
                    "filled_avg_price": "0.45",
                    "lastFillTime": "2026-05-07T15:00:00Z",
                    "ts": "2026-05-07T15:00:08Z",
                    "status": "filled",
                },
            ]
        )

        assert outcomes["closed_outcome_count"] == 1
        assert outcomes["evaluated_outcome_count"] == 1
        assert outcomes["winning_outcomes"] == 1
        assert outcomes["recent_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_closed_outcomes_fall_back_to_timestamp_when_fill_ts_missing(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        outcomes = mod._closed_outcomes_from_filled_orders(
            [
                {
                    "symbol": "DOGE/USD",
                    "side": "buy",
                    "filled_qty": "1.0",
                    "filled_avg_price": "0.20",
                    "timestamp": "2026-05-07T14:00:00Z",
                    "status": "filled",
                },
                {
                    "symbol": "DOGE/USD",
                    "side": "sell",
                    "filled_qty": "1.0",
                    "filled_avg_price": "0.22",
                    "timestamp": "2026-05-07T15:00:00Z",
                    "status": "filled",
                },
            ]
        )

        assert outcomes["closed_outcome_count"] == 1
        assert outcomes["evaluated_outcome_count"] == 1
        assert outcomes["winning_outcomes"] == 1
        assert outcomes["recent_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_closed_outcomes_fall_back_to_hyphenated_filled_at_when_fill_ts_missing(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        outcomes = mod._closed_outcomes_from_filled_orders(
            [
                {
                    "symbol": "AVAX/USD",
                    "side": "buy",
                    "filled_qty": "1.0",
                    "filled_avg_price": "20.00",
                    "filled-at": "2026-05-07T14:00:00Z",
                    "status": "filled",
                },
                {
                    "symbol": "AVAX/USD",
                    "side": "sell",
                    "filled_qty": "1.0",
                    "filled_avg_price": "22.00",
                    "filled-at": "2026-05-07T15:00:00Z",
                    "status": "filled",
                },
            ]
        )

        assert outcomes["closed_outcome_count"] == 1
        assert outcomes["evaluated_outcome_count"] == 1
        assert outcomes["winning_outcomes"] == 1
        assert outcomes["recent_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_closed_outcomes_fall_back_to_hyphenated_updated_at_when_fill_ts_missing(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        outcomes = mod._closed_outcomes_from_filled_orders(
            [
                {
                    "symbol": "LINK/USD",
                    "side": "buy",
                    "filled_qty": "1.0",
                    "filled_avg_price": "15.00",
                    "updated-at": "2026-05-07T14:00:00Z",
                    "status": "filled",
                },
                {
                    "symbol": "LINK/USD",
                    "side": "sell",
                    "filled_qty": "1.0",
                    "filled_avg_price": "16.00",
                    "updated-at": "2026-05-07T15:00:00Z",
                    "status": "filled",
                },
            ]
        )

        assert outcomes["closed_outcome_count"] == 1
        assert outcomes["evaluated_outcome_count"] == 1
        assert outcomes["winning_outcomes"] == 1
        assert outcomes["recent_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_closed_outcomes_fall_back_to_created_at_when_fill_ts_missing(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        outcomes = mod._closed_outcomes_from_filled_orders(
            [
                {
                    "symbol": "MATIC/USD",
                    "side": "buy",
                    "filled_qty": "1.0",
                    "filled_avg_price": "1.00",
                    "created_at": "2026-05-07T14:00:00Z",
                    "status": "filled",
                },
                {
                    "symbol": "MATIC/USD",
                    "side": "sell",
                    "filled_qty": "1.0",
                    "filled_avg_price": "1.10",
                    "created_at": "2026-05-07T15:00:00Z",
                    "status": "filled",
                },
            ]
        )

        assert outcomes["closed_outcome_count"] == 1
        assert outcomes["evaluated_outcome_count"] == 1
        assert outcomes["winning_outcomes"] == 1
        assert outcomes["recent_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_alpaca_live_state_recent_filled_orders_prefer_broker_fill_ts(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.venues.alpaca import AlpacaConfig

        class _Resp:
            def __init__(self, status_code, payload) -> None:
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        class _Client:
            def __init__(self, *args, **kwargs) -> None:
                _ = args, kwargs

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def get(self, path, params=None):
                _ = params
                if path == "/v2/account":
                    return _Resp(200, {"account_number": "PA123", "equity": "100100", "last_equity": "100000"})
                if path == "/v2/positions":
                    return _Resp(200, [])
                if path == "/v2/orders":
                    return _Resp(
                        200,
                        [
                            {
                                "symbol": "BTC/USD",
                                "side": "buy",
                                "filled_qty": "0.5",
                                "filled_avg_price": "60000.0",
                                "broker_fill_ts": "2026-05-07T14:00:00Z",
                                "filled_at": "2026-05-07T14:00:08Z",
                                "ts": "2026-05-07T14:00:09Z",
                                "client_order_id": "btc-open-1",
                                "status": "filled",
                            },
                            {
                                "symbol": "BTC/USD",
                                "side": "sell",
                                "filled_qty": "0.5",
                                "filled_avg_price": "62000.0",
                                "broker_fill_ts": "2026-05-07T15:00:00Z",
                                "filled_at": "2026-05-07T15:00:08Z",
                                "ts": "2026-05-07T15:00:09Z",
                                "client_order_id": "btc-close-1",
                                "status": "filled",
                            }
                        ],
                    )
                raise AssertionError(f"unexpected path {path}")

        alpaca_config = AlpacaConfig(api_key_id="PK1", api_secret_key="SK1")
        monkeypatch.setattr(
            AlpacaConfig,
            "from_env",
            classmethod(lambda cls: alpaca_config),
        )
        import httpx

        monkeypatch.setattr(httpx, "Client", _Client)

        snapshot = mod._alpaca_live_state_snapshot(today_start_iso="2026-05-07T00:00:00Z")

        assert snapshot["ready"] is True
        assert snapshot["today_filled_orders"] == 2
        assert [row["filled_at"] for row in snapshot["recent_filled_orders"]] == [
            "2026-05-07T14:00:00Z",
            "2026-05-07T15:00:00Z",
        ]
        assert snapshot["recent_closed_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_alpaca_live_state_recent_filled_orders_fall_back_to_created_at(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.venues.alpaca import AlpacaConfig

        class _Resp:
            def __init__(self, status_code, payload) -> None:
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        class _Client:
            def __init__(self, *args, **kwargs) -> None:
                _ = args, kwargs

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def get(self, path, params=None):
                _ = params
                if path == "/v2/account":
                    return _Resp(200, {"account_number": "PA123", "equity": "100100", "last_equity": "100000"})
                if path == "/v2/positions":
                    return _Resp(200, [])
                if path == "/v2/orders":
                    return _Resp(
                        200,
                        [
                            {
                                "symbol": "BTC/USD",
                                "side": "buy",
                                "filled_qty": "0.5",
                                "filled_avg_price": "60000.0",
                                "created_at": "2026-05-07T14:00:00Z",
                                "client_order_id": "btc-open-1",
                                "status": "filled",
                            },
                            {
                                "symbol": "BTC/USD",
                                "side": "sell",
                                "filled_qty": "0.5",
                                "filled_avg_price": "62000.0",
                                "created_at": "2026-05-07T15:00:00Z",
                                "client_order_id": "btc-close-1",
                                "status": "filled",
                            }
                        ],
                    )
                raise AssertionError(f"unexpected path {path}")

        alpaca_config = AlpacaConfig(api_key_id="PK1", api_secret_key="SK1")
        monkeypatch.setattr(
            AlpacaConfig,
            "from_env",
            classmethod(lambda cls: alpaca_config),
        )
        import httpx

        monkeypatch.setattr(httpx, "Client", _Client)

        snapshot = mod._alpaca_live_state_snapshot(today_start_iso="2026-05-07T00:00:00Z")

        assert snapshot["ready"] is True
        assert snapshot["today_filled_orders"] == 2
        assert [row["filled_at"] for row in snapshot["recent_filled_orders"]] == [
            "2026-05-07T14:00:00Z",
            "2026-05-07T15:00:00Z",
        ]
        assert snapshot["recent_closed_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_alpaca_live_state_recent_filled_orders_fall_back_to_hyphenated_filled_at(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.venues.alpaca import AlpacaConfig

        class _Resp:
            def __init__(self, status_code, payload) -> None:
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        class _Client:
            def __init__(self, *args, **kwargs) -> None:
                _ = args, kwargs

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def get(self, path, params=None):
                _ = params
                if path == "/v2/account":
                    return _Resp(200, {"account_number": "PA123", "equity": "100100", "last_equity": "100000"})
                if path == "/v2/positions":
                    return _Resp(200, [])
                if path == "/v2/orders":
                    return _Resp(
                        200,
                        [
                            {
                                "symbol": "BTC/USD",
                                "side": "buy",
                                "filled_qty": "0.5",
                                "filled_avg_price": "60000.0",
                                "filled-at": "2026-05-07T14:00:00Z",
                                "client_order_id": "btc-open-1",
                                "status": "filled",
                            },
                            {
                                "symbol": "BTC/USD",
                                "side": "sell",
                                "filled_qty": "0.5",
                                "filled_avg_price": "62000.0",
                                "filled-at": "2026-05-07T15:00:00Z",
                                "client_order_id": "btc-close-1",
                                "status": "filled",
                            }
                        ],
                    )
                raise AssertionError(f"unexpected path {path}")

        alpaca_config = AlpacaConfig(api_key_id="PK1", api_secret_key="SK1")
        monkeypatch.setattr(
            AlpacaConfig,
            "from_env",
            classmethod(lambda cls: alpaca_config),
        )
        import httpx

        monkeypatch.setattr(httpx, "Client", _Client)

        snapshot = mod._alpaca_live_state_snapshot(today_start_iso="2026-05-07T00:00:00Z")

        assert snapshot["ready"] is True
        assert snapshot["today_filled_orders"] == 2
        assert [row["filled_at"] for row in snapshot["recent_filled_orders"]] == [
            "2026-05-07T14:00:00Z",
            "2026-05-07T15:00:00Z",
        ]
        assert snapshot["recent_closed_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_alpaca_live_state_recent_filled_orders_fall_back_to_hyphenated_updated_at(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.venues.alpaca import AlpacaConfig

        class _Resp:
            def __init__(self, status_code, payload) -> None:
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        class _Client:
            def __init__(self, *args, **kwargs) -> None:
                _ = args, kwargs

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def get(self, path, params=None):
                _ = params
                if path == "/v2/account":
                    return _Resp(200, {"account_number": "PA123", "equity": "100100", "last_equity": "100000"})
                if path == "/v2/positions":
                    return _Resp(200, [])
                if path == "/v2/orders":
                    return _Resp(
                        200,
                        [
                            {
                                "symbol": "BTC/USD",
                                "side": "buy",
                                "filled_qty": "0.5",
                                "filled_avg_price": "60000.0",
                                "updated-at": "2026-05-07T14:00:00Z",
                                "client_order_id": "btc-open-1",
                                "status": "filled",
                            },
                            {
                                "symbol": "BTC/USD",
                                "side": "sell",
                                "filled_qty": "0.5",
                                "filled_avg_price": "62000.0",
                                "updated-at": "2026-05-07T15:00:00Z",
                                "client_order_id": "btc-close-1",
                                "status": "filled",
                            }
                        ],
                    )
                raise AssertionError(f"unexpected path {path}")

        alpaca_config = AlpacaConfig(api_key_id="PK1", api_secret_key="SK1")
        monkeypatch.setattr(
            AlpacaConfig,
            "from_env",
            classmethod(lambda cls: alpaca_config),
        )
        import httpx

        monkeypatch.setattr(httpx, "Client", _Client)

        snapshot = mod._alpaca_live_state_snapshot(today_start_iso="2026-05-07T00:00:00Z")

        assert snapshot["ready"] is True
        assert snapshot["today_filled_orders"] == 2
        assert [row["filled_at"] for row in snapshot["recent_filled_orders"]] == [
            "2026-05-07T14:00:00Z",
            "2026-05-07T15:00:00Z",
        ]
        assert snapshot["recent_closed_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_alpaca_live_state_recent_filled_orders_fall_back_to_timestamp(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.venues.alpaca import AlpacaConfig

        class _Resp:
            def __init__(self, status_code, payload) -> None:
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        class _Client:
            def __init__(self, *args, **kwargs) -> None:
                _ = args, kwargs

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def get(self, path, params=None):
                _ = params
                if path == "/v2/account":
                    return _Resp(200, {"account_number": "PA123", "equity": "100100", "last_equity": "100000"})
                if path == "/v2/positions":
                    return _Resp(200, [])
                if path == "/v2/orders":
                    return _Resp(
                        200,
                        [
                            {
                                "symbol": "BTC/USD",
                                "side": "buy",
                                "filled_qty": "0.5",
                                "filled_avg_price": "60000.0",
                                "timestamp": "2026-05-07T14:00:00Z",
                                "client_order_id": "btc-open-1",
                                "status": "filled",
                            },
                            {
                                "symbol": "BTC/USD",
                                "side": "sell",
                                "filled_qty": "0.5",
                                "filled_avg_price": "62000.0",
                                "timestamp": "2026-05-07T15:00:00Z",
                                "client_order_id": "btc-close-1",
                                "status": "filled",
                            }
                        ],
                    )
                raise AssertionError(f"unexpected path {path}")

        alpaca_config = AlpacaConfig(api_key_id="PK1", api_secret_key="SK1")
        monkeypatch.setattr(
            AlpacaConfig,
            "from_env",
            classmethod(lambda cls: alpaca_config),
        )
        import httpx

        monkeypatch.setattr(httpx, "Client", _Client)

        snapshot = mod._alpaca_live_state_snapshot(today_start_iso="2026-05-07T00:00:00Z")

        assert snapshot["ready"] is True
        assert snapshot["today_filled_orders"] == 2
        assert [row["filled_at"] for row in snapshot["recent_filled_orders"]] == [
            "2026-05-07T14:00:00Z",
            "2026-05-07T15:00:00Z",
        ]
        assert snapshot["recent_closed_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_alpaca_live_state_recent_filled_orders_fall_back_to_last_fill_time(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.venues.alpaca import AlpacaConfig

        class _Resp:
            def __init__(self, status_code, payload) -> None:
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        class _Client:
            def __init__(self, *args, **kwargs) -> None:
                _ = args, kwargs

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def get(self, path, params=None):
                _ = params
                if path == "/v2/account":
                    return _Resp(200, {"account_number": "PA123", "equity": "100100", "last_equity": "100000"})
                if path == "/v2/positions":
                    return _Resp(200, [])
                if path == "/v2/orders":
                    return _Resp(
                        200,
                        [
                            {
                                "symbol": "BTC/USD",
                                "side": "buy",
                                "filled_qty": "0.5",
                                "filled_avg_price": "60000.0",
                                "lastFillTime": "2026-05-07T14:00:00Z",
                                "ts": "2026-05-07T14:00:09Z",
                                "client_order_id": "btc-open-1",
                                "status": "filled",
                            },
                            {
                                "symbol": "BTC/USD",
                                "side": "sell",
                                "filled_qty": "0.5",
                                "filled_avg_price": "62000.0",
                                "lastFillTime": "2026-05-07T15:00:00Z",
                                "ts": "2026-05-07T15:00:09Z",
                                "client_order_id": "btc-close-1",
                                "status": "filled",
                            }
                        ],
                    )
                raise AssertionError(f"unexpected path {path}")

        alpaca_config = AlpacaConfig(api_key_id="PK1", api_secret_key="SK1")
        monkeypatch.setattr(
            AlpacaConfig,
            "from_env",
            classmethod(lambda cls: alpaca_config),
        )
        import httpx

        monkeypatch.setattr(httpx, "Client", _Client)

        snapshot = mod._alpaca_live_state_snapshot(today_start_iso="2026-05-07T00:00:00Z")

        assert snapshot["ready"] is True
        assert snapshot["today_filled_orders"] == 2
        assert [row["filled_at"] for row in snapshot["recent_filled_orders"]] == [
            "2026-05-07T14:00:00Z",
            "2026-05-07T15:00:00Z",
        ]
        assert snapshot["recent_closed_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_alpaca_live_state_recent_filled_orders_fall_back_to_executed_at(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod
        from eta_engine.venues.alpaca import AlpacaConfig

        class _Resp:
            def __init__(self, status_code, payload) -> None:
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        class _Client:
            def __init__(self, *args, **kwargs) -> None:
                _ = args, kwargs

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def get(self, path, params=None):
                _ = params
                if path == "/v2/account":
                    return _Resp(200, {"account_number": "PA123", "equity": "100100", "last_equity": "100000"})
                if path == "/v2/positions":
                    return _Resp(200, [])
                if path == "/v2/orders":
                    return _Resp(
                        200,
                        [
                            {
                                "symbol": "BTC/USD",
                                "side": "buy",
                                "filled_qty": "0.5",
                                "filled_avg_price": "60000.0",
                                "executed_at": "2026-05-07T14:00:00Z",
                                "ts": "2026-05-07T14:00:09Z",
                                "client_order_id": "btc-open-1",
                                "status": "filled",
                            },
                            {
                                "symbol": "BTC/USD",
                                "side": "sell",
                                "filled_qty": "0.5",
                                "filled_avg_price": "62000.0",
                                "executed_at": "2026-05-07T15:00:00Z",
                                "ts": "2026-05-07T15:00:09Z",
                                "client_order_id": "btc-close-1",
                                "status": "filled",
                            }
                        ],
                    )
                raise AssertionError(f"unexpected path {path}")

        alpaca_config = AlpacaConfig(api_key_id="PK1", api_secret_key="SK1")
        monkeypatch.setattr(
            AlpacaConfig,
            "from_env",
            classmethod(lambda cls: alpaca_config),
        )
        import httpx

        monkeypatch.setattr(httpx, "Client", _Client)

        snapshot = mod._alpaca_live_state_snapshot(today_start_iso="2026-05-07T00:00:00Z")

        assert snapshot["ready"] is True
        assert snapshot["today_filled_orders"] == 2
        assert [row["filled_at"] for row in snapshot["recent_filled_orders"]] == [
            "2026-05-07T14:00:00Z",
            "2026-05-07T15:00:00Z",
        ]
        assert snapshot["recent_closed_outcomes"][0]["closed_at"] == "2026-05-07T15:00:00Z"

    def test_live_broker_state_aggregates_ibkr_realized_pnl(self, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_alpaca_live_state_snapshot",
            lambda **kwargs: {
                "today_filled_orders": 2,
                "today_realized_pnl": -15.03,
                "unrealized_pnl": -5.34,
                "open_position_count": 2,
            },
        )
        monkeypatch.setattr(
            mod,
            "_ibkr_live_state_snapshot",
            lambda **kwargs: {
                "today_executions": 18,
                "today_realized_pnl": 10133.83,
                "account_mtd_pnl": 2450.25,
                "account_mtd_return_pct": 2.43,
                "account_mtd_source": "ibkr_client_portal_pa_performance_mtd",
                "unrealized_pnl": 0.0,
                "open_position_count": 0,
                "ready": True,
            },
        )
        monkeypatch.setattr(
            mod,
            "_alpaca_per_bot_pnl_cached",
            lambda **kwargs: {"ready": True, "per_bot": {}},
        )
        monkeypatch.setattr(mod, "_recent_live_fill_rows", lambda: [])
        monkeypatch.setattr(mod, "_recent_trade_closes", lambda limit=25: [])

        live = mod._live_broker_state_payload()

        assert live["focus_policy"]["mode"] == "futures_focus"
        assert live["focus_policy"]["active_venues"] == ["ibkr"]
        assert live["focus_policy"]["standby_venues"] == ["tastytrade"]
        assert live["focus_policy"]["dormant_venues"] == ["tradovate"]
        assert live["ready"] is True
        assert live["tradovate"]["status"] in {
            "dormant",
            "dormant_auth_failed",
            "awaiting_auth",
            "auth_failed",
            "paper_enabled",
        }
        assert live["today_actual_fills"] == 18
        assert live["today_realized_pnl"] == 10133.83
        assert live["broker_mtd_pnl"] == 2450.25
        assert live["broker_mtd_return_pct"] == 2.43
        assert live["total_unrealized_pnl"] == 0.0
        assert live["open_position_count"] == 0
        assert live["all_venue_today_actual_fills"] == 20
        assert live["all_venue_today_realized_pnl"] == 10118.8
        assert live["all_venue_total_unrealized_pnl"] == -5.34
        assert live["all_venue_open_position_count"] == 2
        assert live["cellar_today_actual_fills"] == 2
        assert live["cellar_today_realized_pnl"] == -15.03
        assert live["cellar_total_unrealized_pnl"] == -5.34
        assert live["cellar_open_position_count"] == 2
        assert live["ibkr"]["today_realized_pnl"] == 10133.83
        assert live["sources"]["broker_mtd_pnl"] == "ibkr_client_portal_pa_performance_mtd"
        assert live["alpaca"]["today_realized_pnl"] == -15.03
        assert live["alpaca"]["policy_status"] == "paused_backburner"

    def test_live_broker_state_uses_persisted_mtd_when_broker_mtd_missing(
        self,
        tmp_path,
        monkeypatch,
    ):
        from datetime import UTC, datetime

        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path / "state"))
        month_key = datetime.now(UTC).strftime("%Y-%m")
        tracker_path = tmp_path / "state" / "broker_mtd" / "ibkr_net_liq_month_tracker.json"
        tracker_path.parent.mkdir(parents=True, exist_ok=True)
        tracker_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "updated_at": "2026-05-14T16:20:18+00:00",
                    "accounts": {
                        "DU123": {
                            month_key: {
                                "month": month_key,
                                "baseline_net_liquidation": 1_000_000.0,
                                "baseline_origin": "manual_override",
                                "baseline_set_at": "2026-05-01T00:00:00+00:00",
                                "last_net_liquidation": 1_024_387.0,
                                "last_seen_at": "2026-05-14T16:20:18+00:00",
                            },
                        },
                    },
                },
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            mod,
            "_alpaca_live_state_snapshot",
            lambda **kwargs: {"today_filled_orders": 0, "today_realized_pnl": 0.0, "unrealized_pnl": 0.0},
        )
        monkeypatch.setattr(
            mod,
            "_ibkr_live_state_snapshot",
            lambda **kwargs: {
                "today_executions": 0,
                "today_realized_pnl": 0.0,
                "account_mtd_pnl": None,
                "account_mtd_return_pct": None,
                "account_mtd_source": "",
                "unrealized_pnl": 0.0,
                "open_position_count": 0,
                "ready": True,
            },
        )
        monkeypatch.setattr(mod, "_alpaca_per_bot_pnl_cached", lambda **kwargs: {"ready": True, "per_bot": {}})
        monkeypatch.setattr(mod, "_recent_live_fill_rows", lambda: [])
        monkeypatch.setattr(mod, "_recent_trade_closes", lambda limit=25: [])

        live = mod._live_broker_state_payload()

        assert live["broker_mtd_pnl"] == 24387.0
        assert live["broker_mtd_return_pct"] == 2.44
        assert live["ibkr"]["account_mtd_source"] == "ibkr_net_liquidation_month_manual_override"
        assert live["sources"]["broker_mtd_pnl"] == "ibkr_net_liquidation_month_manual_override"

    def test_live_broker_state_uses_trade_close_ledger_for_win_rate_when_fills_lack_pnl(
        self,
        monkeypatch,
    ):
        from datetime import UTC, datetime

        import eta_engine.deploy.scripts.dashboard_api as mod

        now = datetime.now(UTC).isoformat()
        close_rows = [
            {
                "ts": now,
                "bot_id": "mnq_futures_sage",
                "realized_r": 0.18,
                "extra": {"symbol": "MNQ1", "realized_pnl": 166.0},
            },
            {
                "ts": now,
                "bot_id": "volume_profile_mnq",
                "realized_r": 0.17,
                "extra": {"symbol": "MNQ1", "realized_pnl": 150.0},
            },
            {
                "ts": now,
                "bot_id": "mcl_sweep_reclaim",
                "realized_r": -0.73,
                "extra": {"symbol": "MCL1", "realized_pnl": -105.0},
            },
        ]
        monkeypatch.setattr(
            mod,
            "_alpaca_live_state_snapshot",
            lambda **kwargs: {
                "today_filled_orders": 0,
                "today_realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "open_position_count": 0,
                "ready": True,
            },
        )
        monkeypatch.setattr(
            mod,
            "_ibkr_live_state_snapshot",
            lambda **kwargs: {
                "today_executions": 0,
                "today_realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "open_position_count": 0,
                "ready": True,
            },
        )
        monkeypatch.setattr(
            mod,
            "_alpaca_per_bot_pnl_cached",
            lambda **kwargs: {"ready": True, "per_bot": {}},
        )
        monkeypatch.setattr(mod, "_recent_live_fill_rows", lambda: [])
        monkeypatch.setattr(mod, "_recent_trade_closes", lambda limit=25: close_rows)

        live = mod._live_broker_state_payload()
        summary = mod._broker_summary_fields(live)

        assert live["win_rate_30d"] == 0.6667
        assert live["win_rate_30d_source"] == "trade_close_ledger_30d"
        assert live["win_rate_today"] == 0.6667
        assert live["win_rate_source"] == "trade_close_ledger_today"
        assert live["closed_outcome_count_today"] == 3
        assert live["recent_close_count_30d"] == 3
        assert live["recent_close_realized_pnl_30d"] == 211.0
        assert live["close_history"]["default_window"] == "mtd"
        assert live["close_history"]["windows"]["mtd"]["realized_pnl"] == 211.0
        assert live["position_exposure"]["default_close_history_window"] == "mtd"
        assert live["position_exposure"]["close_history"]["windows"]["mtd"]["closed_outcome_count"] == 3
        assert summary["broker_win_rate_30d"] == 0.6667
        assert summary["broker_win_rate_30d_source"] == "trade_close_ledger_30d"
        assert summary["broker_recent_close_realized_pnl_30d"] == 211.0

    def test_position_exposure_defaults_recent_closes_to_mtd_window(self, monkeypatch):
        from datetime import UTC, datetime, timedelta

        import eta_engine.deploy.scripts.dashboard_api as mod

        now = datetime.now(UTC)
        current_month = now.isoformat()
        previous_month = (now.replace(day=1) - timedelta(days=1)).isoformat()
        rows = [
            {
                "ts": current_month,
                "bot_id": "mnq_futures_sage",
                "extra": {"symbol": "MNQ1", "realized_pnl": 166.0},
            },
            {
                "ts": previous_month,
                "bot_id": "old_bot",
                "extra": {"symbol": "BTC", "realized_pnl": 999.0},
            },
        ]
        monkeypatch.setattr(mod, "_recent_trade_closes", lambda limit=25: rows)

        exposure = mod._position_exposure_payload(
            {
                "alpaca": {"ready": True, "open_positions": []},
                "ibkr": {"ready": True, "open_positions": []},
            },
        )

        assert exposure["default_close_history_window"] == "mtd"
        assert exposure["close_history"]["windows"]["mtd"]["realized_pnl"] == 166.0
        assert exposure["close_history"]["windows"]["ytd"]["realized_pnl"] >= 166.0
        assert exposure["recent_close_count"] == 1
        assert exposure["recent_closes"][0]["bot_id"] == "mnq_futures_sage"

    def test_position_exposure_normalizes_broker_positions_and_recent_closes(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        live_state = {
            "open_position_count": 2,
            "alpaca": {
                "ready": True,
                "open_positions": [
                    {
                        "symbol": "BTCUSD",
                        "side": "long",
                        "qty": 0.04,
                        "avg_entry_price": 101000.0,
                        "current_price": 102250.0,
                        "market_value": 4090.0,
                        "unrealized_pl": 50.0,
                        "unrealized_plpc": 0.0123,
                    },
                ],
            },
            "ibkr": {
                "ready": True,
                "open_positions": [
                    {
                        "symbol": "MNQM6",
                        "secType": "FUT",
                        "exchange": "CME",
                        "position": -1.0,
                        "avg_cost": 18400.0,
                        "market_price": 18380.0,
                        "market_value": -18380.0,
                        "unrealized_pnl": 40.0,
                    },
                ],
            },
        }
        recent_closes = [
            {
                "ts": "2026-05-07T16:05:01+00:00",
                "bot_id": "btc_optimized",
                "realized_r": 1.25,
                "action_taken": "closed",
                "layers_updated": ["trade_memory", "edge_optimizer"],
                "layer_errors": [],
                "extra": {
                    "symbol": "BTC",
                    "side": "SELL",
                    "qty": 0.04,
                    "fill_price": 102250.0,
                    "realized_pnl": 50.0,
                    "close_ts": "2026-05-07T16:05:01+00:00",
                },
            },
        ]

        exposure = mod._position_exposure_payload(live_state, recent_closes=recent_closes)

        assert exposure["ready"] is True
        assert exposure["position_scope"] == "futures_focus"
        assert exposure["open_position_count"] == 1
        assert exposure["symbols_open"] == ["MNQM6"]
        assert exposure["cellar_open_position_count"] == 1
        assert exposure["cellar_symbols_open"] == ["BTCUSD"]
        assert exposure["target_exit_visibility"]["status"] == "open_positions_detected"
        alpaca_pos = exposure["cellar_open_positions"][0]
        assert alpaca_pos["venue"] == "alpaca"
        assert alpaca_pos["symbol"] == "BTCUSD"
        assert alpaca_pos["qty"] == 0.04
        assert alpaca_pos["unrealized_pnl"] == 50.0
        assert alpaca_pos["broker_bracket_required"] is False
        ibkr_pos = exposure["open_positions"][0]
        assert ibkr_pos["venue"] == "ibkr"
        assert ibkr_pos["side"] == "short"
        assert ibkr_pos["sec_type"] == "FUT"
        assert ibkr_pos["broker_bracket_required"] is True
        assert exposure["broker_bracket_required_position_count"] == 1
        assert exposure["broker_supervisor_managed_position_count"] == 0
        assert exposure["recent_closes"] == []
        assert exposure["cellar_recent_close_count"] == 1
        close = exposure["cellar_recent_closes"][0]
        assert close["bot_id"] == "btc_optimized"
        assert close["realized_pnl"] == 50.0
        assert close["layers_updated"] == ["trade_memory", "edge_optimizer"]

    def test_position_exposure_carries_supervisor_paper_watch_when_broker_flat(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        live_state = {
            "alpaca": {"ready": True, "open_positions": []},
            "ibkr": {"ready": True, "open_positions": []},
        }
        target_exit_summary = {
            "status": "paper_watching",
            "summary_line": "0 broker open; 1 supervisor paper-local open; 1 supervisor watcher(s)",
            "open_position_count": 1,
            "broker_open_position_count": 0,
            "supervisor_local_position_count": 1,
            "supervisor_watch_count": 1,
            "nearest_target_bot": "mnq_anchor_sweep",
        }

        exposure = mod._position_exposure_payload(
            live_state,
            recent_closes=[],
            target_exit_summary=target_exit_summary,
        )

        assert exposure["open_position_count"] == 0
        assert exposure["broker_open_position_count"] == 0
        assert exposure["supervisor_local_position_count"] == 1
        assert exposure["supervisor_watch_count"] == 1
        assert exposure["target_exit_visibility"]["status"] == "paper_watching"
        assert "supervisor paper-local open" in exposure["target_exit_visibility"]["detail"]
        assert exposure["target_exit_summary"]["nearest_target_bot"] == "mnq_anchor_sweep"

    def test_live_position_exposure_endpoint_returns_read_only_rollup(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_live_broker_state_payload",
            lambda: {
                "open_position_count": 1,
                "alpaca": {
                    "ready": True,
                    "open_positions": [{"symbol": "ETHUSD", "qty": 0.5, "side": "long"}],
                },
                "ibkr": {"ready": True, "open_positions": []},
            },
        )
        monkeypatch.setattr(mod, "_recent_trade_closes", lambda limit=25: [])

        r = app_client.get("/api/live/position_exposure")

        assert r.status_code == 200
        assert "no-store" in r.headers["Cache-Control"]
        payload = r.json()
        assert payload["ready"] is True
        assert payload["source"] == "live_broker_rest+trade_closes"
        assert payload["open_position_count"] == 0
        assert payload["open_positions"] == []
        assert payload["cellar_open_position_count"] == 1
        assert payload["cellar_open_positions"][0]["symbol"] == "ETHUSD"

    def test_live_position_exposure_endpoint_prefers_fleet_merged_paper_watch(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "bot_fleet_roster",
            lambda response, since_days=1, live_broker_probe=True: {
                "live_broker_state": {
                    "position_exposure": {
                        "ready": True,
                        "source": "live_broker_rest+trade_closes",
                        "open_position_count": 0,
                        "broker_open_position_count": 0,
                        "supervisor_local_position_count": 2,
                        "supervisor_watch_count": 2,
                        "target_exit_visibility": {
                            "status": "paper_watching",
                            "detail": "0 broker open; 2 supervisor paper-local open; 2 supervisor watcher(s)",
                        },
                    },
                },
            },
        )

        r = app_client.get("/api/live/position_exposure")

        assert r.status_code == 200
        assert "no-store" in r.headers["Cache-Control"]
        payload = r.json()
        assert payload["open_position_count"] == 0
        assert payload["broker_open_position_count"] == 0
        assert payload["supervisor_local_position_count"] == 2
        assert payload["supervisor_watch_count"] == 2
        assert payload["target_exit_visibility"]["status"] == "paper_watching"

    def test_bot_fleet_enriches_supervisor_bots_from_readiness_snapshot(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        """Live supervisor rows inherit launch posture even when heartbeat omits readiness."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "bots").mkdir(parents=True, exist_ok=True)
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": "2026-04-28T12:00:00+00:00",
                    "mode": "paper_sim",
                    "bots": [
                        {
                            "bot_id": "volume_profile_nq",
                            "symbol": "NQ1",
                            "strategy_kind": "orb",
                            "direction": "long",
                            "n_entries": 5,
                            "n_exits": 5,
                            "realized_pnl": 3.5,
                            "open_position": None,
                            "last_jarvis_verdict": "DENIED",
                            "last_bar_ts": "2026-04-28T12:00:00+00:00",
                            "last_aggregation_reject_reason": "session_gate:outside_rth",
                            "last_aggregation_reject_at": "2026-04-28T12:00:00+00:00",
                        },
                    ],
                },
            ),
        )
        readiness = tmp_path / "bot_strategy_readiness_latest.json"
        readiness.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-04-29T21:45:00+00:00",
                    "source": "bot_strategy_readiness",
                    "summary": {"total_bots": 1, "launch_lanes": {"live_preflight": 1}},
                    "rows": [
                        {
                            "bot_id": "volume_profile_nq",
                            "strategy_id": "volume_profile_nq_v1",
                            "strategy_kind": "confluence_scorecard",
                            "symbol": "NQ1",
                            "timeframe": "5m",
                            "active": True,
                            "promotion_status": "production",
                            "baseline_status": "baseline_present",
                            "data_status": "ready",
                            "launch_lane": "live_preflight",
                            "can_paper_trade": True,
                            "can_live_trade": False,
                            "missing_critical": [],
                            "missing_optional": [],
                            "next_action": "Run per-bot promotion preflight before live routing.",
                        },
                    ],
                },
            ),
        )
        monkeypatch.setenv("ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH", str(readiness))

        r = app_client.get("/api/bot-fleet")
        assert r.status_code == 200
        data = r.json()
        nq_rows = [b for b in data["bots"] if b["name"] == "volume_profile_nq"]
        assert len(nq_rows) == 1
        nq = nq_rows[0]
        assert nq["source"] == "jarvis_strategy_supervisor"
        assert nq["status"] == "running"
        assert nq["current_block_reason"] == "session_gate:outside_rth"
        assert nq["current_block_summary"] == "Entries paused by session gate: outside_rth"
        assert nq["current_block_at"] == "2026-04-28T12:00:00+00:00"
        assert nq["strategy_readiness"]["strategy_id"] == "volume_profile_nq_v1"
        assert nq["launch_lane"] == "live_preflight"
        assert nq["can_paper_trade"] is True
        assert nq["can_live_trade"] is False
        assert nq["readiness_next_action"].startswith("Run per-bot promotion")
        assert data["summary"]["current_blocked_bots"] == 1
        assert data["summary"]["current_blocked_kinds"] == {"session_gate": 1}
        assert data["summary"]["current_blocked_summary_line"] == (
            "Current blockers: 1 bot(s) held - 1 session gate. Top: volume_profile_nq."
        )
        assert data["summary"]["blocked_summary"] == "No actionable blockers; 1 bot(s) await session reopen."
        assert data["summary"]["session_gated_bots"] == 1
        assert data["summary"]["session_gated_kinds"] == {"session_gate": 1}
        assert data["summary"]["session_gated_summary_line"] == (
            "Awaiting session: 1 bot(s) - 1 session gate. Top: volume_profile_nq."
        )
        assert data["summary"]["current_blocked_preview"] == [
            {
                "bot_id": "volume_profile_nq",
                "symbol": "NQ1",
                "kind": "session_gate",
                "summary": "Entries paused by session gate: outside_rth",
                "at": "2026-04-28T12:00:00+00:00",
            },
        ]

    def test_bot_fleet_ignores_local_daily_loss_overlay_on_non_authoritative_host(
        self,
        app_client,
        monkeypatch,
    ):
        import json
        import os
        from pathlib import Path

        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_daily_loss_killswitch_snapshot",
            lambda: {
                "source": "daily_loss_killswitch",
                "status": "tripped",
                "tripped": True,
                "disabled": False,
                "today_pnl_usd": -925.50,
                "limit_usd": -900.0,
                "reason": "day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)",
            },
        )

        state = Path(os.environ["ETA_STATE_DIR"])
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": "2026-05-16T12:00:00+00:00",
                    "mode": "paper_sim",
                    "bots": [
                        {
                            "bot_id": "volume_profile_nq",
                            "symbol": "NQ1",
                            "strategy_kind": "orb",
                            "direction": "long",
                            "n_entries": 5,
                            "n_exits": 5,
                            "realized_pnl": 3.5,
                            "open_position": None,
                            "last_bar_ts": "2026-05-16T12:00:00+00:00",
                            "last_aggregation_reject_reason": (
                                "daily_kill_switch:day_pnl=$-925.50 <= limit=$-900.00 (date=2026-05-15)"
                            ),
                            "last_aggregation_reject_at": "2026-05-16T12:00:00+00:00",
                        },
                    ],
                },
            ),
            encoding="utf-8",
        )
        (state / "paper_live_transition_check.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "blocked",
                    "critical_ready": False,
                    "non_authoritative_gateway_host": True,
                    "operator_queue_launch_blocked_count": 1,
                    "operator_queue_first_launch_next_action": (
                        "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass "
                        "-File .\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 "
                        "-Apply -Role vps"
                    ),
                    "paper_ready_bots": 9,
                    "gates": [],
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        data = r.json()
        nq = next(b for b in data["bots"] if b["name"] == "volume_profile_nq")
        assert nq["current_block_kind"] == ""
        assert data["summary"]["current_blocked_bots"] == 0
        assert data["summary"]["current_blocked_kinds"] == {}
        assert (
            data["summary"]["paper_live_effective_detail"]
            == "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass "
            "-File .\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 -Apply -Role vps"
        )
        assert (
            data["summary"]["paper_live_detail"]
            == "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass "
            "-File .\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 -Apply -Role vps"
        )
        assert (
            data["summary"]["paper_live_first_launch_next_action"]
            == "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass "
            "-File .\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 -Apply -Role vps"
        )
        assert data["summary"]["paper_live_non_authoritative_gateway_host"] is True
        assert data["summary"]["paper_live_capital_lanes_held_by_daily_loss_stop"] is False
        assert data["summary"]["paper_live_daily_loss_advisory_active"] is False
        assert data["summary"]["paper_live_daily_loss_suppressed_non_authoritative_gateway_host"] is True

    def test_bot_fleet_surfaces_tws_gateway_health(self, app_client):
        """The public roster reports broker execution health separately from bot liveness."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        (state / "tws_watchdog.json").write_text(
            json.dumps(
                {
                    "checked_at": "2026-05-05T12:45:22+00:00",
                    "healthy": False,
                    "consecutive_failures": 72,
                    "last_healthy_at": "2026-05-05T06:08:00+00:00",
                    "details": {
                        "host": "127.0.0.1",
                        "port": 4002,
                        "socket_ok": False,
                        "handshake_ok": False,
                        "handshake_detail": "skipped (socket down)",
                        "gateway_crash": {
                            "reason_code": "jvm_native_memory_oom",
                            "summary": "IB Gateway JVM native-memory OOM",
                            "native_allocation": "Native memory allocation failed",
                        },
                        "gateway_process": {
                            "running": True,
                            "pid": 8072,
                            "name": "ibgateway.exe",
                            "working_set_mb": 149.3,
                        },
                    },
                },
            ),
            encoding="utf-8",
        )
        (state / "ibgateway_reauth.json").write_text(
            json.dumps(
                {
                    "status": "auth_pending",
                    "action": "none",
                    "operator_action_required": True,
                    "operator_action": "Complete the IBKR Gateway login or two-factor prompt.",
                    "restart_attempts": 3,
                    "last_task_name": "ETA-IBGateway-DailyRestart",
                    "last_restart_at": "2026-05-05T14:39:16+00:00",
                },
            ),
            encoding="utf-8",
        )
        (state / "ibgateway_repair.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": "2026-05-06T11:01:19+00:00",
                    "gateway_config": {
                        "jts_ini": {
                            "configured": True,
                            "local_server_port": "4002",
                            "trusted_localhost": True,
                            "api_only_enabled": True,
                        },
                        "vmoptions": {
                            "configured": True,
                            "xmx": "512m",
                            "low_memory_profile_configured": True,
                        },
                    },
                    "single_source": {
                        "gateway_task_canonical": True,
                        "port_listeners": [],
                    },
                },
            ),
            encoding="utf-8",
        )
        (state / "ibgateway_install.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": "2026-05-06T13:03:09+00:00",
                    "downloaded": True,
                    "installed": False,
                    "install_requested": False,
                    "install_attempted": False,
                    "installer_path": (
                        r"C:\EvolutionaryTradingAlgo\var\eta_engine\downloads\ibgateway"
                        r"\ibgateway-latest-standalone-windows-x64.exe"
                    ),
                    "installer_length": 325524034,
                    "installer_sha256": "ABC123",
                    "authenticode_status": "NotSigned",
                    "operator_action_required": True,
                    "operator_action": ("IB Gateway 10.46 is not installed at C:\\Jts\\ibgateway\\1046."),
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")
        assert r.status_code == 200
        data = r.json()
        ibkr = data["broker_gateway"]["ibkr"]
        assert ibkr["status"] == "down"
        assert data["broker_gateway"]["status"] == "down"
        assert data["summary"]["ibkr_gateway_status"] == "down"
        assert data["summary"]["ibkr_gateway_raw_status"] == "down"
        assert data["summary"]["ibkr_gateway_non_authoritative_host"] is False
        assert ibkr["healthy"] is False
        assert data["broker_gateway"]["healthy"] is False
        assert ibkr["port"] == 4002
        assert ibkr["consecutive_failures"] == 72
        assert ibkr["detail"] == (
            "gateway process running; API not ready; skipped (socket down); "
            "gateway config verified; latest crash: IB Gateway JVM native-memory OOM; "
            "installer downloaded (NotSigned); "
            "installer action required; "
            "recovery: auth_pending; operator action required"
        )
        assert data["broker_gateway"]["detail"] == ibkr["detail"]
        assert data["summary"]["ibkr_gateway_detail"] == ibkr["detail"]
        assert ibkr["crash"]["reason_code"] == "jvm_native_memory_oom"
        assert ibkr["process"]["running"] is True
        assert ibkr["config"]["jts_ini"]["configured"] is True
        assert ibkr["config"]["vmoptions"]["configured"] is True
        assert ibkr["config"]["single_source"]["gateway_task_canonical"] is True
        assert ibkr["install"]["downloaded"] is True
        assert ibkr["install"]["authenticode_status"] == "NotSigned"
        assert ibkr["install"]["operator_action_required"] is True
        assert ibkr["recovery"]["status"] == "auth_pending"
        assert ibkr["recovery"]["operator_action_required"] is True
        assert ibkr["recovery"]["restart_attempts"] == 3
        assert "recovery: auth_pending" in ibkr["detail"]

    def test_bot_fleet_surfaces_broker_router_execution_state(self, app_client):
        """The roster exposes broker-router execution state separate from signal liveness."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        pending_dir = state / "pending_orders"
        router = state / "router"
        result_dir = router / "fill_results"
        failed_dir = router / "failed"
        processing_dir = router / "processing"
        pending_dir.mkdir(parents=True, exist_ok=True)
        result_dir.mkdir(parents=True, exist_ok=True)
        failed_dir.mkdir(parents=True, exist_ok=True)
        processing_dir.mkdir(parents=True, exist_ok=True)

        (pending_dir / "eth_sage_daily.pending_order.json").write_text(
            json.dumps({"signal_id": "sig-pending"}),
            encoding="utf-8",
        )
        (processing_dir / "btc_hybrid.pending_order.json").write_text(
            json.dumps({"signal_id": "sig-processing"}),
            encoding="utf-8",
        )
        (failed_dir / "mnq_futures_sage.pending_order.json").write_text(
            json.dumps({"signal_id": "sig-failed"}),
            encoding="utf-8",
        )
        (failed_dir / "mnq_futures_sage.pending_order.json.retry_meta.json").write_text(
            json.dumps(
                {
                    "attempts": 3,
                    "last_attempt_ts": "2026-05-05T12:58:52+00:00",
                    "last_reject_reason": "venue=ibkr rejected order_id=sig-reject",
                },
            ),
            encoding="utf-8",
        )
        (result_dir / "sig-reject_result.json").write_text(
            json.dumps(
                {
                    "signal_id": "sig-reject",
                    "bot_id": "mnq_futures_sage",
                    "venue": "ibkr",
                    "ts": "2026-05-05T12:58:52+00:00",
                    "result": {
                        "status": "REJECTED",
                        "order_id": "sig-reject",
                        "filled_qty": 0,
                        "avg_price": 0,
                    },
                },
            ),
            encoding="utf-8",
        )
        (router / "broker_router_heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": "2026-05-05T12:59:00+00:00",
                    "last_poll_ts": "2026-05-05T12:59:00+00:00",
                    "pending_dir": str(pending_dir),
                    "counts": {"submitted": 4, "rejected": 3, "failed": 1, "filled": 0},
                    "recent_events": [{"kind": "failed", "detail": "max_retries"}],
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")
        assert r.status_code == 200
        broker_router = r.json()["broker_router"]
        assert broker_router["status"] == "processing"
        assert broker_router["pending_count"] == 1
        assert broker_router["processing_count"] == 1
        assert broker_router["failed_count"] == 1
        assert broker_router["active_blocker_count"] == 2
        assert "historical_failed_orders" in broker_router["historical_reasons"]
        assert broker_router["fill_results_count"] == 1
        assert broker_router["result_status_counts"]["REJECTED"] == 1
        assert broker_router["latest_result"]["bot_id"] == "mnq_futures_sage"
        assert broker_router["latest_result"]["status"] == "REJECTED"
        assert broker_router["latest_failure"]["attempts"] == 3
        assert broker_router["latest_failure"]["last_reject_reason"] == "venue=ibkr rejected order_id=sig-reject"

    def test_bot_fleet_blocks_active_ibkr_router_work_when_gateway_down(self, app_client):
        """Active IBKR router work is blocked, not merely processing, while Gateway auth is down."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        router = state / "router"
        processing_dir = router / "processing"
        pending_dir = state / "pending_orders"
        processing_dir.mkdir(parents=True, exist_ok=True)
        pending_dir.mkdir(parents=True, exist_ok=True)

        (processing_dir / "mcl_sweep_reclaim.pending_order.json").write_text(
            json.dumps(
                {
                    "ts": "2026-05-09T05:00:00+00:00",
                    "signal_id": "mcl-live-entry",
                    "side": "BUY",
                    "qty": 1,
                    "symbol": "MCL1",
                    "limit_price": 95.25,
                    "stop_price": 94.75,
                    "target_price": 96.25,
                },
            ),
            encoding="utf-8",
        )
        (router / "broker_router_heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": "2026-05-09T05:00:02+00:00",
                    "last_poll_ts": "2026-05-09T05:00:02+00:00",
                    "pending_dir": str(pending_dir),
                    "counts": {"submitted": 1, "rejected": 1, "failed": 0, "filled": 0},
                    "recent_events": [{"kind": "rejected_retry", "detail": "gateway auth pending"}],
                },
            ),
            encoding="utf-8",
        )
        (state / "tws_watchdog.json").write_text(
            json.dumps(
                {
                    "healthy": False,
                    "checked_at": "2026-05-09T05:00:05+00:00",
                    "consecutive_failures": 12,
                    "details": {
                        "host": "127.0.0.1",
                        "port": 4002,
                        "socket_ok": False,
                        "handshake_ok": False,
                        "handshake_detail": "auth pending",
                        "gateway_process": {
                            "running": True,
                            "name": "java.exe",
                            "manager": "IBC",
                        },
                    },
                },
            ),
            encoding="utf-8",
        )
        (state / "ibgateway_reauth.json").write_text(
            json.dumps(
                {
                    "status": "auth_pending",
                    "operator_action_required": True,
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        broker_router = r.json()["broker_router"]
        assert broker_router["status"] == "blocked"
        assert "ibkr_gateway_down" in broker_router["degraded_reasons"]
        assert broker_router["gateway_blocker"]["active"] is True
        assert broker_router["gateway_blocker"]["venue"] == "ibkr"
        assert broker_router["gateway_blocker"]["gateway_status"] == "down"
        assert broker_router["gateway_blocker"]["recovery_status"] == "auth_pending"
        assert broker_router["gateway_blocker"]["active_ibkr_order_count"] == 1

    def test_bot_fleet_derives_ibkr_parent_fill_from_raw_statuses(self, app_client):
        """Dashboard must not show 0 filled when raw IBKR parent status filled."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        router = state / "router"
        result_dir = router / "fill_results"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "sig-open_result.json").write_text(
            json.dumps(
                {
                    "signal_id": "sig-open",
                    "bot_id": "mnq_anchor_sweep",
                    "venue": "ibkr",
                    "ts": "2026-05-07T05:29:08+00:00",
                    "broker_fill_ts": "2026-05-07T05:28:41+00:00",
                    "result": {
                        "status": "OPEN",
                        "order_id": "sig-open",
                        "filled_qty": 0.0,
                        "avg_price": 0.0,
                        "raw": {
                            "ib_statuses": [
                                {
                                    "status": "Filled",
                                    "filled": 1.0,
                                    "avg_fill_price": 28709.5,
                                },
                                {
                                    "status": "Submitted",
                                    "filled": 0.0,
                                    "avg_fill_price": 0.0,
                                },
                            ],
                        },
                    },
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        latest = r.json()["broker_router"]["latest_result"]
        assert latest["status"] == "OPEN"
        assert latest["filled_qty"] == 1.0
        assert latest["avg_price"] == 28709.5
        assert latest["ts"] == "2026-05-07T05:28:41+00:00"

    def test_bot_fleet_router_latest_result_falls_back_to_result_written_ts(self, app_client):
        """Latest router result should stay timestamped even without broker fill time."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        router = state / "router"
        result_dir = router / "fill_results"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "sig-open_result.json").write_text(
            json.dumps(
                {
                    "signal_id": "sig-open",
                    "bot_id": "mnq_anchor_sweep",
                    "venue": "ibkr",
                    "result_written_ts": "2026-05-07T05:29:08+00:00",
                    "result": {
                        "status": "OPEN",
                        "order_id": "sig-open",
                        "filled_qty": 1.0,
                        "avg_price": 28709.5,
                    },
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        latest = r.json()["broker_router"]["latest_result"]
        assert latest["status"] == "OPEN"
        assert latest["ts"] == "2026-05-07T05:29:08+00:00"

    def test_bot_fleet_router_latest_result_prefers_raw_execution_time(self, app_client):
        """Latest router result should use broker-side raw execution time before write time."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        router = state / "router"
        result_dir = router / "fill_results"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "sig-open_result.json").write_text(
            json.dumps(
                {
                    "signal_id": "sig-open",
                    "bot_id": "mnq_anchor_sweep",
                    "venue": "ibkr",
                    "result_written_ts": "2026-05-07T05:29:08+00:00",
                    "result": {
                        "status": "OPEN",
                        "order_id": "sig-open",
                        "filled_qty": 1.0,
                        "avg_price": 28709.5,
                        "raw": {
                            "execution_time": "2026-05-07T05:28:54+00:00",
                        },
                    },
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        latest = r.json()["broker_router"]["latest_result"]
        assert latest["status"] == "OPEN"
        assert latest["ts"] == "2026-05-07T05:28:54+00:00"

    def test_bot_fleet_router_latest_result_prefers_legacy_server_execution_time(self, app_client):
        """Latest router result should honor older nested raw.server timing hints."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        router = state / "router"
        result_dir = router / "fill_results"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "sig-open_result.json").write_text(
            json.dumps(
                {
                    "signal_id": "sig-open",
                    "bot_id": "mnq_anchor_sweep",
                    "venue": "ibkr",
                    "result_written_ts": "2026-05-07T05:29:08+00:00",
                    "result": {
                        "status": "OPEN",
                        "order_id": "sig-open",
                        "filled_qty": 1.0,
                        "avg_price": 28709.5,
                        "raw": {
                            "server": {
                                "execution_time": "2026-05-07T05:28:52+00:00",
                            },
                        },
                    },
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        latest = r.json()["broker_router"]["latest_result"]
        assert latest["status"] == "OPEN"
        assert latest["ts"] == "2026-05-07T05:28:52+00:00"

    def test_bot_fleet_treats_historical_router_rejects_as_history(self, app_client):
        """Old rejected router artifacts should not masquerade as active degradation."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        router = state / "router"
        result_dir = router / "fill_results"
        failed_dir = router / "failed"
        quarantine_dir = router / "quarantine"
        result_dir.mkdir(parents=True, exist_ok=True)
        failed_dir.mkdir(parents=True, exist_ok=True)
        quarantine_dir.mkdir(parents=True, exist_ok=True)

        (failed_dir / "stale.pending_order.json").write_text(
            json.dumps({"signal_id": "stale"}),
            encoding="utf-8",
        )
        (quarantine_dir / "quarantined.pending_order.json").write_text(
            json.dumps({"signal_id": "quarantined"}),
            encoding="utf-8",
        )
        (result_dir / "stale_result.json").write_text(
            json.dumps(
                {
                    "signal_id": "stale",
                    "bot_id": "mnq_futures_sage",
                    "venue": "ibkr",
                    "ts": "2026-05-05T12:58:52+00:00",
                    "result": {"status": "REJECTED", "filled_qty": 0},
                },
            ),
            encoding="utf-8",
        )
        (router / "broker_router_heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": "2026-05-05T12:59:00+00:00",
                    "last_poll_ts": "2026-05-05T12:59:00+00:00",
                    "counts": {"submitted": 4, "rejected": 3, "failed": 1, "filled": 0},
                    "recent_events": [{"kind": "max_retries", "detail": "stale reject"}],
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")
        assert r.status_code == 200
        broker_router = r.json()["broker_router"]
        assert broker_router["status"] == "ok"
        assert broker_router["active_blocker_count"] == 0
        assert broker_router["degraded_reasons"] == []
        assert broker_router["failed_count"] == 1
        assert broker_router["quarantine_count"] == 1
        assert broker_router["result_status_counts"]["REJECTED"] == 1
        assert broker_router["historical_reasons"] == [
            "historical_failed_orders",
            "historical_rejected_results",
            "quarantined_orders",
        ]

    def test_bot_fleet_surfaces_order_hold_before_old_blocked_files(self, app_client):
        """A live order-entry hold is the actionable router state."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        router = state / "router"
        blocked_dir = router / "blocked"
        blocked_dir.mkdir(parents=True, exist_ok=True)
        (blocked_dir / "old.pending_order.json").write_text(
            json.dumps({"signal_id": "old"}),
            encoding="utf-8",
        )
        (router / "broker_router_heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": "2026-05-05T12:59:00+00:00",
                    "last_poll_ts": "2026-05-05T12:59:00+00:00",
                    "counts": {"held": 1},
                    "order_entry_hold": {
                        "active": True,
                        "reason": "ibkr_pending_submit_unconfirmed",
                    },
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")
        assert r.status_code == 200
        broker_router = r.json()["broker_router"]
        assert broker_router["status"] == "held"
        assert broker_router["blocked_count"] == 1
        assert broker_router["active_blocker_count"] == 0
        assert broker_router["order_entry_hold"]["reason"] == "ibkr_pending_submit_unconfirmed"
        assert broker_router["degraded_reasons"] == ["order_entry_hold"]
        assert "historical_blocked_orders" in broker_router["historical_reasons"]

    def test_bot_fleet_surfaces_global_order_hold_when_router_heartbeat_lacks_hold(self, app_client):
        """The canonical order hold file remains authoritative when router heartbeat is sparse."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        router = state / "router"
        router.mkdir(parents=True, exist_ok=True)
        (state / "order_entry_hold.json").write_text(
            json.dumps(
                {
                    "active": True,
                    "reason": "ibgateway_waiting_for_manual_login_or_2fa",
                    "operator": "codex",
                },
            ),
            encoding="utf-8",
        )
        (router / "broker_router_heartbeat.json").write_text(
            json.dumps(
                {
                    "ts": "2026-05-05T12:59:00+00:00",
                    "last_poll_ts": "2026-05-05T12:59:00+00:00",
                    "counts": {"held": 1},
                },
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/bot-fleet")
        assert r.status_code == 200
        broker_router = r.json()["broker_router"]
        assert broker_router["status"] == "held"
        assert broker_router["order_entry_hold"]["reason"] == "ibgateway_waiting_for_manual_login_or_2fa"
        assert broker_router["order_entry_hold"]["source"] == "order_entry_hold_file"
        assert broker_router["order_entry_hold"]["path"].endswith("order_entry_hold.json")
        assert broker_router["degraded_reasons"] == ["order_entry_hold"]

    def test_live_fills_include_ibkr_execution_snapshot_and_filter_pending_router_rows(self, app_client):
        """Live fill stats use real IBKR executions, not PendingSubmit router audit rows."""
        import json
        import os
        from datetime import UTC, datetime
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        now = datetime.now(UTC).isoformat()
        (state / "broker_router_fills.jsonl").write_text(
            json.dumps(
                {
                    "ts": now,
                    "bot_id": "mnq_futures_sage",
                    "symbol": "MNQ",
                    "status": "PendingSubmit",
                    "qty": 1,
                    "price": 100.0,
                },
            )
            + "\n",
            encoding="utf-8",
        )
        (state / "tws_watchdog.json").write_text(
            json.dumps(
                {
                    "checked_at": now,
                    "healthy": True,
                    "consecutive_failures": 0,
                    "last_healthy_at": now,
                    "details": {
                        "host": "127.0.0.1",
                        "port": 4002,
                        "socket_ok": True,
                        "handshake_ok": True,
                        "handshake_detail": "serverVersion=176; clientId=55; attempt=1",
                        "account_snapshot": {
                            "summary": {
                                "accounts": ["DUQ...9869"],
                                "executions_count": 1,
                                "last_execution_ts": now,
                            },
                            "executions": [
                                {
                                    "ts": now,
                                    "account": "DUQ...9869",
                                    "symbol": "CL",
                                    "side": "BOT",
                                    "qty": 1,
                                    "price": 104.32,
                                    "exec_id": "58268.1777959080.11",
                                    "source": "ibkr_execution",
                                },
                            ],
                        },
                    },
                },
            ),
            encoding="utf-8",
        )

        live = app_client.get("/api/bot-fleet").json()["live"]
        assert live["fills_24h"] == 1
        assert live["fills_1h"] == 1
        assert live["source_counts_24h"] == {"ibkr_execution": 1}

        fills = app_client.get("/api/live/fills?limit=5").json()["fills"]
        assert len(fills) == 1
        assert fills[0]["source"] == "ibkr_execution"
        assert fills[0]["symbol"] == "CL"

    def test_live_fills_router_fill_results_prefer_broker_fill_ts(self, app_client):
        """Router fill rows should use broker_fill_ts over later write timestamps."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        result_dir = state / "router" / "fill_results"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "btc-fill_result.json").write_text(
            json.dumps(
                {
                    "signal_id": "btc-fill",
                    "bot_id": "btc_optimized",
                    "broker_fill_ts": "2026-05-16T14:20:01+00:00",
                    "result_written_ts": "2026-05-16T14:20:08+00:00",
                    "ts": "2026-05-16T14:20:09+00:00",
                    "request": {
                        "bot_id": "btc_optimized",
                        "symbol": "BTC",
                        "side": "SELL",
                        "client_order_id": "btc-fill",
                    },
                    "result": {
                        "status": "FILLED",
                        "order_id": "btc-fill",
                        "filled_qty": 0.25,
                        "avg_price": 64250.5,
                    },
                }
            ),
            encoding="utf-8",
        )

        fills = app_client.get("/api/live/fills?limit=5").json()["fills"]

        assert len(fills) == 1
        assert fills[0]["source"] == "broker_router"
        assert fills[0]["bot"] == "btc_optimized"
        assert fills[0]["symbol"] == "BTC"
        assert fills[0]["qty"] == 0.25
        assert fills[0]["price"] == 64250.5
        assert fills[0]["ts"] == "2026-05-16T14:20:01+00:00"

    def test_live_fills_router_fill_results_prefer_raw_execution_time(self, app_client):
        """Router fill rows should prefer raw execution_time over later write timestamps."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        result_dir = state / "router" / "fill_results"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "btc-fill_result.json").write_text(
            json.dumps(
                {
                    "signal_id": "btc-fill",
                    "bot_id": "btc_optimized",
                    "result_written_ts": "2026-05-16T14:20:08+00:00",
                    "ts": "2026-05-16T14:20:09+00:00",
                    "request": {
                        "bot_id": "btc_optimized",
                        "symbol": "BTC",
                        "side": "SELL",
                        "client_order_id": "btc-fill",
                    },
                    "result": {
                        "status": "FILLED",
                        "order_id": "btc-fill",
                        "filled_qty": 0.25,
                        "avg_price": 64250.5,
                        "raw": {
                            "execution_time": "2026-05-16T14:20:04+00:00",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        fills = app_client.get("/api/live/fills?limit=5").json()["fills"]

        assert len(fills) == 1
        assert fills[0]["source"] == "broker_router"
        assert fills[0]["ts"] == "2026-05-16T14:20:04+00:00"

    def test_live_fills_router_fill_results_derive_ibkr_parent_fill_from_raw_statuses(self, app_client):
        """Router fill rows should surface IBKR parent fills even when top-level qty stays zero."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        result_dir = state / "router" / "fill_results"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "mnq-parent_result.json").write_text(
            json.dumps(
                {
                    "signal_id": "mnq-parent",
                    "bot_id": "mnq_anchor_sweep",
                    "result_written_ts": "2026-05-16T14:25:08+00:00",
                    "request": {
                        "bot_id": "mnq_anchor_sweep",
                        "symbol": "MNQ",
                        "side": "BUY",
                        "client_order_id": "mnq-parent",
                    },
                    "result": {
                        "status": "FILLED",
                        "order_id": "mnq-parent",
                        "filled_qty": 0.0,
                        "avg_price": 0.0,
                        "raw": {
                            "ib_statuses": [
                                {
                                    "status": "Filled",
                                    "filled": 1.0,
                                    "avg_fill_price": 28709.5,
                                    "filled_at": "2026-05-16T14:25:01+00:00",
                                },
                                {
                                    "status": "Submitted",
                                    "filled": 0.0,
                                    "avg_fill_price": 0.0,
                                },
                            ],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        fills = app_client.get("/api/live/fills?limit=5").json()["fills"]

        assert len(fills) == 1
        assert fills[0]["source"] == "broker_router"
        assert fills[0]["bot"] == "mnq_anchor_sweep"
        assert fills[0]["symbol"] == "MNQ"
        assert fills[0]["qty"] == 1.0
        assert fills[0]["price"] == 28709.5
        assert fills[0]["ts"] == "2026-05-16T14:25:01+00:00"

    def test_live_fills_legacy_router_rows_prefer_broker_fill_ts(self, app_client):
        """Legacy router JSONL rows should also prefer broker_fill_ts when present."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        (state / "broker_router_fills.jsonl").write_text(
            json.dumps(
                {
                    "ts": "2026-05-16T14:31:09+00:00",
                    "broker_fill_ts": "2026-05-16T14:31:01+00:00",
                    "bot_id": "eth_sage_daily",
                    "symbol": "ETH",
                    "status": "FILLED",
                    "qty": 0.4,
                    "price": 3123.25,
                    "order_id": "eth-fill-1",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        fills = app_client.get("/api/live/fills?limit=5").json()["fills"]

        assert len(fills) == 1
        assert fills[0]["source"] == "broker_router"
        assert fills[0]["bot"] == "eth_sage_daily"
        assert fills[0]["symbol"] == "ETH"
        assert fills[0]["qty"] == 0.4
        assert fills[0]["price"] == 3123.25
        assert fills[0]["ts"] == "2026-05-16T14:31:01+00:00"

    def test_fleet_equity_uses_supervisor_when_curves_are_missing(self, app_client):
        """Fleet equity stays live from supervisor heartbeat when curve files are absent."""
        import json
        import os
        from datetime import UTC, datetime
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        hb = {
            "ts": now,
            "mode": "paper_sim",
            "bots": [
                {
                    "bot_id": "mnq_futures",
                    "symbol": "MNQ1",
                    "strategy_kind": "orb",
                    "direction": "long",
                    "n_entries": 1,
                    "n_exits": 1,
                    "realized_pnl": 2.0,
                    "last_bar_ts": now,
                },
                {
                    "bot_id": "btc_hybrid",
                    "symbol": "BTC",
                    "strategy_kind": "hybrid",
                    "direction": "long",
                    "n_entries": 1,
                    "n_exits": 1,
                    "realized_pnl": -0.5,
                    "last_bar_ts": now,
                },
            ],
        }
        (sup_dir / "heartbeat.json").write_text(json.dumps(hb), encoding="utf-8")

        r = app_client.get("/api/fleet-equity")
        assert r.status_code == 200
        data = r.json()
        assert data["source"] == "supervisor_heartbeat"
        assert data["summary"]["today_pnl"] == 1.5
        assert data["summary"]["current_equity"] == 10001.5
        assert data["lifetime_ledger_attached"] is False
        assert data["lifetime_total_pnl"] is None
        assert data["summary"]["total_pnl_is_lifetime"] is False
        assert data["summary"]["total_pnl_source"] == "supervisor_session_fallback"
        assert data["summary"]["lifetime_ledger_attached"] is False
        assert data["summary"]["lifetime_total_pnl"] is None
        assert len(data["series"]) == 2
        assert data["pnl"] == 1.5
        assert data["source_updated_at"] == now
        assert data["source_heartbeat_count"] == 2
        assert data["dashboard_version"] == "v1"
        assert data["release_stage"] == "pre_beta"
        assert data["beta_launched"] is False
        assert set(data["required_data"]) == {
            "bot_fleet",
            "fleet_equity",
            "auth_session",
            "source_freshness",
        }
        assert data["source_age_s"] <= 5
        assert data["data_ts"] <= data["server_ts"]
        assert data["data_ts"] > data["server_ts"] - 5
        assert data["session_truth_status"] == "live"
        assert (
            data["truth_summary_line"]
            == "Live ETA truth: 2/2 bot heartbeat(s) are fresh; 2 attached, 0 in trade, 2 flat/idle."
        )

    def test_normalize_trade_close_sanitizes_r69_tick_leak(self):
        """REGRESSION: operator saw mnq_futures_sage MNQ1 r=69.0 on $17.25 PnL.

        The 69 is the tick count, not the R value. Dashboard's per-trade
        view (_normalize_trade_close) now runs every row through
        trade_close_sanitizer so the open-book display shows the corrected
        ~0.86R instead of the inflated 69R. The raw value is preserved in
        ``realized_r_raw`` for audit and a flag ``realized_r_sanitized``
        marks the row as touched.
        """
        from eta_engine.deploy.scripts.dashboard_api import _normalize_trade_close

        buggy = {
            "ts": "2026-05-13T08:11:38.350511+00:00",
            "bot_id": "mnq_futures_sage",
            "realized_r": 69.0,  # the bug — tick count written into R field
            "action_taken": "approve_full",
            "direction": "SHORT",
            "extra": {
                "realized_pnl": 17.25,
                "fill_price": 29375.0,
                "qty": 0.5,
                "symbol": "MNQ1",
                "side": "SELL",
            },
        }
        out = _normalize_trade_close(buggy)
        assert out is not None
        # Sanitized realized_r should be ~0.86, not 69
        assert out["realized_r"] is not None
        assert abs(out["realized_r"] - 0.8625) < 1e-6
        # Raw original preserved for audit
        assert out["realized_r_raw"] == 69.0
        # Flag set so dashboard renderer can show a "sanitized" badge
        assert out["realized_r_sanitized"] is True
        # Other fields untouched
        assert out["bot_id"] == "mnq_futures_sage"
        assert out["symbol"] == "MNQ1"
        assert out["realized_pnl"] == 17.25

    def test_normalize_trade_close_passes_clean_value_through(self):
        """A legitimate small R value passes through with realized_r_sanitized=False."""
        from eta_engine.deploy.scripts.dashboard_api import _normalize_trade_close

        clean = {
            "ts": "2026-05-13T07:51:50.280411+00:00",
            "bot_id": "mnq_futures_sage",
            "realized_r": -1.18,
            "action_taken": "approve_full",
            "direction": "SHORT",
            "extra": {
                "realized_pnl": -23.6,
                "fill_price": 29400.0,
                "qty": 1.0,
                "symbol": "MNQ1",
                "side": "BUY",
            },
        }
        out = _normalize_trade_close(clean)
        assert out is not None
        assert out["realized_r"] == -1.18
        assert out["realized_r_raw"] == -1.18
        assert out["realized_r_sanitized"] is False

    def test_normalize_trade_close_drops_unrecoverable_suspect(self):
        """A value that's huge AND can't be recovered: realized_r becomes None."""
        from eta_engine.deploy.scripts.dashboard_api import _normalize_trade_close

        suspect = {
            "ts": "2026-05-05T03:16:47.591093+00:00",
            "bot_id": "ym_sweep_reclaim",
            "realized_r": 32661.39,  # raw USD leaked into R field
            "action_taken": "approve_full",
            "extra": {
                "realized_pnl": 0.0,  # nothing useful to recompute from
                "symbol": "YM",  # YM not in known dollar_per_r table
            },
        }
        out = _normalize_trade_close(suspect)
        assert out is not None
        assert out["realized_r"] is None  # sanitizer rejected
        assert out["realized_r_raw"] == 32661.39  # raw preserved for audit
        assert out["realized_r_sanitized"] is True

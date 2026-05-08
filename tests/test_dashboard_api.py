"""
Tests for deploy.scripts.dashboard_api -- FastAPI backend for the Apex
Predator dashboard.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Point dashboard_api at a temp state dir + return a TestClient."""
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ETA_LOG_DIR", str(tmp_path / "logs"))
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
    return TestClient(mod.app)


class TestDashboardAPI:
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

    def test_heartbeat(self, app_client):
        r = app_client.get("/api/heartbeat")
        assert r.status_code == 200
        assert r.json()["quota_state"] == "OK"

    def test_dashboard(self, app_client):
        r = app_client.get("/api/dashboard")
        assert r.status_code == 200
        assert r.json()["regime"] == "NEUTRAL"
        assert "operator_queue" in r.json()
        assert "paper_live_transition" in r.json()
        assert "no-store" in r.headers["Cache-Control"]

    def test_dashboard_cold_start_still_exposes_operator_queue(self, tmp_path, app_client):
        state = tmp_path / "state"
        (state / "dashboard_payload.json").unlink()

        r = app_client.get("/api/dashboard")

        assert r.status_code == 200
        assert r.json()["_warning"] == "no_data"
        assert "operator_queue" in r.json()

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

    def test_jarvis_operator_queue_endpoint_fails_soft(self, app_client, monkeypatch):
        from eta_engine.scripts import jarvis_status

        def boom(**_kwargs):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(jarvis_status, "build_operator_queue_summary", boom)

        r = app_client.get("/api/jarvis/operator_queue")

        assert r.status_code == 200
        assert r.json()["error"] == "probe exploded"
        assert r.json()["top_blockers"] == []

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
        assert "cc-strategy-supercharge-results" in cards
        assert "cc-paper-live-transition" in cards
        assert "fl-roster" in cards
        assert "fl-controls" in cards
        assert "fl-equity-curve" in cards
        assert cards["cc-verdict-stream"]["source"] == "sse"
        assert cards["cc-paper-live-transition"]["endpoint"] == "/api/jarvis/paper_live_transition"
        assert (
            cards["cc-strategy-supercharge-results"]["endpoint"]
            == "/api/jarvis/strategy_supercharge_results"
        )
        assert cards["fl-controls"]["source"] == "client"
        assert cards["fl-roster"]["endpoint"] == "/api/bot-fleet?since_days=1"
        assert cards["fl-equity-curve"]["endpoint"].startswith("/api/fleet-equity?")
        assert all(card["status"] not in {"dead", "stale"} for card in data["cards"])

    def test_dashboard_diagnostics_rollup_explains_live_sources(self, app_client):
        r = app_client.get("/api/dashboard/diagnostics")

        assert r.status_code == 200
        data = r.json()
        assert data["dashboard_version"] == "v1"
        assert data["release_stage"] == "pre_beta"
        assert data["source_of_truth"] == "dashboard_diagnostics"
        assert data["service"]["status"] == "ok"
        assert data["service"]["uptime_s"] >= 0
        assert data["paths"]["state_dir"].endswith("state")
        assert data["cards"]["summary"]["dead"] == 0
        assert data["cards"]["summary"]["stale"] == 0
        assert data["bot_fleet"]["bot_total"] >= 0
        assert data["bot_fleet"]["confirmed_bots"] == 0
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
        assert data["checks"]["operator_queue_contract"] is True
        assert data["checks"]["paper_live_transition_contract"] is True
        assert data["dashboard_proxy_watchdog"]["status"] in {
            "ok",
            "missing",
            "stale",
            "failed",
            "degraded",
            "unknown",
        }
        assert data["checks"]["dashboard_proxy_watchdog_contract"] is True

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
        assert (
            data["direct"]["equity"]["point_count"]
            == data["diagnostics"]["equity"]["point_count"]
        )
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
        assert payload["paper_live_transition"]["paper_ready_bots"] == 10
        assert payload["paper_live_transition"]["first_launch_blocker_op_id"] == "OP-19"
        assert payload["paper_live_transition"]["first_failed_gate"]["name"] == "op19_gateway_runtime"

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
        assert watchdog["heartbeat_path"].endswith("dashboard_proxy_watchdog_heartbeat.json")
        assert payload["checks"]["dashboard_proxy_watchdog_contract"] is True

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
        audit = state / "jarvis_audit.jsonl"
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
        audit = state / "jarvis_audit.jsonl"
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
        audit = state / "jarvis_audit.jsonl"
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

    # ------------------------------------------------------------------ #
    # Broker readiness + BTC fleet endpoints
    # ------------------------------------------------------------------ #

    def test_brokers_endpoint_returns_all_three_readiness_reports(self, app_client):
        r = app_client.get("/api/brokers")
        assert r.status_code == 200
        j = r.json()
        # Alpaca was added 2026-05-05 as the active crypto-paper venue.
        assert set(j["brokers"].keys()) == {"ibkr", "tastytrade", "alpaca"}
        # All three adapters must at least be importable -- they all carry
        # `adapter_available=True` in their readiness output.
        assert j["brokers"]["ibkr"]["adapter_available"] is True
        assert j["brokers"]["tastytrade"]["adapter_available"] is True
        assert j["brokers"]["alpaca"]["adapter_available"] is True
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
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        fleet_dir = state / "broker_fleet"
        fleet_dir.mkdir(parents=True, exist_ok=True)

        # Manifest
        (fleet_dir / "btc_broker_fleet_latest.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": "2026-04-24T10:00:00+00:00",
                    "fleet": "btc_broker_paper_fleet",
                    "requested_workers": 4,
                    "running_workers": 2,
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
                }
            ),
            encoding="utf-8",
        )

        r = app_client.get("/api/btc/lanes")
        assert r.status_code == 200
        j = r.json()
        assert j["lane_count"] == 2
        # Sorted by filename so directional comes first (d < g)
        directional = next(lane for lane in j["lanes"] if lane["lane"] == "directional")
        grid = next(lane for lane in j["lanes"] if lane["lane"] == "grid")
        assert directional["broker"] == "tastytrade"
        assert grid["broker"] == "ibkr"
        assert grid["active_order_id"] == "srv-I-1"
        assert grid["heartbeat_status"] == "RUNNING"
        assert grid["pid"] == 12345
        assert grid["execution_state"] == "ACTIVE"
        assert j["manifest"]["fleet"] == "btc_broker_paper_fleet"

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
        bot_dir = state / "bots" / "sol_optimized"
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "status.json").write_text(
            json.dumps(
                {
                    "name": "sol_optimized",
                    "symbol": "SOL",
                    "tier": "confluence_scorecard",
                    "venue": "paper-sim",
                    "status": "running",
                    "todays_pnl": 0.0,
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
                            "bot_id": "sol_optimized",
                            "strategy_id": "sol_optimized_v1",
                            "strategy_kind": "confluence_scorecard",
                            "symbol": "SOL",
                            "timeframe": "1h",
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
        eth = next(b for b in r.json()["bots"] if b["name"] == "sol_optimized")
        assert eth["strategy_readiness"]["strategy_id"] == "sol_optimized_v1"
        assert eth["launch_lane"] == "paper_soak"
        assert eth["can_paper_trade"] is True
        assert eth["can_live_trade"] is False
        assert eth["readiness_next_action"] == "Run paper-soak and broker drift checks before live routing."

        drill = app_client.get("/api/bot-fleet/sol_optimized")
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
                        }
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
            "_live_broker_state_payload",
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

    def test_bot_fleet_includes_supervisor_bots(self, app_client, tmp_path):
        """Supervisor heartbeat bots appear in /api/bot-fleet even when state/bots/ is empty."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        # Ensure state/bots/ exists but is empty (no legacy bots)
        (state / "bots").mkdir(parents=True, exist_ok=True)

        # Write supervisor heartbeat
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        hb = {
            "ts": "2026-04-28T12:00:00+00:00",
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
                    "last_signal_at": "2026-04-28T11:59:00+00:00",
                    "last_bar_ts": "2026-04-28T12:00:00+00:00",
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
                        "entry_ts": "2026-04-28T11:58:30+00:00",
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
                    "last_bar_ts": "2026-04-28T12:00:00+00:00",
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
        assert mnq["last_signal_ts"] == "2026-04-28T11:59:00+00:00"
        assert mnq["last_signal_side"] == "LONG"
        assert mnq["last_activity_ts"] == "2026-04-28T11:59:00+00:00"
        assert mnq["last_activity_side"] == "LONG"
        assert mnq["last_activity_type"] == "signal"
        assert mnq["last_bar_ts"] == "2026-04-28T12:00:00+00:00"
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
        assert btc["last_signal_ts"] == "2026-04-28T11:58:30+00:00"
        assert btc["last_activity_type"] == "signal"
        assert btc["bracket_stop"] == 66200.0
        assert btc["bracket_target"] == 68400.0
        assert btc["broker_bracket"] is False
        assert btc["bracket_src"] == "supervisor_local"
        assert data["latest_signal_ts"] == "2026-04-28T11:59:00+00:00"
        assert data["summary"]["latest_signal_ts"] == "2026-04-28T11:59:00+00:00"
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
                    "bot_id": "sol_optimized",
                    "symbol": "SOL",
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
        assert "sol_optimized" in names
        assert "rsi_mr_mnq" not in names

        debug = app_client.get("/api/bot-fleet?include_disabled=true")
        rows = {row["name"]: row for row in debug.json()["bots"]}
        assert rows["rsi_mr_mnq"]["registry_deactivated"] is True
        assert rows["rsi_mr_mnq"]["registry_active"] is False
        assert rows["sol_optimized"]["registry_active"] is True

    def test_bot_fleet_drilldown_prefers_supervisor_open_position(self, app_client):
        """Per-bot drilldown must not hide live supervisor positions behind legacy status."""
        import json
        import os
        from pathlib import Path

        state = Path(os.environ["ETA_STATE_DIR"])
        legacy_dir = state / "bots" / "btc_hybrid"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "status.json").write_text(
            json.dumps({
                "name": "btc_hybrid",
                "symbol": "BTC",
                "status": "idle",
                "open_positions": 0,
                "open_position": {},
                "position_state": {"state": "flat"},
            }),
            encoding="utf-8",
        )
        sup_dir = state / "jarvis_intel" / "supervisor"
        sup_dir.mkdir(parents=True, exist_ok=True)
        (sup_dir / "heartbeat.json").write_text(
            json.dumps({
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
            }),
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

    def test_bot_fleet_embeds_live_broker_state(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "_live_broker_state_payload",
            lambda: {
                "ready": True,
                "today_actual_fills": 12,
                "today_realized_pnl": 125.5,
                "total_unrealized_pnl": 42.25,
                "open_position_count": 3,
                "server_ts": 1778119427.0,
            },
        )

        r = app_client.get("/api/bot-fleet")

        assert r.status_code == 200
        live_broker = r.json()["live_broker_state"]
        assert live_broker["ready"] is True
        assert live_broker["today_actual_fills"] == 12
        assert live_broker["open_position_count"] == 3

    def test_derive_ibkr_today_realized_pnl_prefers_futures_bucket(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        assert mod._derive_ibkr_today_realized_pnl(
            {"futures_pnl": 10133.83, "unrealized_pnl": 0.0}
        ) == 10133.83
        assert mod._derive_ibkr_today_realized_pnl(
            {"futures_pnl": 10133.83, "unrealized_pnl": 133.83}
        ) == 10000.0
        assert mod._derive_ibkr_today_realized_pnl(
            {"account_summary_realized_pnl": 321.98}
        ) == 321.98

    def test_closed_outcomes_from_alpaca_filled_order_pairs(self):
        import eta_engine.deploy.scripts.dashboard_api as mod

        outcomes = mod._closed_outcomes_from_filled_orders([
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
        ])

        assert outcomes["closed_outcome_count"] == 2
        assert outcomes["evaluated_outcome_count"] == 2
        assert outcomes["winning_outcomes"] == 1
        assert outcomes["losing_outcomes"] == 1
        assert outcomes["win_rate"] == 0.5
        assert [row["realized_pnl"] for row in outcomes["recent_outcomes"]] == [-5.0, 5.0]

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

        live = mod._live_broker_state_payload()

        assert live["today_actual_fills"] == 20
        assert live["today_realized_pnl"] == 10118.8
        assert live["total_unrealized_pnl"] == -5.34
        assert live["open_position_count"] == 2
        assert live["ibkr"]["today_realized_pnl"] == 10133.83
        assert live["alpaca"]["today_realized_pnl"] == -15.03

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
        assert exposure["open_position_count"] == 2
        assert exposure["symbols_open"] == ["BTCUSD", "MNQM6"]
        assert exposure["target_exit_visibility"]["status"] == "open_positions_detected"
        alpaca_pos = exposure["open_positions"][0]
        assert alpaca_pos["venue"] == "alpaca"
        assert alpaca_pos["symbol"] == "BTCUSD"
        assert alpaca_pos["qty"] == 0.04
        assert alpaca_pos["unrealized_pnl"] == 50.0
        ibkr_pos = exposure["open_positions"][1]
        assert ibkr_pos["venue"] == "ibkr"
        assert ibkr_pos["side"] == "short"
        assert ibkr_pos["sec_type"] == "FUT"
        close = exposure["recent_closes"][0]
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
        assert payload["open_position_count"] == 1
        assert payload["open_positions"][0]["symbol"] == "ETHUSD"

    def test_live_position_exposure_endpoint_prefers_fleet_merged_paper_watch(self, app_client, monkeypatch):
        import eta_engine.deploy.scripts.dashboard_api as mod

        monkeypatch.setattr(
            mod,
            "bot_fleet_roster",
            lambda response, since_days=1: {
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
        assert nq["strategy_readiness"]["strategy_id"] == "volume_profile_nq_v1"
        assert nq["launch_lane"] == "live_preflight"
        assert nq["can_paper_trade"] is True
        assert nq["can_live_trade"] is False
        assert nq["readiness_next_action"].startswith("Run per-bot promotion")

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
                    "operator_action": (
                        "IB Gateway 10.46 is not installed at C:\\Jts\\ibgateway\\1046."
                    ),
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
        assert data["truth_summary_line"] == "Live ETA truth: 2/2 bot heartbeat(s) are fresh."

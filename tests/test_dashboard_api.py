"""
Tests for deploy.scripts.dashboard_api -- FastAPI backend for the Apex
Predator dashboard.
"""

from __future__ import annotations

import json

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
        assert "fl-roster" in cards
        assert "fl-controls" in cards
        assert "fl-equity-curve" in cards
        assert cards["cc-verdict-stream"]["source"] == "sse"
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
        bot_dir = state / "bots" / "eth_compression"
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "status.json").write_text(
            json.dumps(
                {
                    "name": "eth_compression",
                    "symbol": "ETH",
                    "tier": "compression",
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
                            "bot_id": "eth_compression",
                            "strategy_id": "eth_compression_v1",
                            "strategy_kind": "compression",
                            "symbol": "ETH",
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
        eth = next(b for b in r.json()["bots"] if b["name"] == "eth_compression")
        assert eth["strategy_readiness"]["strategy_id"] == "eth_compression_v1"
        assert eth["launch_lane"] == "paper_soak"
        assert eth["can_paper_trade"] is True
        assert eth["can_live_trade"] is False
        assert eth["readiness_next_action"] == "Run paper-soak and broker drift checks before live routing."

        drill = app_client.get("/api/bot-fleet/eth_compression")
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
                    "summary": {"total_bots": 1, "launch_lanes": {"live_preflight": 1}},
                    "rows": [
                        {
                            "bot_id": "nq_daily_drb",
                            "strategy_id": "nq_daily_drb_v1",
                            "strategy_kind": "daily_drb",
                            "symbol": "NQ",
                            "timeframe": "1d",
                            "active": True,
                            "promotion_status": "production",
                            "baseline_status": "baseline_present",
                            "data_status": "ready",
                            "launch_lane": "live_preflight",
                            "can_paper_trade": True,
                            "can_live_trade": False,
                            "next_action": "Run per-bot promotion preflight before live routing.",
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
        nq = next(b for b in data["bots"] if b["name"] == "nq_daily_drb")
        assert nq["source"] == "bot_strategy_readiness_snapshot"
        assert nq["status"] == "readiness_only"
        assert nq["strategy_readiness"]["strategy_id"] == "nq_daily_drb_v1"
        assert nq["launch_lane"] == "live_preflight"
        assert nq["can_paper_trade"] is True
        assert nq["readiness_next_action"].startswith("Run per-bot promotion")

        filtered = app_client.get("/api/bot-fleet?bot=nq_daily_drb")
        assert filtered.status_code == 200
        assert [row["name"] for row in filtered.json()["bots"]] == ["nq_daily_drb"]

        drill = app_client.get("/api/bot-fleet/nq_daily_drb")
        assert drill.status_code == 200
        drill_data = drill.json()
        assert drill_data["status"]["source"] == "bot_strategy_readiness_snapshot"
        assert drill_data["status"]["status"] == "readiness_only"
        assert drill_data["strategy_readiness"]["launch_lane"] == "live_preflight"
        assert "_warning" not in drill_data

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
                    "bot_id": "mnq_futures",
                    "symbol": "MNQ1",
                    "strategy_kind": "orb",
                    "direction": "long",
                    "n_entries": 5,
                    "n_exits": 5,
                    "realized_pnl": 2.0,
                    "open_position": None,
                    "last_jarvis_verdict": "APPROVED",
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
                    "open_position": {"side": "BUY", "entry_price": 67000.0},
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
        assert "mnq_futures" in names, f"mnq_futures missing from roster: {names}"
        assert "btc_hybrid" in names, f"btc_hybrid missing from roster: {names}"

        mnq = next(b for b in data["bots"] if b["name"] == "mnq_futures")
        assert mnq["todays_pnl"] == 2.0
        assert mnq["status"] == "running"
        assert mnq["source"] == "jarvis_strategy_supervisor"
        assert mnq["last_trade_ts"] is None
        assert mnq["last_trade_side"] is None
        assert mnq["last_trade_qty"] is None
        assert mnq["last_trade_r"] is None
        assert mnq["last_signal_ts"] == "2026-04-28T12:00:00+00:00"
        assert mnq["last_signal_side"] == "LONG"
        assert mnq["last_activity_ts"] == "2026-04-28T12:00:00+00:00"
        assert mnq["last_activity_side"] == "LONG"
        assert mnq["last_activity_type"] == "signal"
        assert mnq["venue"] == "paper-sim"
        assert mnq["tier"] == "orb"
        assert mnq["strategy_readiness"]["launch_lane"] == "live_preflight"
        assert mnq["launch_lane"] == "live_preflight"
        assert mnq["can_paper_trade"] is True
        assert mnq["can_live_trade"] is False

        drill = app_client.get("/api/bot-fleet/mnq_futures")
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

    def test_supervisor_heartbeat_freshness_is_not_last_signal_freshness(self, app_client):
        """A quiet bot should stay live when the supervisor heartbeat is fresh."""
        from datetime import UTC, datetime, timedelta
        import os
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
        from datetime import UTC, datetime, timedelta
        import os
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
                            "bot_id": "nq_futures",
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
                            "bot_id": "nq_futures",
                            "strategy_id": "nq_orb_v1",
                            "strategy_kind": "orb",
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
        nq_rows = [b for b in data["bots"] if b["name"] == "nq_futures"]
        assert len(nq_rows) == 1
        nq = nq_rows[0]
        assert nq["source"] == "jarvis_strategy_supervisor"
        assert nq["status"] == "running"
        assert nq["strategy_readiness"]["strategy_id"] == "nq_orb_v1"
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

        r = app_client.get("/api/bot-fleet")
        assert r.status_code == 200
        data = r.json()
        ibkr = data["broker_gateway"]["ibkr"]
        assert ibkr["status"] == "down"
        assert ibkr["healthy"] is False
        assert ibkr["port"] == 4002
        assert ibkr["consecutive_failures"] == 72
        assert ibkr["detail"] == (
            "gateway process running; API not ready; skipped (socket down); "
            "latest crash: IB Gateway JVM native-memory OOM; recovery: auth_pending; operator action required"
        )
        assert ibkr["crash"]["reason_code"] == "jvm_native_memory_oom"
        assert ibkr["process"]["running"] is True
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

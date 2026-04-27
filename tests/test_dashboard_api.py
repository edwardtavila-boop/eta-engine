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
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("APEX_LOG_DIR", str(tmp_path / "logs"))
    # Pin the BTC fleet dir so the dashboard doesn't accidentally see a
    # real fleet directory sitting in the dev package tree.
    monkeypatch.setenv(
        "APEX_BTC_FLEET_DIR",
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

    def test_heartbeat(self, app_client):
        r = app_client.get("/api/heartbeat")
        assert r.status_code == 200
        assert r.json()["quota_state"] == "OK"

    def test_dashboard(self, app_client):
        r = app_client.get("/api/dashboard")
        assert r.status_code == 200
        assert r.json()["regime"] == "NEUTRAL"

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
        assert len(r.json()["tasks"]) == 20

    def test_fire_unknown_task(self, app_client):
        r = app_client.post("/api/tasks/nonsense/fire")
        assert r.status_code == 404

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

        state = Path(os.environ["APEX_STATE_DIR"])
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

        state = Path(os.environ["APEX_STATE_DIR"])
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

        state = Path(os.environ["APEX_STATE_DIR"])
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

    def test_brokers_endpoint_returns_both_readiness_reports(self, app_client):
        r = app_client.get("/api/brokers")
        assert r.status_code == 200
        j = r.json()
        assert set(j["brokers"].keys()) == {"ibkr", "tastytrade"}
        # Both adapters must at least be importable -- they both have
        # `adapter_available=True` in their readiness output.
        assert j["brokers"]["ibkr"]["adapter_available"] is True
        assert j["brokers"]["tastytrade"]["adapter_available"] is True
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

        state = Path(os.environ["APEX_STATE_DIR"])
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

        state = Path(os.environ["APEX_STATE_DIR"])
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

        state = Path(os.environ["APEX_STATE_DIR"])
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
        monkeypatch.setenv("APEX_MNQ_SUPERVISOR_DIR", str(mnq_dir))

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
        monkeypatch.setenv("APEX_MNQ_SUPERVISOR_DIR", str(mnq_dir))
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
        monkeypatch.setenv("APEX_BTC_FLEET_DIR", str(fleet_dir))
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

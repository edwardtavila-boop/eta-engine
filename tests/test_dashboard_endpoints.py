# eta_engine/tests/test_dashboard_endpoints.py
"""General dashboard endpoint tests."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from eta_engine.deploy.scripts.dashboard_api import app

    return TestClient(app)


def test_serve_theme_css(client, tmp_path, monkeypatch) -> None:
    """The dashboard serves theme.css from the resolved status_page parent."""
    from eta_engine.deploy.scripts import dashboard_api

    monkeypatch.setattr(dashboard_api, "_STATUS_PAGE", tmp_path / "index.html")
    css_path = tmp_path / "theme.css"
    css_path.write_text("/* test css */", encoding="utf-8")

    r = client.get("/theme.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    assert "/* test css */" in r.text


def test_serve_js_module(client, tmp_path, monkeypatch) -> None:
    """The dashboard serves js modules from the resolved status_page/js dir."""
    from eta_engine.deploy.scripts import dashboard_api

    monkeypatch.setattr(dashboard_api, "_STATUS_PAGE", tmp_path / "index.html")
    js_dir = tmp_path / "js"
    js_dir.mkdir(parents=True, exist_ok=True)
    (js_dir / "auth.js").write_text("export const x = 1;", encoding="utf-8")

    r = client.get("/js/auth.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"].lower()
    assert "export const x" in r.text


def test_status_page_contains_blocker_topline_anchors(client) -> None:
    """The served status page keeps the blocker banner/card surface wired in."""
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="blockerStatusText"' in r.text
    assert 'id="heldBots"' in r.text
    assert 'id="approvedCountSub"' in r.text
    assert "function blockedFleetRollup" in r.text


def test_supercharge_serves_held_diagnostics_chip_logic(client) -> None:
    """The diagnostics chip source should expose held-bot topline logic."""
    r = client.get("/js/supercharge.js")
    assert r.status_code == 200
    assert "current_blocked_bots" in r.text
    assert "| held ${blockedBots}" in r.text


def test_service_worker_cleanup_unregisters_stale_clients(client) -> None:
    """Stale browser service-worker registrations should get a cleanup script."""
    r = client.get("/service-worker.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"].lower()
    assert "registration.unregister" in r.text
    assert "skipWaiting" in r.text
    assert "no-store" in r.headers.get("cache-control", "").lower()


def test_js_path_traversal_blocked(client, tmp_path, monkeypatch) -> None:
    """Reject path-traversal attempts."""
    from eta_engine.deploy.scripts import dashboard_api

    monkeypatch.setattr(dashboard_api, "_STATUS_PAGE", tmp_path / "index.html")
    r = client.get("/js/../dashboard_api.py")
    # FastAPI normalizes the path first, so this should 404
    assert r.status_code in (400, 404)


def test_js_module_rejects_dot_prefix(tmp_path, monkeypatch) -> None:
    """Directly exercise the 400-branch filename validator."""
    from eta_engine.deploy.scripts import dashboard_api

    monkeypatch.setattr(dashboard_api, "_STATUS_PAGE", tmp_path / "index.html")
    with pytest.raises(HTTPException) as exc:
        dashboard_api.serve_js_module(".env")
    assert exc.value.status_code == 400


def test_governor_returns_warning_when_state_missing(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/governor")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_warning") == "no_data"


def test_governor_returns_data_when_state_present(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    gov = tmp_path / "jarvis_governor.json"
    gov.write_text('{"grade":"A","score":0.92}', encoding="utf-8")
    r = client.get("/api/jarvis/governor")
    assert r.status_code == 200
    assert r.json()["grade"] == "A"


def test_edge_leaderboard_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/edge_leaderboard")
    assert r.status_code == 200
    body = r.json()
    assert "top" in body and "bottom" in body
    assert body["top"] == [] and body["bottom"] == []


def test_edge_leaderboard_with_data(client, tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    edge = tmp_path / "sage" / "edge_tracker.json"
    edge.parent.mkdir(parents=True)
    edge.write_text(
        json.dumps(
            {
                "schools": {
                    "dow_theory": {"n_obs": 50, "n_aligned_wins": 35, "n_aligned_losses": 10, "sum_r": 12.5},
                    "fibonacci": {"n_obs": 50, "n_aligned_wins": 10, "n_aligned_losses": 35, "sum_r": -8.0},
                }
            }
        ),
        encoding="utf-8",
    )
    r = client.get("/api/jarvis/edge_leaderboard")
    assert r.status_code == 200
    body = r.json()
    assert any(s["school"] == "dow_theory" for s in body["top"])
    assert any(s["school"] == "fibonacci" for s in body["bottom"])


def test_edge_leaderboard_rejects_bad_bot_id(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/edge_leaderboard?bot=..%2F..%2Fetc%2Fpasswd")
    assert r.status_code == 400
    # And a few other bad shapes
    # empty string is borderline; ok if you want to allow
    assert client.get("/api/jarvis/edge_leaderboard?bot=").status_code in (400, 200)
    assert client.get("/api/jarvis/edge_leaderboard?bot=foo/bar").status_code == 400


def test_model_tier_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/model_tier")
    assert r.status_code == 200
    assert r.json().get("_warning") == "no_data"


def test_kaizen_latest_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/kaizen_latest")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_warning") == "no_data"


def test_kaizen_latest_returns_markdown(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    tickets = tmp_path / "kaizen" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "2026-04-26_TKT-001.md").write_text("# Ticket 001\nbody", encoding="utf-8")
    r = client.get("/api/jarvis/kaizen_latest")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Ticket 001"
    assert "body" in body["markdown"]


def test_bot_fleet_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/bot-fleet")
    assert r.status_code == 200
    body = r.json()
    assert body["bots"] == []  # no bot status files yet


def test_bot_fleet_assembles_roster(client, tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    bots_dir = tmp_path / "bots"
    for name in ("mnq", "btc_hybrid"):
        (bots_dir / name).mkdir(parents=True)
        (bots_dir / name / "status.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "symbol": name.upper(),
                    "tier": "FUTURES",
                    "venue": "tastytrade",
                    "status": "running",
                    "todays_pnl": 12.50,
                    "open_positions": 1,
                    "last_signal_ts": "2026-04-27T14:00:00Z",
                    "heartbeat_ts": "2026-04-27T14:32:00Z",
                    "jarvis_attached": True,
                    "journal_attached": True,
                    "online_learner_attached": False,
                }
            ),
            encoding="utf-8",
        )
    r = client.get("/api/bot-fleet")
    assert r.status_code == 200
    bots = r.json()["bots"]
    assert len(bots) == 2
    assert {b["name"] for b in bots} == {"mnq", "btc_hybrid"}


def test_bot_fleet_drilldown(client, tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    bot_dir = tmp_path / "bots" / "mnq"
    bot_dir.mkdir(parents=True)
    (bot_dir / "status.json").write_text(json.dumps({"name": "mnq"}), encoding="utf-8")
    (bot_dir / "recent_fills.json").write_text(
        json.dumps([{"ts": "2026-04-27T13:00Z", "side": "long", "price": 21000, "qty": 1, "realized_r": 1.2}]),
        encoding="utf-8",
    )
    (bot_dir / "recent_verdicts.json").write_text(
        json.dumps([{"ts": "2026-04-27T13:00Z", "verdict": "APPROVED", "sage_modulation": "v22_sage_loosened"}]),
        encoding="utf-8",
    )
    r = client.get("/api/bot-fleet/mnq")
    assert r.status_code == 200
    body = r.json()
    assert body["status"]["name"] == "mnq"
    assert len(body["recent_fills"]) == 1
    assert len(body["recent_verdicts"]) == 1


def test_bot_fleet_drilldown_enriches_sentiment_verdict_summary(client, tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    bot_dir = tmp_path / "bots" / "btc_hybrid"
    bot_dir.mkdir(parents=True)
    (bot_dir / "status.json").write_text(json.dumps({"name": "btc_hybrid"}), encoding="utf-8")
    (bot_dir / "recent_verdicts.json").write_text(
        json.dumps(
            [
                {
                    "ts": "2026-05-15T12:11:39Z",
                    "verdict": {
                        "final_verdict": "PROCEED",
                        "sentiment_pressure_status": "risk_on",
                        "sentiment_modulation": "tailwind",
                        "sentiment_pressure_lead_asset": "BTC",
                    },
                    "sage_modulation": "loosened",
                }
            ]
        ),
        encoding="utf-8",
    )
    r = client.get("/api/bot-fleet/btc_hybrid")
    assert r.status_code == 200
    verdict = r.json()["recent_verdicts"][0]
    assert verdict["verdict_label"] == "PROCEED"
    assert verdict["sentiment_pressure_status"] == "risk_on"
    assert verdict["sentiment_modulation"] == "tailwind"
    assert verdict["sentiment_summary"] == "risk_on / tailwind / lead=BTC"


def test_bot_fleet_drilldown_falls_back_to_canonical_verdict_log(client, tmp_path, monkeypatch) -> None:
    import json

    from eta_engine.scripts import workspace_roots

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    bot_dir = tmp_path / "bots" / "mnq_futures_sage"
    bot_dir.mkdir(parents=True)
    (bot_dir / "status.json").write_text(json.dumps({"name": "mnq_futures_sage", "symbol": "MNQ1"}), encoding="utf-8")
    verdict_log = tmp_path / "jarvis_intel" / "verdicts.jsonl"
    verdict_log.parent.mkdir(parents=True, exist_ok=True)
    verdict_log.write_text(
        json.dumps(
            {
                "ts": "2026-05-15T12:20:55+00:00",
                "request_id": "mnq_futures_sage_d7a207fd",
                "subsystem": "bot.es",
                "final_verdict": "APPROVED",
                "final_size_multiplier": 0.75,
                "base_reason": "trade_ok",
                "sentiment_pressure_status": "risk_off",
                "sentiment_modulation": "headwind_strong",
                "sentiment_pressure_lead_asset": "macro",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(workspace_roots, "ETA_JARVIS_VERDICTS_PATH", verdict_log)
    monkeypatch.setattr(workspace_roots, "ETA_LEGACY_JARVIS_VERDICTS_PATH", tmp_path / "missing.jsonl")

    r = client.get("/api/bot-fleet/mnq_futures_sage")
    assert r.status_code == 200
    verdict = r.json()["recent_verdicts"][0]
    assert verdict["verdict_label"] == "APPROVED"
    assert verdict["bot_id"] == "mnq_futures_sage"
    assert verdict["subsystem"] == "bot.mnq_futures_sage"
    assert verdict["source_subsystem"] == "bot.es"
    assert verdict["verdict_match_source"] == "request_id"
    assert verdict["sentiment_pressure_status"] == "risk_off"
    assert verdict["sentiment_modulation"] == "headwind_strong"
    assert verdict["sentiment_summary"] == "risk_off / headwind_strong / lead=macro"


def test_bot_fleet_drilldown_surfaces_current_block_summary(
    client,
    tmp_path,
    monkeypatch,
) -> None:
    import json

    from eta_engine.scripts import workspace_roots

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(workspace_roots, "ETA_JARVIS_VERDICTS_PATH", tmp_path / "missing_verdicts.jsonl")
    monkeypatch.setattr(
        workspace_roots,
        "ETA_LEGACY_JARVIS_VERDICTS_PATH",
        tmp_path / "missing_legacy_verdicts.jsonl",
    )
    bot_dir = tmp_path / "bots" / "mnq_futures_sage"
    bot_dir.mkdir(parents=True)
    (bot_dir / "status.json").write_text(
        json.dumps(
            {
                "name": "mnq_futures_sage",
                "symbol": "MNQ1",
                "last_aggregation_reject_reason": "daily_kill_switch:day_pnl=-925.50 <= limit=-900.00",
                "last_aggregation_reject_at": "2026-05-15T12:43:05+00:00",
            }
        ),
        encoding="utf-8",
    )

    r = client.get("/api/bot-fleet/mnq_futures_sage")

    assert r.status_code == 200
    body = r.json()
    assert body["recent_verdicts"] == []
    assert body["current_block_source"] == "aggregation"
    assert body["current_block_kind"] == "daily_kill_switch"
    assert body["current_block_reason"] == "daily_kill_switch:day_pnl=-925.50 <= limit=-900.00"
    assert body["current_block_summary"] == "Entries halted by daily kill switch: day_pnl=-925.50 <= limit=-900.00"
    assert body["current_block_at"] == "2026-05-15T12:43:05+00:00"


def test_bot_fleet_drilldown_surfaces_supervisor_block_summary(
    client,
    tmp_path,
    monkeypatch,
) -> None:
    import json
    import time

    from eta_engine.deploy.scripts import dashboard_api
    from eta_engine.scripts import workspace_roots

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(workspace_roots, "ETA_JARVIS_VERDICTS_PATH", tmp_path / "missing_verdicts.jsonl")
    monkeypatch.setattr(
        workspace_roots,
        "ETA_LEGACY_JARVIS_VERDICTS_PATH",
        tmp_path / "missing_legacy_verdicts.jsonl",
    )
    bot_dir = tmp_path / "bots" / "volume_profile_nq"
    bot_dir.mkdir(parents=True)
    (bot_dir / "status.json").write_text(
        json.dumps({"name": "volume_profile_nq", "symbol": "NQ1"}),
        encoding="utf-8",
    )
    supervisor_row = dashboard_api._sup_bot_to_roster_row(
        {
            "id": "volume_profile_nq",
            "name": "volume_profile_nq",
            "symbol": "NQ1",
            "status": "idle",
            "strategy": "confluence_scorecard",
            "heartbeat_ts": "2026-05-15T12:57:54+00:00",
            "last_bar_ts": "2026-05-15T12:57:52+00:00",
            "last_aggregation_reject_reason": "session_gate:outside_rth",
            "last_aggregation_reject_at": "2026-05-15T12:57:52+00:00",
        },
        time.time(),
    )
    monkeypatch.setattr(
        dashboard_api,
        "_supervisor_roster_rows",
        lambda now_ts, bot=None: [supervisor_row] if bot in (None, "volume_profile_nq") else [],
    )

    r = client.get("/api/bot-fleet/volume_profile_nq")

    assert r.status_code == 200
    body = r.json()
    assert body["current_block_source"] == "aggregation"
    assert body["current_block_kind"] == "session_gate"
    assert body["current_block_reason"] == "session_gate:outside_rth"
    assert body["current_block_summary"] == "Entries paused by session gate: outside_rth"
    assert body["current_block_at"] == "2026-05-15T12:57:52+00:00"


def test_bot_fleet_drilldown_rejects_conflicting_verdict_request_id(client, tmp_path, monkeypatch) -> None:
    import json

    from eta_engine.scripts import workspace_roots

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    bot_dir = tmp_path / "bots" / "mnq_futures_sage"
    bot_dir.mkdir(parents=True)
    (bot_dir / "status.json").write_text(json.dumps({"name": "mnq_futures_sage", "symbol": "MNQ1"}), encoding="utf-8")
    verdict_log = tmp_path / "jarvis_intel" / "verdicts.jsonl"
    verdict_log.parent.mkdir(parents=True, exist_ok=True)
    verdict_log.write_text(
        json.dumps(
            {
                "ts": "2026-05-15T12:28:24+00:00",
                "request_id": "mbt_funding_basis_4ce0d708",
                "subsystem": "bot.mnq",
                "final_verdict": "APPROVED",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(workspace_roots, "ETA_JARVIS_VERDICTS_PATH", verdict_log)
    monkeypatch.setattr(workspace_roots, "ETA_LEGACY_JARVIS_VERDICTS_PATH", tmp_path / "missing.jsonl")

    r = client.get("/api/bot-fleet/mnq_futures_sage")

    assert r.status_code == 200
    assert r.json()["recent_verdicts"] == []


def test_bot_fleet_drilldown_unknown_bot(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/bot-fleet/no-such-bot")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_warning") == "no_data"
    assert body["recent_fills"] == []
    assert body["recent_verdicts"] == []


def test_risk_gates_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/risk_gates")
    assert r.status_code == 200
    body = r.json()
    assert body["bots"] == []
    assert body["fleet_aggregate"].get("_warning") == "no_data"


def test_risk_gates_assembles(client, tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    safety = tmp_path / "safety"
    safety.mkdir()
    (safety / "kill_switch_latch.json").write_text(
        json.dumps(
            {
                "mnq": {"latch_state": "armed"},
                "btc_hybrid": {"latch_state": "tripped", "reason": "dd_kill"},
            }
        ),
        encoding="utf-8",
    )
    (safety / "fleet_risk_gate_state.json").write_text(
        json.dumps(
            {
                "fleet_dd_pct": 1.2,
                "fleet_dd_threshold_pct": 5.0,
            }
        ),
        encoding="utf-8",
    )
    r = client.get("/api/risk_gates")
    assert r.status_code == 200
    body = r.json()
    bot_states = {b["bot_id"]: b for b in body["bots"]}
    assert bot_states["mnq"]["latch_state"] == "armed"
    assert bot_states["btc_hybrid"]["latch_state"] == "tripped"
    assert body["fleet_aggregate"]["fleet_dd_pct"] == 1.2


def test_position_reconciler_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/positions/reconciler")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_warning") == "no_data"


def test_position_reconciler_returns_drift(client, tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    safety = tmp_path / "safety"
    safety.mkdir()
    (safety / "position_reconciler_latest.json").write_text(
        json.dumps(
            {
                "ts": "2026-04-27T14:00:00Z",
                "drifts": [{"bot": "mnq", "internal_qty": 1, "broker_qty": 0}],
            }
        ),
        encoding="utf-8",
    )
    r = client.get("/api/positions/reconciler")
    assert r.status_code == 200
    body = r.json()
    assert len(body["drifts"]) == 1
    assert body["drifts"][0]["bot"] == "mnq"


def test_equity_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/equity")
    assert r.status_code == 200
    assert r.json().get("_warning") == "no_data"


def test_equity_returns_curve(client, tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    blot = tmp_path / "blotter"
    blot.mkdir()
    (blot / "equity_curve.json").write_text(
        json.dumps(
            {
                "today": [{"ts": "...", "equity": 50000.0}],
                "thirty_day": [{"ts": "...", "equity": 49500.0}],
            }
        ),
        encoding="utf-8",
    )
    r = client.get("/api/equity")
    assert r.status_code == 200
    assert "today" in r.json()


def test_equity_default_returns_today(client, tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    blot = tmp_path / "blotter"
    blot.mkdir()
    (blot / "equity_curve.json").write_text(
        json.dumps(
            {
                "today": [{"ts": "2026-04-28T00:00Z", "equity": 50000}, {"ts": "2026-04-28T03:00Z", "equity": 50150}],
                "week": [{"ts": "2026-04-21", "equity": 49500}],
                "month": [{"ts": "2026-03-28", "equity": 48000}],
            }
        ),
        encoding="utf-8",
    )
    r = client.get("/api/equity")
    assert r.status_code == 200
    body = r.json()
    assert body["range"] == "1d"
    assert len(body["series"]) == 2
    assert body["summary"]["current_equity"] == 50150
    assert body["summary"]["today_pnl"] == 150


def test_equity_per_bot(client, tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    bot_dir = tmp_path / "bots" / "mnq"
    bot_dir.mkdir(parents=True)
    (bot_dir / "equity_curve.json").write_text(
        json.dumps(
            {
                "today": [{"ts": "2026-04-28T00:00Z", "equity": 12000}, {"ts": "2026-04-28T03:00Z", "equity": 12150}],
            }
        ),
        encoding="utf-8",
    )
    r = client.get("/api/equity?bot=mnq")
    body = r.json()
    assert body["bot_id"] == "mnq"
    assert body["summary"]["current_equity"] == 12150


def test_equity_invalid_range_returns_400(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/equity?range=lifetime")
    assert r.status_code == 400


def test_equity_invalid_bot_returns_400(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/equity?bot=../../etc/passwd")
    assert r.status_code == 400


def test_equity_bot_with_no_data_returns_200_with_warning(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/equity?bot=mnq")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_warning") == "no_data"
    assert body["series"] == []


def test_preflight_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/preflight")
    assert r.status_code == 200
    body = r.json()
    assert "throttles" in body
    assert body["throttles"] == []


def test_preflight_with_throttles(client, tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    safety = tmp_path / "safety"
    safety.mkdir()
    (safety / "preflight_correlation_latest.json").write_text(
        json.dumps(
            {
                "throttles": [
                    {"symbol_a": "MNQ", "symbol_b": "NQ", "cap_mult": 0.50, "rho": 0.95},
                ],
            }
        ),
        encoding="utf-8",
    )
    r = client.get("/api/preflight")
    body = r.json()
    assert len(body["throttles"]) == 1


def test_sage_modulation_stats_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/sage_modulation_stats")
    assert r.status_code == 200
    body = r.json()
    assert body["per_bot"] == {}


def test_sage_modulation_toggle_get_default_off(client, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ETA_FF_V22_SAGE_MODULATION", raising=False)
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/sage_modulation_toggle")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_sage_modulation_toggle_get_when_on(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_FF_V22_SAGE_MODULATION", "true")
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/sage_modulation_toggle")
    assert r.json()["enabled"] is True


def test_sage_modulation_toggle_post_requires_step_up(client, tmp_path, monkeypatch) -> None:
    """POST without step-up cookie returns 401 or 403."""
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    r = client.post("/api/jarvis/sage_modulation_toggle", json={"enabled": True})
    # Without session: 401; without step-up: 403; both are "blocked"
    assert r.status_code in (401, 403)


# ────────────────────────────────────────────────────────────────────
# 2026-05-13: Prop Firm Dashboard endpoints + Market Data Status
# ────────────────────────────────────────────────────────────────────


def test_api_prop_accounts_returns_active_only_by_default(client) -> None:
    """GET /api/prop/accounts returns only ACTIVE_ACCOUNTS by default."""
    r = client.get("/api/prop/accounts")
    assert r.status_code == 200
    d = r.json()
    aids = {a["account_id"] for a in d.get("accounts", [])}
    assert aids == {"paper-test", "blusky-50K-launch"}
    assert d.get("include_inactive") is False


def test_api_prop_accounts_include_inactive(client) -> None:
    """?include_inactive=true returns every REGISTRY entry."""
    r = client.get("/api/prop/accounts?include_inactive=true")
    assert r.status_code == 200
    d = r.json()
    aids = {a["account_id"] for a in d.get("accounts", [])}
    # Must include the 4 dormant accounts plus the 2 active ones
    assert {"apex-50K-eval", "apex-50K-funded", "topstep-50K", "etf-50K"}.issubset(aids)
    assert "paper-test" in aids
    assert "blusky-50K-launch" in aids
    assert d.get("include_inactive") is True


def test_api_prop_snapshot_returns_active_only_by_default(client) -> None:
    """Default snapshot lists only ACTIVE_ACCOUNTS."""
    r = client.get("/api/prop/snapshot")
    assert r.status_code == 200
    d = r.json()
    aids = {a["rules"]["account_id"] for a in d.get("accounts", [])}
    assert aids == {"paper-test", "blusky-50K-launch"}


def test_api_prop_snapshot_one_known_account(client) -> None:
    """Single-account snapshot returns the full breakdown."""
    r = client.get("/api/prop/snapshot/blusky-50K-launch")
    assert r.status_code == 200
    d = r.json()
    snap = d.get("snapshot")
    assert snap is not None
    assert snap["rules"]["account_id"] == "blusky-50K-launch"
    assert snap["rules"]["starting_balance"] == 50_000.0
    assert snap["rules"]["daily_loss_limit"] == 1_500.0
    assert "severity" in snap


def test_api_prop_snapshot_one_unknown_account(client) -> None:
    """Unknown account_id returns an error payload, not a 500."""
    r = client.get("/api/prop/snapshot/does-not-exist")
    assert r.status_code == 200
    d = r.json()
    assert "error" in d
    assert "unknown" in d["error"].lower()


def test_api_data_status_shape(client) -> None:
    """/api/data/status returns the expected schema regardless of inventory state."""
    r = client.get("/api/data/status")
    assert r.status_code == 200
    d = r.json()
    assert "asof" in d
    assert "catalog" in d
    assert "live_signals" in d
    assert "capture_tasks" in d
    assert d.get("schema_version") == 1
    # capture_tasks is a list (may be empty if schtasks not available)
    assert isinstance(d["capture_tasks"], list)


def test_prop_page_html_renders(client) -> None:
    """GET /prop returns HTML with the Market Data Pipeline section."""
    r = client.get("/prop")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    body = r.text
    # The HTML must reference the data status endpoint and the
    # Market Data Pipeline section so an operator sees both panels.
    assert "/api/prop/snapshot" in body
    assert "Market Data Pipeline" in body or "/api/data/status" in body

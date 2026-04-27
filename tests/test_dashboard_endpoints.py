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
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/governor")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_warning") == "no_data"


def test_governor_returns_data_when_state_present(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    gov = tmp_path / "jarvis_governor.json"
    gov.write_text('{"grade":"A","score":0.92}', encoding="utf-8")
    r = client.get("/api/jarvis/governor")
    assert r.status_code == 200
    assert r.json()["grade"] == "A"


def test_edge_leaderboard_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/edge_leaderboard")
    assert r.status_code == 200
    body = r.json()
    assert "top" in body and "bottom" in body
    assert body["top"] == [] and body["bottom"] == []


def test_edge_leaderboard_with_data(client, tmp_path, monkeypatch) -> None:
    import json
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    edge = tmp_path / "sage" / "edge_tracker.json"
    edge.parent.mkdir(parents=True)
    edge.write_text(json.dumps({
        "schools": {
            "dow_theory": {"n_obs": 50, "n_aligned_wins": 35, "n_aligned_losses": 10, "sum_r": 12.5},
            "fibonacci":  {"n_obs": 50, "n_aligned_wins": 10, "n_aligned_losses": 35, "sum_r": -8.0},
        }
    }), encoding="utf-8")
    r = client.get("/api/jarvis/edge_leaderboard")
    assert r.status_code == 200
    body = r.json()
    assert any(s["school"] == "dow_theory" for s in body["top"])
    assert any(s["school"] == "fibonacci" for s in body["bottom"])


def test_model_tier_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/model_tier")
    assert r.status_code == 200
    assert r.json().get("_warning") == "no_data"


def test_kaizen_latest_cold_start(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    r = client.get("/api/jarvis/kaizen_latest")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_warning") == "no_data"


def test_kaizen_latest_returns_markdown(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
    tickets = tmp_path / "kaizen" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "2026-04-26_TKT-001.md").write_text("# Ticket 001\nbody", encoding="utf-8")
    r = client.get("/api/jarvis/kaizen_latest")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Ticket 001"
    assert "body" in body["markdown"]

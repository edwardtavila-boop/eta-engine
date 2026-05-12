"""Tests for jarvis_status_server - the operator-facing contact point."""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from contextlib import contextmanager
from http.server import HTTPServer
from typing import Any


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _server(host: str = "127.0.0.1") -> Any:
    """Spin a status server on an ephemeral port in a daemon thread."""
    from eta_engine.scripts import jarvis_status_server

    port = _free_port()
    srv = HTTPServer((host, port), jarvis_status_server._Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # Give the listener a moment to bind
        time.sleep(0.05)
        yield host, port
    finally:
        srv.shutdown()


def _get_text(host: str, port: int, path: str) -> tuple[int, str]:
    req = urllib.request.Request(f"http://{host}:{port}{path}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _get_json(host: str, port: int, path: str) -> tuple[int, Any]:
    status, body = _get_text(host, port, path)
    return status, json.loads(body)


def test_root_returns_html(monkeypatch) -> None:
    """GET / returns operator-facing HTML."""
    from eta_engine.scripts import jarvis_status_server
    monkeypatch.setattr(jarvis_status_server, "_try_zeus_snapshot", lambda: {})
    monkeypatch.setattr(jarvis_status_server, "_try_tool_list", lambda: [])

    with _server() as (host, port):
        status, body = _get_text(host, port, "/")
    assert status == 200
    assert "<!DOCTYPE html>" in body
    assert "Hermes" in body
    assert "JARVIS" in body


def test_health_returns_ok_json() -> None:
    """GET /health returns simple alive check."""
    with _server() as (host, port):
        status, payload = _get_json(host, port, "/health")
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["service"] == "jarvis_status_server"


def test_contact_returns_addresses(monkeypatch) -> None:
    """GET /contact returns the full operator contact card."""
    from eta_engine.scripts import jarvis_status_server
    monkeypatch.setattr(jarvis_status_server, "_try_tool_list",
                        lambda: ["jarvis_fleet_status", "jarvis_zeus"])

    with _server() as (host, port):
        status, payload = _get_json(host, port, "/contact")
    assert status == 200
    assert payload["platform"] == "Hermes-JARVIS Brain-OS"
    assert "addresses" in payload
    assert "hermes_api" in payload["addresses"]
    assert "tunnel_command" in payload["addresses"]
    assert payload["available_tools_count"] == 2


def test_status_returns_zeus_summary(monkeypatch) -> None:
    """GET /status returns the cached zeus snapshot."""
    from eta_engine.scripts import jarvis_status_server

    snap = {
        "asof": "2026-05-12T22:00:00+00:00",
        "fleet_status": {"n_bots": 48, "tier_counts": {"ELITE": 6}},
        "regime": {"regime": "CALM_TREND", "confidence": 0.7},
    }
    monkeypatch.setattr(jarvis_status_server, "_try_zeus_snapshot", lambda: snap)
    monkeypatch.setattr(jarvis_status_server, "_try_tool_list", lambda: [])
    # Bust the cache
    jarvis_status_server._SNAPSHOT_CACHE["asof"] = 0.0
    jarvis_status_server._SNAPSHOT_CACHE["data"] = None

    with _server() as (host, port):
        status, payload = _get_json(host, port, "/status")
    assert status == 200
    assert payload["zeus"]["fleet_status"]["n_bots"] == 48
    assert payload["zeus"]["regime"]["regime"] == "CALM_TREND"


def test_tools_returns_categorized_list(monkeypatch) -> None:
    """GET /tools returns tools grouped by category."""
    from eta_engine.scripts import jarvis_status_server

    tools = [
        "jarvis_fleet_status",          # read
        "jarvis_set_size_modifier",     # write
        "jarvis_kill_switch",            # destructive
        "jarvis_attribution_cube",       # analytics
        "jarvis_register_agent",         # coordination
        "jarvis_cost_today",             # telemetry
        "jarvis_zeus",                   # unified
    ]
    monkeypatch.setattr(jarvis_status_server, "_try_tool_list", lambda: tools)

    with _server() as (host, port):
        status, payload = _get_json(host, port, "/tools")
    assert status == 200
    assert payload["total"] == 7
    assert "jarvis_fleet_status" in payload["by_category"]["read"]
    assert "jarvis_set_size_modifier" in payload["by_category"]["write"]
    assert "jarvis_kill_switch" in payload["by_category"]["destructive"]
    assert "jarvis_attribution_cube" in payload["by_category"]["analytics"]
    assert "jarvis_register_agent" in payload["by_category"]["coordination"]
    assert "jarvis_cost_today" in payload["by_category"]["telemetry"]
    assert "jarvis_zeus" in payload["by_category"]["unified"]


def test_unknown_path_returns_404() -> None:
    """GET /this-route-does-not-exist returns 404."""
    with _server() as (host, port):
        try:
            urllib.request.urlopen(f"http://{host}:{port}/no-such-route",
                                   timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            return
    raise AssertionError("expected HTTPError for unknown path")


def test_snapshot_cache_within_ttl(monkeypatch) -> None:
    """Two requests within the cache window only call zeus once."""
    from eta_engine.scripts import jarvis_status_server

    call_count = {"n": 0}

    def counted_snap():
        call_count["n"] += 1
        return {"fleet_status": {"n_bots": call_count["n"]}}

    monkeypatch.setattr(jarvis_status_server, "_try_zeus_snapshot", counted_snap)
    # Reset cache
    jarvis_status_server._SNAPSHOT_CACHE["asof"] = 0.0
    jarvis_status_server._SNAPSHOT_CACHE["data"] = None

    with _server() as (host, port):
        _get_json(host, port, "/status")
        _get_json(host, port, "/status")
        _get_json(host, port, "/status")
    # 3 requests but only 1 zeus call (cache TTL covers them all)
    assert call_count["n"] == 1


def test_handler_never_raises_on_internal_error(monkeypatch) -> None:
    """If snapshot fetch raises unexpectedly, we still respond with 500."""
    from eta_engine.scripts import jarvis_status_server

    def boom():
        raise RuntimeError("simulated zeus failure")

    monkeypatch.setattr(jarvis_status_server, "_cached_snapshot", boom)

    with _server() as (host, port):
        try:
            urllib.request.urlopen(f"http://{host}:{port}/status", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 500
            return
    raise AssertionError("expected HTTPError when handler internals fail")


def test_categorize_tools_handles_unknown_tools() -> None:
    """Tools not in any predefined set fall into the 'read' bucket."""
    from eta_engine.scripts import jarvis_status_server

    cats = jarvis_status_server._categorize_tools([
        "jarvis_brand_new_tool",  # unknown
        "jarvis_zeus",             # unified
    ])
    assert "jarvis_brand_new_tool" in cats["read"]
    assert "jarvis_zeus" in cats["unified"]


def test_contact_card_includes_skills() -> None:
    """contact card lists all known operator-facing skills."""
    from eta_engine.scripts import jarvis_status_server

    card = jarvis_status_server._contact_card()
    skills = card["available_skills"]
    assert "jarvis-zeus" in skills
    assert "jarvis-daily-review" in skills
    assert "jarvis-council" in skills


def test_html_renders_alive_dots_when_data_present(monkeypatch) -> None:
    """When zeus + tools both return data, dots render green."""
    from eta_engine.scripts import jarvis_status_server

    snap = {"fleet_status": {"n_bots": 1}, "regime": {"regime": "CALM_TREND"}}
    html = jarvis_status_server._render_html(snap, ["jarvis_fleet_status"], 1.0)
    assert "dot-green" in html
    assert "CALM_TREND" in html


def test_html_renders_yellow_dots_when_data_absent() -> None:
    """When both sub-systems return empty, dots are yellow not red."""
    from eta_engine.scripts import jarvis_status_server

    html = jarvis_status_server._render_html({}, [], 0.0)
    assert "dot-yellow" in html


def test_serve_binds_to_specified_port(monkeypatch) -> None:
    """serve() with an explicit port binds and is shutdown-able."""
    from eta_engine.scripts import jarvis_status_server

    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), jarvis_status_server._Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.05)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=5,
        ) as resp:
            assert resp.status == 200
    finally:
        srv.shutdown()

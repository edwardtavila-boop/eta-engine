"""Tests for hermes_bridge_health — the layered health-check script."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class _MockHermesHandler(BaseHTTPRequestHandler):
    """In-process Hermes API mock for layer-2/3/4 probing."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — silence test noise
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — http.server contract
        if self.path == "/health":
            self._send_json({"status": "ok", "platform": "hermes-agent"})
        elif self.path == "/v1/models":
            if self.headers.get("Authorization") != "Bearer test-key":
                self._send_json({"error": "auth"}, status=401)
                return
            self._send_json({"data": [{"id": "deepseek-v4-pro"}]})
        else:
            self._send_json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        # Echo a basic OK to /v1/chat/completions so layer 4 passes
        if self.path == "/v1/chat/completions":
            if self.headers.get("Authorization") != "Bearer test-key":
                self._send_json({"error": "auth"}, status=401)
                return
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self._send_json(
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": "pong"}},
                    ],
                }
            )
        else:
            self._send_json({"error": "not_found"}, status=404)


def _start_mock_server() -> tuple[HTTPServer, int]:
    # Bind to an ephemeral port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = HTTPServer(("127.0.0.1", port), _MockHermesHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def test_probe_tunnel_succeeds_against_listening_port() -> None:
    """When the port is open, probe_tunnel returns ok=True."""
    from eta_engine.scripts import hermes_bridge_health

    server, port = _start_mock_server()
    try:
        ok, detail, _ = hermes_bridge_health.probe_tunnel("127.0.0.1", port)
        assert ok
        assert "connected" in detail
    finally:
        server.shutdown()


def test_probe_tunnel_fails_against_closed_port() -> None:
    """No listener → ok=False, no exception."""
    from eta_engine.scripts import hermes_bridge_health

    # Pick a port unlikely to be open
    ok, detail, _ = hermes_bridge_health.probe_tunnel("127.0.0.1", 1)
    assert not ok
    assert "cannot connect" in detail


def test_probe_gateway_returns_ok_on_200() -> None:
    """Mock /health returns 200 → probe passes."""
    from eta_engine.scripts import hermes_bridge_health

    server, port = _start_mock_server()
    try:
        ok, detail, _ = hermes_bridge_health.probe_gateway("127.0.0.1", port, api_key=None)
        assert ok, f"got: {detail}"
    finally:
        server.shutdown()


def test_probe_auth_passes_with_correct_key_fails_without() -> None:
    """Auth header is checked by the mock; correct key → 200 → ok."""
    from eta_engine.scripts import hermes_bridge_health

    server, port = _start_mock_server()
    try:
        ok_with_key, _, _ = hermes_bridge_health.probe_auth(
            "127.0.0.1",
            port,
            api_key="test-key",
        )
        ok_without, _, _ = hermes_bridge_health.probe_auth(
            "127.0.0.1",
            port,
            api_key=None,
        )
        assert ok_with_key
        assert not ok_without
    finally:
        server.shutdown()


def test_probe_audit_passes_when_log_missing(tmp_path: Path) -> None:
    """Fresh install (no audit log yet) → status pass with a hint."""
    from eta_engine.scripts import hermes_bridge_health

    missing = tmp_path / "no_log_here.jsonl"
    ok, detail, _ = hermes_bridge_health.probe_audit(missing)
    assert ok
    assert "no audit log" in detail


def test_probe_audit_validates_jsonl(tmp_path: Path) -> None:
    """A few well-formed JSON lines → pass."""
    from eta_engine.scripts import hermes_bridge_health

    log = tmp_path / "audit.jsonl"
    with log.open("w", encoding="utf-8") as fh:
        for i in range(5):
            fh.write(json.dumps({"i": i, "tool": "smoke"}) + "\n")
    ok, _, extras = hermes_bridge_health.probe_audit(log)
    assert ok
    assert extras["size"] > 0


def test_probe_audit_detects_garbage(tmp_path: Path) -> None:
    """Last 10 lines fail to parse → ok=False."""
    from eta_engine.scripts import hermes_bridge_health

    log = tmp_path / "audit.jsonl"
    log.write_text("garbage garbage\n" * 50, encoding="utf-8")
    ok, _, _ = hermes_bridge_health.probe_audit(log)
    assert not ok


def test_probe_memory_db_handles_missing(tmp_path: Path) -> None:
    """Missing memory DB on fresh install → pass with hint."""
    from eta_engine.scripts import hermes_bridge_health

    missing = tmp_path / "no.db"
    ok, detail, _ = hermes_bridge_health.probe_memory_db(missing)
    assert ok
    assert "fresh install" in detail


def test_probe_memory_db_detects_corrupt(tmp_path: Path) -> None:
    """A non-SQLite file → ok=False."""
    from eta_engine.scripts import hermes_bridge_health

    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not sqlite")
    ok, _, _ = hermes_bridge_health.probe_memory_db(bad)
    assert not ok


def test_probe_with_timing_catches_exceptions() -> None:
    """If a probe raises, the wrapper turns it into ok=False, no propagation."""
    from eta_engine.scripts import hermes_bridge_health

    def explode():
        raise RuntimeError("simulated probe crash")

    r = hermes_bridge_health._probe_with_timing("synthetic", explode)
    assert r.name == "synthetic"
    assert r.ok is False
    assert "simulated probe crash" in r.detail
    assert r.elapsed_ms >= 0


def test_main_returns_nonzero_when_any_layer_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """End-to-end: any failing layer → exit code 1."""
    from eta_engine.scripts import hermes_bridge_health

    # Force tunnel to a port nothing's listening on so the run reports failures
    rc = hermes_bridge_health.main(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "1",
            "--audit-path",
            str(tmp_path / "no_audit.jsonl"),
            "--memory-db",
            str(tmp_path / "no_mem.db"),
            "--skip",
            "llm,jarvis_mcp,memory,overrides",  # skip layers that need a real gateway
        ]
    )
    captured = capsys.readouterr()
    assert "HERMES-JARVIS BRIDGE HEALTH" in captured.out
    # Tunnel + gateway + auth probes WILL fail against port 1 → rc=1
    assert rc == 1


def test_main_json_output(tmp_path: Path, capsys) -> None:
    """--json mode emits a parseable JSON document."""
    from eta_engine.scripts import hermes_bridge_health

    rc = hermes_bridge_health.main(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "1",
            "--audit-path",
            str(tmp_path / "no_audit.jsonl"),
            "--memory-db",
            str(tmp_path / "no_mem.db"),
            "--skip",
            "llm,jarvis_mcp,memory,overrides",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "results" in payload
    assert "all_ok" in payload
    assert isinstance(payload["results"], list)
    assert rc == (0 if payload["all_ok"] else 1)

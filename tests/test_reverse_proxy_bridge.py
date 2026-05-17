from __future__ import annotations

import http.server
import json
import threading
import urllib.error
import urllib.request

from eta_engine.deploy.scripts.reverse_proxy_bridge import (
    _BridgeHTTPServer,
    _is_expected_disconnect_exception,
    build_handler,
)


class _Origin(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
        payload = json.dumps({"path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler naming
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.send_response(201)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_reverse_proxy_forwards_get_with_query() -> None:
    origin = _serve(_Origin)
    proxy = _serve(
        build_handler(
            target=f"http://127.0.0.1:{origin.server_port}",
            timeout=3,
        )
    )

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{proxy.server_port}/health?probe=1", timeout=3) as response:
            assert response.status == 200
            payload = json.loads(response.read().decode())
        assert payload == {"path": "/health?probe=1"}
    finally:
        proxy.shutdown()
        origin.shutdown()


def test_reverse_proxy_forwards_post_body() -> None:
    origin = _serve(_Origin)
    proxy = _serve(
        build_handler(
            target=f"http://127.0.0.1:{origin.server_port}",
            timeout=3,
        )
    )

    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/submit",
            data=b"paper-live",
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 201
            assert response.read() == b"paper-live"
    finally:
        proxy.shutdown()
        origin.shutdown()


def test_reverse_proxy_returns_bad_gateway_when_origin_down() -> None:
    proxy = _serve(build_handler(target="http://127.0.0.1:9", timeout=0.2))

    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{proxy.server_port}/health", timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 502
        else:
            raise AssertionError("expected 502 from bridge")
    finally:
        proxy.shutdown()


def test_expected_disconnect_exception_classifier() -> None:
    assert _is_expected_disconnect_exception(ConnectionResetError(10054, "reset"))
    assert _is_expected_disconnect_exception(BrokenPipeError(32, "broken pipe"))
    assert _is_expected_disconnect_exception(RuntimeError("boom")) is False


def test_bridge_server_suppresses_expected_disconnect_errors(monkeypatch) -> None:
    server = _BridgeHTTPServer(("127.0.0.1", 0), build_handler(target="http://127.0.0.1:9", timeout=0.2))
    called: list[tuple[object, tuple[str, int]]] = []

    def _record_super(self: http.server.ThreadingHTTPServer, request: object, client_address: tuple[str, int]) -> None:
        called.append((request, client_address))

    monkeypatch.setattr(http.server.ThreadingHTTPServer, "handle_error", _record_super)
    try:
        monkeypatch.setattr(
            "eta_engine.deploy.scripts.reverse_proxy_bridge.sys.exc_info",
            lambda: (ConnectionResetError, ConnectionResetError(10054, "reset"), None),
        )
        server.handle_error(object(), ("127.0.0.1", 1234))
        assert called == []

        monkeypatch.setattr(
            "eta_engine.deploy.scripts.reverse_proxy_bridge.sys.exc_info",
            lambda: (RuntimeError, RuntimeError("boom"), None),
        )
        server.handle_error("request", ("127.0.0.1", 4321))
        assert called == [("request", ("127.0.0.1", 4321))]
    finally:
        server.server_close()

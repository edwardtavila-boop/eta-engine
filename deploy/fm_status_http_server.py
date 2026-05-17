"""Stdlib HTTP server for the Force Multiplier status contract.

WinSW runs this entrypoint so the service does not depend on user-scoped
FastAPI/uvicorn packages.
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from eta_engine.deploy.fm_status_payload import build_status_payload


class ForceMultiplierStatusHandler(BaseHTTPRequestHandler):
    server_version = "EtaFmStatus/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
        if self.path.split("?", 1)[0] == "/api/fm/status":
            self._send_json(build_status_payload())
            return
        if self.path.split("?", 1)[0] in {"/health", "/healthz"}:
            self._send_json({"status": "ok"})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve ETA Force Multiplier status over HTTP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8422)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), ForceMultiplierStatusHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

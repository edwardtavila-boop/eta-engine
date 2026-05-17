"""Small stdlib reverse proxy for local VPS compatibility bridges."""

from __future__ import annotations

import argparse
import http.server
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _join_target(target: str, path: str) -> str:
    base = target.rstrip("/") + "/"
    return urllib.parse.urljoin(base, path.lstrip("/"))


def _forwardable_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for key, value in headers:
        if key.lower() in HOP_BY_HOP_HEADERS or key.lower() == "host":
            continue
        forwarded[key] = value
    return forwarded


def _is_expected_disconnect_exception(exc: BaseException | None) -> bool:
    return isinstance(exc, (BrokenPipeError, ConnectionResetError))


def build_handler(target: str, timeout: float) -> type[http.server.BaseHTTPRequestHandler]:
    """Create a request handler bound to a target origin."""

    class ReverseProxyHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler naming
            self._proxy(send_body=False)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
            self._proxy(send_body=True)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler naming
            self._proxy(send_body=True)

        def do_PUT(self) -> None:  # noqa: N802 - stdlib handler naming
            self._proxy(send_body=True)

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler naming
            self._proxy(send_body=True)

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler naming
            self._proxy(send_body=True)

        def _proxy(self, *, send_body: bool) -> None:
            body = None
            length = self.headers.get("Content-Length")
            if length:
                body = self.rfile.read(int(length))

            url = _join_target(target, self.path)
            headers = _forwardable_headers(self.headers.items())
            headers["X-Forwarded-Host"] = self.headers.get("Host", "")
            headers["X-Forwarded-Proto"] = "http"

            request = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method=self.command,
            )

            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    self._send_response(response.status, response.getheaders(), response.read(), send_body)
            except urllib.error.HTTPError as exc:
                self._send_response(exc.code, exc.headers.items(), exc.read(), send_body)
            except Exception as exc:  # noqa: BLE001 - this is the edge bridge fallback.
                self.send_error(502, "Bad Gateway", explain=str(exc))

        def _send_response(
            self,
            status: int,
            headers: Iterable[tuple[str, str]],
            body: bytes,
            send_body: bool,
        ) -> None:
            self.send_response(status)
            for key, value in headers:
                if key.lower() in HOP_BY_HOP_HEADERS:
                    continue
                if key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body) if send_body else 0))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

    return ReverseProxyHandler


class _BridgeHTTPServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        _exc_type, exc, _tb = sys.exc_info()
        if _is_expected_disconnect_exception(exc):
            return
        super().handle_error(request, client_address)


def run_proxy(*, listen_host: str, listen_port: int, target: str, timeout: float) -> None:
    handler = build_handler(target=target, timeout=timeout)
    server = _BridgeHTTPServer((listen_host, listen_port), handler)
    print(
        f"reverse_proxy_bridge listening on {listen_host}:{listen_port} -> {target}",
        file=sys.stderr,
        flush=True,
    )
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    run_proxy(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        target=args.target,
        timeout=args.timeout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

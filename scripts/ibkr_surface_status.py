"""Report the two IBKR gateway surfaces separately.

The Jarvis supervisor's ``paper_live`` direct-order path uses TWS / IB Gateway's
socket API (ib_insync on port 4002). Several data/readiness sidecars use the
Client Portal Gateway REST API (usually port 5000). Keeping those statuses
separate prevents a missing Client Portal instance from being misreported as
"IBKR unavailable" for the supervisor order route.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import ssl
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
_ETA_RUNTIME_STATE_DIR = _WORKSPACE_ROOT / "var" / "eta_engine" / "state"
_STATUS_PATH = _ETA_RUNTIME_STATE_DIR / "ibkr_surface_status.json"
_TWS_WATCHDOG_PATH = _ETA_RUNTIME_STATE_DIR / "tws_watchdog.json"
_CLIENT_PORTAL_REAUTH_PATH = _ETA_RUNTIME_STATE_DIR / "ibkr_reauth.json"

_DEFAULT_TWS_HOST = "127.0.0.1"
_DEFAULT_TWS_PORT = 4002
_DEFAULT_CLIENT_PORTAL_URL = "https://127.0.0.1:5000"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _check_tcp(host: str, port: int, *, timeout: float = 1.5) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "tcp_connect_ok"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _client_portal_auth_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/api"):
        return f"{base}/iserver/auth/status"
    return f"{base}/v1/api/iserver/auth/status"


def _http_get_json(url: str, *, timeout: float) -> tuple[bool, dict[str, Any], str]:
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, context=ssl_ctx, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", errors="replace")
        detail = f"HTTPError {exc.code}"
        if body:
            detail = f"{detail}: {body[:240]}"
        return True, {}, detail
    except Exception as exc:  # noqa: BLE001 - diagnostics should report any local failure.
        return False, {}, f"{type(exc).__name__}: {exc}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return True, {}, f"non_json_response: {body[:240]}"
    return True, data if isinstance(data, dict) else {}, "http_get_ok"


def _build_tws_surface(
    *,
    host: str,
    port: int,
    watchdog_path: Path,
    timeout: float,
) -> dict[str, Any]:
    tcp_reachable, tcp_detail = _check_tcp(host, port, timeout=timeout)
    watchdog = _read_json(watchdog_path)
    details = watchdog.get("details") if isinstance(watchdog.get("details"), dict) else {}
    watchdog_healthy = watchdog.get("healthy")
    handshake_ok = details.get("handshake_ok") if isinstance(details, dict) else None

    ready = watchdog_healthy is True and handshake_ok is True
    if ready:
        status = "ready"
        confidence = "watchdog_handshake"
    elif tcp_reachable:
        status = "tcp_open_handshake_unknown"
        confidence = "socket_probe"
    else:
        status = "not_ready"
        confidence = "socket_probe"

    return {
        "surface": "tws_api",
        "role": "Required for jarvis_strategy_supervisor paper_live direct orders via ib_insync.",
        "host": host,
        "port": port,
        "required_for": [
            "paper_live supervisor direct order routing",
            "LiveIbkrVenue",
            "TWS/IB Gateway API clients",
        ],
        "ready": ready,
        "status": status,
        "confidence": confidence,
        "tcp_reachable": tcp_reachable,
        "tcp_detail": tcp_detail,
        "watchdog_checked_at": watchdog.get("checked_at"),
        "watchdog_healthy": watchdog_healthy,
        "watchdog_last_healthy_at": watchdog.get("last_healthy_at"),
        "watchdog_consecutive_failures": watchdog.get("consecutive_failures"),
        "handshake_ok": handshake_ok,
        "handshake_detail": details.get("handshake_detail") if isinstance(details, dict) else None,
        "account_summary": (
            details.get("account_snapshot", {}).get("summary")
            if isinstance(details.get("account_snapshot"), dict)
            else None
        ),
    }


def _build_client_portal_surface(
    *,
    base_url: str,
    reauth_path: Path,
    timeout: float,
    enabled: bool,
) -> dict[str, Any]:
    auth_url = _client_portal_auth_url(base_url)
    reauth_state = _read_json(reauth_path)
    if enabled:
        reachable, auth_status, detail = _http_get_json(auth_url, timeout=timeout)
    else:
        reachable, auth_status, detail = False, {}, "skipped"

    authenticated = auth_status.get("authenticated")
    ready = reachable and authenticated is True
    if ready:
        status = "ready"
    elif reachable:
        status = "reachable_not_authenticated"
    elif enabled:
        status = "not_ready"
    else:
        status = "skipped"

    return {
        "surface": "client_portal_rest",
        "role": (
            "Used by Client Portal REST data/readiness/flatten sidecars; "
            "not the supervisor's direct paper_live order route."
        ),
        "base_url": base_url.rstrip("/"),
        "auth_status_url": auth_url,
        "default_port": 5000,
        "required_for": [
            "Client Portal historical/data helpers",
            "Client Portal auth/status checks",
            "external watchdog REST flatten/global-cancel sidecars",
        ],
        "ready": ready,
        "status": status,
        "http_reachable": reachable,
        "authenticated": authenticated,
        "detail": detail,
        "reauth_last_status": reauth_state.get("last_status"),
        "reauth_last_check": reauth_state.get("last_check"),
        "reauth_last_error": reauth_state.get("last_error"),
    }


def _operator_action(tws: dict[str, Any], client_portal: dict[str, Any]) -> str:
    tws_ready = bool(tws.get("ready"))
    client_portal_ready = bool(client_portal.get("ready"))
    if tws_ready and client_portal_ready:
        return "Both IBKR surfaces are ready; paper_live can use TWS 4002 and REST sidecars can use Client Portal 5000."
    if tws_ready:
        return (
            "paper_live order routing is ready through TWS 4002; install or start "
            "Client Portal 5000 only for REST/data sidecars."
        )
    if client_portal_ready:
        return "Client Portal 5000 is ready, but paper_live direct orders still need TWS/IB Gateway API 4002."
    if tws.get("status") == "tcp_open_handshake_unknown":
        return (
            "TWS 4002 accepts TCP, but no fresh API handshake is confirmed; "
            "run tws_watchdog before paper_live promotion."
        )
    return (
        "Keep supervisor in paper_sim until TWS/IB Gateway API 4002 is running "
        "and the watchdog confirms an API handshake."
    )


def build_status(
    *,
    tws_host: str | None = None,
    tws_port: int | None = None,
    client_portal_url: str | None = None,
    timeout: float = 1.5,
    check_client_portal: bool = True,
    tws_watchdog_path: Path = _TWS_WATCHDOG_PATH,
    client_portal_reauth_path: Path = _CLIENT_PORTAL_REAUTH_PATH,
) -> dict[str, Any]:
    host = tws_host or os.environ.get("ETA_IBKR_TWS_HOST", _DEFAULT_TWS_HOST)
    port = tws_port or int(os.environ.get("ETA_IBKR_TWS_PORT", str(_DEFAULT_TWS_PORT)))
    portal_url = client_portal_url or os.environ.get(
        "IBKR_GATEWAY_URL",
        _DEFAULT_CLIENT_PORTAL_URL,
    )

    tws = _build_tws_surface(
        host=host,
        port=port,
        watchdog_path=tws_watchdog_path,
        timeout=timeout,
    )
    client_portal = _build_client_portal_surface(
        base_url=portal_url,
        reauth_path=client_portal_reauth_path,
        timeout=timeout,
        enabled=check_client_portal,
    )
    action = _operator_action(tws, client_portal)

    return {
        "generated_at": _utc_now_iso(),
        "paper_live_required_surface": "tws_api",
        "safe_default_mode": "paper_sim",
        "summary": {
            "paper_live_ready": bool(tws.get("ready")),
            "client_portal_ready": bool(client_portal.get("ready")),
            "operator_action": action,
        },
        "surfaces": {
            "tws_api": tws,
            "client_portal_rest": client_portal,
        },
        "notes": [
            "TWS/IB Gateway socket API on 4002 is the supervisor paper_live direct-order surface.",
            (
                "Client Portal Gateway REST on 5000 is a separate sidecar surface for REST helpers "
                "and should not be used as the sole paper_live readiness signal."
            ),
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tws-host", default=None)
    parser.add_argument("--tws-port", type=int, default=None)
    parser.add_argument("--client-portal-url", default=None)
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--skip-client-portal", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--output", type=Path, default=_STATUS_PATH)
    parser.add_argument("--tws-watchdog-path", type=Path, default=_TWS_WATCHDOG_PATH)
    parser.add_argument("--client-portal-reauth-path", type=Path, default=_CLIENT_PORTAL_REAUTH_PATH)
    args = parser.parse_args(argv)

    status = build_status(
        tws_host=args.tws_host,
        tws_port=args.tws_port,
        client_portal_url=args.client_portal_url,
        timeout=args.timeout,
        check_client_portal=not args.skip_client_portal,
        tws_watchdog_path=args.tws_watchdog_path,
        client_portal_reauth_path=args.client_portal_reauth_path,
    )
    if not args.no_write:
        _write_json(args.output, status)
    print(json.dumps(status, indent=2, default=str))
    return 0 if status["summary"]["paper_live_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

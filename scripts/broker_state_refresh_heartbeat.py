"""Refresh the read-only live broker-state cache for dashboard truth.

This module only calls the local dashboard API's broker-state refresh endpoint.
It never submits, cancels, modifies, flattens, or promotes orders. The goal is
to keep the public dashboard fast while preventing stale cached PnL from being
mistaken for live broker truth.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402

DEFAULT_URLS = (
    "http://127.0.0.1:8421/api/live/broker_state?refresh=1",
    "http://127.0.0.1:8000/api/live/broker_state?refresh=1",
)
DEFAULT_OUT = workspace_roots.ETA_RUNTIME_STATE_DIR / "broker_state_refresh_heartbeat.json"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _fetch_json(url: str, *, timeout_s: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "eta-broker-state-refresh-heartbeat"},
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
        body = response.read(4_000_000).decode("utf-8", errors="replace")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError(f"broker refresh endpoint returned {type(payload).__name__}, expected object")
    return payload


def _compact_success(payload: dict[str, Any], *, endpoint: str, elapsed_ms: float) -> dict[str, Any]:
    ready = bool(payload.get("ready"))
    snapshot_state = str(payload.get("broker_snapshot_state") or "")
    return {
        "schema_version": 1,
        "generated_at_utc": _utc_now(),
        "source": "broker_state_refresh_heartbeat",
        "ok": ready,
        "status": snapshot_state or ("fresh" if ready else "not_ready"),
        "endpoint": endpoint,
        "elapsed_ms": round(elapsed_ms, 1),
        "broker_source": str(payload.get("source") or ""),
        "broker_ready": ready,
        "broker_snapshot_state": snapshot_state,
        "broker_snapshot_age_s": payload.get("broker_snapshot_age_s"),
        "broker_mtd_pnl": payload.get("broker_mtd_pnl"),
        "broker_mtd_return_pct": payload.get("broker_mtd_return_pct"),
        "today_realized_pnl": payload.get("today_realized_pnl"),
        "total_unrealized_pnl": payload.get("total_unrealized_pnl"),
        "open_position_count": payload.get("open_position_count"),
        "today_actual_fills": payload.get("today_actual_fills"),
        "reporting_timezone": str(payload.get("reporting_timezone") or ""),
        "order_action_allowed": False,
        "live_money_gate_bypassed": False,
        "error": "",
    }


def _compact_failure(errors: list[str], *, elapsed_ms: float) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at_utc": _utc_now(),
        "source": "broker_state_refresh_heartbeat",
        "ok": False,
        "status": "failed",
        "endpoint": "",
        "elapsed_ms": round(elapsed_ms, 1),
        "broker_ready": False,
        "order_action_allowed": False,
        "live_money_gate_bypassed": False,
        "error": " | ".join(errors),
    }


def refresh_broker_state(
    *,
    urls: list[str],
    timeout_s: float,
    out_path: Path | None,
) -> dict[str, Any]:
    started = time.monotonic()
    errors: list[str] = []
    for url in urls:
        try:
            payload = _fetch_json(url, timeout_s=timeout_s)
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        heartbeat = _compact_success(payload, endpoint=url, elapsed_ms=(time.monotonic() - started) * 1000)
        break
    else:
        heartbeat = _compact_failure(errors, elapsed_ms=(time.monotonic() - started) * 1000)

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(heartbeat, indent=2, sort_keys=True), encoding="utf-8")
    return heartbeat


def render_text(heartbeat: dict[str, Any]) -> str:
    return (
        "broker_state_refresh "
        f"status={heartbeat.get('status')} ok={heartbeat.get('ok')} "
        f"source={heartbeat.get('broker_source') or heartbeat.get('source')} "
        f"age_s={heartbeat.get('broker_snapshot_age_s')} "
        f"mtd={heartbeat.get('broker_mtd_pnl')} "
        f"today={heartbeat.get('today_realized_pnl')} "
        f"open={heartbeat.get('total_unrealized_pnl')} "
        f"positions={heartbeat.get('open_position_count')} "
        f"endpoint={heartbeat.get('endpoint') or 'none'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="broker_state_refresh_heartbeat")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--timeout-s", type=float, default=12.0)
    parser.add_argument("--url", action="append", default=None, help="endpoint to try; may be repeated")
    parser.add_argument("--json", action="store_true", help="print JSON heartbeat")
    parser.add_argument("--no-write", action="store_true", help="do not write heartbeat artifact")
    args = parser.parse_args(argv)
    if not args.no_write:
        try:
            args.out = workspace_roots.resolve_under_workspace(args.out, label="--out")
        except ValueError as exc:
            parser.error(str(exc))

    heartbeat = refresh_broker_state(
        urls=list(args.url or DEFAULT_URLS),
        timeout_s=max(1.0, args.timeout_s),
        out_path=None if args.no_write else args.out,
    )
    if args.json:
        print(json.dumps(heartbeat, indent=2, sort_keys=True))
    else:
        print(render_text(heartbeat))
    return 0 if heartbeat.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

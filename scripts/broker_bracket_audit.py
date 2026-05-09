"""Read-only broker-native bracket/OCO coverage audit."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from eta_engine.scripts import workspace_roots  # noqa: E402

DEFAULT_FLEET_URL = "https://ops.evolutionarytradingalgo.com/api/bot-fleet"
DEFAULT_OUT = workspace_roots.ETA_BROKER_BRACKET_AUDIT_PATH


def _as_dict(value: Any) -> dict[str, Any]:  # noqa: ANN401
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:  # noqa: ANN401
    return value if isinstance(value, list) else []


def _as_int(value: Any) -> int:  # noqa: ANN401
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _fetch_json(url: str, timeout_s: float = 10.0) -> dict[str, Any]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "eta-broker-bracket-audit"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _derive_position_summary(fleet: dict[str, Any]) -> dict[str, int]:
    target_exit_summary = _as_dict(fleet.get("target_exit_summary"))
    if target_exit_summary:
        return {
            "broker_open_position_count": _as_int(
                target_exit_summary.get("broker_open_position_count"),
            ),
            "broker_bracket_count": _as_int(target_exit_summary.get("broker_bracket_count")),
            "supervisor_local_position_count": _as_int(
                target_exit_summary.get("supervisor_local_position_count"),
            ),
        }

    summary = _as_dict(fleet.get("summary"))
    if summary:
        return {
            "broker_open_position_count": _as_int(summary.get("broker_open_position_count")),
            "broker_bracket_count": _as_int(summary.get("broker_bracket_count")),
            "supervisor_local_position_count": _as_int(summary.get("supervisor_local_position_count")),
        }

    open_count = 0
    bracket_count = 0
    supervisor_local = 0
    for raw_bot in _as_list(fleet.get("bots")):
        bot = _as_dict(raw_bot)
        positions = _as_int(bot.get("open_positions"))
        if positions <= 0:
            continue
        open_count += positions
        if bot.get("broker_bracket"):
            bracket_count += positions
        else:
            supervisor_local += positions
    return {
        "broker_open_position_count": open_count,
        "broker_bracket_count": bracket_count,
        "supervisor_local_position_count": supervisor_local,
    }


def _adapter_support() -> dict[str, Any]:
    support: dict[str, Any] = {
        "ibkr_futures_server_oco": False,
        "alpaca_equity_server_bracket": False,
        "tradovate_order_payload_brackets": False,
    }
    try:
        from eta_engine.venues import ibkr_live  # noqa: PLC0415

        support["ibkr_futures_server_oco"] = hasattr(ibkr_live, "_build_futures_bracket_orders")
    except Exception:  # noqa: BLE001
        pass
    try:
        from eta_engine.venues.alpaca import AlpacaVenue  # noqa: PLC0415

        support["alpaca_equity_server_bracket"] = hasattr(AlpacaVenue, "place_order")
    except Exception:  # noqa: BLE001
        pass
    try:
        # DORMANT broker audit only: this import checks bracket-order
        # support and does not activate Tradovate or submit orders.
        from eta_engine.venues.tradovate import TradovateVenue  # noqa: PLC0415

        support["tradovate_order_payload_brackets"] = hasattr(TradovateVenue, "bracket_order")
    except Exception:  # noqa: BLE001
        pass
    return support


def build_bracket_audit(*, fleet: dict[str, Any] | None = None) -> dict[str, Any]:
    fleet = fleet or _fetch_json(DEFAULT_FLEET_URL)
    position_summary = _derive_position_summary(fleet)
    open_count = position_summary["broker_open_position_count"]
    bracket_count = position_summary["broker_bracket_count"]
    supervisor_local = position_summary["supervisor_local_position_count"]
    adapter_support = _adapter_support()
    adapter_ok = bool(adapter_support.get("ibkr_futures_server_oco")) and bool(
        adapter_support.get("tradovate_order_payload_brackets"),
    )

    if open_count == 0 and supervisor_local == 0 and adapter_ok:
        summary = "READY_NO_OPEN_EXPOSURE"
    elif open_count > 0 and bracket_count >= open_count and supervisor_local == 0 and adapter_ok:
        summary = "READY_OPEN_EXPOSURE_BRACKETED"
    elif not adapter_ok:
        summary = "BLOCKED_ADAPTER_SUPPORT"
    else:
        summary = "BLOCKED_UNBRACKETED_EXPOSURE"

    return {
        "kind": "eta_broker_bracket_audit",
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "summary": summary,
        "position_summary": position_summary,
        "adapter_support": adapter_support,
        "ready_for_prop_dry_run": summary in {"READY_NO_OPEN_EXPOSURE", "READY_OPEN_EXPOSURE_BRACKETED"},
        "next_action": (
            "Wait for or flatten current paper exposure before prop dry-run."
            if summary == "BLOCKED_UNBRACKETED_EXPOSURE"
            else "Broker-native bracket/OCO audit is clear."
        ),
    }


def write_report(report: dict[str, Any], path: Path = DEFAULT_OUT) -> Path:
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only broker bracket/OCO coverage audit")
    parser.add_argument("--fleet-url", default=DEFAULT_FLEET_URL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    fleet = _fetch_json(args.fleet_url)
    report = build_bracket_audit(fleet=fleet)
    out_path = None if args.no_write else write_report(report, args.out)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(f"broker bracket audit: {report['summary']}")
        print(f"ready for prop dry-run: {report['ready_for_prop_dry_run']}")
        print(f"positions: {report['position_summary']}")
        if out_path is not None:
            print(f"wrote: {out_path}")
    return 0 if report["ready_for_prop_dry_run"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

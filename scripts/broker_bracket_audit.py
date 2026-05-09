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


def _as_float(value: Any) -> float | None:  # noqa: ANN401
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:  # noqa: ANN401
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _fetch_json(url: str, timeout_s: float = 10.0) -> dict[str, Any]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "eta-broker-bracket-audit"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _derive_position_summary(fleet: dict[str, Any]) -> dict[str, Any]:
    target_exit_summary = _as_dict(fleet.get("target_exit_summary"))
    if target_exit_summary:
        broker_open = _as_int(target_exit_summary.get("broker_open_position_count"))
        bracket_required = _as_int(
            target_exit_summary.get("broker_bracket_required_position_count"),
        )
        bracket_count = _as_int(target_exit_summary.get("broker_bracket_count"))
        missing_brackets = _as_int(target_exit_summary.get("missing_bracket_count"))
        if bracket_required <= 0 and broker_open > 0:
            bracket_required = broker_open
        if missing_brackets <= 0 and bracket_required > bracket_count:
            missing_brackets = bracket_required - bracket_count
        return {
            "broker_open_position_count": broker_open,
            "broker_bracket_required_position_count": bracket_required,
            "broker_bracket_count": bracket_count,
            "missing_bracket_count": missing_brackets,
            "supervisor_local_position_count": _as_int(
                target_exit_summary.get("supervisor_local_position_count"),
            ),
        }

    summary = _as_dict(fleet.get("summary"))
    if summary:
        broker_open = _as_int(summary.get("broker_open_position_count"))
        bracket_required = _as_int(summary.get("broker_bracket_required_position_count"))
        bracket_count = _as_int(summary.get("broker_bracket_count"))
        if bracket_required <= 0 and broker_open > 0:
            bracket_required = broker_open
        return {
            "broker_open_position_count": broker_open,
            "broker_bracket_required_position_count": bracket_required,
            "broker_bracket_count": bracket_count,
            "missing_bracket_count": max(
                0,
                _as_int(summary.get("missing_bracket_count")) or (bracket_required - bracket_count),
            ),
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
        "broker_bracket_required_position_count": open_count,
        "broker_bracket_count": bracket_count,
        "missing_bracket_count": max(0, open_count - bracket_count),
        "supervisor_local_position_count": supervisor_local,
    }


def _position_qty(position: dict[str, Any]) -> float | None:
    for key in ("qty", "position", "quantity", "size"):
        qty = _as_float(position.get(key))
        if qty is not None:
            return qty
    return None


def _position_requires_broker_bracket(position: dict[str, Any]) -> bool:
    explicit = position.get("broker_bracket_required")
    if explicit is not None:
        return _as_bool(explicit)
    if _as_bool(position.get("broker_bracket") or position.get("has_broker_bracket")):
        return False
    venue = str(position.get("venue") or "").strip().lower()
    sec_type = str(position.get("sec_type") or position.get("secType") or "").strip().upper()
    return venue in {"ibkr", "tasty", "tastytrade"} and sec_type in {"FUT", "FOP"}


def _normalize_open_position(
    raw_position: object,
    *,
    default_venue: str | None = None,
) -> dict[str, Any]:
    position = _as_dict(raw_position)
    symbol = str(
        position.get("symbol")
        or position.get("localSymbol")
        or position.get("contractSymbol")
        or "",
    ).strip()
    if not symbol:
        return {}
    qty = _position_qty(position)
    side = str(position.get("side") or "").strip().lower()
    if not side and qty is not None:
        side = "long" if qty > 0 else "short" if qty < 0 else ""
    sec_type = position.get("sec_type") or position.get("secType") or position.get("security_type")
    venue = str(position.get("venue") or position.get("broker") or default_venue or "").strip().lower()
    normalized = {
        "venue": venue,
        "symbol": symbol,
        "side": side,
        "qty": abs(qty) if qty is not None else None,
        "sec_type": sec_type,
        "exchange": position.get("exchange"),
        "market_value": _as_float(position.get("market_value")),
        "unrealized_pnl": _as_float(position.get("unrealized_pnl")),
    }
    normalized["broker_bracket_required"] = _position_requires_broker_bracket({
        **position,
        **normalized,
    })
    normalized["coverage_status"] = "requires_manual_oco_verification"
    return normalized


def _candidate_open_positions(fleet: dict[str, Any]) -> list[dict[str, Any]]:
    live_broker_state = _as_dict(fleet.get("live_broker_state"))
    sources: list[tuple[list[Any], str | None]] = [
        (_as_list(_as_dict(fleet.get("position_exposure")).get("open_positions")), None),
        (
            _as_list(_as_dict(live_broker_state.get("position_exposure")).get("open_positions")),
            None,
        ),
        (_as_list(_as_dict(live_broker_state.get("ibkr")).get("open_positions")), "ibkr"),
        (_as_list(_as_dict(live_broker_state.get("tastytrade")).get("open_positions")), "tastytrade"),
        (_as_list(_as_dict(live_broker_state.get("tasty")).get("open_positions")), "tasty"),
    ]
    positions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, float | None]] = set()
    for rows, default_venue in sources:
        for raw_position in rows:
            position = _normalize_open_position(raw_position, default_venue=default_venue)
            if not position:
                continue
            key = (
                str(position.get("venue") or ""),
                str(position.get("symbol") or ""),
                str(position.get("sec_type") or ""),
                position.get("qty"),
            )
            if key in seen:
                continue
            seen.add(key)
            positions.append(position)
    return positions


def _unprotected_positions(
    fleet: dict[str, Any],
    *,
    missing_brackets: int,
) -> list[dict[str, Any]]:
    if missing_brackets <= 0:
        return []
    positions = [
        position
        for position in _candidate_open_positions(fleet)
        if position.get("broker_bracket_required") is True
    ]
    return positions[:missing_brackets]


def _position_descriptor(position: dict[str, Any]) -> str:
    symbol = str(position.get("symbol") or "position").strip()
    venue = str(position.get("venue") or "broker").strip().upper()
    sec_type = str(position.get("sec_type") or "").strip().upper()
    return " ".join(part for part in (symbol, venue, sec_type) if part)


def _append_detail_once(message: str, detail: str) -> str:
    if not detail:
        return message
    if not message:
        return detail
    return message if detail in message else f"{message}; {detail}"


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
    bracket_required = position_summary["broker_bracket_required_position_count"]
    missing_brackets = position_summary["missing_bracket_count"]
    supervisor_local = position_summary["supervisor_local_position_count"]
    target_exit_summary = _as_dict(fleet.get("target_exit_summary"))
    unprotected_positions = _unprotected_positions(
        fleet,
        missing_brackets=missing_brackets,
    )
    if unprotected_positions:
        position_summary = dict(position_summary)
        position_summary["unprotected_symbols"] = sorted(
            {
                str(position.get("symbol") or "")
                for position in unprotected_positions
                if position.get("symbol")
            },
        )
    adapter_support = _adapter_support()
    adapter_ok = bool(adapter_support.get("ibkr_futures_server_oco")) and bool(
        adapter_support.get("tradovate_order_payload_brackets"),
    )

    if open_count == 0 and supervisor_local == 0 and adapter_ok:
        summary = "READY_NO_OPEN_EXPOSURE"
    elif bracket_required > 0 and missing_brackets == 0 and supervisor_local == 0 and adapter_ok:
        summary = "READY_OPEN_EXPOSURE_BRACKETED"
    elif not adapter_ok:
        summary = "BLOCKED_ADAPTER_SUPPORT"
    else:
        summary = "BLOCKED_UNBRACKETED_EXPOSURE"

    if summary == "BLOCKED_UNBRACKETED_EXPOSURE" and missing_brackets > 0:
        next_action = (
            f"{missing_brackets} broker bracket-required position"
            f"{'' if missing_brackets == 1 else 's'} missing broker-native OCO; "
            "verify manual broker OCO coverage or flatten current paper exposure before prop dry-run."
        )
    elif summary == "BLOCKED_UNBRACKETED_EXPOSURE":
        next_action = "Wait for or flatten current paper exposure before prop dry-run."
    else:
        next_action = "Broker-native bracket/OCO audit is clear."

    if unprotected_positions:
        descriptor = _position_descriptor(unprotected_positions[0])
        next_action = _append_detail_once(
            next_action,
            f"{descriptor} missing broker-native OCO",
        )

    return {
        "kind": "eta_broker_bracket_audit",
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "summary": summary,
        "target_exit_status": target_exit_summary.get("status"),
        "stale_position_status": target_exit_summary.get("stale_position_status"),
        "position_summary": position_summary,
        "unprotected_positions": unprotected_positions,
        "primary_unprotected_position": unprotected_positions[0] if unprotected_positions else None,
        "adapter_support": adapter_support,
        "ready_for_prop_dry_run": summary in {"READY_NO_OPEN_EXPOSURE", "READY_OPEN_EXPOSURE_BRACKETED"},
        "next_action": next_action,
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

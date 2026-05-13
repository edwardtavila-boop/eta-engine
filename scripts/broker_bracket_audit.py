"""Read-only broker-native bracket/OCO coverage audit."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from eta_engine.scripts import workspace_roots  # noqa: E402

DEFAULT_FLEET_URL = "https://ops.evolutionarytradingalgo.com/api/bot-fleet"
DEFAULT_LOCAL_FLEET_URL = "http://127.0.0.1:8420/api/bot-fleet"
DEFAULT_OUT = workspace_roots.ETA_BROKER_BRACKET_AUDIT_PATH
DEFAULT_MANUAL_ACK_PATH = workspace_roots.ETA_BROKER_BRACKET_MANUAL_ACK_PATH
FUTURES_MULTIPLIERS = {
    "6E": 125000.0,
    "CL": 1000.0,
    "ES": 50.0,
    "GC": 100.0,
    "M2K": 5.0,
    "MBT": 0.1,
    "MCL": 100.0,
    "MES": 5.0,
    "MET": 0.1,
    "MGC": 10.0,
    "MNQ": 2.0,
    "MYM": 0.5,
    "NG": 10000.0,
    "NQ": 20.0,
    "RTY": 50.0,
    "YM": 5.0,
    "ZN": 1000.0,
}
_OPEN_ORDER_DONE_STATUSES = {
    "apicancelled",
    "cancelled",
    "filled",
    "inactive",
}


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


def _clean_symbol(value: object) -> str:
    return str(value or "").strip().upper().replace("/", "").replace("-", "")


def _open_order_symbol(order: dict[str, Any]) -> str:
    contract = _as_dict(order.get("contract"))
    for key in ("local_symbol", "localSymbol", "contract_symbol", "contractSymbol", "symbol"):
        symbol = _clean_symbol(order.get(key))
        if symbol:
            return symbol
    for key in ("local_symbol", "localSymbol", "symbol"):
        symbol = _clean_symbol(contract.get(key))
        if symbol:
            return symbol
    return ""


def _open_order_action(order: dict[str, Any]) -> str:
    return str(order.get("action") or order.get("side") or "").strip().upper()


def _open_order_qty(order: dict[str, Any]) -> float:
    for key in (
        "remaining",
        "remaining_qty",
        "remainingQuantity",
        "total_quantity",
        "totalQuantity",
        "qty",
        "quantity",
    ):
        qty = _as_float(order.get(key))
        if qty is not None:
            return abs(qty)
    return 0.0


def _open_order_status(order: dict[str, Any]) -> str:
    return str(order.get("status") or order.get("order_status") or "").strip()


def _open_order_is_active(order: dict[str, Any]) -> bool:
    status = _open_order_status(order).lower()
    return status not in _OPEN_ORDER_DONE_STATUSES


def _open_order_linkage_key(order: dict[str, Any]) -> str:
    oca_group = str(order.get("oca_group") or order.get("ocaGroup") or "").strip()
    if oca_group:
        return f"oca:{oca_group}"
    parent_id = _as_int(order.get("parent_id") or order.get("parentId"))
    if parent_id > 0:
        return f"parent:{parent_id}"
    return ""


def _open_order_leg_kind(order: dict[str, Any]) -> str:
    order_type = (
        str(
            order.get("order_type") or order.get("orderType") or order.get("type") or "",
        )
        .strip()
        .upper()
    )
    if "STP" in order_type or "STOP" in order_type or "TRAIL" in order_type:
        return "stop"
    if order_type in {"LMT", "LIMIT"} or "LIMIT" in order_type:
        return "target"
    return ""


def _normalize_open_order(raw_order: object) -> dict[str, Any]:
    order = _as_dict(raw_order)
    if not order:
        return {}
    symbol = _open_order_symbol(order)
    if not symbol:
        return {}
    normalized = {
        "symbol": symbol,
        "action": _open_order_action(order),
        "order_type": str(order.get("order_type") or order.get("orderType") or "").strip().upper(),
        "qty": _open_order_qty(order),
        "status": _open_order_status(order),
        "parent_id": _as_int(order.get("parent_id") or order.get("parentId")),
        "oca_group": str(order.get("oca_group") or order.get("ocaGroup") or "").strip(),
        "order_id": order.get("order_id") or order.get("orderId"),
        "perm_id": order.get("perm_id") or order.get("permId"),
    }
    normalized["linkage_key"] = _open_order_linkage_key(normalized)
    normalized["leg_kind"] = _open_order_leg_kind(normalized)
    return normalized if _open_order_is_active(normalized) else {}


def _protective_action_for_position(position: dict[str, Any]) -> str:
    side = str(position.get("side") or "").strip().lower()
    if side in {"long", "buy"}:
        return "SELL"
    if side in {"short", "sell"}:
        return "BUY"
    qty = _position_qty(position)
    if qty is not None:
        return "SELL" if qty > 0 else "BUY" if qty < 0 else ""
    return ""


def _open_orders_from_fleet(fleet: dict[str, Any]) -> list[Any]:
    orders: list[Any] = []
    for key in ("open_orders", "broker_open_orders"):
        orders.extend(_as_list(fleet.get(key)))
    live_broker_state = _as_dict(fleet.get("live_broker_state"))
    for venue in ("ibkr", "tastytrade", "tasty"):
        venue_state = _as_dict(live_broker_state.get(venue))
        for key in ("open_orders", "open_trades"):
            orders.extend(_as_list(venue_state.get(key)))
    return orders


def build_broker_oco_evidence(
    positions: list[dict[str, Any]],
    open_orders: list[Any],
) -> dict[str, Any]:
    """Conservatively prove broker-native OCO coverage from read-only open orders."""
    normalized_orders = [order for order in (_normalize_open_order(raw_order) for raw_order in open_orders) if order]
    evidence_rows: list[dict[str, Any]] = []
    for position in positions:
        symbol = _position_symbol(position)
        qty = _as_float(position.get("qty"))
        if not symbol or qty is None or qty <= 0:
            continue
        protective_action = _protective_action_for_position(position)
        if not protective_action:
            continue
        groups: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"stop_qty": 0.0, "target_qty": 0.0, "order_ids": []},
        )
        for order in normalized_orders:
            if order.get("symbol") != symbol:
                continue
            if order.get("action") != protective_action:
                continue
            leg_kind = str(order.get("leg_kind") or "")
            linkage_key = str(order.get("linkage_key") or "")
            if leg_kind not in {"stop", "target"} or not linkage_key:
                continue
            group = groups[linkage_key]
            if leg_kind == "stop":
                group["stop_qty"] = float(group["stop_qty"]) + float(order.get("qty") or 0.0)
            else:
                group["target_qty"] = float(group["target_qty"]) + float(order.get("qty") or 0.0)
            if order.get("order_id") is not None:
                group["order_ids"].append(order.get("order_id"))
        covering_groups = []
        covered_qty = 0.0
        for linkage_key, group in sorted(groups.items()):
            stop_qty = float(group.get("stop_qty") or 0.0)
            target_qty = float(group.get("target_qty") or 0.0)
            group_covered_qty = min(stop_qty, target_qty)
            if group_covered_qty <= 0:
                continue
            covered_qty += group_covered_qty
            covering_groups.append(
                {
                    "linkage_key": linkage_key,
                    "stop_qty": round(stop_qty, 8),
                    "target_qty": round(target_qty, 8),
                    "covered_qty": round(group_covered_qty, 8),
                    "order_ids": group.get("order_ids") or [],
                },
            )
        verified = covered_qty + 1e-9 >= qty
        evidence_rows.append(
            {
                "venue": _position_venue(position),
                "symbol": symbol,
                "sec_type": str(position.get("sec_type") or "").strip().upper(),
                "side": str(position.get("side") or "").strip().lower(),
                "qty": qty,
                "protective_action": protective_action,
                "covered_qty": round(covered_qty, 8),
                "coverage_status": "broker_oco_verified" if verified else "broker_oco_missing",
                "covering_groups": covering_groups,
            },
        )
    verified_symbols = sorted(
        {row["symbol"] for row in evidence_rows if row.get("coverage_status") == "broker_oco_verified"},
    )
    return {
        "kind": "eta_broker_oco_evidence",
        "schema_version": 1,
        "source": "broker_open_orders",
        "open_order_count": len(normalized_orders),
        "checked_position_count": len(evidence_rows),
        "verified_count": len(verified_symbols),
        "verified_symbols": verified_symbols,
        "positions": evidence_rows,
    }


def _fetch_json(url: str, timeout_s: float = 10.0, attempts: int = 2) -> dict[str, Any]:
    for _attempt in range(max(1, attempts)):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "eta-broker-bracket-audit"})
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
            continue
    return {}


def load_fleet_payload(url: str = DEFAULT_FLEET_URL) -> dict[str, Any]:
    """Load bot-fleet truth, falling back to local Command Center when public ops is slow."""
    primary = _fetch_json(url, timeout_s=10.0)
    if _fleet_has_position_truth(primary):
        return primary
    if url == DEFAULT_LOCAL_FLEET_URL:
        return primary
    local = _fetch_json(DEFAULT_LOCAL_FLEET_URL, timeout_s=20.0)
    if _fleet_has_position_truth(local):
        return local
    return primary or local


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_manual_oco_ack(path: Path = DEFAULT_MANUAL_ACK_PATH) -> dict[str, Any]:
    """Load the operator's manual broker-OCO verification latch."""
    return _load_json(path)


def _parse_dt(raw: object) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        parsed = raw
    else:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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


def _fleet_has_position_truth(fleet: dict[str, Any]) -> bool:
    if not fleet:
        return False
    target_exit_summary = _as_dict(fleet.get("target_exit_summary"))
    if target_exit_summary:
        return True
    summary = _as_dict(fleet.get("summary"))
    position_keys = {
        "broker_open_position_count",
        "broker_bracket_required_position_count",
        "broker_bracket_count",
        "missing_bracket_count",
        "supervisor_local_position_count",
    }
    if summary and any(key in summary for key in position_keys):
        return True
    if "bots" in fleet:
        return True
    if _as_list(_as_dict(fleet.get("position_exposure")).get("open_positions")):
        return True
    live_broker_state = _as_dict(fleet.get("live_broker_state"))
    if _as_list(_as_dict(live_broker_state.get("position_exposure")).get("open_positions")):
        return True
    for venue in ("ibkr", "tastytrade", "tasty"):
        if _as_list(_as_dict(live_broker_state.get(venue)).get("open_positions")):
            return True
    return False


def _position_qty(position: dict[str, Any]) -> float | None:
    for key in ("qty", "position", "quantity", "size"):
        qty = _as_float(position.get(key))
        if qty is not None:
            return qty
    return None


def _first_float(position: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _as_float(position.get(key))
        if value is not None:
            return value
    return None


def _futures_multiplier(position: dict[str, Any], symbol: str) -> float | None:
    multiplier = _as_float(position.get("multiplier") or position.get("contract_multiplier"))
    if multiplier is not None and multiplier > 0:
        return multiplier
    symbol_key = symbol.strip().upper()
    for root, value in sorted(FUTURES_MULTIPLIERS.items(), key=lambda item: len(item[0]), reverse=True):
        if symbol_key.startswith(root):
            return value
    return None


def _normalize_avg_entry_price(
    position: dict[str, Any],
    *,
    symbol: str,
    sec_type: object,
    raw_avg_entry_price: float | None,
    current_price: float | None,
) -> float | None:
    if raw_avg_entry_price is None:
        return None
    if str(sec_type or "").strip().upper() not in {"FUT", "FOP"}:
        return raw_avg_entry_price
    multiplier = _futures_multiplier(position, symbol)
    if not multiplier or current_price is None:
        return raw_avg_entry_price
    candidate = raw_avg_entry_price / multiplier
    if abs(candidate - current_price) < abs(raw_avg_entry_price - current_price):
        return candidate
    return raw_avg_entry_price


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
        position.get("symbol") or position.get("localSymbol") or position.get("contractSymbol") or "",
    ).strip()
    if not symbol:
        return {}
    qty = _position_qty(position)
    side = str(position.get("side") or "").strip().lower()
    if not side and qty is not None:
        side = "long" if qty > 0 else "short" if qty < 0 else ""
    sec_type = position.get("sec_type") or position.get("secType") or position.get("security_type")
    venue = str(position.get("venue") or position.get("broker") or default_venue or "").strip().lower()
    current_price = _first_float(
        position,
        ("current_price", "mark_price", "market_price", "last_price", "currentPrice"),
    )
    raw_avg_entry_price = _first_float(
        position,
        ("avg_entry_price", "average_cost", "averageCost", "avgCost", "avg_price", "avgPrice"),
    )
    normalized = {
        "venue": venue,
        "symbol": symbol,
        "side": side,
        "qty": abs(qty) if qty is not None else None,
        "sec_type": sec_type,
        "exchange": position.get("exchange"),
        "avg_entry_price": _normalize_avg_entry_price(
            position,
            symbol=symbol,
            sec_type=sec_type,
            raw_avg_entry_price=raw_avg_entry_price,
            current_price=current_price,
        ),
        "current_price": current_price,
        "unrealized_pct": _first_float(position, ("unrealized_pct", "unrealized_percent")),
        "market_value": _as_float(position.get("market_value")),
        "unrealized_pnl": _as_float(position.get("unrealized_pnl")),
    }
    normalized["broker_bracket_required"] = _position_requires_broker_bracket(
        {
            **position,
            **normalized,
        }
    )
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


def _position_symbol(position: dict[str, Any]) -> str:
    return str(position.get("symbol") or "").strip().upper()


def _position_venue(position: dict[str, Any]) -> str:
    return str(position.get("venue") or "").strip().lower()


def _manual_ack_entries(manual_ack: dict[str, Any]) -> list[dict[str, Any]]:
    if not manual_ack:
        return []
    entries = _as_list(manual_ack.get("acks"))
    if entries:
        return [_as_dict(entry) for entry in entries if _as_dict(entry)]
    return [manual_ack]


def _single_manual_ack_covers(
    position: dict[str, Any],
    manual_ack: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    if not manual_ack or not _as_bool(manual_ack.get("verified")):
        return False
    ack_symbol = str(manual_ack.get("symbol") or "").strip().upper()
    if not ack_symbol or ack_symbol != _position_symbol(position):
        return False
    ack_venue = str(manual_ack.get("venue") or "").strip().lower()
    if ack_venue and ack_venue != _position_venue(position):
        return False
    expires_at = _parse_dt(manual_ack.get("expires_at_utc"))
    if expires_at is None:
        return False
    return expires_at > (now or datetime.now(UTC))


def _manual_ack_covers(
    position: dict[str, Any],
    manual_ack: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    return any(_single_manual_ack_covers(position, entry, now=now) for entry in _manual_ack_entries(manual_ack))


def _position_coverage_key(position: dict[str, Any]) -> tuple[str, str, str, float | None]:
    return (
        _position_venue(position),
        _position_symbol(position),
        str(position.get("sec_type") or "").strip().upper(),
        position.get("qty"),
    )


def _broker_oco_evidence_for_position(
    position: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    symbol = _position_symbol(position)
    venue = _position_venue(position)
    sec_type = str(position.get("sec_type") or "").strip().upper()
    for row in _as_list(evidence.get("positions")):
        item = _as_dict(row)
        if str(item.get("coverage_status") or "") != "broker_oco_verified":
            continue
        if _clean_symbol(item.get("symbol")) != symbol:
            continue
        item_venue = str(item.get("venue") or "").strip().lower()
        if item_venue and item_venue != venue:
            continue
        item_sec_type = str(item.get("sec_type") or "").strip().upper()
        if item_sec_type and item_sec_type != sec_type:
            continue
        return item
    return {}


def _unprotected_positions(
    fleet: dict[str, Any],
    *,
    missing_brackets: int,
) -> list[dict[str, Any]]:
    if missing_brackets <= 0:
        return []
    positions = [
        position for position in _candidate_open_positions(fleet) if position.get("broker_bracket_required") is True
    ]
    return positions[:missing_brackets]


def _position_descriptor(position: dict[str, Any]) -> str:
    symbol = str(position.get("symbol") or "position").strip()
    venue = str(position.get("venue") or "broker").strip().upper()
    sec_type = str(position.get("sec_type") or "").strip().upper()
    return " ".join(part for part in (symbol, venue, sec_type) if part)


def _append_detail_once(message: str, detail: str) -> str:
    message = str(message or "").strip()
    detail = str(detail or "").strip()
    if not detail:
        return message
    if not message:
        return detail
    if detail in message:
        return message
    separator = " " if message.endswith((".", "!", "?")) else "; "
    return f"{message}{separator}{detail}"


def _operator_actions(summary: str, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if summary == "BLOCKED_FLEET_TRUTH_UNAVAILABLE":
        return [
            {
                "id": "restore_bot_fleet_position_truth",
                "label": "Restore bot-fleet position truth",
                "manual": False,
                "order_action": False,
                "blocks_prop_dry_run": True,
                "symbol": None,
                "detail": "Restore /api/bot-fleet position truth before treating broker exposure as flat.",
            },
        ]
    if summary != "BLOCKED_UNBRACKETED_EXPOSURE":
        return []
    primary = positions[0] if positions else {}
    symbols = sorted(
        {
            str(position.get("symbol") or "").strip().upper()
            for position in positions
            if str(position.get("symbol") or "").strip()
        },
    )
    descriptor = (
        ", ".join(symbols) if symbols else (_position_descriptor(primary) if primary else "current broker exposure")
    )
    oco_verb = "have" if len(symbols) > 1 else "has"
    symbol = str(primary.get("symbol") or "").strip() or None
    return [
        {
            "id": "verify_manual_broker_oco",
            "label": "Verify broker OCO coverage",
            "manual": True,
            "order_action": False,
            "blocks_prop_dry_run": True,
            "symbol": symbol,
            "symbols": symbols,
            "detail": f"Confirm {descriptor} {oco_verb} broker-native TP/SL OCO attached outside ETA.",
        },
        {
            "id": "flatten_unprotected_paper_exposure",
            "label": "Flatten unprotected paper exposure",
            "manual": True,
            "order_action": True,
            "blocks_prop_dry_run": True,
            "symbol": symbol,
            "symbols": symbols,
            "detail": f"Alternative: flatten {descriptor} before prop dry-run if no OCO exists.",
        },
    ]


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


def build_bracket_audit(
    *,
    fleet: dict[str, Any] | None = None,
    manual_ack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fleet = load_fleet_payload() if fleet is None else fleet or {}
    manual_ack = manual_ack or {}
    fleet_truth_present = _fleet_has_position_truth(fleet)
    position_summary = _derive_position_summary(fleet)
    open_count = position_summary["broker_open_position_count"]
    bracket_required = position_summary["broker_bracket_required_position_count"]
    missing_brackets = position_summary["missing_bracket_count"]
    supervisor_local = position_summary["supervisor_local_position_count"]
    target_exit_summary = _as_dict(fleet.get("target_exit_summary"))
    candidate_unprotected_positions = _unprotected_positions(
        fleet,
        missing_brackets=missing_brackets,
    )
    broker_oco_evidence = build_broker_oco_evidence(
        candidate_unprotected_positions,
        _open_orders_from_fleet(fleet),
    )
    manual_oco_verified_positions = [
        position for position in candidate_unprotected_positions if _manual_ack_covers(position, manual_ack)
    ]
    manual_verified_keys = {_position_coverage_key(position) for position in manual_oco_verified_positions}
    broker_oco_verified_positions: list[dict[str, Any]] = []
    for position in candidate_unprotected_positions:
        if _position_coverage_key(position) in manual_verified_keys:
            continue
        evidence = _broker_oco_evidence_for_position(position, broker_oco_evidence)
        if not evidence:
            continue
        verified_position = dict(position)
        verified_position["coverage_status"] = "broker_oco_verified"
        verified_position["broker_oco_evidence"] = evidence
        broker_oco_verified_positions.append(verified_position)

    verified_keys = manual_verified_keys | {
        _position_coverage_key(position) for position in broker_oco_verified_positions
    }
    unprotected_positions = [
        position
        for position in candidate_unprotected_positions
        if _position_coverage_key(position) not in verified_keys
    ]
    missing_brackets = max(0, missing_brackets - len(verified_keys))

    position_summary = dict(position_summary)
    position_summary["missing_bracket_count"] = missing_brackets
    position_summary["manual_oco_verified_count"] = len(manual_oco_verified_positions)
    position_summary["manual_oco_verified_symbols"] = sorted(
        {str(position.get("symbol") or "") for position in manual_oco_verified_positions if position.get("symbol")},
    )
    position_summary["broker_oco_verified_count"] = len(broker_oco_verified_positions)
    position_summary["broker_oco_verified_symbols"] = sorted(
        {str(position.get("symbol") or "") for position in broker_oco_verified_positions if position.get("symbol")},
    )

    if unprotected_positions:
        position_summary = dict(position_summary)
        position_summary["unprotected_symbols"] = sorted(
            {str(position.get("symbol") or "") for position in unprotected_positions if position.get("symbol")},
        )
    adapter_support = _adapter_support()
    adapter_ok = bool(adapter_support.get("ibkr_futures_server_oco")) and bool(
        adapter_support.get("tradovate_order_payload_brackets"),
    )

    if not fleet_truth_present:
        summary = "BLOCKED_FLEET_TRUTH_UNAVAILABLE"
    elif open_count == 0 and supervisor_local == 0 and adapter_ok:
        summary = "READY_NO_OPEN_EXPOSURE"
    elif manual_oco_verified_positions and missing_brackets == 0 and not unprotected_positions and adapter_ok:
        summary = "READY_OPEN_EXPOSURE_MANUAL_OCO_VERIFIED"
    elif bracket_required > 0 and missing_brackets == 0 and supervisor_local == 0 and adapter_ok:
        summary = "READY_OPEN_EXPOSURE_BRACKETED"
    elif not adapter_ok:
        summary = "BLOCKED_ADAPTER_SUPPORT"
    else:
        summary = "BLOCKED_UNBRACKETED_EXPOSURE"

    if summary == "BLOCKED_FLEET_TRUTH_UNAVAILABLE":
        next_action = (
            "Bot-fleet position truth is unavailable; restore /api/bot-fleet before treating broker exposure as flat."
        )
    elif summary == "BLOCKED_UNBRACKETED_EXPOSURE" and missing_brackets > 0:
        next_action = (
            f"{missing_brackets} broker bracket-required position"
            f"{'' if missing_brackets == 1 else 's'} missing broker-native OCO; "
            "verify manual broker OCO coverage or flatten current paper exposure before prop dry-run."
        )
    elif summary == "BLOCKED_UNBRACKETED_EXPOSURE":
        next_action = "Wait for or flatten current paper exposure before prop dry-run."
    elif summary == "READY_OPEN_EXPOSURE_MANUAL_OCO_VERIFIED":
        next_action = "Broker-native bracket/OCO audit is clear via manual OCO verification."
    else:
        next_action = "Broker-native bracket/OCO audit is clear."

    if unprotected_positions:
        descriptor = _position_descriptor(unprotected_positions[0])
        next_action = _append_detail_once(
            next_action,
            f"{descriptor} missing broker-native OCO",
        )

    ready_for_prop_dry_run = summary in {
        "READY_NO_OPEN_EXPOSURE",
        "READY_OPEN_EXPOSURE_BRACKETED",
        "READY_OPEN_EXPOSURE_MANUAL_OCO_VERIFIED",
    }
    return {
        "kind": "eta_broker_bracket_audit",
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "summary": summary,
        "target_exit_status": target_exit_summary.get("status"),
        "stale_position_status": target_exit_summary.get("stale_position_status"),
        "fleet_truth_present": fleet_truth_present,
        "position_summary": position_summary,
        "manual_oco_ack": {
            "present": bool(_manual_ack_entries(manual_ack)),
            "symbol": manual_ack.get("symbol"),
            "venue": manual_ack.get("venue"),
            "verified": _as_bool(manual_ack.get("verified"))
            or any(_as_bool(entry.get("verified")) for entry in _manual_ack_entries(manual_ack)),
            "operator": manual_ack.get("operator"),
            "verified_at_utc": manual_ack.get("verified_at_utc"),
            "expires_at_utc": manual_ack.get("expires_at_utc"),
            "ack_count": len(_manual_ack_entries(manual_ack)),
            "symbols": sorted(
                {
                    str(entry.get("symbol") or "").strip().upper()
                    for entry in _manual_ack_entries(manual_ack)
                    if str(entry.get("symbol") or "").strip()
                },
            ),
        },
        "broker_oco_evidence": broker_oco_evidence,
        "broker_oco_verified_positions": broker_oco_verified_positions,
        "manual_oco_verified_positions": manual_oco_verified_positions,
        "unprotected_positions": unprotected_positions,
        "primary_unprotected_position": unprotected_positions[0] if unprotected_positions else None,
        "adapter_support": adapter_support,
        "ready_for_prop_dry_run": ready_for_prop_dry_run,
        "operator_action_required": not ready_for_prop_dry_run,
        "operator_action": next_action,
        "operator_actions": _operator_actions(summary, unprotected_positions),
        "next_action": next_action,
    }


def write_report(report: dict[str, Any], path: Path = DEFAULT_OUT) -> Path:
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def build_manual_oco_ack(
    *,
    symbol: str,
    operator: str,
    venue: str = "",
    note: str = "",
    expires_hours: float = 24.0,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "kind": "eta_broker_bracket_manual_oco_ack",
        "schema_version": 1,
        "symbol": symbol.strip().upper(),
        "venue": venue.strip().lower(),
        "verified": True,
        "operator": operator.strip(),
        "verified_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(hours=expires_hours)).isoformat(),
        "note": note.strip(),
    }


def build_manual_oco_ack_ledger(
    *,
    symbols: list[str],
    operator: str,
    venue: str = "",
    note: str = "",
    expires_hours: float = 24.0,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    clean_symbols = list(
        dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip()),
    )
    return {
        "kind": "eta_broker_bracket_manual_oco_ack_ledger",
        "schema_version": 2,
        "verified": True,
        "operator": operator.strip(),
        "verified_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(hours=expires_hours)).isoformat(),
        "note": note.strip(),
        "acks": [
            {
                "kind": "eta_broker_bracket_manual_oco_ack",
                "schema_version": 1,
                "symbol": symbol,
                "venue": venue.strip().lower(),
                "verified": True,
                "operator": operator.strip(),
                "verified_at_utc": now.isoformat(),
                "expires_at_utc": (now + timedelta(hours=expires_hours)).isoformat(),
                "note": note.strip(),
            }
            for symbol in clean_symbols
        ],
    }


def write_manual_oco_ack(
    ack: dict[str, Any],
    path: Path = DEFAULT_MANUAL_ACK_PATH,
) -> Path:
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(ack, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only broker bracket/OCO coverage audit")
    parser.add_argument("--fleet-url", default=DEFAULT_FLEET_URL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--manual-ack-path", type=Path, default=DEFAULT_MANUAL_ACK_PATH)
    parser.add_argument("--ack-manual-oco", action="store_true")
    parser.add_argument("--symbol", help="Broker symbol manually verified with broker-native OCO")
    parser.add_argument("--symbols", help="Comma-separated broker symbols manually verified with broker-native OCO")
    parser.add_argument("--venue", default="ibkr", help="Broker venue for the manual OCO verification")
    parser.add_argument("--operator", help="Operator name recording the manual broker-OCO verification")
    parser.add_argument("--note", default="", help="Optional evidence note for the manual OCO verification")
    parser.add_argument("--expires-hours", type=float, default=24.0)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    if args.ack_manual_oco:
        if not args.confirm:
            parser.error("--ack-manual-oco requires --confirm after manually verifying broker OCO coverage")
        symbols = []
        if args.symbol:
            symbols.append(str(args.symbol))
        if args.symbols:
            symbols.extend(str(symbol) for symbol in str(args.symbols).split(","))
        symbols = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
        if not symbols:
            parser.error("--ack-manual-oco requires --symbol or --symbols")
        if not args.operator:
            parser.error("--ack-manual-oco requires --operator")
        if args.expires_hours <= 0:
            parser.error("--expires-hours must be positive")
        if len(symbols) == 1:
            ack = build_manual_oco_ack(
                symbol=symbols[0],
                venue=args.venue,
                operator=args.operator,
                note=args.note,
                expires_hours=args.expires_hours,
            )
        else:
            ack = build_manual_oco_ack_ledger(
                symbols=symbols,
                venue=args.venue,
                operator=args.operator,
                note=args.note,
                expires_hours=args.expires_hours,
            )
        out_path = write_manual_oco_ack(ack, args.manual_ack_path)
        if args.json:
            print(json.dumps({"manual_oco_ack": ack, "path": str(out_path)}, indent=2, sort_keys=True))
        else:
            print(f"manual broker OCO ack recorded: {', '.join(symbols)} ({args.venue.strip().lower()})")
            print(f"expires: {ack['expires_at_utc']}")
            print(f"wrote: {out_path}")
        return 0

    fleet = load_fleet_payload(args.fleet_url)
    report = build_bracket_audit(fleet=fleet, manual_ack=load_manual_oco_ack(args.manual_ack_path))
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

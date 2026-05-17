"""Bounded IBKR paper-position reconciliation from ``reconcile_last.json``.

The script reads the current broker-vs-supervisor reconcile artifact and can
close only the measured broker-only or broker-excess quantities. It is not a
strategy router and it does not promote, acknowledge, or clear trading gates.

Dry-run is the default. Passing ``--confirm`` submits market orders through the
IBKR Gateway/TWS socket after re-querying broker positions and verifying the
artifact is still fresh enough to trust.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from ib_insync import IB, MarketOrder

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ensure_main_thread_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


_ensure_main_thread_event_loop()

workspace_roots = import_module("eta_engine.scripts.workspace_roots")
FUTURES_MAP = import_module("eta_engine.venues.ibkr_live").FUTURES_MAP

logger = logging.getLogger(__name__)

DEFAULT_RECONCILE_PATH = workspace_roots.ETA_JARVIS_SUPERVISOR_RECONCILE_PATH
DEFAULT_JOURNAL_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "ibkr_reconcile_actions.jsonl"


@dataclass(slots=True)
class ReconcileOrderPlan:
    symbol: str
    category: str
    broker_qty: float
    supervisor_qty: float
    action: str
    quantity: float
    reason: str


@dataclass(slots=True)
class ReconcileOrderResult:
    symbol: str
    category: str
    action: str
    quantity: float
    status: str
    local_symbol: str = ""
    order_id: int | None = None
    detail: str = ""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return default


def _symbol_root(value: object) -> str:
    raw = str(value or "").upper().strip().replace("/", "")
    if not raw:
        return ""
    for suffix in ("USDT", "USD"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw.rstrip("0123456789") or raw


def _action_for_qty(qty: float) -> str:
    if qty > 0:
        return "SELL"
    if qty < 0:
        return "BUY"
    raise ValueError("zero quantity has no flatten action")


def _patch_contract_exchange(contract: Any) -> None:  # noqa: ANN401
    if getattr(contract, "secType", "") != "FUT":
        return
    if getattr(contract, "exchange", ""):
        return
    mapping = FUTURES_MAP.get(str(getattr(contract, "symbol", "")).upper())
    if mapping:
        contract.exchange = mapping[1]


def load_reconcile(path: Path = DEFAULT_RECONCILE_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(f"missing reconcile artifact: {path}") from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid reconcile artifact JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid reconcile artifact shape: {path}")
    return payload


def assert_reconcile_fresh(payload: dict[str, Any], *, max_age_s: float) -> float:
    checked_at = _parse_ts(payload.get("checked_at") or payload.get("generated_at_utc"))
    if checked_at is None:
        raise RuntimeError("reconcile artifact has no parseable checked_at")
    age_s = (_utc_now() - checked_at).total_seconds()
    if age_s > max_age_s:
        raise RuntimeError(f"reconcile artifact stale: age_s={age_s:.1f} max_age_s={max_age_s}")
    return max(0.0, age_s)


def build_plans(
    payload: dict[str, Any],
    *,
    symbols: set[str] | None = None,
    include_broker_only: bool = True,
    include_divergent: bool = True,
) -> list[ReconcileOrderPlan]:
    plans: list[ReconcileOrderPlan] = []
    symbol_filter = {s.upper() for s in symbols} if symbols else None

    if include_broker_only:
        for row in payload.get("broker_only", []) if isinstance(payload.get("broker_only"), list) else []:
            if not isinstance(row, dict):
                continue
            symbol = _symbol_root(row.get("symbol"))
            broker_qty = _float(row.get("broker_qty"))
            if not symbol or abs(broker_qty) <= 1e-9:
                continue
            if symbol_filter and symbol not in symbol_filter:
                continue
            plans.append(
                ReconcileOrderPlan(
                    symbol=symbol,
                    category="broker_only",
                    broker_qty=broker_qty,
                    supervisor_qty=0.0,
                    action=_action_for_qty(broker_qty),
                    quantity=abs(broker_qty),
                    reason="broker has exposure supervisor does not claim",
                )
            )

    if include_divergent:
        for row in payload.get("divergent", []) if isinstance(payload.get("divergent"), list) else []:
            if not isinstance(row, dict):
                continue
            symbol = _symbol_root(row.get("symbol"))
            broker_qty = _float(row.get("broker_qty"))
            supervisor_qty = _float(row.get("supervisor_qty"))
            delta = broker_qty - supervisor_qty
            if not symbol or abs(delta) <= 1e-9:
                continue
            if symbol_filter and symbol not in symbol_filter:
                continue
            plans.append(
                ReconcileOrderPlan(
                    symbol=symbol,
                    category="divergent_excess",
                    broker_qty=broker_qty,
                    supervisor_qty=supervisor_qty,
                    action=_action_for_qty(delta),
                    quantity=abs(delta),
                    reason="broker position exceeds supervisor claim",
                )
            )
    return plans


def _query_positions(ib: IB) -> dict[str, tuple[Any, float]]:  # noqa: ANN401
    positions: dict[str, tuple[Any, float]] = {}
    for broker_position in ib.positions():
        qty = _float(getattr(broker_position, "position", 0.0))
        if abs(qty) <= 1e-9:
            continue
        contract = broker_position.contract
        root = _symbol_root(getattr(contract, "symbol", ""))
        if root:
            positions[root] = (contract, qty)
    return positions


def _validate_plan_against_broker(plan: ReconcileOrderPlan, current_qty: float) -> None:
    if abs(current_qty) <= 1e-9:
        raise RuntimeError(f"{plan.symbol}: broker is already flat")
    if _action_for_qty(current_qty) != plan.action:
        raise RuntimeError(
            f"{plan.symbol}: current broker side changed; expected action {plan.action}, current_qty={current_qty}"
        )
    if abs(current_qty) + 1e-9 < plan.quantity:
        raise RuntimeError(
            f"{plan.symbol}: current_qty={current_qty} is smaller than planned close quantity {plan.quantity}"
        )
    if plan.category == "divergent_excess":
        current_delta = current_qty - plan.supervisor_qty
        if abs(current_delta) + 1e-9 < plan.quantity:
            raise RuntimeError(
                f"{plan.symbol}: current excess {current_delta} is smaller than planned close quantity {plan.quantity}"
            )
        if _action_for_qty(current_delta) != plan.action:
            raise RuntimeError(
                f"{plan.symbol}: current excess side changed; expected {plan.action}, current_delta={current_delta}"
            )


def execute_plans(
    plans: list[ReconcileOrderPlan],
    *,
    host: str,
    port: int,
    client_id: int,
    confirm: bool,
) -> list[ReconcileOrderResult]:
    ib = IB()
    results: list[ReconcileOrderResult] = []
    try:
        ib.connect(host, port, clientId=client_id, timeout=10)
        positions = _query_positions(ib)
        for plan in plans:
            contract, current_qty = positions.get(plan.symbol, (None, 0.0))
            if contract is None:
                results.append(
                    ReconcileOrderResult(
                        symbol=plan.symbol,
                        category=plan.category,
                        action=plan.action,
                        quantity=plan.quantity,
                        status="skipped",
                        detail="broker position missing at execution check",
                    )
                )
                continue
            try:
                _validate_plan_against_broker(plan, current_qty)
            except RuntimeError as exc:
                results.append(
                    ReconcileOrderResult(
                        symbol=plan.symbol,
                        category=plan.category,
                        action=plan.action,
                        quantity=plan.quantity,
                        status="blocked",
                        local_symbol=str(getattr(contract, "localSymbol", "") or ""),
                        detail=str(exc),
                    )
                )
                continue
            _patch_contract_exchange(contract)
            local_symbol = str(getattr(contract, "localSymbol", "") or "")
            if not confirm:
                results.append(
                    ReconcileOrderResult(
                        symbol=plan.symbol,
                        category=plan.category,
                        action=plan.action,
                        quantity=plan.quantity,
                        status="dry_run",
                        local_symbol=local_symbol,
                        detail=plan.reason,
                    )
                )
                continue
            order = MarketOrder(plan.action, plan.quantity)
            order.tif = "DAY"
            trade = ib.placeOrder(contract, order)
            for _ in range(50):
                ib.sleep(0.1)
                if trade.orderStatus.status in {"Submitted", "PreSubmitted", "Filled", "Cancelled", "Inactive"}:
                    break
            status = str(trade.orderStatus.status or "unknown")
            results.append(
                ReconcileOrderResult(
                    symbol=plan.symbol,
                    category=plan.category,
                    action=plan.action,
                    quantity=plan.quantity,
                    status=status,
                    local_symbol=local_symbol,
                    order_id=int(trade.order.orderId) if getattr(trade.order, "orderId", None) is not None else None,
                    detail=f"current_qty_before={current_qty}",
                )
            )
        return results
    finally:
        with contextlib.suppress(Exception):
            ib.disconnect()


def _append_journal(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=902)
    parser.add_argument("--reconcile-path", type=Path, default=DEFAULT_RECONCILE_PATH)
    parser.add_argument("--journal-path", type=Path, default=DEFAULT_JOURNAL_PATH)
    parser.add_argument("--max-age-s", type=float, default=180.0)
    parser.add_argument("--symbols", default="", help="Comma-separated symbol roots to reconcile, e.g. MCL,MYM,MNQ.")
    parser.add_argument("--broker-only", action="store_true", help="Only act on broker-only rows.")
    parser.add_argument("--divergent-only", action="store_true", help="Only act on divergent broker-excess rows.")
    parser.add_argument("--confirm", action="store_true", help="Actually submit the bounded market orders.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    symbols = {s.strip().upper() for s in ns.symbols.split(",") if s.strip()} or None
    include_broker_only = not ns.divergent_only
    include_divergent = not ns.broker_only
    started_at = _utc_now()
    payload = load_reconcile(ns.reconcile_path)
    age_s = assert_reconcile_fresh(payload, max_age_s=ns.max_age_s)
    plans = build_plans(
        payload,
        symbols=symbols,
        include_broker_only=include_broker_only,
        include_divergent=include_divergent,
    )
    results = execute_plans(
        plans,
        host=ns.host,
        port=ns.port,
        client_id=ns.client_id,
        confirm=ns.confirm,
    )
    out = {
        "generated_at_utc": _utc_now().isoformat(),
        "elapsed_ms": round((_utc_now() - started_at).total_seconds() * 1000, 1),
        "source": "reconcile_ibkr_position_mismatches",
        "confirm": bool(ns.confirm),
        "order_action_attempted": bool(ns.confirm),
        "reconcile_path": str(ns.reconcile_path),
        "reconcile_age_s": round(age_s, 1),
        "plans": [asdict(plan) for plan in plans],
        "results": [asdict(result) for result in results],
        "order_action_allowed": False,
    }
    _append_journal(ns.journal_path, out)
    if ns.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(json.dumps(out["results"], indent=2, sort_keys=True))

    failed_statuses = {"blocked", "Cancelled", "Inactive", "unknown"}
    if ns.confirm and any(result.status in failed_statuses for result in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

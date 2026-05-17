from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable


@dataclass(frozen=True)
class ExitQtyDecision:
    supervisor_qty: float
    broker_qty: float | None
    exit_qty: float


@dataclass(frozen=True)
class ExitRealization:
    point_value: float
    pnl: float
    realized_r: float


def _normalized_entry_side(side: object) -> str:
    side_text = str(side).strip().upper()
    if side_text in {"BUY", "LONG"}:
        return "BUY"
    if side_text in {"SELL", "SHORT"}:
        return "SELL"
    raise ValueError(f"unknown exit side: {side!r}")


def reconcile_exit_qty(
    pos: dict[str, Any],
    broker_qty: float | None,
    *,
    bot_id: str,
    logger: logging.Logger,
) -> ExitQtyDecision:
    try:
        supervisor_qty = abs(float(pos.get("qty", 0) or 0))
    except (TypeError, ValueError):
        supervisor_qty = 0.0

    if broker_qty is not None:
        broker_qty = abs(float(broker_qty))
        if not math.isfinite(broker_qty):
            broker_qty = None

    if broker_qty is None:
        logger.info(
            "submit_exit: broker qty unavailable for %s; using supervisor qty=%.6f",
            bot_id,
            supervisor_qty,
        )
        exit_qty = supervisor_qty
    elif broker_qty < supervisor_qty:
        logger.warning(
            "QTY DIVERGENCE %s: supervisor believes %.6f, broker holds %.6f - sizing exit against broker qty",
            bot_id,
            supervisor_qty,
            broker_qty,
        )
        exit_qty = broker_qty
    else:
        exit_qty = supervisor_qty

    return ExitQtyDecision(
        supervisor_qty=supervisor_qty,
        broker_qty=broker_qty,
        exit_qty=exit_qty,
    )


def compute_paper_exit_fill_price(
    pos: dict[str, Any],
    bar: dict[str, Any],
    *,
    symbol: str,
    adverse_bps: float,
    round_to_tick_fn: Callable[[float, str], float],
) -> float:
    entry_side = _normalized_entry_side(pos["side"])
    side_close = "SELL" if entry_side == "BUY" else "BUY"
    sign_slip_exit = 1.0 if side_close == "BUY" else -1.0
    exit_reason = str(pos.get("exit_reason") or "")
    entry_price = float(pos["entry_price"])
    ref_close = float(bar.get("close", entry_price))

    if exit_reason == "paper_stop" and pos.get("bracket_stop") is not None:
        try:
            stop_price = float(pos["bracket_stop"])
            fill_price = stop_price + sign_slip_exit * (stop_price * adverse_bps / 10_000.0)
        except (TypeError, ValueError):
            fill_price = ref_close + sign_slip_exit * (ref_close * adverse_bps / 10_000.0)
    elif exit_reason == "paper_target" and pos.get("bracket_target") is not None:
        try:
            fill_price = float(pos["bracket_target"])
        except (TypeError, ValueError):
            fill_price = ref_close + sign_slip_exit * (ref_close * adverse_bps / 10_000.0)
    else:
        fill_price = ref_close + sign_slip_exit * (ref_close * adverse_bps / 10_000.0)

    return float(round_to_tick_fn(fill_price, symbol))


def build_entry_snapshot(pos: dict[str, Any]) -> dict[str, Any]:
    return {
        "side": pos.get("side"),
        "entry_price": pos.get("entry_price"),
        "qty": pos.get("qty"),
        "bracket_stop": pos.get("bracket_stop"),
        "bracket_target": pos.get("bracket_target"),
        "signal_id": pos.get("signal_id"),
        "entry_fill_age_s": pos.get("entry_fill_age_s"),
        "entry_fill_latency_source": pos.get("entry_fill_latency_source"),
        "entry_fill_age_precision": pos.get("entry_fill_age_precision"),
        "broker_fill_ts": pos.get("broker_fill_ts"),
        "broker_router_result_ts": pos.get("broker_router_result_ts"),
        "fill_to_adopt_delay_s": pos.get("fill_to_adopt_delay_s"),
        "fill_result_write_delay_s": pos.get("fill_result_write_delay_s"),
    }


def build_exit_fill_record_payload(
    *,
    bot_id: str,
    signal_id: str,
    side: str,
    symbol: str,
    qty: float,
    fill_price: float,
    fill_ts: str,
    realized_r: float,
    pnl: float,
) -> dict[str, Any]:
    return {
        "bot_id": bot_id,
        "signal_id": signal_id,
        "side": side,
        "symbol": symbol,
        "qty": qty,
        "fill_price": round(fill_price, 4),
        "fill_ts": fill_ts,
        "paper": True,
        "realized_r": round(realized_r, 4),
        "realized_pnl": round(pnl, 4),
        "note": f"close pnl={pnl:+.2f}",
    }


def apply_exit_accounting(bot: object, *, pnl: float) -> None:
    bot.realized_pnl += pnl
    bot.cash += pnl
    bot.n_exits += 1


def maybe_route_paper_live_exit(
    *,
    mode: str,
    write_pending_order_fn: Callable[..., None],
    bot: object,
    rec: object,
) -> None:
    if mode != "paper_live":
        return
    with contextlib.suppress(Exception):
        write_pending_order_fn(bot, rec, reduce_only=True)


def compute_exit_realization(
    pos: dict[str, Any],
    *,
    symbol: str,
    fill_price: float,
    exit_qty: float,
    cash: float,
    logger: logging.Logger,
    point_value_fn: Callable[[str, str], float | None] | None = None,
) -> ExitRealization:
    resolver = point_value_fn
    if resolver is None:
        try:
            from eta_engine.feeds.instrument_specs import effective_point_value

            resolver = effective_point_value
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "point_value lookup import failed for %s, defaulting to 1.0: %s",
                symbol,
                exc,
            )
            resolver = None

    try:
        point_value = float(resolver(symbol, "auto") or 1.0) if resolver is not None else 1.0
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "point_value lookup failed for %s, defaulting to 1.0: %s",
            symbol,
            exc,
        )
        point_value = 1.0

    entry_price = float(pos["entry_price"])
    entry_side = _normalized_entry_side(pos["side"])
    sign = 1.0 if entry_side == "BUY" else -1.0
    pnl_per_unit = (fill_price - entry_price) * sign
    pnl = pnl_per_unit * exit_qty * point_value

    risk_unit = 0.0
    initial_risk_unit = pos.get("initial_risk_unit")
    if initial_risk_unit is not None:
        try:
            risk_unit = float(initial_risk_unit)
        except (TypeError, ValueError):
            risk_unit = 0.0
    if risk_unit <= 0:
        plan_stop = pos.get("bracket_stop")
        if plan_stop is not None:
            try:
                risk_unit = abs(float(plan_stop) - entry_price) * exit_qty * point_value
            except (TypeError, ValueError):
                risk_unit = 0.0
    if risk_unit <= 0:
        risk_unit = cash * 0.01

    realized_r = pnl / max(risk_unit, 1e-9) if risk_unit > 0 else 0.0
    return ExitRealization(
        point_value=point_value,
        pnl=pnl,
        realized_r=realized_r,
    )

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable


@dataclass(frozen=True)
class DirectIbkrEntryPlan:
    request: Any
    ref_price: float
    stop_price: float
    target_price: float
    bracket_src: str


@dataclass(frozen=True)
class DirectIbkrEntryOutcome:
    action: str
    reason: str
    filled_qty: float


def build_entry_fill_record_payload(
    *,
    bot_id: str,
    signal_id: str,
    side: str,
    symbol: str,
    qty: float,
    fill_price: float,
    fill_ts: str,
    mode: str,
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
        "note": f"mode={mode}",
    }


def record_optimistic_entry(
    *,
    bot: object,
    rec: object,
    logger: logging.Logger,
    persist_open_position_fn: Callable[[object], None],
    round_to_tick_fn: Callable[[float, str], float],
    warned_bots: set[str] | None = None,
    compute_bracket_fn: Callable[..., tuple[float, float, str]] | None = None,
    lookup_bot_bracket_params_fn: Callable[[str], tuple[float | None, float | None]] | None = None,
    point_value_fn: Callable[[str, str], float | None] | None = None,
) -> set[str]:
    warned = warned_bots if warned_bots is not None else set()
    bot.open_position = {
        "side": rec.side,
        "qty": rec.qty,
        "entry_price": rec.fill_price,
        "entry_ts": rec.fill_ts,
        "signal_id": rec.signal_id,
    }
    persist_open_position_fn(bot)

    try:
        bracket_fn = compute_bracket_fn
        params_fn = lookup_bot_bracket_params_fn
        if bracket_fn is None or params_fn is None:
            from eta_engine.scripts.bracket_sizing import (
                compute_bracket,
                lookup_bot_bracket_params,
            )

            bracket_fn = bracket_fn or compute_bracket
            params_fn = params_fn or lookup_bot_bracket_params

        stop_mult, target_mult = params_fn(bot.bot_id)
        planned_stop, planned_target, planned_src = bracket_fn(
            side=rec.side,
            entry_price=rec.fill_price,
            bars=bot.sage_bars,
            stop_mult_override=stop_mult,
            target_mult_override=target_mult,
        )
        bot.open_position["bracket_stop"] = round(
            round_to_tick_fn(planned_stop, bot.symbol),
            4,
        )
        bot.open_position["bracket_target"] = round(
            round_to_tick_fn(planned_target, bot.symbol),
            4,
        )
        bot.open_position["bracket_src"] = f"paper:{planned_src}"

        resolver = point_value_fn
        if resolver is None:
            try:
                from eta_engine.feeds.instrument_specs import effective_point_value

                resolver = effective_point_value
            except Exception:  # noqa: BLE001
                resolver = None

        try:
            point_value = float(resolver(bot.symbol, "auto") or 1.0) if resolver is not None else 1.0
        except Exception:  # noqa: BLE001
            point_value = 1.0

        initial_stop_distance = abs(
            float(bot.open_position["bracket_stop"]) - float(rec.fill_price),
        )
        initial_risk_unit = initial_stop_distance * abs(float(rec.qty)) * point_value
        bot.open_position["initial_stop_distance"] = round(initial_stop_distance, 6)
        bot.open_position["initial_risk_unit"] = round(initial_risk_unit, 4)
        persist_open_position_fn(bot)
    except Exception as exc:  # noqa: BLE001
        if bot.bot_id not in warned:
            logger.warning(
                "paper-bracket compute failed for %s (first occurrence): %s",
                bot.bot_id,
                exc,
            )
            warned.add(bot.bot_id)
        else:
            logger.debug(
                "paper-bracket compute failed for %s: %s",
                bot.bot_id,
                exc,
            )

    return warned


def paper_live_direct_crypto_bypasses_broker(
    symbol: str,
    *,
    crypto_live_env: str | None = None,
) -> bool:
    symbol_root = symbol.upper().rstrip("0123456789").replace("USD", "")
    is_crypto = symbol_root in {"BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "MBT", "MET"}
    crypto_live = (crypto_live_env or "").lower() in {"1", "true", "yes", "on"}
    return is_crypto and not crypto_live


def build_direct_ibkr_entry_plan(
    *,
    bot: object,
    rec: object,
    bar: dict[str, Any],
    round_to_tick_fn: Callable[[float, str], float],
    compute_bracket_fn: Callable[..., tuple[float, float, str]] | None = None,
    lookup_bot_bracket_params_fn: Callable[[str], tuple[float | None, float | None]] | None = None,
    order_request_cls: type | None = None,
    order_type_market: object | None = None,
    side_buy: object | None = None,
    side_sell: object | None = None,
) -> DirectIbkrEntryPlan:
    bracket_fn = compute_bracket_fn
    params_fn = lookup_bot_bracket_params_fn
    request_cls = order_request_cls
    market_type = order_type_market
    buy_side = side_buy
    sell_side = side_sell
    if (
        bracket_fn is None
        or params_fn is None
        or request_cls is None
        or market_type is None
        or buy_side is None
        or sell_side is None
    ):
        from eta_engine.scripts.bracket_sizing import (
            compute_bracket,
            lookup_bot_bracket_params,
        )
        from eta_engine.venues.base import OrderRequest, OrderType, Side

        bracket_fn = bracket_fn or compute_bracket
        params_fn = params_fn or lookup_bot_bracket_params
        request_cls = request_cls or OrderRequest
        market_type = market_type or OrderType.MARKET
        buy_side = buy_side or Side.BUY
        sell_side = sell_side or Side.SELL

    stop_mult, target_mult = params_fn(bot.bot_id)
    ref_price = float(rec.fill_price) if rec.fill_price else float(bar.get("close", 0.0)) or 1.0
    stop_price, target_price, bracket_src = bracket_fn(
        side=rec.side,
        entry_price=ref_price,
        bars=bot.sage_bars,
        stop_mult_override=stop_mult,
        target_mult_override=target_mult,
    )
    is_buy = rec.side.upper() == "BUY"
    invalid = (
        ref_price <= 0
        or stop_price <= 0
        or target_price <= 0
        or (is_buy and not (stop_price < ref_price < target_price))
        or (not is_buy and not (target_price < ref_price < stop_price))
    )
    if invalid:
        raise ValueError(
            f"insane bracket geometry (side={rec.side} ref={ref_price:.4f} "
            f"stop={stop_price:.4f} target={target_price:.4f} src={bracket_src})"
        )

    request = request_cls(
        symbol=rec.symbol,
        side=buy_side if is_buy else sell_side,
        qty=abs(float(rec.qty)) or 1,
        order_type=market_type,
        price=round(round_to_tick_fn(ref_price, rec.symbol), 4),
        stop_price=round(round_to_tick_fn(stop_price, rec.symbol), 4),
        target_price=round(round_to_tick_fn(target_price, rec.symbol), 4),
        bot_id=bot.bot_id,
        client_order_id=rec.signal_id,
    )
    return DirectIbkrEntryPlan(
        request=request,
        ref_price=ref_price,
        stop_price=round(round_to_tick_fn(stop_price, rec.symbol), 4),
        target_price=round(round_to_tick_fn(target_price, rec.symbol), 4),
        bracket_src=bracket_src,
    )


def apply_entry_accounting(bot: object, *, fill_ts: str) -> None:
    bot.n_entries += 1
    bot.last_signal_at = fill_ts


def direct_ibkr_result_reason(result: object) -> str:
    raw = getattr(result, "raw", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    return (
        raw.get("reason")
        or ("deduped: " + str(raw.get("note", "")) if raw.get("deduped") else "")
        or "n/a"
    )


def finalize_direct_ibkr_entry_result(
    *,
    bot: object,
    rec: object,
    result: object,
    logger: logging.Logger,
    entry_plan: DirectIbkrEntryPlan,
    record_signal_fn: Callable[[object, object, object], None],
    record_fill_fn: Callable[..., None],
    rollback_recorded_entry_fn: Callable[[str], None],
    clear_recorded_entry_without_reject_fn: Callable[[str], None],
) -> DirectIbkrEntryOutcome:
    reason = direct_ibkr_result_reason(result)
    filled_qty = float(getattr(result, "filled_qty", 0) or 0)
    status_value = str(getattr(getattr(result, "status", None), "value", "") or "")
    filled_statuses = {"PARTIAL", "FILLED"}

    if status_value in filled_statuses and filled_qty > 0 and bot.open_position is not None:
        bot.open_position["qty"] = min(abs(float(bot.open_position.get("qty", 0) or 0)), filled_qty)
        bot.open_position["broker_bracket"] = True
        bot.open_position["bracket_stop"] = entry_plan.stop_price
        bot.open_position["bracket_target"] = entry_plan.target_price
        bot.open_position["bracket_src"] = entry_plan.bracket_src
        bot.consecutive_broker_rejects = 0
        try:
            record_signal_fn(bot, rec, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("l2 record_signal failed for %s: %s", bot.bot_id, exc)
        if status_value == "FILLED":
            try:
                record_fill_fn(
                    signal_id=rec.signal_id,
                    broker_exec_id=str(
                        getattr(result, "raw", {}).get("ibkr_order_id", "") or getattr(result, "order_id", ""),
                    ),
                    exit_reason="ENTRY",
                    side="LONG" if rec.side.upper() == "BUY" else "SHORT",
                    actual_fill_price=float(getattr(result, "avg_price", 0) or entry_plan.ref_price),
                    qty_filled=int(abs(float(getattr(result, "filled_qty", 0) or 0))),
                    commission_usd=float(getattr(result, "fees", 0) or 0),
                    intended_price=float(entry_plan.ref_price),
                    tick_size=0.25,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("l2 record_fill failed for %s: %s", bot.bot_id, exc)
        return DirectIbkrEntryOutcome(
            action="filled",
            reason=reason,
            filled_qty=filled_qty,
        )

    if status_value == "OPEN" and filled_qty <= 0:
        rec.note = f"{rec.note};direct_ibkr_pending_order"
        clear_recorded_entry_without_reject_fn("direct_ibkr_open_without_fill")
        return DirectIbkrEntryOutcome(
            action="pending",
            reason=reason,
            filled_qty=filled_qty,
        )

    rollback_recorded_entry_fn(
        f"broker_result={status_value}; filled_qty={filled_qty}; reason={reason}",
    )
    return DirectIbkrEntryOutcome(
        action="rejected",
        reason=reason,
        filled_qty=filled_qty,
    )


def rollback_recorded_entry(
    *,
    bot: object,
    rec: object,
    reason: str,
    logger: logging.Logger,
    clear_persisted_open_position_fn: Callable[[object], None],
) -> None:
    if bot.open_position is not None and bot.open_position.get("signal_id") == rec.signal_id:
        bot.open_position = None
        clear_persisted_open_position_fn(bot)
    bot.n_entries = max(0, bot.n_entries - 1)
    bot.consecutive_broker_rejects += 1
    logger.critical(
        "BROKER REJECT %s: paper_live entry rolled back (reason=%s "
        "symbol=%s side=%s qty=%.6f signal_id=%s consecutive_rejects=%d)",
        bot.bot_id,
        reason,
        rec.symbol,
        rec.side,
        rec.qty,
        rec.signal_id,
        bot.consecutive_broker_rejects,
    )


def clear_recorded_entry_without_reject(
    *,
    bot: object,
    rec: object,
    reason: str,
    logger: logging.Logger,
    clear_persisted_open_position_fn: Callable[[object], None],
) -> None:
    if bot.open_position is not None and bot.open_position.get("signal_id") == rec.signal_id:
        bot.open_position = None
        clear_persisted_open_position_fn(bot)
    bot.n_entries = max(0, bot.n_entries - 1)
    logger.info(
        "BROKER PENDING %s: local open_position cleared until fill evidence arrives "
        "(reason=%s symbol=%s side=%s qty=%.6f signal_id=%s)",
        bot.bot_id,
        reason,
        rec.symbol,
        rec.side,
        rec.qty,
        rec.signal_id,
    )

"""Fail-closed prop-account entry risk governor.

Prop accounts are different from ordinary paper accounts: a single
oversized bracket can violate trailing drawdown rules before a human sees
the dashboard. This module gives the broker router a small, testable
pre-venue gate that answers one question:

    "If this bracket stops out immediately, does the account survive?"

The gate is deliberately conservative. Missing live equity inputs block
new entries; reduce-only exits are allowed so protection can still close
exposure.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from eta_engine.feeds.instrument_specs import effective_point_value

if TYPE_CHECKING:
    from collections.abc import Mapping

_MONTH_CODES = frozenset("FGHJKMNQUVXZ")
_FUTURES_ROOTS = tuple(
    sorted(
        {
            "MNQ",
            "NQ",
            "ES",
            "MES",
            "RTY",
            "M2K",
            "YM",
            "MYM",
            "GC",
            "MGC",
            "CL",
            "MCL",
            "NG",
            "6E",
            "M6E",
            "ZN",
            "BTC",
            "MBT",
            "ETH",
            "MET",
        },
        key=len,
        reverse=True,
    ),
)


@dataclass(frozen=True, slots=True)
class PropRiskVerdict:
    allow: bool
    reason: str
    context: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PropRiskRule:
    alias: str
    starting_balance_usd: float
    current_equity_usd: float
    peak_equity_usd: float
    trailing_drawdown_usd: float
    daily_loss_limit_usd: float
    daily_loss_used_usd: float = 0.0
    liquidation_buffer_usd: float = 250.0
    max_order_risk_usd: float | None = None
    open_risk_usd: float = 0.0
    daily_profit_usd: float = 0.0
    consistency_profit_cap_usd: float | None = None
    consistency_buffer_usd: float = 0.0

    @property
    def trailing_floor_usd(self) -> float:
        """Trailing liquidation floor, frozen at the starting balance.

        Most futures prop evaluations trail until the threshold reaches
        the starting balance, then freeze there. This formula keeps the
        initial floor at ``start - trailing_drawdown`` and never gives
        credit below that baseline.
        """

        start_floor = self.starting_balance_usd - self.trailing_drawdown_usd
        trailing_floor = self.peak_equity_usd - self.trailing_drawdown_usd
        return min(self.starting_balance_usd, max(start_floor, trailing_floor))

    @property
    def daily_room_usd(self) -> float:
        return (
            self.daily_loss_limit_usd
            - self.daily_loss_used_usd
            - self.liquidation_buffer_usd
        )

    @property
    def trailing_room_usd(self) -> float:
        return (
            self.current_equity_usd
            - self.trailing_floor_usd
            - self.liquidation_buffer_usd
        )

    @property
    def usable_entry_room_usd(self) -> float:
        room = min(self.daily_room_usd, self.trailing_room_usd)
        if self.max_order_risk_usd is not None:
            room = min(room, self.max_order_risk_usd)
        return room - self.open_risk_usd

    @property
    def consistency_room_usd(self) -> float | None:
        if self.consistency_profit_cap_usd is None:
            return None
        return (
            self.consistency_profit_cap_usd
            - self.daily_profit_usd
            - self.consistency_buffer_usd
        )


class PendingOrderLike(Protocol):
    bot_id: str
    qty: float
    symbol: str
    limit_price: float
    stop_price: float | None
    reduce_only: bool


def estimate_bracket_risk_usd(
    *,
    symbol: str,
    qty: float,
    entry_price: float,
    stop_price: float,
) -> float | None:
    """Estimate worst-case stop loss in dollars for one bracketed entry."""

    root = _futures_root(symbol)
    if root is None:
        return None
    try:
        qty_f = abs(float(qty))
        entry_f = float(entry_price)
        stop_f = float(stop_price)
        point_value = float(effective_point_value(root, route="futures"))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) and v > 0.0 for v in (qty_f, entry_f, stop_f)):
        return None
    if not math.isfinite(point_value) or point_value <= 0.0:
        return None
    return round(abs(entry_f - stop_f) * qty_f * point_value, 2)


def evaluate_prop_order(
    order: PendingOrderLike,
    account: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> PropRiskVerdict:
    """Return a fail-closed verdict for a prop-account pending order."""

    if bool(getattr(order, "reduce_only", False)):
        return PropRiskVerdict(
            allow=True,
            reason="reduce_only_exit",
            context={"bot_id": getattr(order, "bot_id", ""), "risk_usd": 0.0},
        )

    stop_price = getattr(order, "stop_price", None)
    if stop_price is None:
        return PropRiskVerdict(
            allow=False,
            reason="prop_missing_stop",
            context={"symbol": getattr(order, "symbol", ""), "risk_usd": None},
        )

    rule, missing = _rule_from_account(account, env=env)
    if rule is None:
        return PropRiskVerdict(
            allow=False,
            reason="prop_risk_rule_incomplete",
            context={
                "alias": str(account.get("alias") or ""),
                "missing_fields": missing,
            },
        )

    risk_usd = estimate_bracket_risk_usd(
        symbol=str(getattr(order, "symbol", "")),
        qty=float(getattr(order, "qty", 0.0) or 0.0),
        entry_price=float(getattr(order, "limit_price", 0.0) or 0.0),
        stop_price=float(stop_price),
    )
    if risk_usd is None or risk_usd <= 0.0:
        return PropRiskVerdict(
            allow=False,
            reason="prop_risk_unknown_contract",
            context={
                "alias": rule.alias,
                "symbol": getattr(order, "symbol", ""),
                "risk_usd": risk_usd,
            },
        )

    context = _rule_context(rule, risk_usd)
    consistency_room = rule.consistency_room_usd
    if consistency_room is not None and consistency_room <= 0.0:
        return PropRiskVerdict(
            allow=False,
            reason="prop_consistency_profit_cap_reached",
            context=context,
        )
    if risk_usd > rule.usable_entry_room_usd:
        return PropRiskVerdict(
            allow=False,
            reason="prop_risk_exceeds_headroom",
            context=context,
        )
    return PropRiskVerdict(
        allow=True,
        reason="prop_risk_within_headroom",
        context=context,
    )


def prop_order_risk_denial(
    order: PendingOrderLike,
    account: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Broker-router adapter: return a gate denial dict or ``None``."""

    verdict = evaluate_prop_order(order, account, env=env)
    if verdict.allow:
        return None
    return {
        "gate": "prop_risk_governor",
        "allow": False,
        "reason": verdict.reason,
        "context": verdict.context,
    }


def _rule_from_account(
    account: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[PropRiskRule | None, list[str]]:
    env_map = env if env is not None else os.environ
    missing: list[str] = []

    def required(key: str) -> float | None:
        value = _resolve_float(account, key, env=env_map)
        if value is None:
            missing.append(key)
        return value

    starting_balance = required("starting_balance_usd")
    current_equity = required("current_equity_usd")
    trailing_drawdown = required("trailing_drawdown_usd")
    daily_loss_limit = required("daily_loss_limit_usd")

    peak_configured = (
        "peak_equity_usd" in account
        or "peak_equity_env" in account
        or "peak_equity_usd_env" in account
    )
    peak_equity = _resolve_float(account, "peak_equity_usd", env=env_map)
    if peak_configured and peak_equity is None:
        missing.append("peak_equity_usd")

    if missing:
        return None, sorted(set(missing))

    assert starting_balance is not None
    assert current_equity is not None
    assert trailing_drawdown is not None
    assert daily_loss_limit is not None
    if peak_equity is None:
        peak_equity = max(starting_balance, current_equity)

    daily_loss_used = _resolve_float(account, "daily_loss_used_usd", env=env_map)
    if daily_loss_used is None:
        realized_pnl = _resolve_float(account, "daily_realized_pnl_usd", env=env_map)
        daily_loss_used = max(0.0, -(realized_pnl or 0.0))

    buffer_usd = _resolve_float(account, "liquidation_buffer_usd", env=env_map)
    max_order_risk = _resolve_float(account, "max_order_risk_usd", env=env_map)
    open_risk = _resolve_float(account, "open_risk_usd", env=env_map)
    realized_pnl = _resolve_float(account, "daily_realized_pnl_usd", env=env_map)
    daily_profit = _resolve_float(account, "daily_profit_usd", env=env_map)
    if daily_profit is None:
        daily_profit = max(0.0, realized_pnl or 0.0)
    consistency_cap = _consistency_profit_cap(account, env=env_map)
    consistency_buffer = _resolve_float(account, "consistency_buffer_usd", env=env_map)

    values = (
        starting_balance,
        current_equity,
        peak_equity,
        trailing_drawdown,
        daily_loss_limit,
        daily_loss_used,
        buffer_usd if buffer_usd is not None else 250.0,
        open_risk if open_risk is not None else 0.0,
        daily_profit,
        consistency_buffer if consistency_buffer is not None else 0.0,
    )
    if not all(math.isfinite(v) for v in values):
        return None, ["non_finite_prop_risk_value"]
    if starting_balance <= 0.0 or current_equity <= 0.0:
        return None, ["non_positive_prop_equity"]
    if trailing_drawdown <= 0.0 or daily_loss_limit <= 0.0:
        return None, ["non_positive_prop_limit"]

    return (
        PropRiskRule(
            alias=str(account.get("alias") or "prop_account"),
            starting_balance_usd=starting_balance,
            current_equity_usd=current_equity,
            peak_equity_usd=peak_equity,
            trailing_drawdown_usd=trailing_drawdown,
            daily_loss_limit_usd=daily_loss_limit,
            daily_loss_used_usd=max(0.0, daily_loss_used),
            liquidation_buffer_usd=buffer_usd if buffer_usd is not None else 250.0,
            max_order_risk_usd=max_order_risk,
            open_risk_usd=max(0.0, open_risk if open_risk is not None else 0.0),
            daily_profit_usd=max(0.0, daily_profit),
            consistency_profit_cap_usd=consistency_cap,
            consistency_buffer_usd=max(
                0.0,
                consistency_buffer if consistency_buffer is not None else 0.0,
            ),
        ),
        [],
    )


def _resolve_float(
    account: Mapping[str, object],
    key: str,
    *,
    env: Mapping[str, str],
) -> float | None:
    env_field = _env_field_name(key)
    explicit_env_key = str(account.get(env_field) or account.get(f"{key}_env") or "").strip()
    raw = (
        str(env.get(explicit_env_key) or "").strip()
        if explicit_env_key
        else str(account.get(key) or "").strip()
    )
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _env_field_name(key: str) -> str:
    if key.endswith("_usd"):
        return f"{key[:-4]}_env"
    return f"{key}_env"


def _consistency_profit_cap(
    account: Mapping[str, object],
    *,
    env: Mapping[str, str],
) -> float | None:
    cap = _resolve_float(account, "consistency_profit_cap_usd", env=env)
    if cap is not None:
        return cap
    target_profit = _resolve_float(account, "target_profit_usd", env=env)
    pct = _resolve_float(
        account,
        "consistency_max_day_profit_pct_of_target",
        env=env,
    )
    if target_profit is None or pct is None:
        return None
    pct_fraction = pct / 100.0 if pct > 1.0 else pct
    if target_profit <= 0.0 or pct_fraction <= 0.0:
        return None
    return round(target_profit * pct_fraction, 2)


def _rule_context(rule: PropRiskRule, risk_usd: float) -> dict[str, Any]:
    return {
        "alias": rule.alias,
        "risk_usd": round(risk_usd, 2),
        "starting_balance_usd": round(rule.starting_balance_usd, 2),
        "current_equity_usd": round(rule.current_equity_usd, 2),
        "peak_equity_usd": round(rule.peak_equity_usd, 2),
        "trailing_floor_usd": round(rule.trailing_floor_usd, 2),
        "daily_room_usd": round(rule.daily_room_usd, 2),
        "trailing_room_usd": round(rule.trailing_room_usd, 2),
        "max_order_risk_usd": (
            None
            if rule.max_order_risk_usd is None
            else round(rule.max_order_risk_usd, 2)
        ),
        "open_risk_usd": round(rule.open_risk_usd, 2),
        "usable_entry_room_usd": round(rule.usable_entry_room_usd, 2),
        "liquidation_buffer_usd": round(rule.liquidation_buffer_usd, 2),
        "daily_profit_usd": round(rule.daily_profit_usd, 2),
        "consistency_profit_cap_usd": (
            None
            if rule.consistency_profit_cap_usd is None
            else round(rule.consistency_profit_cap_usd, 2)
        ),
        "consistency_buffer_usd": round(rule.consistency_buffer_usd, 2),
        "consistency_room_usd": (
            None
            if rule.consistency_room_usd is None
            else round(rule.consistency_room_usd, 2)
        ),
    }


def _futures_root(symbol: str) -> str | None:
    compact = re.sub(r"[^A-Z0-9]", "", str(symbol or "").upper())
    if not compact:
        return None
    for root in _FUTURES_ROOTS:
        if compact in (root, f"{root}1"):
            return root
        if not compact.startswith(root):
            continue
        suffix = compact[len(root):]
        if not suffix:
            return root
        if suffix in {"USD", "USDT"}:
            return None
        if suffix[0] in _MONTH_CODES and any(ch.isdigit() for ch in suffix):
            return root
    return None

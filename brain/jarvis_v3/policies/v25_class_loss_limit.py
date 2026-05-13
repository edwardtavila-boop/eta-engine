"""Candidate policy v25 — PER-CLASS DAILY LOSS LIMIT (2026-05-04 wave-8).

Hypothesis
----------
A bad day in one asset class shouldn't cascade into the others. v25
reads the supervisor heartbeat, aggregates `realized_pnl` per
instrument_class, and FREEZES risk-adding actions for any class whose
loss exceeds the threshold (default -$300 / class). Capital preservation
on rough days.

Wraps v24 (or the lower stack). Falls back to wrapped verdict on any
error.

Design
------
On each request, read `var/eta_engine/state/jarvis_intel/supervisor/
heartbeat.json` (cached for 30s to avoid rereading on every gate),
sum `realized_pnl` over bots whose `instrument_class` falls in the
target class. If that class total < -loss_limit, return DEFERRED for
risk-adding actions in that class.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.brain.jarvis_admin import (
    ActionRequest,
    ActionResponse,
    ActionType,
    Verdict,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.brain.jarvis_context import JarvisContext

logger = logging.getLogger(__name__)


HEARTBEAT_PATH = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\supervisor\heartbeat.json")
_CACHE_TTL_SECONDS = 30.0
_HEARTBEAT_CACHE: dict[str, float | dict] = {"loaded_at": 0.0, "data": {}}


def _class_loss_limit() -> float:
    """Daily realized-PnL floor before a class is frozen. Negative number."""
    try:
        return float(os.environ.get("JARVIS_V25_CLASS_LOSS_LIMIT", "-300"))
    except (TypeError, ValueError):
        return -300.0


def _load_heartbeat_cached() -> dict:
    now = time.time()
    if (
        isinstance(_HEARTBEAT_CACHE.get("data"), dict)
        and _HEARTBEAT_CACHE["data"]
        and now - float(_HEARTBEAT_CACHE.get("loaded_at", 0.0)) < _CACHE_TTL_SECONDS
    ):
        return _HEARTBEAT_CACHE["data"]  # type: ignore[return-value]
    if not HEARTBEAT_PATH.exists():
        return {}
    try:
        data = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    _HEARTBEAT_CACHE["data"] = data
    _HEARTBEAT_CACHE["loaded_at"] = now
    return data


def _class_realized_pnl(class_name: str) -> float | None:
    """Sum realized_pnl across bots whose instrument_class matches."""
    if not class_name:
        return None
    hb = _load_heartbeat_cached()
    bots = hb.get("bots") if isinstance(hb, dict) else None
    if not isinstance(bots, list):
        return None
    try:
        from eta_engine.brain.jarvis_v3.policies.v23_fleet_aware import (
            _INSTRUMENT_CLASS_TO_BROAD,
        )
        from eta_engine.strategies.per_bot_registry import get_for_bot
    except Exception:  # noqa: BLE001
        return None

    total = 0.0
    matched = 0
    for bot_state in bots:
        if not isinstance(bot_state, dict):
            continue
        bot_id = str(bot_state.get("bot_id") or "").strip()
        if not bot_id:
            continue
        try:
            a = get_for_bot(bot_id)
        except Exception:  # noqa: BLE001
            continue
        if a is None:
            continue
        try:
            raw = str(a.extras.get("instrument_class", "")).strip().lower()
        except Exception:  # noqa: BLE001
            continue
        broad = _INSTRUMENT_CLASS_TO_BROAD.get(raw, "")
        if broad != class_name:
            continue
        try:
            pnl = float(bot_state.get("realized_pnl") or 0.0)
        except (TypeError, ValueError):
            continue
        total += pnl
        matched += 1
    return total if matched > 0 else None


def _resolve_class(req: ActionRequest) -> str:
    bot_id = ""
    try:
        bot_id = str(req.payload.get("bot_id") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    if not bot_id:
        return ""
    try:
        from eta_engine.brain.jarvis_v3.policies.v23_fleet_aware import (
            _INSTRUMENT_CLASS_TO_BROAD,
        )
        from eta_engine.strategies.per_bot_registry import get_for_bot

        a = get_for_bot(bot_id)
    except Exception:  # noqa: BLE001
        return ""
    if a is None:
        return ""
    try:
        raw = str(a.extras.get("instrument_class", "")).strip().lower()
    except Exception:  # noqa: BLE001
        return ""
    return _INSTRUMENT_CLASS_TO_BROAD.get(raw, "")


def evaluate_v25(
    req: ActionRequest,
    ctx: JarvisContext,
    *,
    base_resp: ActionResponse | None = None,
    wrapped_evaluator: Callable[[ActionRequest, JarvisContext], ActionResponse] | None = None,
) -> ActionResponse:
    """v25 layer. Freezes risk-adding actions when class daily loss > limit."""
    if base_resp is None:
        if wrapped_evaluator is None:
            from eta_engine.brain.jarvis_v3.policies.v24_correlation_throttle import evaluate_v24

            base_resp = evaluate_v24(req, ctx)
        else:
            base_resp = wrapped_evaluator(req, ctx)

    risk_actions = {
        ActionType.SIGNAL_EMIT,
        ActionType.ORDER_PLACE,
        ActionType.STRATEGY_DEPLOY,
        ActionType.CAPITAL_ALLOCATE,
    }
    if req.action not in risk_actions:
        return base_resp
    if base_resp.verdict not in (Verdict.APPROVED, Verdict.CONDITIONAL):
        return base_resp

    cls = _resolve_class(req)
    if not cls:
        return base_resp

    pnl = _class_realized_pnl(cls)
    if pnl is None:
        return base_resp

    limit = _class_loss_limit()
    if pnl <= limit:
        bot_id = str(req.payload.get("bot_id") or "") if isinstance(req.payload, dict) else ""
        try:
            from eta_engine.brain.jarvis_v3.policies._v3_events import emit_event

            emit_event(
                layer="v25",
                event="class_loss_freeze",
                bot_id=bot_id,
                cls=cls,
                details={"realized_pnl": round(pnl, 2), "limit": limit},
                severity="WARN",
            )
        except Exception:  # noqa: BLE001
            pass
        return base_resp.model_copy(
            update={
                "verdict": Verdict.DEFERRED,
                "reason": f"v25 class loss limit: {cls} realized PnL ${pnl:.2f} <= ${limit:.2f}",
                "reason_code": "v25_class_loss_freeze",
                "conditions": (base_resp.conditions or [])
                + [
                    f"frozen_class={cls}",
                    f"class_realized_pnl={pnl:.2f}",
                    f"loss_limit={limit:.2f}",
                ],
            }
        )
    return base_resp


def reset_cache() -> None:
    _HEARTBEAT_CACHE["data"] = {}
    _HEARTBEAT_CACHE["loaded_at"] = 0.0

"""Candidate policy v24 — CORRELATION THROTTLE (2026-05-04 wave-8).

Hypothesis
----------
When 3+ bots in the same instrument class all want to enter long (or all
short) within a few minutes, they're all tracking the same underlying
move. Approving all of them stacks correlated risk. v24 enforces a
per-class concurrent-entry cap.

Wraps v23 (which wraps v22/v17). Falls back to wrapped verdict on any
error. Activated by JARVIS_V3_ADVANCED env / feature flag.

Design
------
Module-level ring buffer per (instrument_class, side) tracks recent
SIGNAL_EMIT / ORDER_PLACE approvals. When a new request would push
the count over `max_concurrent_per_class_side` within the
`window_seconds` lookback, return DEFERRED.

State is in-process — resets on supervisor restart. That's fine for a
5-minute window: cold-start risk is low and the supervisor restarts
infrequently.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING

from eta_engine.brain.jarvis_admin import (
    ActionRequest,
    ActionResponse,
    ActionType,
    Verdict,
)

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_context import JarvisContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds (env-tunable)
# ---------------------------------------------------------------------------


def _max_concurrent_per_class_side() -> int:
    try:
        return int(os.environ.get("JARVIS_V24_MAX_CONCURRENT", "3"))
    except (TypeError, ValueError):
        return 3


def _window_seconds() -> float:
    try:
        return float(os.environ.get("JARVIS_V24_WINDOW_SECONDS", "300"))
    except (TypeError, ValueError):
        return 300.0


# ---------------------------------------------------------------------------
# In-process ring buffer
# ---------------------------------------------------------------------------


# key: (class, side)  →  deque[timestamp]
# At most max_concurrent_per_class_side + 1 entries kept (older evicted).
_RECENT_APPROVALS: dict[tuple[str, str], deque[float]] = defaultdict(
    lambda: deque(maxlen=10)
)


def _prune_old(buffer: deque[float], window_s: float, now: float) -> None:
    """Drop entries older than `window_s` seconds before `now`."""
    cutoff = now - window_s
    while buffer and buffer[0] < cutoff:
        buffer.popleft()


def _resolve_class_and_side(req: ActionRequest, ctx: "JarvisContext") -> tuple[str, str]:
    """Return (class, side) for v24 bookkeeping. Empty class disables throttle."""
    bot_id = ""
    try:
        bot_id = str(req.payload.get("bot_id") or "").strip()
    except Exception:  # noqa: BLE001
        return ("", "")
    if not bot_id:
        return ("", "")
    try:
        from eta_engine.brain.jarvis_v3.policies.v23_fleet_aware import (
            _INSTRUMENT_CLASS_TO_BROAD,
        )
        from eta_engine.strategies.per_bot_registry import get_for_bot
        a = get_for_bot(bot_id)
    except Exception:  # noqa: BLE001
        return ("", "")
    if a is None:
        return ("", "")
    raw = ""
    try:
        raw = str(a.extras.get("instrument_class", "")).strip().lower()
    except Exception:  # noqa: BLE001
        return ("", "")
    cls = _INSTRUMENT_CLASS_TO_BROAD.get(raw, "")
    side_raw = str(req.payload.get("side") or "").strip().lower()
    if side_raw in {"long", "buy"}:
        side = "long"
    elif side_raw in {"short", "sell"}:
        side = "short"
    else:
        side = "unknown"
    return (cls, side)


def evaluate_v24(
    req: ActionRequest,
    ctx: "JarvisContext",
    *,
    base_resp: ActionResponse | None = None,
    wrapped_evaluator=None,
) -> ActionResponse:
    """v24 layer. Wraps a base verdict (typically v23's) with concurrent-entry
    throttle.

    If `base_resp` is provided, treats it as the wrapped policy's verdict.
    Otherwise calls `wrapped_evaluator` (must be passed).
    """
    if base_resp is None:
        if wrapped_evaluator is None:
            from eta_engine.brain.jarvis_v3.policies.v23_fleet_aware import evaluate_v23
            base_resp = evaluate_v23(req, ctx)
        else:
            base_resp = wrapped_evaluator(req, ctx)

    # Only throttle risk-adding actions that the wrapped policy approved.
    risk_actions = {ActionType.SIGNAL_EMIT, ActionType.ORDER_PLACE}
    if req.action not in risk_actions:
        return base_resp
    if base_resp.verdict not in (Verdict.APPROVED, Verdict.CONDITIONAL):
        return base_resp

    cls, side = _resolve_class_and_side(req, ctx)
    if not cls or side == "unknown":
        # Can't classify — don't interfere with the base verdict.
        return base_resp

    key = (cls, side)
    window_s = _window_seconds()
    max_n = _max_concurrent_per_class_side()
    now = time.time()
    buffer = _RECENT_APPROVALS[key]
    _prune_old(buffer, window_s, now)

    if len(buffer) >= max_n:
        # Throttle: too many recent same-side approvals in this class.
        bot_id = str(req.payload.get("bot_id") or "") if isinstance(req.payload, dict) else ""
        try:
            from eta_engine.brain.jarvis_v3.policies._v3_events import emit_event
            emit_event(
                layer="v24", event="correlation_throttle",
                bot_id=bot_id, cls=cls,
                details={"side": side, "recent_count": len(buffer),
                         "max_per_window": max_n, "window_s": int(window_s)},
                severity="INFO",
            )
        except Exception:  # noqa: BLE001
            pass
        return base_resp.model_copy(update={
            "verdict": Verdict.DEFERRED,
            "reason": f"v24 correlation throttle: {len(buffer)} {cls}/{side} approvals in last {int(window_s)}s",
            "reason_code": "v24_correlation_throttle",
            "conditions": (base_resp.conditions or []) + [
                f"throttle_class={cls}",
                f"throttle_side={side}",
                f"recent_count={len(buffer)}",
                f"max_per_window={max_n}",
            ],
        })

    # Record this approval so future requests see it
    buffer.append(now)
    return base_resp


def reset_state() -> None:
    """Clear the in-process buffer. Used by tests."""
    _RECENT_APPROVALS.clear()

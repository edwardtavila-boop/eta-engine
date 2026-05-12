"""Candidate policy v27 — LIVE-VS-LAB SHARPE DRIFT (2026-05-04 wave-8).

Hypothesis
----------
A bot's lab sharpe is the operator's pre-deployment estimate of edge.
If the live realized expectancy diverges from that lab number — say
live exp_R is 30% of lab exp_R after 10+ trades — that's regime change,
broken parameters, or genuine alpha decay. v27 reads heartbeat metrics,
estimates per-bot live R-expectancy (`realized_pnl / n_exits` proxied
into per-trade dollars), normalizes to lab `exp_R`, and downgrades size
when drift exceeds the threshold.

Wraps v26 (or the lower stack). Falls back to wrapped verdict on any
error.

Caveats
-------
realized_pnl is in account currency, not R-multiples. We can only
compute a relative proxy: live_per_trade = realized_pnl / n_exits.
If that's negative when lab said exp_R > 0, that's the alpha-decay
signal. We don't try to normalize to R precisely; just check sign +
magnitude relative to lab claim.
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


HEARTBEAT_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\supervisor\heartbeat.json"
)
_CACHE_TTL_SECONDS = 30.0
_HEARTBEAT_CACHE: dict[str, float | dict] = {"loaded_at": 0.0, "data": {}}


def _min_exits_for_drift_check() -> int:
    """Don't penalize bots until they have at least N exits — small samples lie."""
    try:
        return int(os.environ.get("JARVIS_V27_MIN_EXITS", "10"))
    except (TypeError, ValueError):
        return 10


def _drift_threshold_factor() -> float:
    """If live PnL/exit < (factor * lab exp_R), treat as drifted."""
    try:
        return float(os.environ.get("JARVIS_V27_DRIFT_FACTOR", "0.30"))
    except (TypeError, ValueError):
        return 0.30


def _drifted_size_factor() -> float:
    """Size cap multiplier applied to drifted bots."""
    try:
        return float(os.environ.get("JARVIS_V27_DRIFTED_SIZE", "0.50"))
    except (TypeError, ValueError):
        return 0.50


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


def _bot_state(bot_id: str) -> dict | None:
    if not bot_id:
        return None
    hb = _load_heartbeat_cached()
    bots = hb.get("bots") if isinstance(hb, dict) else None
    if not isinstance(bots, list):
        return None
    for bs in bots:
        if isinstance(bs, dict) and str(bs.get("bot_id") or "") == bot_id:
            return bs
    return None


def _lab_exp_r(bot_id: str) -> float | None:
    """Pull lab `exp_R` from the registry's most-recent lab_audit stamp."""
    try:
        from eta_engine.strategies.per_bot_registry import get_for_bot
        a = get_for_bot(bot_id)
    except Exception:  # noqa: BLE001
        return None
    if a is None:
        return None
    extras = a.extras if a.extras else {}
    if not isinstance(extras, dict):
        return None
    candidates = []
    for k, v in extras.items():
        if isinstance(k, str) and isinstance(v, dict) and (
            k.startswith("lab_audit_") or k.startswith("lab_promotion_")
        ):
            candidates.append((k, v))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0], reverse=True)
    for _, stamp in candidates:
        v = stamp.get("exp_R")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            continue
    return None


def _is_drifted(state: dict, lab_exp_r: float) -> tuple[bool, float]:
    """Returns (drifted, ratio) where ratio = (live_pnl_per_exit / lab_exp_r)."""
    n_exits = state.get("n_exits", 0)
    if not isinstance(n_exits, (int, float)) or n_exits < _min_exits_for_drift_check():
        return (False, 1.0)
    try:
        realized_pnl = float(state.get("realized_pnl") or 0.0)
    except (TypeError, ValueError):
        return (False, 1.0)
    live_per_exit = realized_pnl / max(float(n_exits), 1.0)
    # Compare sign-and-magnitude. If lab said positive expectancy and live is
    # negative, definitely drifted. If both positive but live is way smaller,
    # also drifted.
    if lab_exp_r > 0:
        if live_per_exit <= 0:
            return (True, 0.0)
        ratio = live_per_exit / max(lab_exp_r, 1e-9)
        return (ratio < _drift_threshold_factor(), ratio)
    return (False, 1.0)  # if lab itself was negative/zero, can't drift


def evaluate_v27(
    req: ActionRequest,
    ctx: JarvisContext,
    *,
    base_resp: ActionResponse | None = None,
    wrapped_evaluator: Callable[[ActionRequest, JarvisContext], ActionResponse] | None = None,
) -> ActionResponse:
    """v27 layer. Downgrades size when live realized expectancy drifts from lab."""
    if base_resp is None:
        if wrapped_evaluator is None:
            from eta_engine.brain.jarvis_v3.policies.v26_fill_confirmation import evaluate_v26
            base_resp = evaluate_v26(req, ctx)
        else:
            base_resp = wrapped_evaluator(req, ctx)

    risk_actions = {ActionType.SIGNAL_EMIT, ActionType.ORDER_PLACE}
    if req.action not in risk_actions:
        return base_resp
    if base_resp.verdict not in (Verdict.APPROVED, Verdict.CONDITIONAL):
        return base_resp

    bot_id = ""
    try:
        bot_id = str(req.payload.get("bot_id") or "").strip()
    except Exception:  # noqa: BLE001
        return base_resp
    if not bot_id:
        return base_resp

    lab_exp = _lab_exp_r(bot_id)
    if lab_exp is None:
        return base_resp  # untested bots already got 0.30x from v23

    state = _bot_state(bot_id)
    if state is None:
        return base_resp

    drifted, ratio = _is_drifted(state, lab_exp)
    if drifted:
        factor = _drifted_size_factor()
        existing_cap = base_resp.size_cap_mult
        new_cap = factor if existing_cap is None else min(existing_cap, factor)
        try:
            from eta_engine.brain.jarvis_v3.policies._v3_events import emit_event
            emit_event(
                layer="v27", event="sharpe_drift",
                bot_id=bot_id,
                details={"lab_exp_r": round(lab_exp, 3), "live_ratio": round(ratio, 3),
                         "size_factor": factor},
                severity="WARN",
            )
        except Exception:  # noqa: BLE001
            pass
        return base_resp.model_copy(update={
            "verdict": Verdict.CONDITIONAL,
            "reason": f"v27 sharpe drift: live/lab ratio {ratio:.2f} < {_drift_threshold_factor():.2f}",
            "reason_code": "v27_sharpe_drift",
            "size_cap_mult": new_cap,
            "conditions": (base_resp.conditions or []) + [
                f"v27_lab_exp_r={lab_exp:.3f}",
                f"v27_live_ratio={ratio:.2f}",
                f"v27_size_factor={factor:.2f}",
            ],
        })
    return base_resp


def reset_cache() -> None:
    _HEARTBEAT_CACHE["data"] = {}
    _HEARTBEAT_CACHE["loaded_at"] = 0.0


def evaluate_advanced_stack(req: ActionRequest, ctx: JarvisContext) -> ActionResponse:
    """Run the full advanced stack: v23 → v24 → v25 → v26 → v27.

    Each layer wraps the prior. Single entrypoint for JarvisAdmin's
    JARVIS_V3_ADVANCED dispatch.
    """
    from eta_engine.brain.jarvis_v3.policies.v23_fleet_aware import evaluate_v23
    from eta_engine.brain.jarvis_v3.policies.v24_correlation_throttle import evaluate_v24
    from eta_engine.brain.jarvis_v3.policies.v25_class_loss_limit import evaluate_v25
    from eta_engine.brain.jarvis_v3.policies.v26_fill_confirmation import evaluate_v26

    resp = evaluate_v23(req, ctx)
    resp = evaluate_v24(req, ctx, base_resp=resp)
    resp = evaluate_v25(req, ctx, base_resp=resp)
    resp = evaluate_v26(req, ctx, base_resp=resp)
    resp = evaluate_v27(req, ctx, base_resp=resp)
    return resp

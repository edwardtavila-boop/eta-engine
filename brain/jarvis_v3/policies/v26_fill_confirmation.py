"""Candidate policy v26 — FILL-CONFIRMATION HEALTH (2026-05-04 wave-8).

Hypothesis
----------
A bot whose JARVIS-approved signals don't translate to actual fills is
broken — either its execution path is misconfigured, the venue is rejecting,
or there's a wire-up bug. v26 detects this drift by comparing the bot's
heartbeat metrics:

  * `last_signal_at` — most recent SIGNAL_EMIT
  * `n_entries`      — supervisor-tracked sim entries
  * `last_bar_ts`    — last bar processed

If the bot has been emitting signals (last_signal_at recent) but no
n_entries increment over a window, that's an EXECUTION HEALTH problem.
Reduce trust by capping size_cap_mult to 0.50x (or freeze if
silent for too long).

Wraps v25 (or the lower stack). Falls back to wrapped verdict on any
error.

Note: in DeepSeek's paper_live path, orders go direct via LiveIbkrVenue,
bypassing broker_router. So we can't just count broker_router_fills.jsonl
entries. The supervisor's `n_entries` is the most reliable execution
signal — it increments on a successful entry regardless of routing path.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
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


HEARTBEAT_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\supervisor\heartbeat.json"
)
# Secondary signal: broker_router fills journal. If supervisor's path goes
# through broker_router (queue-based), failed/rejected fills land here.
# DeepSeek's LiveIbkrVenue path bypasses this — heartbeat n_entries is the
# primary signal; broker_router_fills is corroborating evidence when present.
BROKER_ROUTER_FILLS_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\broker_router_fills.jsonl"
)
_CACHE_TTL_SECONDS = 30.0
_HEARTBEAT_CACHE: dict[str, float | dict] = {"loaded_at": 0.0, "data": {}}
_BROKER_FILLS_CACHE: dict[str, float | list] = {"loaded_at": 0.0, "data": []}


def _stale_signal_seconds() -> float:
    """If signal age > N but n_entries hasn't moved, flag as degraded."""
    try:
        return float(os.environ.get("JARVIS_V26_STALE_SIGNAL_SECONDS", "1800"))
    except (TypeError, ValueError):
        return 1800.0


def _degraded_size_factor() -> float:
    try:
        return float(os.environ.get("JARVIS_V26_DEGRADED_FACTOR", "0.50"))
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


def _load_broker_router_fills_cached() -> list[dict]:
    """Read recent broker_router fills (last 200 lines)."""
    now = time.time()
    if (
        isinstance(_BROKER_FILLS_CACHE.get("data"), list)
        and now - float(_BROKER_FILLS_CACHE.get("loaded_at", 0.0)) < _CACHE_TTL_SECONDS
    ):
        return _BROKER_FILLS_CACHE["data"]  # type: ignore[return-value]
    if not BROKER_ROUTER_FILLS_PATH.exists():
        return []
    fills: list[dict] = []
    try:
        with BROKER_ROUTER_FILLS_PATH.open("r", encoding="utf-8") as fh:
            tail = fh.readlines()[-200:]
        for line in tail:
            try:
                row = json.loads(line.strip())
                if isinstance(row, dict):
                    fills.append(row)
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    _BROKER_FILLS_CACHE["data"] = fills
    _BROKER_FILLS_CACHE["loaded_at"] = now
    return fills


def _broker_router_rejects_for_bot(bot_id: str) -> int:
    """Count broker_router rejected/failed fills for this bot in the recent window.

    'Recent' is bounded by ETA_V26_REJECT_WINDOW_S (default 600s = 10 min).
    Older entries are ignored so v26 doesn't spuriously flag bots whose
    rejections come from a previous deployment / a now-fixed bug. Without
    a freshness window v26 would keep firing forever on a single legacy
    bad entry in broker_router_fills.jsonl.
    """
    if not bot_id:
        return 0
    fills = _load_broker_router_fills_cached()
    window_s = float(os.getenv("ETA_V26_REJECT_WINDOW_S", "600"))
    cutoff = datetime.now(UTC).timestamp() - window_s
    rejected = 0
    for f in fills:
        if f.get("bot_id") != bot_id:
            continue
        status = str(f.get("status") or "").lower()
        if not ("reject" in status or "fail" in status or status == "error"):
            continue
        # Parse timestamp; ignore rows older than the freshness window
        ts_str = f.get("ts") or f.get("timestamp") or ""
        try:
            ts_dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts_dt.astimezone(UTC).timestamp() < cutoff:
                continue
        except (ValueError, TypeError):
            # Unparseable timestamp = treat as ancient and skip
            continue
        rejected += 1
    return rejected


def _is_execution_degraded(bot_state: dict) -> bool:
    """True iff bot has emitted a signal recently but n_entries hasn't grown.

    Two corroborating signals:
      1) supervisor heartbeat: signals firing without n_entries increment
      2) broker_router_fills: rejection/failure entries for this bot

    Either path can flag — we're conservative, prefer false-positives
    (size cap) over false-negatives (let a broken bot keep firing).
    """
    last_signal_at = bot_state.get("last_signal_at")
    n_entries = bot_state.get("n_entries", 0)
    bot_id = str(bot_state.get("bot_id") or "")

    # Path 2: broker_router rejections
    if bot_id and _broker_router_rejects_for_bot(bot_id) >= 3:
        return True

    if not last_signal_at:
        return False  # no signals yet → not degraded
    if not isinstance(n_entries, (int, float)) or n_entries <= 0:
        # Path 1: signals but zero entries — possibly degraded, but require
        # checking signal age so we don't false-positive on cold-start bots.
        try:
            sig_dt = datetime.fromisoformat(str(last_signal_at).replace("Z", "+00:00"))
            age = (datetime.now(UTC) - sig_dt.astimezone(UTC)).total_seconds()
        except (ValueError, TypeError):
            return False
        return age > _stale_signal_seconds()
    return False


def evaluate_v26(
    req: ActionRequest,
    ctx: "JarvisContext",
    *,
    base_resp: ActionResponse | None = None,
    wrapped_evaluator=None,
) -> ActionResponse:
    """v26 layer. Reduces size when bot's execution health is degraded."""
    if base_resp is None:
        if wrapped_evaluator is None:
            from eta_engine.brain.jarvis_v3.policies.v25_class_loss_limit import evaluate_v25
            base_resp = evaluate_v25(req, ctx)
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

    state = _bot_state(bot_id)
    if state is None:
        return base_resp

    if _is_execution_degraded(state):
        factor = _degraded_size_factor()
        existing_cap = base_resp.size_cap_mult
        new_cap = factor if existing_cap is None else min(existing_cap, factor)
        try:
            from eta_engine.brain.jarvis_v3.policies._v3_events import emit_event
            emit_event(
                layer="v26", event="execution_degraded",
                bot_id=bot_id,
                details={"size_factor": factor,
                         "broker_router_rejects": _broker_router_rejects_for_bot(bot_id)},
                severity="WARN",
            )
        except Exception:  # noqa: BLE001
            pass
        return base_resp.model_copy(update={
            "verdict": Verdict.CONDITIONAL,
            "reason": f"v26 execution-health degraded: signals firing without entries, size halved",
            "reason_code": "v26_execution_degraded",
            "size_cap_mult": new_cap,
            "conditions": (base_resp.conditions or []) + [
                "v26_degraded=True",
                f"v26_size_factor={factor:.2f}",
            ],
        })
    return base_resp


def reset_cache() -> None:
    _HEARTBEAT_CACHE["data"] = {}
    _HEARTBEAT_CACHE["loaded_at"] = 0.0
    _BROKER_FILLS_CACHE["data"] = []
    _BROKER_FILLS_CACHE["loaded_at"] = 0.0

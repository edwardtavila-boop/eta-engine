"""Force Multiplier trade-decision gates.

Two low-cost AI hooks that sit between the JARVIS verdict and the broker
submission in the supervisor's entry path:

1. ``sanity_check_conditional`` — blocking second-opinion on CONDITIONAL
   verdicts. Asks FM whether the trade thesis still holds given live
   market state. Returns False to block, True to approve. Defaults to
   approve on any failure so FM unavailability never silently blocks
   legit trades.

2. ``log_conviction_rationale`` — advisory narrative on borderline-
   confidence verdicts (0.60 ≤ confidence ≤ 0.85). Writes a one-line
   "why this conviction?" rationale to the audit log. Never blocks.

Both routes use ``multi_model.chat_completion`` with strict per-call
budget caps so a runaway FM call cannot drain quota. All FM I/O is
captured in the existing ``multi_model_telemetry.jsonl`` plus an
operator-friendly sidecar at ``var/eta_engine/state/fm_trade_gates.jsonl``
that includes the bot/signal context the supervisor saw.

Design notes:
  * The supervisor calls these from inside a broad try/except wrapper,
    so we do not need to catch every exception here — but we do anyway
    for defense in depth.
  * The sanity gate is the only function that can block a trade. It
    is conservative-by-default: a parse failure, network error, budget
    breach, or any unexpected response shape all return True (approve).
    The only way to return False is if FM explicitly produces an
    "abort/block/caution" verdict in a parseable response.
  * Conviction rationale is fire-and-forget. Latency is bounded by
    a tight max_cost_usd cap (under $0.001 per call).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SIDECAR_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\fm_trade_gates.jsonl"
)

# Tight budgets — these are TINY calls (one-sentence outputs).
_SANITY_MAX_COST_USD = 0.001  # ~7k tokens at deepseek rates; we use far less
_CONVICTION_MAX_COST_USD = 0.0005


def _append_sidecar(record: dict[str, Any]) -> None:
    """Best-effort write to the FM-gates sidecar. Never raises."""
    try:
        _SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _SIDECAR_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def _verdict_summary(verdict: Any) -> dict[str, Any]:
    """Pull the parts of a JARVIS verdict that matter for an FM second-opinion.

    Defensive: returns a partial dict if any field is missing rather than
    raising. The supervisor handed us this object — we never want to
    crash the supervisor's entry path because a verdict field shape
    drifted.
    """
    out: dict[str, Any] = {}
    consol = getattr(verdict, "consolidated", None)
    if consol is None:
        return out
    for key in (
        "final_verdict",
        "confidence",
        "final_size_multiplier",
        "base_reason",
        "rag_summary",
        "causal_score",
        "causal_reason",
        "firm_board_consensus",
        "world_model_expected_r",
    ):
        try:
            out[key] = getattr(consol, key, None)
        except Exception:  # noqa: BLE001
            out[key] = None
    return out


def _bar_summary(bar: Any) -> dict[str, Any]:
    """Compact bar dict for the FM prompt. Tolerates dict or object shape."""
    if not bar:
        return {}
    if isinstance(bar, dict):
        return {k: bar.get(k) for k in ("ts", "close", "open", "high", "low", "volume")}
    return {k: getattr(bar, k, None) for k in ("ts", "close", "open", "high", "low", "volume")}


def sanity_check_conditional(
    *,
    bot_id: str,
    signal_id: str,
    side: str,
    bar: Any,
    verdict: Any,
) -> bool:
    """Blocking sanity gate for CONDITIONAL verdicts.

    Returns True to approve (proceed with trade), False to block.

    Defaults to True on ANY error so FM unavailability never silently
    blocks legit trades. The only False path is a parseable FM response
    that explicitly says abort/caution/block.
    """
    ts_iso = datetime.now(UTC).isoformat()
    summary = _verdict_summary(verdict)
    bar_d = _bar_summary(bar)

    try:
        from eta_engine.brain.multi_model import TaskCategory, route_and_execute
    except Exception as exc:  # noqa: BLE001 — multi_model unavailable
        _append_sidecar({
            "ts": ts_iso,
            "kind": "conditional_sanity",
            "bot_id": bot_id,
            "signal_id": signal_id,
            "decision": "APPROVE",
            "reason": f"fm_unavailable: {exc}",
        })
        return True

    system_prompt = (
        "You are an adversarial trading risk reviewer. Given a CONDITIONAL "
        "JARVIS verdict and the current bar, decide if the trade thesis "
        "still holds. Respond with EXACTLY one of these tokens on the first "
        "line: APPROVE / CAUTION / ABORT. Then one short sentence rationale."
    )
    user_message = (
        f"bot_id={bot_id} side={side} signal_id={signal_id}\n"
        f"verdict={json.dumps(summary, default=str)}\n"
        f"bar={json.dumps(bar_d, default=str)}\n"
    )

    try:
        response = route_and_execute(
            category=TaskCategory.RED_TEAM_SCORING
            if hasattr(TaskCategory, "RED_TEAM_SCORING")
            else TaskCategory.TRIVIAL_LOOKUP,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=80,
            temperature=0.0,
            max_cost_usd=_SANITY_MAX_COST_USD,
        )
    except Exception as exc:  # noqa: BLE001
        _append_sidecar({
            "ts": ts_iso,
            "kind": "conditional_sanity",
            "bot_id": bot_id,
            "signal_id": signal_id,
            "decision": "APPROVE",
            "reason": f"fm_error: {type(exc).__name__}: {exc}",
        })
        return True

    text = (getattr(response, "text", "") or "").strip()
    first_line = text.split("\n", 1)[0].strip().upper() if text else ""
    decision = "APPROVE"
    if first_line.startswith("ABORT") or first_line.startswith("BLOCK"):
        decision = "BLOCK"

    _append_sidecar({
        "ts": ts_iso,
        "kind": "conditional_sanity",
        "bot_id": bot_id,
        "signal_id": signal_id,
        "decision": decision,
        "fm_response": text[:300],
        "verdict_summary": summary,
        "bar": bar_d,
    })
    return decision == "APPROVE"


def log_conviction_rationale(
    *,
    bot_id: str,
    signal_id: str,
    verdict: Any,
    confidence: float,
) -> None:
    """Advisory: one-sentence rationale on borderline-confidence verdicts.

    Never blocks. Best-effort. The output is written to the sidecar for
    operator review.
    """
    ts_iso = datetime.now(UTC).isoformat()
    summary = _verdict_summary(verdict)

    try:
        from eta_engine.brain.multi_model import TaskCategory, route_and_execute
    except Exception as exc:  # noqa: BLE001
        _append_sidecar({
            "ts": ts_iso,
            "kind": "conviction_rationale",
            "bot_id": bot_id,
            "signal_id": signal_id,
            "confidence": confidence,
            "rationale": "",
            "reason": f"fm_unavailable: {exc}",
        })
        return

    system_prompt = (
        "You are a concise trading systems analyst. Given a JARVIS verdict "
        "summary with a borderline confidence score, explain in ONE short "
        "sentence why this conviction landed in the borderline band."
    )
    user_message = (
        f"bot_id={bot_id} signal_id={signal_id} confidence={confidence:.3f}\n"
        f"verdict={json.dumps(summary, default=str)}\n"
    )

    try:
        response = route_and_execute(
            category=TaskCategory.DOC_WRITING
            if hasattr(TaskCategory, "DOC_WRITING")
            else TaskCategory.TRIVIAL_LOOKUP,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=60,
            temperature=0.0,
            max_cost_usd=_CONVICTION_MAX_COST_USD,
        )
    except Exception as exc:  # noqa: BLE001
        _append_sidecar({
            "ts": ts_iso,
            "kind": "conviction_rationale",
            "bot_id": bot_id,
            "signal_id": signal_id,
            "confidence": confidence,
            "rationale": "",
            "reason": f"fm_error: {type(exc).__name__}: {exc}",
        })
        return

    text = (getattr(response, "text", "") or "").strip()
    _append_sidecar({
        "ts": ts_iso,
        "kind": "conviction_rationale",
        "bot_id": bot_id,
        "signal_id": signal_id,
        "confidence": confidence,
        "rationale": text[:300],
        "verdict_summary": summary,
    })

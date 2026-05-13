"""
JARVIS v3 // consult_replay (T7)

Deterministic re-execution of a SINGLE past consult with optional
hypothetical overrides. Answers "what would JARVIS have done at 14:23
yesterday if I had pinned momentum=1.5 instead?"

NOT to be confused with the older ``replay_engine`` module which does
multi-day journal replay against today's policy stack. This module is
the operator-facing counterfactual surface: pick a consult, ask
"what if," see the alt verdict.

Cascade model (v1)
------------------

Same surrogate-cascade caveat as T6 (causal_attribution): the consult
cascade is approximated by a weighted sum of school scores. The
surrogate captures verdict-direction faithfully (PROCEED ↔ AVOID
crossings); size-magnitude is approximate. A future v2 can swap in the
real ``jarvis_full.JarvisFull.consult`` re-invocation using the v2
trace snapshot.

Public interface
----------------

* ``replay(consult_id, override_overrides=..., override_hot_weights=...,
            override_school_inputs=...)`` — full re-execution.
* ``counterfactual(consult_id, pin_size_modifier=..., pin_school=...,
                    pin_weight=...)`` — operator-friendly wrapper.
* ``ReplayResult`` dataclass.

NEVER raises. All failure paths return a ReplayResult with the
``error`` field set.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.consult_replay")

EXPECTED_HOOKS = ("replay", "counterfactual")


@dataclass(frozen=True)
class ReplayResult:
    consult_id: str
    base_verdict: str  # what the surrogate cascade reproduces from the trace
    base_final_score: float
    replay_verdict: str  # what the surrogate cascade outputs with overrides
    replay_final_score: float
    matched_base: bool  # True iff no overrides AND verdict reproduces
    overrides_applied: dict[str, Any]
    diff: dict[str, Any]  # field-by-field delta
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _empty_result(consult_id: str, error: str) -> ReplayResult:
    return ReplayResult(
        consult_id=consult_id,
        base_verdict="UNKNOWN",
        base_final_score=0.0,
        replay_verdict="UNKNOWN",
        replay_final_score=0.0,
        matched_base=False,
        overrides_applied={},
        diff={},
        error=error,
    )


def _find_record(consult_id: str, lookback: int = 5000) -> dict[str, Any] | None:
    try:
        from eta_engine.brain.jarvis_v3 import trace_emitter

        records = trace_emitter.tail(n=lookback) or []
        for rec in records:
            if isinstance(rec, dict) and rec.get("consult_id") == consult_id:
                return rec
    except Exception as exc:  # noqa: BLE001
        logger.warning("consult_replay._find_record failed: %s", exc)
    return None


def _extract_scores(school_inputs: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(school_inputs, dict):
        return out
    for school, payload in school_inputs.items():
        if not isinstance(payload, dict):
            continue
        try:
            out[str(school)] = float(payload.get("score", 0.0))
        except (TypeError, ValueError):
            continue
    return out


def _surrogate_consolidate(
    scores: dict[str, float],
    hot_weights: dict[str, float] | None,
) -> tuple[float, str]:
    """Deterministic weighted-average surrogate. Mirrors the one in
    ``causal_attribution`` so the two modules agree on verdict labels.
    """
    if not scores:
        return 0.0, "HOLD"
    total_w = 0.0
    total_ws = 0.0
    for school, raw in scores.items():
        try:
            s = float(raw)
        except (TypeError, ValueError):
            continue
        w_raw = (hot_weights or {}).get(school, 1.0)
        try:
            w = float(w_raw)
        except (TypeError, ValueError):
            w = 1.0
        if w <= 0:
            continue
        total_w += w
        total_ws += w * s
    if total_w == 0:
        return 0.0, "HOLD"
    final = total_ws / total_w
    if abs(final) < 0.05:
        return final, "HOLD"
    return final, "PROCEED" if final > 0 else "AVOID"


def _apply_size_override(
    verdict: str,
    final_score: float,
    overrides: dict[str, Any] | None,
) -> tuple[str, float]:
    """size_modifier override of 0 blocks the verdict; other values pass through."""
    if not isinstance(overrides, dict):
        return verdict, final_score
    sm = overrides.get("size_modifier")
    if sm is None:
        return verdict, final_score
    try:
        sm_val = float(sm)
    except (TypeError, ValueError):
        return verdict, final_score
    if sm_val == 0.0:
        return "BLOCKED", 0.0
    return verdict, final_score


def replay(
    consult_id: str,
    override_overrides: dict[str, Any] | None = None,
    override_hot_weights: dict[str, float] | None = None,
    override_school_inputs: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
) -> ReplayResult:
    """Re-execute ``consult_id`` with optional overrides.

    Without override args, this is a determinism check — matched_base
    should be True iff the surrogate cascade reproduces what the real
    consolidator did. With override args, it shows the alt verdict.
    """
    if not consult_id:
        return _empty_result(consult_id, "missing_consult_id")

    rec = record or _find_record(consult_id)
    if rec is None:
        return _empty_result(consult_id, f"consult_not_found:{consult_id}")

    try:
        from eta_engine.brain.jarvis_v3 import trace_emitter

        if not trace_emitter.is_v2_record(rec):
            return _empty_result(consult_id, "pre_v2_record_no_replay_data")
        replay_inputs = trace_emitter.extract_replay_inputs(rec)
    except Exception as exc:  # noqa: BLE001
        return _empty_result(consult_id, f"replay_extract_failed:{exc}")

    if replay_inputs is None:
        return _empty_result(consult_id, "pre_v2_record_no_replay_data")

    base_school_inputs = replay_inputs.get("school_inputs") or {}
    base_hot_weights = replay_inputs.get("hot_weights_snapshot") or {}
    base_overrides = replay_inputs.get("overrides_snapshot") or {}

    base_scores = _extract_scores(base_school_inputs)
    base_final_raw, base_verdict_raw = _surrogate_consolidate(
        base_scores,
        base_hot_weights,
    )
    base_verdict, base_final = _apply_size_override(
        base_verdict_raw,
        base_final_raw,
        base_overrides,
    )

    replay_school_inputs = override_school_inputs if override_school_inputs is not None else base_school_inputs
    replay_hot_weights = override_hot_weights if override_hot_weights is not None else base_hot_weights
    replay_overrides = override_overrides if override_overrides is not None else base_overrides

    replay_scores = _extract_scores(replay_school_inputs)
    replay_final_raw, replay_verdict_raw = _surrogate_consolidate(
        replay_scores,
        replay_hot_weights,
    )
    replay_verdict, replay_final = _apply_size_override(
        replay_verdict_raw,
        replay_final_raw,
        replay_overrides,
    )

    overrides_applied = {
        "school_inputs_changed": override_school_inputs is not None,
        "hot_weights_changed": override_hot_weights is not None,
        "overrides_changed": override_overrides is not None,
    }
    matched_base = (
        not any(overrides_applied.values()) and base_verdict == replay_verdict and abs(base_final - replay_final) < 1e-6
    )
    diff = {
        "verdict": [base_verdict, replay_verdict],
        "final_score": [base_final, replay_final],
        "final_score_delta": replay_final - base_final,
    }

    return ReplayResult(
        consult_id=consult_id,
        base_verdict=base_verdict,
        base_final_score=base_final,
        replay_verdict=replay_verdict,
        replay_final_score=replay_final,
        matched_base=matched_base,
        overrides_applied=overrides_applied,
        diff=diff,
    )


def counterfactual(
    consult_id: str,
    pin_size_modifier: float | None = None,
    pin_school: str | None = None,
    pin_weight: float | None = None,
    record: dict[str, Any] | None = None,
) -> ReplayResult:
    """Operator-friendly wrapper around ``replay``.

    Common patterns:

      counterfactual("abc123", pin_size_modifier=0.5)
        → "what if I had trimmed to 0.5x"

      counterfactual("abc123", pin_school="momentum", pin_weight=1.5)
        → "what if momentum had been weighted 1.5x at consult time"
    """
    overrides_arg: dict[str, Any] | None = None
    hot_weights_arg: dict[str, float] | None = None

    if pin_size_modifier is not None:
        try:
            overrides_arg = {"size_modifier": float(pin_size_modifier)}
        except (TypeError, ValueError):
            return _empty_result(consult_id, "pin_size_modifier_not_numeric")

    if pin_school is not None and pin_weight is not None:
        rec = record or _find_record(consult_id)
        if rec is not None:
            try:
                from eta_engine.brain.jarvis_v3 import trace_emitter

                ri = trace_emitter.extract_replay_inputs(rec)
                if ri is not None:
                    hot_weights_arg = dict(ri.get("hot_weights_snapshot") or {})
                    try:
                        hot_weights_arg[str(pin_school)] = float(pin_weight)
                    except (TypeError, ValueError):
                        return _empty_result(consult_id, "pin_weight_not_numeric")
            except Exception as exc:  # noqa: BLE001
                logger.warning("counterfactual hot-weight setup failed: %s", exc)

    return replay(
        consult_id=consult_id,
        override_overrides=overrides_arg,
        override_hot_weights=hot_weights_arg,
        record=record,
    )

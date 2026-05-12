"""JARVIS Conductor — wires Streams 1-5 into the consult flow.

Calls each stream in order, wraps each in try/except so any stream
failure falls back to legacy behavior. Does NOT overrule JarvisAdmin.

The conductor sits at the end of `JarvisFull.consult()` (after the
existing Wave-12→17 size pipeline has produced ``final_size``) and:

  1. enriches context (multi-TF + nearby events + session)            — Stream 4
  2. asks portfolio_brain for a size_modifier or block_reason          — Stream 1
  3. reads hot_learner's current per-school weights for the asset      — Stream 3
  4. applies ``portfolio_modifier`` to base_size and clamps to [0,1.5]
  5. emits one structured trace line                                   — Stream 2
  6. returns a ConductorResult the caller folds into the verdict

The companion observer (``observe_close``) runs from
``jarvis_strategy_supervisor._propagate_close()`` so closed trades
update hot_learner's weights for the next consult.

Every stream call is best-effort. If any one fails, the conductor
logs a warning and continues with the legacy fallback (base_size).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("eta_engine.jarvis_conductor")

EXPECTED_HOOKS = ("orchestrate", "observe_close", "build_school_inputs_from_sage")


def build_school_inputs_from_sage(sage_report: Any) -> dict[str, dict[str, Any]]:  # noqa: ANN401
    """Translate a SageReport into the T6/T7 school_inputs dict.

    Each school's signed score = +conviction (aligned with entry side),
    -conviction (misaligned), or 0 (neutral). T6's surrogate cascade
    treats this signed score as the school's RAW vote so flipping a
    school's score sign answers "what if this school had said the
    opposite?".

    Returns an empty dict if ``sage_report`` is None, malformed, or has
    no per_school dict. NEVER raises.
    """
    out: dict[str, dict[str, Any]] = {}
    if sage_report is None:
        return out
    per_school = getattr(sage_report, "per_school", None)
    if not isinstance(per_school, dict):
        return out
    for school_name, verdict in per_school.items():
        try:
            bias_val = getattr(verdict, "bias", None)
            bias_str = bias_val.value if hasattr(bias_val, "value") else str(bias_val or "")
            conviction = float(getattr(verdict, "conviction", 0.0))
            aligned = bool(getattr(verdict, "aligned_with_entry", False))
            rationale = str(getattr(verdict, "rationale", "") or "")
            # Signed score: +conviction aligned, -conviction misaligned, 0 neutral.
            if bias_str.lower() == "neutral":
                score = 0.0
            elif aligned:
                score = conviction
            else:
                score = -conviction
            out[str(school_name)] = {
                "score": round(score, 4),
                "conviction": round(conviction, 4),
                "bias": bias_str,
                "rationale": rationale[:200],
                "rng_seed": None,
            }
        except Exception:  # noqa: BLE001 — per-school capture is best-effort
            continue
    return out


@dataclass
class ConductorResult:
    """Outcome of a single conductor pass."""

    final_size: float
    block_reason: str | None
    consult_id: str
    school_weights: dict[str, float]
    portfolio_modifier: float
    enriched_context: Any = None
    elapsed_ms: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)
    # Per-stream Hermes Agent call outcomes captured during this consult:
    # keys are the call site name ("narrative", "web_search",
    # "memory_persist", "memory_recall"); each value is a dict with at
    # least ``ok`` (bool), ``elapsed_ms`` (float), and ``error`` (str | None).
    # Populated by Phase B hot-path wiring; trace_emitter writes it out
    # so the operator dashboard / wiring audit can spot when JARVIS is
    # silently bypassing Hermes (e.g. backoff is suppressing all calls).
    hermes_calls: dict = field(default_factory=dict)


def orchestrate(
    *,
    req: Any,  # noqa: ANN401 — req is JarvisAdmin.ActionRequest, structurally typed
    base_size: float,
    trace_path: Path | None = None,
    school_inputs: dict[str, dict[str, Any]] | None = None,
) -> ConductorResult:
    """Run the 5-stream pipeline; never raise.

    Parameters
    ----------
    req : Any
        The action request being evaluated. Must expose ``bot_id``,
        ``asset_class``, optionally ``symbol`` and ``action``.
    base_size : float
        The size_multiplier computed by the existing Wave-12→17
        pipeline (composite × coach × budget × quantum × OOD × premortem
        × dissent × clashes, clamped to [0,2]).
    trace_path : Path | None
        Override trace destination. Default uses
        ``trace_emitter.DEFAULT_TRACE_PATH``.
    school_inputs : dict | None
        Optional per-school RAW vote snapshot. When the caller (typically
        ``JarvisFull.consult``) has a populated ``sage_report.per_school``,
        passing it here unlocks full T6 causal attribution + T7 replay
        on this consult's trace record. Shape:
        ``{school_name: {"score": float, "conviction": float,
                         "bias": str, "rationale": str, "rng_seed": int|None}}``
        ``score`` is signed by aligned_with_entry (+conviction for aligned,
        -conviction for misaligned, 0 for neutral) so the T6 surrogate
        cascade can attribute marginal effects directly.
        ``None`` falls back to an empty dict — backward-compatible.

    Returns
    -------
    ConductorResult
        ``final_size`` is the conductor-adjusted size in [0, 1.5].
        ``block_reason`` is non-None when the portfolio brain vetoes
        the consult. ``consult_id`` is always set so the caller can
        cross-reference the trace line.
    """
    t0 = time.perf_counter()
    # Stream 2: identifier (cheap, never raises)
    try:
        from eta_engine.brain.jarvis_v3 import trace_emitter
        consult_id = trace_emitter.new_consult_id()
    except Exception as exc:  # noqa: BLE001
        logger.warning("trace_emitter.new_consult_id failed: %s", exc)
        consult_id = ""

    asset_class = str(getattr(req, "asset_class", "") or "default")
    symbol = str(getattr(req, "symbol", "") or "")

    # Stream 4: enrich context
    enriched = None
    try:
        from eta_engine.brain.jarvis_v3 import context_enricher
        enriched = context_enricher.enrich(symbol=symbol, asset_class=asset_class)
    except Exception as exc:  # noqa: BLE001
        logger.warning("context_enricher.enrich failed: %s", exc)

    # Stream 1: portfolio assess
    portfolio_modifier = 1.0
    block_reason: str | None = None
    portfolio_notes: tuple[str, ...] = ()
    portfolio_ctx_for_trace: Any = None  # captured for v2 schema emit below
    try:
        from eta_engine.brain.jarvis_v3 import portfolio_brain
        ctx = portfolio_brain.snapshot()
        portfolio_ctx_for_trace = ctx
        verdict = portfolio_brain.assess(req, ctx)
        portfolio_modifier = float(verdict.size_modifier)
        block_reason = verdict.block_reason
        portfolio_notes = tuple(verdict.notes or ())
    except Exception as exc:  # noqa: BLE001
        logger.warning("portfolio_brain failed: %s", exc)

    # Stream 3: hot learner per-school weights
    school_weights: dict[str, float] = {}
    try:
        from eta_engine.brain.jarvis_v3 import hot_learner
        school_weights = dict(hot_learner.current_weights(asset_class))
    except Exception as exc:  # noqa: BLE001
        logger.warning("hot_learner.current_weights failed: %s", exc)

    # Compose final size
    final_size = (
        0.0 if block_reason
        else max(0.0, min(1.5, float(base_size) * portfolio_modifier))
    )

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    result = ConductorResult(
        final_size=final_size,
        block_reason=block_reason,
        consult_id=consult_id,
        school_weights=school_weights,
        portfolio_modifier=portfolio_modifier,
        enriched_context=enriched,
        elapsed_ms=elapsed_ms,
        notes=portfolio_notes,
    )

    # Stream 2: emit trace (best-effort; never raises). Schema v2:
    # capture replay-input snapshot fields so T6 (causal_attribution)
    # and T7 (consult_replay) can operate on this record. The
    # capture_v2_extras() helper handles every never-raise concern.
    try:
        from eta_engine.brain.jarvis_v3 import trace_emitter
        bot_id_str = str(getattr(req, "bot_id", ""))
        v2_extras = trace_emitter.capture_v2_extras(
            bot_id=bot_id_str,
            asset_class=asset_class,
            portfolio_ctx=portfolio_ctx_for_trace,
            hot_weights=school_weights,
            # Caller (JarvisFull.consult) supplies per-school RAW votes
            # built from sage_report.per_school. Empty dict when sage was
            # unavailable for this consult.
            school_inputs=school_inputs or {},
            rng_master_seed=None,
        )
        rec = trace_emitter.TraceRecord(
            ts=datetime.now(UTC).isoformat(),
            bot_id=bot_id_str,
            consult_id=consult_id,
            action=str(getattr(req, "action", "")),
            verdict={
                "base_size": float(base_size),
                "final_size": final_size,
                "block_reason": block_reason,
            },
            schools={k: {"hot_weight": v} for k, v in school_weights.items()},
            portfolio={
                "size_modifier": portfolio_modifier,
                "block_reason": block_reason,
                "notes": list(portfolio_notes),
            },
            context=_summarize_enriched(enriched),
            hot_learn={"weights": school_weights},
            final_size=final_size,
            block_reason=block_reason,
            elapsed_ms=elapsed_ms,
            **v2_extras,
        )
        trace_emitter.emit(rec, path=trace_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trace_emitter.emit failed: %s", exc)

    return result


def observe_close(
    *,
    asset_class: str,
    school_attribution: dict[str, float],
    r_outcome: float,
) -> None:
    """Forward a closed trade to hot_learner; never raise."""
    try:
        from eta_engine.brain.jarvis_v3 import hot_learner
        hot_learner.observe_close(
            asset=asset_class or "default",
            school_attribution=school_attribution,
            r_outcome=r_outcome,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("hot_learner.observe_close failed: %s", exc)


def _summarize_enriched(ec: Any) -> dict:  # noqa: ANN401 — ec is a duck-typed EnrichedContext from context_enricher
    """Compact context view for the trace stream (full ctx is too large)."""
    if ec is None:
        return {}
    try:
        return {
            "session": getattr(ec, "session", ""),
            "time_of_day_risk": float(getattr(ec, "time_of_day_risk", 0.0)),
            "multi_tf_agreement": float(getattr(ec, "multi_tf_agreement", 0.0)),
            "nearby_event_kinds": [
                getattr(e, "kind", "") for e in getattr(ec, "nearby_events", ()) or ()
            ],
        }
    except Exception:  # noqa: BLE001
        return {}

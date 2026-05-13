"""Postmortem generator (Wave-13, 2026-04-27).

Every -1.5R or worse loss should produce a structured postmortem
that:

  * Reconstructs the original ConsolidatedVerdict (which layers
    were consulted, what they said)
  * Identifies which layer's signal was MOST WRONG (highest
    contribution to the bad outcome)
  * Suggests adjustments to feed into kaizen + meta-learner
  * Persists to ``state/jarvis_intel/postmortems/<signal_id>.md``

The auto-generated postmortems become input to:
  * meta-learner: regression tests for hyperparameter changes
  * kaizen loop: candidate tickets for policy mutations
  * operator review queue: human eyes on systematic failures

Trigger pattern (called from feedback_loop.close_trade):

    if realized_r <= -1.5:
        from eta_engine.brain.jarvis_v3.postmortem import generate_postmortem
        pm = generate_postmortem(
            signal_id=signal_id, realized_r=realized_r,
            verdict_log_path=...,
        )
        # pm is persisted automatically

Pure stdlib. The "narrative" leg is template-driven (no LLM); when
LLM access is wired in production a richer narrative is a drop-in.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VERDICT_LOG = ROOT / "state" / "jarvis_intel" / "verdicts.jsonl"
DEFAULT_POSTMORTEM_DIR = ROOT / "state" / "jarvis_intel" / "postmortems"


@dataclass
class LayerAttribution:
    """Per-layer attribution: how much did this layer contribute to
    the bad call?"""

    layer: str
    layer_signal: float  # the layer's expressed value
    contribution_score: float  # in [-1, +1]; negative = layer's
    # signal was wrong
    note: str = ""


@dataclass
class Postmortem:
    """Structured postmortem for one losing trade."""

    signal_id: str
    realized_r: float
    severity: str  # "moderate" / "severe" / "catastrophic"
    ts_generated: str
    ts_original_decision: str = ""
    direction: str = ""
    regime: str = ""
    session: str = ""
    base_verdict: str = ""
    final_verdict: str = ""
    layer_attributions: list[LayerAttribution] = field(default_factory=list)
    root_cause_hypothesis: str = ""
    suggested_adjustments: list[str] = field(default_factory=list)
    operator_action_required: bool = False

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "realized_r": self.realized_r,
            "severity": self.severity,
            "ts_generated": self.ts_generated,
            "ts_original_decision": self.ts_original_decision,
            "direction": self.direction,
            "regime": self.regime,
            "session": self.session,
            "base_verdict": self.base_verdict,
            "final_verdict": self.final_verdict,
            "root_cause_hypothesis": self.root_cause_hypothesis,
            "operator_action_required": self.operator_action_required,
            "suggested_adjustments": self.suggested_adjustments,
            "layer_attributions": [
                {
                    "layer": a.layer,
                    "layer_signal": a.layer_signal,
                    "contribution_score": a.contribution_score,
                    "note": a.note,
                }
                for a in self.layer_attributions
            ],
        }

    def to_markdown(self) -> str:
        sev_marker = {
            "moderate": "[!]",
            "severe": "[!!]",
            "catastrophic": "[!!!]",
        }.get(self.severity, "")
        lines: list[str] = []
        lines.append(f"# Postmortem {sev_marker}: {self.signal_id}")
        lines.append("")
        lines.append(f"- **realized R:** {self.realized_r:+.2f}")
        lines.append(f"- **severity:** {self.severity}")
        lines.append(f"- **direction:** {self.direction}")
        lines.append(f"- **regime / session:** {self.regime} / {self.session}")
        lines.append(f"- **base verdict:** {self.base_verdict}")
        lines.append(f"- **final verdict:** {self.final_verdict}")
        lines.append(f"- **decided at:** {self.ts_original_decision}")
        lines.append(f"- **postmortem generated:** {self.ts_generated}")
        if self.operator_action_required:
            lines.append("- **OPERATOR ACTION REQUIRED**")
        lines.append("")
        lines.append("## Root cause hypothesis")
        lines.append("")
        lines.append(self.root_cause_hypothesis or "(none identified)")
        lines.append("")
        lines.append("## Layer attributions")
        lines.append("")
        if self.layer_attributions:
            lines.append("| Layer | Signal | Contribution | Note |")
            lines.append("|---|---|---|---|")
            for a in self.layer_attributions:
                lines.append(
                    f"| {a.layer} | {a.layer_signal:+.3f} | {a.contribution_score:+.3f} | {a.note} |",
                )
        else:
            lines.append("(no layer outputs available)")
        lines.append("")
        lines.append("## Suggested adjustments")
        lines.append("")
        if self.suggested_adjustments:
            for s in self.suggested_adjustments:
                lines.append(f"- {s}")
        else:
            lines.append("(none)")
        lines.append("")
        return "\n".join(lines)


# ─── Severity ────────────────────────────────────────────────────


def _severity(realized_r: float) -> str:
    if realized_r <= -3.0:
        return "catastrophic"
    if realized_r <= -2.0:
        return "severe"
    return "moderate"


# ─── Layer attribution ──────────────────────────────────────────


def _attribute_layers(verdict_record: dict, realized_r: float) -> list[LayerAttribution]:
    """Score each layer's contribution to the bad call.

    Convention: contribution_score in [-1, +1]
      * +1 = layer correctly opposed the trade (and was overruled)
      * -1 = layer strongly endorsed the trade
      *  0 = layer was neutral / didn't fire

    For a LOSER, layers with high positive endorsement scored
    NEGATIVE contributions (they pushed us into the loss).
    """
    out: list[LayerAttribution] = []

    # Causal layer
    causal_score = float(verdict_record.get("causal_score", 0.0))
    causal_reason = str(verdict_record.get("causal_reason", ""))
    out.append(
        LayerAttribution(
            layer="causal",
            layer_signal=causal_score,
            contribution_score=round(-causal_score, 3) if realized_r < 0 else round(causal_score, 3),
            note=causal_reason[:100],
        )
    )

    # Firm board consensus
    fb_consensus = float(verdict_record.get("firm_board_consensus", 0.0))
    out.append(
        LayerAttribution(
            layer="firm_board",
            layer_signal=fb_consensus,
            contribution_score=(
                round(-(fb_consensus - 0.5) * 2, 3) if realized_r < 0 else round((fb_consensus - 0.5) * 2, 3)
            ),
            note=f"consensus {fb_consensus:.2f}",
        )
    )

    # World-model expected R
    wm_r = float(verdict_record.get("world_model_expected_r", 0.0))
    out.append(
        LayerAttribution(
            layer="world_model",
            layer_signal=wm_r,
            contribution_score=round(
                (-wm_r if realized_r < 0 else wm_r) / 2.0,
                3,
            ),
            note=(f"expected R was {wm_r:+.2f}, realized {realized_r:+.2f}"),
        )
    )

    # RAG cautions: positive contribution if we ignored cautions
    rag_cautions = verdict_record.get("rag_cautions") or []
    rag_score = -0.5 if rag_cautions else 0.0
    out.append(
        LayerAttribution(
            layer="rag",
            layer_signal=len(rag_cautions),
            contribution_score=round(
                rag_score if realized_r < 0 else 0.0,
                3,
            ),
            note=(f"had {len(rag_cautions)} caution(s)" if rag_cautions else "no cautions"),
        )
    )

    return out


def _root_cause_hypothesis(
    attributions: list[LayerAttribution],
    verdict_record: dict,
) -> str:
    """Pick the layer with the most-negative contribution and
    formulate a hypothesis."""
    if not attributions:
        return "no attribution data available"
    # Most-negative = the layer whose endorsement pushed us into the loss
    worst = min(attributions, key=lambda a: a.contribution_score)
    if worst.contribution_score >= 0:
        return (
            "no single layer materially endorsed this trade; loss may "
            "be due to genuine market noise rather than model error"
        )
    layer = worst.layer
    if layer == "causal":
        return (
            f"causal layer endorsed the trade (score {worst.layer_signal:+.2f}) "
            f"but the realized outcome contradicts the inferred causal "
            f"linkage; intervention-lookup may be over-fit to recent winners"
        )
    if layer == "firm_board":
        return (
            f"firm-board reached high consensus ({worst.layer_signal:.2f}) "
            f"that proved wrong; the role-debate logic may be missing "
            f"a perspective specific to this regime/session"
        )
    if layer == "world_model":
        return (
            f"world-model expected positive R ({worst.layer_signal:+.2f}) "
            f"but reality delivered a loss; the transition tensor and "
            f"reward distribution may be stale for this state"
        )
    if layer == "rag":
        return (
            "RAG retrieved analog episodes that endorsed this setup, "
            "but the recent regime appears to have shifted -- the "
            "analogs may no longer apply"
        )
    return f"{layer} layer signal contributed most to the bad call"


def _suggested_adjustments(
    severity: str,
    attributions: list[LayerAttribution],
) -> list[str]:
    """Generate operator-actionable suggestions."""
    out: list[str] = []
    if severity == "catastrophic":
        out.append(
            "URGENT: pause this signal class via operator_override until manual review",
        )
    if not attributions:
        return out
    worst = min(attributions, key=lambda a: a.contribution_score)
    layer = worst.layer
    if layer == "causal":
        out.append(
            "rerun causal_discovery with a fresh time window and re-evaluate causal-veto threshold",
        )
    if layer == "firm_board":
        out.append(
            "tighten risk_committee_severity (bandit-mutate up by ~10%)",
        )
    if layer == "world_model":
        out.append(
            "trigger meta_learner shadow trial with cooler world-model rollout horizon (h=3 instead of h=5)",
        )
    if layer == "rag":
        out.append(
            "increase rag_caution_size_shrink to 0.30 (from 0.25)",
        )
    if severity in {"severe", "catastrophic"}:
        out.append(
            "add this signal_id to the meta-learner's regression "
            "test set so future hyperparameter changes must not "
            "re-approve it",
        )
    return out


# ─── Main entry point ────────────────────────────────────────────


def generate_postmortem(
    *,
    signal_id: str,
    realized_r: float,
    verdict_log_path: Path = DEFAULT_VERDICT_LOG,
    output_dir: Path = DEFAULT_POSTMORTEM_DIR,
    auto_persist: bool = True,
) -> Postmortem:
    """Build a Postmortem from the journaled verdict + realized R.

    If ``auto_persist`` is True, writes the markdown to
    ``output_dir/<signal_id>.md`` and the JSON to
    ``output_dir/<signal_id>.json``.
    """
    severity = _severity(realized_r)

    # Find the original verdict record
    verdict_record: dict | None = None
    if verdict_log_path.exists():
        try:
            for line in verdict_log_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(r.get("signal_id", "")) == signal_id or str(r.get("request_id", "")) == signal_id:
                    verdict_record = r
                    break
        except OSError as exc:
            logger.warning("postmortem: verdict log read failed (%s)", exc)

    if verdict_record is None:
        # Best-effort: still emit a postmortem with what we know
        verdict_record = {}

    attributions = _attribute_layers(verdict_record, realized_r)
    root_cause = _root_cause_hypothesis(attributions, verdict_record)
    suggestions = _suggested_adjustments(severity, attributions)

    pm = Postmortem(
        signal_id=signal_id,
        realized_r=float(realized_r),
        severity=severity,
        ts_generated=datetime.now(UTC).isoformat(),
        ts_original_decision=str(verdict_record.get("ts", "")),
        direction=str(verdict_record.get("direction", "")),
        regime=str(verdict_record.get("raw", {}).get("regime", "")),
        session=str(verdict_record.get("raw", {}).get("session", "")),
        base_verdict=str(verdict_record.get("base_verdict", "")),
        final_verdict=str(verdict_record.get("final_verdict", "")),
        layer_attributions=attributions,
        root_cause_hypothesis=root_cause,
        suggested_adjustments=suggestions,
        operator_action_required=(severity == "catastrophic"),
    )

    if auto_persist:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / f"{_safe_name(signal_id)}.md").write_text(
                pm.to_markdown(),
                encoding="utf-8",
            )
            (output_dir / f"{_safe_name(signal_id)}.json").write_text(
                json.dumps(asdict(pm), indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("postmortem: persist failed (%s)", exc)

    return pm


def _safe_name(s: str) -> str:
    """Sanitize signal_id for filesystem use."""
    out = []
    for ch in s:
        if ch.isalnum() or ch in {"-", "_", "."}:
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:128]

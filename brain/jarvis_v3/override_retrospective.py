from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.operator_coach import OperatorCoach

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_PATH = ROOT / "state" / "jarvis_intel" / "override_retros.jsonl"


@dataclass
class OverrideEvent:
    request_id: str
    subsystem: str
    action: str
    regime: str
    session: str
    jarvis_verdict: str
    operator_override_level: str
    override_reason: str = ""
    operator_action_taken: str = ""
    coach_before: str = ""
    coach_after: str = ""
    ts: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_captured(
        cls,
        *,
        request_id: str,
        subsystem: str,
        action: str,
        regime: str,
        session: str,
        jarvis_verdict: str,
        operator_override_level: str,
        override_reason: str = "",
    ) -> OverrideEvent:
        return cls(
            request_id=request_id,
            subsystem=subsystem,
            action=action,
            regime=regime,
            session=session,
            jarvis_verdict=jarvis_verdict,
            operator_override_level=operator_override_level,
            override_reason=override_reason,
            ts=datetime.now(UTC).isoformat(),
        )


@dataclass
class Retrospective:
    event: OverrideEvent
    narrative: str
    lesson: str
    action_items: list[str] = field(default_factory=list)
    ts: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict:
        return {
            "event": self.event.to_dict(),
            "narrative": self.narrative,
            "lesson": self.lesson,
            "action_items": self.action_items,
            "ts": self.ts,
        }


class OverrideRetroLogger:
    def __init__(self, *, log_path: Path = DEFAULT_LOG_PATH) -> None:
        self.log_path = log_path

    @classmethod
    def default(cls) -> OverrideRetroLogger:
        return cls()

    def capture(
        self,
        *,
        request_id: str,
        subsystem: str,
        action: str,
        regime: str,
        session: str,
        jarvis_verdict: str,
        operator_override_level: str,
        override_reason: str = "",
    ) -> OverrideEvent:
        event = OverrideEvent.from_captured(
            request_id=request_id,
            subsystem=subsystem,
            action=action,
            regime=regime,
            session=session,
            jarvis_verdict=jarvis_verdict,
            operator_override_level=operator_override_level,
            override_reason=override_reason,
        )
        self._log_event(event)
        return event

    def generate_retrospective(
        self,
        event: OverrideEvent,
        *,
        coach: OperatorCoach | None = None,
    ) -> Retrospective:
        narrative = self._build_narrative(event)
        lesson = self._derive_lesson(event)
        action_items = self._build_action_items(event, coach=coach)

        retro = Retrospective(
            event=event,
            narrative=narrative,
            lesson=lesson,
            action_items=action_items,
        )
        self._log_retro(retro)

        if coach is not None:
            coach.record_outcome(
                regime=event.regime,
                session=event.session,
                action=event.action,
                was_overridden=(event.operator_override_level in {"HARD_PAUSE", "KILL", "SOFT_PAUSE"}),
            )

        return retro

    def _build_narrative(self, event: OverrideEvent) -> str:
        verdict = event.jarvis_verdict
        override = event.operator_override_level
        return (
            f"[RETRO] Operator {override} over Jarvis {verdict} "
            f"for {event.subsystem} {event.action} "
            f"during {event.regime}/{event.session}. "
            f"Reason: {event.override_reason or 'not specified'}. "
            f"Jarvis will update priors for this (regime, session, action) cell."
        )

    def _derive_lesson(self, event: OverrideEvent) -> str:
        if "KILL" in event.operator_override_level:
            return (
                f"Operator issued hard block on {event.action} during "
                f"{event.regime}. Review if Jarvis should pre-emptively "
                f"tighten in this regime."
            )
        if event.operator_override_level == "SOFT_PAUSE":
            return (
                f"Operator paused entries during {event.regime}/{event.session}. "
                f"Consider whether Jarvis should detect this pattern "
                f"and self-soften next time."
            )
        return f"Operator adjusted Jarvis decision for {event.action}. Logging as training datum for Coach."

    def _build_action_items(
        self,
        event: OverrideEvent,
        *,
        coach: OperatorCoach | None = None,
    ) -> list[str]:
        items: list[str] = []
        if coach is not None:
            advice = coach.should_defer_to_operator(
                regime=event.regime,
                session=event.session,
                action=event.action,
            )
            if advice.recommendation == "escalate":
                items.append(
                    f"Coach recommends ESCALATION for "
                    f"({event.regime}, {event.session}, {event.action}) "
                    f"— override probability {advice.override_probability:.0%}"
                )
            elif advice.recommendation == "soften":
                items.append(
                    f"Coach recommends softening at "
                    f"{advice.suggested_size_shrink:.0%} size for "
                    f"({event.regime}, {event.session}, {event.action})"
                )
        if event.override_reason:
            items.append(f"Log override reason: {event.override_reason}")
        items.append("Updated Coach posterior for (regime, session, action) cell.")
        return items

    def _log_event(self, event: OverrideEvent) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "override_event", **event.to_dict()}) + "\n")
        except OSError as exc:
            logger.warning("override_retro: event log failed: %s", exc)

    def _log_retro(self, retro: Retrospective) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "retrospective", **retro.to_dict()}) + "\n")
        except OSError as exc:
            logger.warning("override_retro: retro log failed: %s", exc)

    def recent_overrides(self, n: int = 10) -> list[dict]:
        if not self.log_path.exists():
            return []
        try:
            lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
            events = []
            for line in lines[-n * 2 :]:
                try:
                    data = json.loads(line)
                    if data.get("type") == "override_event":
                        events.append(data)
                except (json.JSONDecodeError, KeyError):
                    continue
            return events[-n:]
        except OSError:
            return []

    def override_rate(self) -> dict:
        if not self.log_path.exists():
            return {"total": 0, "overrides": 0, "rate": 0.0}
        try:
            total = 0
            overrides = 0
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                try:
                    data = json.loads(line)
                    if data.get("type") != "override_event":
                        continue
                    total += 1
                    ol = data.get("operator_override_level", "")
                    if ol in {"HARD_PAUSE", "KILL", "SOFT_PAUSE"}:
                        overrides += 1
                except (json.JSONDecodeError, KeyError):
                    continue
            return {
                "total_requests": total,
                "operator_overrides": overrides,
                "override_rate": round(overrides / total, 3) if total else 0.0,
            }
        except OSError:
            return {"total": 0, "overrides": 0, "rate": 0.0}

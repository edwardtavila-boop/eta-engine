"""
JARVIS v3 // next_level.voice
=============================
Telegram / TTS / SMS operator interface.

Lets the operator talk to JARVIS from anywhere:
  * Inbound: ``jarvis status``, ``jarvis why denied abc123``, ``jarvis regime``
    -- parsed through nl_query and answered.
  * Outbound: CRITICAL alerts go via Telegram first, SMS fallback.
  * TTS briefing: on operator command, JARVIS reads a 30s summary.

This module provides the PORT (pure business logic); the ADAPTER layer
(actual Telegram Bot API / Twilio / pyttsx3) is a thin shim that lives
in ``scripts/`` and passes inbound messages here via ``handle_inbound``.

No network calls -- all I/O is injected.
"""

from __future__ import annotations

from collections.abc import Callable  # noqa: TC003  (runtime use in __init__ sig)
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.jarvis_v3 import nl_query


class Channel(StrEnum):
    TELEGRAM = "TELEGRAM"
    SMS = "SMS"
    TTS = "TTS"
    CONSOLE = "CONSOLE"


class InboundMessage(BaseModel):
    """A message the operator sent to JARVIS."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    channel: Channel
    sender: str = "operator.edward"
    text: str = Field(min_length=1)


class OutboundMessage(BaseModel):
    """A message JARVIS is sending out."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    channel: Channel
    priority: str = Field(pattern="^(INFO|WARN|CRITICAL)$", default="INFO")
    text: str = Field(min_length=1)
    # If True, adapter should try backup channels (e.g. SMS) on failure.
    fanout_on_failure: bool = False


class BriefingRequest(BaseModel):
    """Short structured request for a voice briefing."""

    model_config = ConfigDict(frozen=True)

    horizon_minutes: int = 30
    max_tokens: int = 200


class BriefingScript(BaseModel):
    """The 30s TTS briefing, deterministically assembled."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    script: str = Field(min_length=1)
    tokens: int = Field(ge=0)
    highlights: list[str]


class VoiceHub:
    """Pure in-process hub that maps text to actions + composes briefings.

    Adapter injection:
      * ``sender``    -- async callable (channel, text, priority) -> None
      * ``audit_path`` -- where nl_query runs its queries against
    """

    def __init__(
        self,
        audit_path: Path | str,
        sender: Callable[[Channel, str, str], None] | None = None,
    ) -> None:
        self.audit_path = Path(audit_path)
        self.sender = sender

    def handle_inbound(self, msg: InboundMessage) -> OutboundMessage:
        """Parse the incoming text, dispatch to nl_query, return a reply."""
        # Strip "jarvis" prefix if present
        text = msg.text.strip()
        for prefix in ("jarvis", "@jarvis", "/jarvis"):
            if text.lower().startswith(prefix):
                text = text[len(prefix) :].strip(": ").strip()
                break
        result = nl_query.dispatch(self.audit_path, text)
        return OutboundMessage(
            ts=datetime.now(UTC),
            channel=msg.channel,
            priority="INFO",
            text=f"[{result.intent}] {result.summary}",
        )

    def emit_critical(
        self,
        code: str,
        body: str,
        channels: tuple[Channel, ...] = (Channel.TELEGRAM, Channel.SMS),
    ) -> list[OutboundMessage]:
        """Build CRITICAL alert messages across multiple channels."""
        now = datetime.now(UTC)
        out: list[OutboundMessage] = []
        for ch in channels:
            out.append(
                OutboundMessage(
                    ts=now,
                    channel=ch,
                    priority="CRITICAL",
                    text=f"[{code}] {body}",
                    fanout_on_failure=(ch == Channel.TELEGRAM),
                )
            )
        return out

    def build_briefing(
        self,
        *,
        regime: str,
        session_phase: str,
        stress: float,
        open_risk_r: float,
        daily_dd_pct: float,
        active_alerts: list[str] | None = None,
        top_subsystems: list[str] | None = None,
    ) -> BriefingScript:
        """Assemble a 30s-ish TTS briefing script."""
        lines: list[str] = []
        highlights: list[str] = []
        lines.append(f"JARVIS briefing, {datetime.now(UTC).strftime('%H:%M UTC')}.")
        lines.append(f"Regime is {regime}, session phase {session_phase}.")
        stress_word = "low" if stress < 0.3 else "moderate" if stress < 0.5 else "elevated" if stress < 0.7 else "high"
        lines.append(f"Stress is {stress_word} at {stress:.0%}.")
        highlights.append(f"regime={regime}")
        highlights.append(f"stress={stress:.0%}")
        if daily_dd_pct > 0.01:
            lines.append(
                f"Daily drawdown {daily_dd_pct:.1%}. Capital-first tenet alert.",
            )
            highlights.append(f"dd={daily_dd_pct:.1%}")
        if open_risk_r > 0:
            lines.append(f"Open risk {open_risk_r:.2f} R across active positions.")
            highlights.append(f"open_risk={open_risk_r:.2f}R")
        if active_alerts:
            lines.append(f"Active alerts: {', '.join(active_alerts[:3])}.")
            highlights.extend(active_alerts[:3])
        if top_subsystems:
            lines.append(
                f"Most active subsystems: {', '.join(top_subsystems[:3])}.",
            )
        lines.append("End of briefing.")
        script = " ".join(lines)
        return BriefingScript(
            ts=datetime.now(UTC),
            script=script,
            tokens=len(script.split()),
            highlights=highlights,
        )

    async def send(self, msg: OutboundMessage) -> None:
        """Hand off to the injected sender adapter (Telegram / SMS / TTS)."""
        if self.sender is None:
            return
        self.sender(msg.channel, msg.text, msg.priority)

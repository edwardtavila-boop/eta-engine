from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.jarvis_full import FullJarvisVerdict

logger = logging.getLogger(__name__)


def _get_firm_signals_webhook() -> str | None:
    import os
    return os.environ.get("FIRM_DISCORD_WEBHOOK_FIRM_SIGNALS") or None


def dispatch_verdict(
    verdict: FullJarvisVerdict,
    alerter: object | None = None,
) -> None:
    consolidated = verdict.consolidated

    if consolidated.final_verdict == "APPROVED" and not verdict.is_blocked():
        return

    webhook = _get_firm_signals_webhook()
    if not webhook and alerter is None:
        return

    level: str = "WARN"
    if verdict.is_blocked():
        level = "KILL"
    elif consolidated.final_verdict in ("DENIED", "DEFERRED"):
        level = "ERROR"
    elif consolidated.final_verdict == "CONDITIONAL":
        level = "WARN"

    title = (
        f"JARVIS: {consolidated.final_verdict} "
        f"for {consolidated.subsystem} {consolidated.action}"
    )
    message_parts = [f"Confidence: {consolidated.confidence:.0%}"]
    message_parts.append(f"Reason: {consolidated.base_reason}")
    if verdict.final_size_multiplier is not None:
        message_parts.append(
            f"Size: {verdict.final_size_multiplier:.0%}"
        )
    if verdict.narrative_terse:
        message_parts.append(f"Narrative: {verdict.narrative_terse}")
    message = " | ".join(message_parts)

    if alerter is not None and hasattr(alerter, "send") and webhook:
        import asyncio

        from eta_engine.obs.alerts import Alert, AlertLevel
        level_map = {
            "INFO": AlertLevel.INFO,
            "WARN": AlertLevel.WARN,
            "ERROR": AlertLevel.ERROR,
            "CRITICAL": AlertLevel.CRITICAL,
            "KILL": AlertLevel.KILL,
        }
        alert = Alert(
            level=level_map.get(level, AlertLevel.WARN),
            title=title,
            message=message,
            context={
                "subsystem": str(consolidated.subsystem),
                "action": str(consolidated.action),
                "verdict": consolidated.final_verdict,
                "confidence": f"{consolidated.confidence:.0%}",
            },
            dedup_key=f"verdict:{consolidated.subsystem}:{consolidated.action}",
        )
        try:
            asyncio.create_task(alerter.send(alert))
        except Exception as exc:
            logger.warning("verdict dispatch send failed: %s", exc)

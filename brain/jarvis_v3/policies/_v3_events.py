"""Shared event-log helper for the v23-v27 advanced layers.

Each layer emits structured events to a common JSONL so observability
surfaces (Hermes Telegram, dashboard, weekly digest) can subscribe to
the same stream. Append-only, swallow all errors — never crash JARVIS
because of a logging issue.

Event shape::

    {
      "ts": "2026-05-04T16:55:00+00:00",
      "layer": "v25",
      "event": "class_loss_freeze",
      "bot_id": "btc_hybrid",
      "class": "crypto",
      "details": {"realized_pnl": -350.0, "limit": -300.0},
      "severity": "WARN"  # one of INFO/WARN/CRIT
    }
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


EVENT_LOG_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_v3_events.jsonl"
)


def emit_event(
    *,
    layer: str,
    event: str,
    bot_id: str = "",
    cls: str = "",
    details: dict[str, Any] | None = None,
    severity: str = "INFO",
) -> None:
    """Append an event to the v3 events log. Swallows all errors."""
    try:
        rec = {
            "ts": datetime.now(UTC).isoformat(),
            "layer": layer,
            "event": event,
            "bot_id": bot_id,
            "class": cls,
            "details": details or {},
            "severity": severity,
        }
        EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        # Fire-and-forget Hermes alert for routed events. Never blocks
        # emit_event — the dispatcher launches a background thread.
        try:
            from eta_engine.scripts.hermes_dispatcher import dispatch as _hermes_dispatch
            _hermes_dispatch(rec)
        except Exception as _hermes_exc:  # noqa: BLE001
            logger.debug("hermes dispatch failed (non-fatal): %s", _hermes_exc)
    except Exception as exc:  # noqa: BLE001 -- never crash JARVIS for telemetry
        logger.debug("v3 event emit failed (%s): %s", event, exc)

"""Bridge from jarvis_v3 event emission to Hermes Telegram alerts.

The supervisor and the v3 layers (v24 correlation_throttle, v25
class_loss_freeze, v26 execution_degraded, v27 sharpe_drift, plus
the supervisor's feed_health emitter) all write to the canonical
``var/eta_engine/state/jarvis_v3_events.jsonl`` stream. Operator
visibility used to require manual log inspection — this module
routes the high-severity events through the existing Hermes bridge
so the operator gets a Telegram ping when something needs attention.

Two integration points:

  dispatch(rec) — called inline from emit_event when severity is
    WARN/CRITICAL. Synchronous wrapper around the async hermes
    send_alert(); fire-and-forget so emit_event never blocks on
    a slow Telegram round-trip.

  tail_and_dispatch(path, since_offset) — standalone tail-mode
    daemon for replaying events the supervisor missed (e.g. if
    Hermes was down at the time). Tracks file offset across runs
    via ETA_HERMES_DISPATCHER_OFFSET_FILE.

Routing rules:

  layer="feed_health" event="feed_degraded"     → WARN, "Feed degraded"
  layer="v25"         event="class_loss_freeze" → CRITICAL, "Class loss freeze"
  layer="v26"         event="execution_degraded"→ WARN, "Execution degraded"
  layer="v27"         event="sharpe_drift"      → WARN, "Sharpe drift"
  layer="v24"         event="correlation_throttle" → INFO, no alert (too chatty)

Operator opt-out: ``ETA_HERMES_ALERTS_DISABLED=1`` mutes everything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from eta_engine.scripts import workspace_roots

logger = logging.getLogger(__name__)


# (layer, event) → ("level", "title prefix"); missing keys = no alert.
_ROUTING: dict[tuple[str, str], tuple[str, str]] = {
    ("feed_health", "feed_degraded"): ("WARN", "Feed degraded"),
    ("v25", "class_loss_freeze"): ("CRITICAL", "Class loss freeze"),
    ("v26", "execution_degraded"): ("WARN", "Execution degraded"),
    ("v27", "sharpe_drift"): ("WARN", "Sharpe drift"),
    # v24 correlation_throttle intentionally omitted — too chatty for
    # Telegram. Operator can read it from heartbeat or events file.
}


def _alerts_disabled() -> bool:
    return os.getenv("ETA_HERMES_ALERTS_DISABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _format_text(rec: dict[str, Any], title: str) -> str:
    bot_id = rec.get("bot_id") or "fleet"
    cls = rec.get("cls") or rec.get("class") or ""
    details = rec.get("details") or {}
    detail_pairs = [f"{k}={v}" for k, v in details.items()][:6]
    body = ", ".join(detail_pairs) if detail_pairs else "(no details)"
    return f"{title} | {bot_id}{f' / {cls}' if cls else ''}\n{body}"


def dispatch(rec: dict[str, Any]) -> None:
    """Route a single v3 event record to Hermes if mapped + enabled.

    Fire-and-forget: any exception is logged but never propagates.
    """
    if _alerts_disabled():
        return
    layer = str(rec.get("layer") or "")
    event = str(rec.get("event") or "")
    routing = _ROUTING.get((layer, event))
    if routing is None:
        return
    level, title = routing
    text = _format_text(rec, title)
    try:
        from eta_engine.brain.jarvis_v3.hermes_bridge import send_alert

        # Async function; run in a background thread + event loop so the
        # caller (emit_event) never blocks. Don't reuse the supervisor's
        # loop — that's the LiveIbkrVenue dispatcher and we don't want
        # alert traffic interleaving with order traffic.
        def _runner() -> None:
            try:
                asyncio.run(send_alert(title, text, level=level))
            except Exception as exc:  # noqa: BLE001
                logger.debug("hermes dispatch failed: %s", exc)

        threading.Thread(target=_runner, name="hermes-dispatch", daemon=True).start()
    except ImportError:
        logger.debug("hermes_bridge unavailable; skipping alert")


def tail_and_dispatch(
    path: Path | str | None = None,
    *,
    follow: bool = True,
    poll_interval_s: float = 5.0,
) -> int:
    """Standalone tail-mode loop for catching up missed events.

    Reads the file from a persisted offset (so re-runs don't replay
    the whole history), dispatches each new line through dispatch(),
    optionally polls forever (--follow) or returns after one pass.
    """
    if path is None:
        path = workspace_roots.ETA_JARVIS_V3_EVENTS_PATH
    path = Path(path)
    if not path.exists():
        logger.warning("no events file at %s", path)
        return 1

    offset_file = Path(
        os.getenv(
            "ETA_HERMES_DISPATCHER_OFFSET_FILE",
            str(path.parent / ".hermes_dispatcher_offset"),
        )
    )
    try:
        start = int(offset_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        start = path.stat().st_size  # default: only new lines from now on

    import time

    while True:
        try:
            cur_size = path.stat().st_size
        except OSError:
            cur_size = start
        if cur_size > start:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    fh.seek(start)
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        dispatch(rec)
                    start = fh.tell()
                with offset_file.open("w", encoding="utf-8") as oh:
                    oh.write(str(start))
            except OSError as exc:
                logger.warning("tail read failed: %s", exc)
        if not follow:
            return 0
        time.sleep(poll_interval_s)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--follow", action="store_true", default=True)
    p.add_argument("--once", action="store_true", help="Process current pending events and exit")
    p.add_argument("--path", type=Path, default=None)
    p.add_argument("--poll", type=float, default=5.0)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(
        tail_and_dispatch(
            args.path,
            follow=not args.once,
            poll_interval_s=args.poll,
        )
    )

"""APEX PREDATOR  //  core.mcc_intent
======================================
Single-source-of-truth reader for operator intent files written by the
JARVIS Master Command Center (``scripts/jarvis_dashboard.py``).

The MCC records operator intent to disk; the supervisor / bots read
from these files on each tick and apply the intent. This module is
the glue: it owns the file-path conventions, parses the records, and
constructs the kill-switch :class:`KillVerdict` for the tick loop.

Files produced by the MCC (paths kept in sync with ``scripts.jarvis_dashboard``):

    KILL_REQUEST    -- single JSON blob from POST /api/cmd/kill-switch-trip
    PAUSE_REQUESTS  -- JSONL stream of pause/unpause intents per bot
    ALERT_ACKS      -- JSONL stream of operator-acknowledged alert ids

These paths are module-level so tests can monkeypatch them without
touching real state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apex_predator.core.kill_switch_runtime import KillVerdict

# Paths -- MUST stay in sync with scripts.jarvis_dashboard module-level paths.
KILL_REQUEST: Path = Path("~/.local/state/apex_predator/mcc_kill_request.json").expanduser()
PAUSE_REQUESTS: Path = Path("~/.local/state/apex_predator/mcc_pause_requests.jsonl").expanduser()
ALERT_ACKS: Path = Path("~/.local/state/apex_predator/mcc_alert_acks.jsonl").expanduser()


# ---------------------------------------------------------------------------
# Kill request
# ---------------------------------------------------------------------------


def read_kill_request() -> dict[str, Any] | None:
    """Return the operator's pending manual kill request, or ``None``.

    The file is written atomically by the MCC on every ``kill-switch-trip``
    POST. Corrupt / unreadable / missing => return None (the supervisor
    just no-ops; the framework's own kill switch is unaffected).
    """
    if not KILL_REQUEST.exists():
        return None
    try:
        rec = json.loads(KILL_REQUEST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


def kill_request_as_verdict() -> KillVerdict | None:
    """Translate the pending MCC kill request into a :class:`KillVerdict`.

    Action is fixed at FLATTEN_ALL / CRITICAL -- the operator's manual
    trip is always treated as a portfolio-wide circuit blow. The latch's
    first-trip-wins logic dedupes against existing trips automatically.

    Returns ``None`` when no MCC kill request is pending.
    """
    rec = read_kill_request()
    if rec is None:
        return None
    # Local import keeps this module import-cheap (kill_switch_runtime
    # pulls in pydantic models and config parsing).
    from apex_predator.core.kill_switch_runtime import (
        KillAction,
        KillSeverity,
        KillVerdict,
    )

    return KillVerdict(
        action=KillAction.FLATTEN_ALL,
        severity=KillSeverity.CRITICAL,
        reason=str(rec.get("reason") or "manual operator trip via MCC"),
        scope=str(rec.get("scope") or "global"),
        evidence={
            "source": "mcc",
            "operator": rec.get("operator"),
            "tripped_at": rec.get("tripped_at"),
        },
    )


def clear_kill_request() -> bool:
    """Delete the MCC kill-request file. Idempotent.

    The supervisor calls this after the latch has successfully recorded
    the verdict, so subsequent ticks don't re-emit the same trip. Returns
    True iff the file was present and removed.
    """
    try:
        KILL_REQUEST.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Pause / unpause intent
# ---------------------------------------------------------------------------


def read_pause_requests() -> list[dict[str, Any]]:
    """Return every well-formed pause/unpause intent record (chronological)."""
    if not PAUSE_REQUESTS.exists():
        return []
    try:
        text = PAUSE_REQUESTS.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def latest_pause_intent(bot_id: str) -> str | None:
    """Return the most-recent intent ('pause' | 'unpause') for ``bot_id``.

    Returns ``None`` when there's no MCC intent on record for this bot --
    the supervisor leaves ``state.is_paused`` untouched in that case.
    """
    for rec in reversed(read_pause_requests()):
        if rec.get("bot_id") != bot_id:
            continue
        intent = rec.get("intent")
        if intent in ("pause", "unpause"):
            return intent
    return None


def apply_pause_intent(bot_id: str, current_paused: bool) -> bool:
    """Resolve effective pause state by overlaying MCC intent on current.

    * If the MCC has the bot marked **pause**, return True regardless of
      ``current_paused`` (operator override).
    * If the MCC has the bot marked **unpause**, return False -- BUT
      only the MCC's own confirm-token gate (enforced at POST time)
      protects this path from accidents. The "boots paused / never
      auto-unpause" rule still holds: a bot's first-tick default is True.
    * No MCC intent on record => return ``current_paused`` unchanged.
    """
    intent = latest_pause_intent(bot_id)
    if intent == "pause":
        return True
    if intent == "unpause":
        return False
    return current_paused

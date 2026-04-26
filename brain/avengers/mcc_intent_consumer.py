"""APEX PREDATOR  //  brain.avengers.mcc_intent_consumer
==============================================================
Closes the operator-control loop between the Master Command Center
PWA and the runtime fleet.

The MCC's POST handlers (``scripts/jarvis_dashboard.py``) write
intent files under ``~/.local/state/apex_predator/mcc_*``. Without a
consumer, those files just accumulate -- the operator's "pause" or
"trip kill switch" click never reaches the bots. This module is the
consumer: it runs on every avenger-daemon tick, reads the intent
files, applies them to live runtime state (kill-switch latch,
paused-bots set), and unlinks one-shot files so the same intent
isn't re-applied on the next tick.

The consumer is intentionally pure -- no asyncio, no network. It
takes paths on construction and processes whatever's there.

Intent file inventory
---------------------
``mcc_kill_request.json``         (one-shot, JSON object)
    Operator clicked "Trip kill switch" in MCC.
    Schema: {tripped_at: <iso>, operator: <str>, reason: <str>, scope: <str>}
    Action: trip the KillSwitchLatch with a synthesized FLATTEN_ALL
    KillVerdict, then unlink the file.

``mcc_kill_clear_request.json``   (one-shot, JSON object)
    Operator clicked "Reset kill switch" in MCC.
    Schema: {operator: <str>, reason: <str>}
    Action: clear the KillSwitchLatch, then unlink the file.

``mcc_pause_requests.jsonl``      (append-only, JSONL)
    Each line: {ts: <iso>, intent: "pause"|"unpause", bot_id: <str>,
                operator: <str>, reason: <str>}
    Action: tail-read NEW lines (offset tracked in
    ``mcc_pause_offset.json``), update the paused-bots set written
    to ``mcc_paused_bots.json``. The runtime reads that set each
    tick to skip paused bots.

Failure mode
------------
Every read / write goes through a try/except that logs at WARNING
and returns counts. The daemon's tick path can never crash from a
malformed intent file.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from apex_predator.core.kill_switch_latch import KillSwitchLatch
from apex_predator.core.kill_switch_runtime import (
    KillAction,
    KillSeverity,
    KillVerdict,
)

logger = logging.getLogger(__name__)


_MCC_STATE_DIR_DEFAULT = Path(
    "~/.local/state/apex_predator",
).expanduser()


@dataclass(frozen=True)
class IntentPaths:
    """All MCC intent file paths in one bag so tests can override
    cleanly without touching ``Path.home()``."""
    state_dir:           Path
    kill_request:        Path
    kill_clear_request:  Path
    pause_requests:      Path
    pause_offset:        Path
    paused_bots:         Path

    @classmethod
    def for_dir(cls, state_dir: Path) -> IntentPaths:
        return cls(
            state_dir=state_dir,
            kill_request=state_dir / "mcc_kill_request.json",
            kill_clear_request=state_dir / "mcc_kill_clear_request.json",
            pause_requests=state_dir / "mcc_pause_requests.jsonl",
            pause_offset=state_dir / "mcc_pause_offset.json",
            paused_bots=state_dir / "mcc_paused_bots.json",
        )


@dataclass
class ConsumeResult:
    """Counts returned by :func:`consume_mcc_intents`. Daemon journals
    these per tick so a regression (e.g. an intent file that grows
    but never drains) shows up in the heartbeat record."""
    kill_tripped:    bool = False
    kill_cleared:    bool = False
    pause_applied:   int  = 0
    unpause_applied: int  = 0
    paused_bots_now: list[str] = field(default_factory=list)
    errors:          list[str] = field(default_factory=list)


def consume_mcc_intents(
    *,
    paths: IntentPaths | None = None,
    latch: KillSwitchLatch | None = None,
    state_dir: Path | None = None,
    latch_path: Path | None = None,
) -> ConsumeResult:
    """Drain every MCC intent file once. Returns a count summary.

    Parameters
    ----------
    paths
        Override the file layout (tests). If unset, defaults to
        ``IntentPaths.for_dir(state_dir or ~/.local/state/apex_predator)``.
    latch
        The :class:`KillSwitchLatch` to trip / clear. If unset, a
        default-pathed latch is constructed at ``latch_path`` or
        ``state_dir/kill_switch_latch.json``.
    state_dir
        Convenience: resolves ``paths`` + ``latch_path`` defaults.
    latch_path
        Convenience: latch file path when ``latch`` isn't supplied.

    The function NEVER raises. Failures go into ``ConsumeResult.errors``.
    """
    if paths is None:
        paths = IntentPaths.for_dir(state_dir or _MCC_STATE_DIR_DEFAULT)
    if latch is None:
        lp = latch_path or (paths.state_dir / "kill_switch_latch.json")
        latch = KillSwitchLatch(lp)

    result = ConsumeResult()
    paths.state_dir.mkdir(parents=True, exist_ok=True)

    _drain_kill_request(paths, latch, result)
    _drain_kill_clear_request(paths, latch, result)
    _drain_pause_requests(paths, result)
    return result


# ---------------------------------------------------------------------------
# Drainers
# ---------------------------------------------------------------------------

def _drain_kill_request(
    paths: IntentPaths,
    latch: KillSwitchLatch,
    result: ConsumeResult,
) -> None:
    if not paths.kill_request.exists():
        return
    try:
        rec = json.loads(paths.kill_request.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result.errors.append(f"kill_request parse: {exc!s}")
        return
    if not isinstance(rec, dict):
        result.errors.append("kill_request not a JSON object; left in place")
        return

    operator = str(rec.get("operator") or "MCC-OPERATOR")
    reason   = str(rec.get("reason")   or "MCC kill-switch trip")
    scope    = str(rec.get("scope")    or "ALL")

    verdict = KillVerdict(
        action=KillAction.FLATTEN_ALL,
        severity=KillSeverity.CRITICAL,
        reason=f"MCC operator trip ({operator}): {reason}",
        scope="global" if scope.upper() == "ALL" else scope.lower(),
        evidence={
            "source":   "mcc_intent_consumer",
            "operator": operator,
            "scope_in": scope,
        },
    )
    try:
        changed = latch.record_verdict(verdict)
    except Exception as exc:  # noqa: BLE001 -- daemon must never crash
        result.errors.append(f"latch.record_verdict: {exc!s}")
        return
    result.kill_tripped = bool(changed)

    try:
        paths.kill_request.unlink()
    except OSError as exc:
        result.errors.append(f"kill_request unlink: {exc!s}")


def _drain_kill_clear_request(
    paths: IntentPaths,
    latch: KillSwitchLatch,
    result: ConsumeResult,
) -> None:
    if not paths.kill_clear_request.exists():
        return
    try:
        rec = json.loads(paths.kill_clear_request.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result.errors.append(f"kill_clear_request parse: {exc!s}")
        return
    if not isinstance(rec, dict):
        result.errors.append("kill_clear_request not a JSON object; left in place")
        return

    operator = str(rec.get("operator") or "MCC-OPERATOR")
    try:
        latch.clear(cleared_by=operator)
    except ValueError as exc:
        result.errors.append(f"latch.clear: {exc!s}")
        return
    except Exception as exc:  # noqa: BLE001 -- daemon must never crash
        result.errors.append(f"latch.clear: {exc!s}")
        return
    result.kill_cleared = True

    try:
        paths.kill_clear_request.unlink()
    except OSError as exc:
        result.errors.append(f"kill_clear_request unlink: {exc!s}")


def _drain_pause_requests(paths: IntentPaths, result: ConsumeResult) -> None:
    """Tail-read pause/unpause intents and reduce them into a
    paused-bots set persisted to ``paths.paused_bots``."""
    if not paths.pause_requests.exists():
        # Still surface the current paused set (empty or last-known).
        result.paused_bots_now = sorted(_load_paused_bots(paths))
        return

    offset = _load_offset(paths)
    try:
        with paths.pause_requests.open("rb") as fh:
            fh.seek(offset)
            new_bytes = fh.read()
            new_offset = fh.tell()
    except OSError as exc:
        result.errors.append(f"pause_requests read: {exc!s}")
        return

    paused = _load_paused_bots(paths)
    for raw in new_bytes.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            result.errors.append(f"pause_requests bad json: {line[:80]!r}")
            continue
        if not isinstance(row, dict):
            continue
        intent = str(row.get("intent") or "").lower()
        bot_id = str(row.get("bot_id") or "").strip()
        if not bot_id:
            continue
        if intent == "pause":
            paused.add(bot_id)
            result.pause_applied += 1
        elif intent == "unpause":
            paused.discard(bot_id)
            result.unpause_applied += 1
        else:
            result.errors.append(f"pause_requests unknown intent: {intent!r}")

    if not _save_paused_bots(paths, paused):
        result.errors.append("paused_bots write failed")
    if not _save_offset(paths, new_offset):
        result.errors.append("pause_offset write failed")
    result.paused_bots_now = sorted(paused)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_offset(paths: IntentPaths) -> int:
    if not paths.pause_offset.exists():
        return 0
    try:
        raw = json.loads(paths.pause_offset.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    val = raw.get("offset") if isinstance(raw, dict) else None
    if isinstance(val, int) and val >= 0:
        return val
    return 0


def _save_offset(paths: IntentPaths, offset: int) -> bool:
    try:
        paths.pause_offset.write_text(
            json.dumps({"offset": int(offset)}) + "\n",
            encoding="utf-8",
        )
        return True
    except OSError as exc:
        logger.warning("mcc_intent_consumer: offset write failed: %s", exc)
        return False


def _load_paused_bots(paths: IntentPaths) -> set[str]:
    if not paths.paused_bots.exists():
        return set()
    try:
        raw = json.loads(paths.paused_bots.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(raw, dict):
        return set()
    bots = raw.get("paused")
    if isinstance(bots, list):
        return {str(b) for b in bots if isinstance(b, str) and b}
    return set()


def _save_paused_bots(paths: IntentPaths, paused: set[str]) -> bool:
    try:
        paths.paused_bots.write_text(
            json.dumps({"paused": sorted(paused)}, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
    except OSError as exc:
        logger.warning("mcc_intent_consumer: paused_bots write failed: %s", exc)
        return False


__all__ = [
    "ConsumeResult",
    "IntentPaths",
    "consume_mcc_intents",
]

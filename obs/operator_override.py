"""Operator override panel (Tier-3 #17, 2026-04-27).

A small JSON-file-based override channel so the operator can pause/
resume the fleet without restarting daemons or touching env vars.

Three levels of override:

  1. SOFT_PAUSE  -- bots stop opening NEW positions; existing flat
                    naturally as their stops/exits hit
  2. HARD_PAUSE  -- + flatten all open positions immediately
  3. KILL        -- + arm the fleet kill switch (requires manual reset)

The override file lives at ``state/operator_override.json``. Bots /
JARVIS check it on every tick; the cost is one file stat per tick
(< 1ms). Empty file or missing == NORMAL operation.

Operator commands::

    python -m eta_engine.obs.operator_override pause-soft "macro event imminent"
    python -m eta_engine.obs.operator_override pause-hard "regime shift detected"
    python -m eta_engine.obs.operator_override kill "operator stop"
    python -m eta_engine.obs.operator_override resume
    python -m eta_engine.obs.operator_override status
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
OVERRIDE_PATH = ROOT / "state" / "operator_override.json"


class OverrideLevel(StrEnum):
    NORMAL = "NORMAL"
    SOFT_PAUSE = "SOFT_PAUSE"
    HARD_PAUSE = "HARD_PAUSE"
    KILL = "KILL"


@dataclass(frozen=True)
class OverrideState:
    level: OverrideLevel
    set_by: str
    set_at: datetime
    reason: str
    expires_at: datetime | None = None


def get_state() -> OverrideState:
    """Read current override state. Returns NORMAL when file missing
    OR expired."""
    if not OVERRIDE_PATH.exists():
        return OverrideState(
            level=OverrideLevel.NORMAL,
            set_by="default",
            set_at=datetime.now(UTC),
            reason="no override file",
        )
    try:
        data = json.loads(OVERRIDE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return OverrideState(
            level=OverrideLevel.NORMAL,
            set_by="default",
            set_at=datetime.now(UTC),
            reason="override file unreadable",
        )

    try:
        level = OverrideLevel(data.get("level", "NORMAL"))
    except ValueError:
        level = OverrideLevel.NORMAL

    set_at_str = data.get("set_at")
    try:
        set_at = datetime.fromisoformat(set_at_str.replace("Z", "+00:00"))
        if set_at.tzinfo is None:
            set_at = set_at.replace(tzinfo=UTC)
    except (TypeError, ValueError, AttributeError):
        set_at = datetime.now(UTC)

    expires_at = None
    if data.get("expires_at"):
        try:
            expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
        except (TypeError, ValueError, AttributeError):
            pass

    # Auto-expire
    if expires_at is not None and datetime.now(UTC) > expires_at:
        level = OverrideLevel.NORMAL

    return OverrideState(
        level=level,
        set_by=str(data.get("set_by", "")),
        set_at=set_at,
        reason=str(data.get("reason", "")),
        expires_at=expires_at,
    )


def set_state(
    level: OverrideLevel,
    *,
    reason: str,
    set_by: str = "operator",
    expires_at: datetime | None = None,
) -> Path:
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_PATH.write_text(
        json.dumps(
            {
                "level": level.value,
                "set_by": set_by,
                "set_at": datetime.now(UTC).isoformat(),
                "reason": reason,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return OVERRIDE_PATH


def is_paused(*, hard_only: bool = False) -> bool:
    """Convenience: is the fleet paused?"""
    state = get_state()
    if hard_only:
        return state.level in {OverrideLevel.HARD_PAUSE, OverrideLevel.KILL}
    return state.level != OverrideLevel.NORMAL


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_soft = sub.add_parser("pause-soft", help="Stop NEW entries; existing positions wind down")
    p_soft.add_argument("reason")

    p_hard = sub.add_parser("pause-hard", help="Stop entries + flatten existing positions")
    p_hard.add_argument("reason")

    p_kill = sub.add_parser("kill", help="Arm the fleet kill switch (manual reset required)")
    p_kill.add_argument("reason")

    sub.add_parser("resume", help="Clear override; resume NORMAL operation")
    sub.add_parser("status", help="Print current override state")

    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.cmd == "pause-soft":
        path = set_state(OverrideLevel.SOFT_PAUSE, reason=args.reason)
        print(f"  SOFT_PAUSE engaged. Reason: {args.reason}\n  state: {path}")
    elif args.cmd == "pause-hard":
        path = set_state(OverrideLevel.HARD_PAUSE, reason=args.reason)
        print(f"  HARD_PAUSE engaged. Reason: {args.reason}\n  state: {path}")
    elif args.cmd == "kill":
        path = set_state(OverrideLevel.KILL, reason=args.reason)
        print(
            f"  KILL engaged. Reason: {args.reason}\n"
            "  Manual reset required: "
            "`python -m eta_engine.obs.operator_override resume`"
        )
    elif args.cmd == "resume":
        if OVERRIDE_PATH.exists():
            OVERRIDE_PATH.unlink()
        print("  Override cleared. Fleet returns to NORMAL.")
    elif args.cmd == "status":
        s = get_state()
        print(f"  level:      {s.level.value}")
        print(f"  set_by:     {s.set_by}")
        print(f"  set_at:     {s.set_at.isoformat()}")
        print(f"  reason:     {s.reason}")
        print(f"  expires_at: {s.expires_at.isoformat() if s.expires_at else '—'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
